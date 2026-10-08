"""my_show.py — 最小可用的"双 AI 语音电台"启动器（本地部署补充，非仓库原生物）。

公开仓库是 open-core：总启动器 lumi.py、人设 persona/、形象层都不在开源范围内。
本脚本用 main.py 同款的**真实编排核心**（EventBus / StateMachine / SpeakerScheduler /
SpeechOutputArbiter），把已配置的自定义 LLM（.env: CUSTOM_LLM_*）和 MiMo TTS
（.env: MIMO_TTS_*，角色音色 CHARACTER_*_VOICE_ID）串成一个可以直接玩的直播：

两个 AI 角色轮流语音聊天；你在终端里以观众身份打字发弹幕，弹幕里写谁的名字
（Lumi / Nox）就路由给谁回答；不写名字则由调度核心按轮换决定。声音从默认
扬声器播出（接直播时可把 cable_index 换成虚拟声卡）。

用法：
    python my_show.py              # 交互模式：输入弹幕回车发送；直接回车=让 AI 自聊
    python my_show.py --turns 3    # 自动模式：不读输入，自动聊 3 轮退出（用于测试）
    python my_show.py --no-tts     # 只出字幕不出声（无音频设备/调试用）

退出：Ctrl+C，或弹幕输入 exit / 退出。
"""
import argparse
import asyncio
import base64
import functools
import http.server
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import websockets

# ---- 设置中心：必须先于 fast_brain / mimo_tts 导入，把设置文件非空项写入
# 环境变量（后续模块的 load_dotenv 不覆盖已有值，于是"设置文件 > .env"成立）----
import show_settings
show_settings.load_and_apply_env()

from event_bus import EventBus
from state_machine import StateMachine, State
from speaker_scheduler import SpeakerScheduler
from speech_output_arbiter import SpeechOutputArbiter, POLICY_QUEUE
from voice_config import get_speaker_config
import voice_registry as 音色库
import fast_brain
import mimo_tts
from tts_emitter import IndependentTTSEmitter

# ---- 占位人设（自拟的示例角色，非原作者私有 persona；想改性格改这里）----
PERSONAS = {
    "Lumi": (
        "你是 Lumi，一个元气活泼的虚拟主播，正在和搭档 Nox 一起直播。"
        "说话简短口语化，每次只说一到两句，爱接梗、爱调侃搭档。"
        "禁止任何动作/神态描写（不要括号、不要星号），只说出口的话本身。"
    ),
    "Nox": (
        "你是 Nox，一个高冷毒舌但心软的虚拟主播，正在和搭档 Lumi 一起直播。"
        "说话简短口语化，每次只说一到两句，擅长冷吐槽，偶尔流露出在意。"
        "禁止任何动作/神态描写（不要括号、不要星号），只说出口的话本身。"
    ),
}
HISTORY_MAX_TURNS = 16

# 句子切分：在这些标点后断句（流式合成与整段合成共用）
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")


def _clean(text: str) -> str:
    """剥掉动作描写括号与首尾空白（配合 system prompt 的双保险，同 lumi_tts 思路）。"""
    text = re.sub(r"（[^）]*）|\([^)]*\)|【[^】]*】|\[[^\]]*\]", "", text)
    return text.strip()


def _prepare_clone_audio(audio: bytes, mime: str):
    """把参考音频裁到前 20 秒并尽量压缩。

    克隆音色每次合成都要内联整段参考音频，越小越好。有 ffmpeg：统一转
    24kHz 单声道 mp3（667KB wav → 约 60KB）；无 ffmpeg：wav 用标准库裁剪，
    mp3 原样保留。返回 (bytes, mime)。
    """
    stamp = int(time.time() * 1000)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        src = show_settings.CLONES_DIR / f"_in_{stamp}{show_settings.CLONE_MIMES[mime]}"
        dst = show_settings.CLONES_DIR / f"_out_{stamp}.mp3"
        try:
            show_settings.CLONES_DIR.mkdir(exist_ok=True)
            src.write_bytes(audio)
            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", str(src),
                 "-t", "20", "-ac", "1", "-ar", "24000", "-b:a", "48k", str(dst)],
                check=True, timeout=60, capture_output=True)
            out = dst.read_bytes()
            if out:
                print(f"[音色克隆] 参考音频已转码压缩：{len(audio) // 1024}KB → {len(out) // 1024}KB")
                return out, "audio/mpeg"
        except Exception as e:
            print(f"[音色克隆] ffmpeg 处理失败({e})，按原始文件入库")
        finally:
            for f in (src, dst):
                try:
                    f.unlink(missing_ok=True)
                except OSError:
                    pass
    if mime == "audio/wav":
        try:
            import io as _io
            import wave
            w = wave.open(_io.BytesIO(audio), "rb")
            rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            max_frames = rate * 20
            if w.getnframes() > max_frames:
                frames = w.readframes(max_frames)
                w.close()
                buf = _io.BytesIO()
                ww = wave.open(buf, "wb")
                ww.setnchannels(ch)
                ww.setsampwidth(width)
                ww.setframerate(rate)
                ww.writeframes(frames)
                ww.close()
                print(f"[音色克隆] wav 已裁剪到前 20 秒")
                return buf.getvalue(), "audio/wav"
            w.close()
        except Exception:
            pass
    return audio, mime


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # 静态服务不打日志，避免刷屏
        pass

    def end_headers(self):
        # index.html 改一动就要生效：禁止启发式缓存，每次都向服务器重新验证
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()


class _ShowHTTPServer(http.server.ThreadingHTTPServer):
    # HTTPServer 默认 allow_reuse_address=1，在 Windows 上等于 SO_REUSEADDR，
    # 两个实例会静默同绑一个端口、请求随机落到旧进程——这里关掉，端口被占直接报错。
    allow_reuse_address = False


class Stage:
    """直播舞台：把说话事件推给 Live2D 网页前端（live2d/frontend/index.html）。

    - WS 127.0.0.1:8768 广播 speaking / subtitle / clear 事件，前端驱动口型与表情
    - 静态文件 127.0.0.1:8000 服务 live2d 目录，OBS 浏览器源加载
      http://127.0.0.1:8000/frontend/
    只绑定本机回环；前端与 OBS 默认同机使用。
    """

    def __init__(self, web_root, ws_port=8768, web_port=8000):
        self._clients = set()
        self._loop = None
        self._start_ws(ws_port)
        self._start_web(web_root, web_port)

    def _start_ws(self, ws_port):
        async def handler(ws):
            self._clients.add(ws)
            try:
                async for _ in ws:  # 保持连接，忽略前端消息
                    pass
            finally:
                self._clients.discard(ws)

        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            async def serve():
                async with websockets.serve(handler, "127.0.0.1", ws_port):
                    await asyncio.Future()

            try:
                self._loop.run_until_complete(serve())
            except OSError as e:
                print(f"[舞台] WS 端口 {ws_port} 被占用——很可能已有一个节目在运行。"
                      f"请勿重复启动，本实例的舞台事件不可用。({e})")

        threading.Thread(target=run, daemon=True, name="stage-ws").start()

    def _start_web(self, web_root, web_port):
        handler = functools.partial(_QuietHandler, directory=str(web_root))
        try:
            httpd = _ShowHTTPServer(("127.0.0.1", web_port), handler)
        except OSError as e:
            print(f"[舞台] 网页端口 {web_port} 被占用——很可能已有一个节目在运行。({e})")
            return
        threading.Thread(target=httpd.serve_forever, daemon=True,
                         name="stage-web").start()

    def broadcast(self, obj):
        if not self._loop or not self._clients:
            return
        msg = json.dumps(obj, ensure_ascii=False)

        async def _send():
            for ws in list(self._clients):
                try:
                    await ws.send(msg)
                except Exception:
                    self._clients.discard(ws)

        asyncio.run_coroutine_threadsafe(_send(), self._loop)

    def speaking(self, speaker: str, on: bool):
        self.broadcast({"type": "speaking", "speaker": speaker, "on": on})

    def subtitle(self, speaker: str, text: str, more: bool = False, delta: str = ""):
        # more=True 表示同一次发言的后续句子（聊天页把 delta 拼进同一条气泡）
        self.broadcast({"type": "subtitle", "speaker": speaker,
                        "text": text, "more": more, "delta": delta})

    def clear(self):
        self.broadcast({"type": "clear"})


class Show:
    def __init__(self, enable_tts: bool, stage: "Stage | None" = None):
        self.bus = EventBus()
        self.state = StateMachine(self.bus)
        self.scheduler = SpeakerScheduler(active_speakers=["Lumi", "Nox"])
        self.arbiter = SpeechOutputArbiter(event_bus=self.bus)
        self.history = {name: [] for name in ("Lumi", "Nox")}
        self.enable_tts = enable_tts
        self.tts_ok = False
        self.emitters = {}
        self.stage = stage
        show_settings.apply_personas(PERSONAS)  # 设置文件里的人设覆盖默认（空=用默认）

        if enable_tts:
            try:
                import pyaudiowpatch as pyaudio
                self._pa = pyaudio.PyAudio()
                mimo_tts.init(pa_instance=self._pa)
                mimo_tts.set_volume(show_settings.get()["tts"].get("volume", 1.0))
                for name in ("Lumi", "Nox"):
                    self.emitters[name] = self._make_emitter(name)
                self.tts_ok = True
            except Exception as e:
                print(f"[TTS] 语音不可用，退化为纯文字模式: {e}")

    def _make_emitter(self, name: str):
        voice_name = get_speaker_config(name).voice_name
        voice_id = 音色库.get_voice_id(voice_name)
        model = 音色库.get_voice_model(voice_name)
        # 克隆音色（clone:名字）：发声时内联参考音频，并切换到音色复刻模型
        if show_settings.is_clone_voice(voice_id):
            clone = show_settings.get_clone(show_settings.clone_name(voice_id))
            if clone:
                try:
                    voice_id = show_settings.clone_data_uri(clone)
                    model = show_settings.CLONE_MODEL
                except OSError as e:
                    print(f"  [TTS] 克隆音色音频读取失败({e})，回退预置音色")
                    voice_id = 音色库.get_voice_id(voice_name)
                    model = 音色库.get_voice_model(voice_name)
            else:
                print(f"  [TTS] 克隆音色 {voice_id} 不在音色库中，回退预置音色")
                voice_id = 音色库.get_voice_id(voice_name)
        synth = mimo_tts.MimoSynth()
        synth.on_error = lambda msg, _n=name: self._tts_failed(_n, msg)
        return IndependentTTSEmitter(
            synth,
            voice_id=voice_id,
            model=model,
            cable_index=None,  # 直播接虚拟声卡时改成 cable 序号
        )

    def _tts_failed(self, speaker: str, message: str):
        """单句合成最终失败：聊天页系统提示（直播时用户看得见），不再静默。"""
        if self.stage:
            self.stage.broadcast({"type": "sys",
                                  "text": f"⚠️ {speaker} 语音：{message}"})

    def _reset_voice(self) -> bool:
        """重建全部发声链路：声卡流失效/默认输出变化后的自救，无需重启节目。"""
        if not self.tts_ok:
            return False
        for name in list(self.emitters):
            try:
                self.emitters[name].abort()
            except Exception:
                pass
            try:
                self.emitters[name] = self._make_emitter(name)
            except Exception as e:
                print(f"  [TTS] 重建 {name} 发声链路失败: {e}")
                self.tts_ok = False
                return False
        print("  [TTS] 发声链路已重置（新流绑定当前默认输出设备）")
        return True

    # ---- LLM（流式：边生成边分句发声，首句延迟大幅降低）----
    def _build_prompt(self, speaker: str, viewer_msgs: list, partner_last: str):
        """拼本次请求：更新历史，返回 (history, messages, kwargs, client, model_id)。"""
        client, model_id, brand = fast_brain.resolve_call_target()
        history = self.history[speaker]
        if viewer_msgs:
            who = viewer_msgs[-1].get("label") or "观众"
            history.append({"role": "user",
                            "content": f"[弹幕] {who}：{viewer_msgs[-1]['text']}"})
        elif partner_last:
            history.append({"role": "user", "content": f"[stage note] {partner_last}"})
        else:
            history.append({"role": "user", "content": "[stage note] 直播继续，随便聊一句"})
        trimmed = history[-HISTORY_MAX_TURNS * 2:]
        messages = [{"role": "system", "content": PERSONAS[speaker]}] + trimmed
        kwargs = {k: v for k, v in brand.items()
                  if k in ("temperature", "top_p", "frequency_penalty", "presence_penalty")}
        return history, messages, kwargs, client, model_id

    def ask_and_speak(self, speaker: str, viewer_msgs: list, partner_last: str) -> str:
        """流式生成并同步发声。返回清洗后的完整回复（供搭档镜像）；失败返回 ""。"""
        history, messages, kwargs, client, model_id = self._build_prompt(
            speaker, viewer_msgs, partner_last)
        output = self.arbiter.request_start(speaker=speaker, source="chat",
                                            policy=POLICY_QUEUE)
        if output is None:
            print(f"  [仲裁] {speaker} 排队等待发言权")
            history.pop()  # 没说成，撤掉这条输入避免历史污染
            return ""

        emitter = self.emitters.get(speaker) if self.tts_ok else None
        if self.stage:
            self.stage.speaking(speaker, True)

        state = {"full": "", "shown": "", "buffer": "", "spoken": 0}

        def say(sentence: str):
            """一句话：清洗 → 控制台 → 字幕（气泡增量式）→ 喂给发声链路。"""
            cleaned = _clean(sentence)
            if not cleaned:
                return
            print(f"  {speaker}: {cleaned}")
            if self.stage:
                self.stage.subtitle(speaker, state["shown"] + cleaned,
                                    more=state["spoken"] > 0, delta=cleaned)
            if emitter:
                emitter.feed(cleaned)
            state["shown"] += cleaned
            state["spoken"] += 1
            if state["spoken"] == 1:
                print(f"  [节目] {speaker} 首句出声（LLM 启动后 "
                      f"{time.time() - t0:.1f}s）")

        t0 = time.time()
        try:
            stream = None
            for attempt in range(3):
                try:
                    stream = client.chat.completions.create(
                        model=model_id, messages=messages, max_tokens=120,
                        stream=True, **kwargs)
                    break
                except Exception as e:
                    if attempt == 2:
                        print(f"  [LLM] 连续 3 次请求失败，改说占位句: {e}")
                        print("        （免费档 API 限流 429 时，稍等或升级套餐；历史已保留）")
                    else:
                        time.sleep(3 * (attempt + 1))
            if stream is not None:
                for event in stream:
                    choices = getattr(event, "choices", None)
                    if not choices:
                        continue
                    piece = getattr(choices[0].delta, "content", None)
                    if not piece:
                        continue
                    state["full"] += piece
                    state["buffer"] += piece
                    parts = [s for s in _SENT_SPLIT.split(state["buffer"]) if s.strip()]
                    if len(parts) > 1:
                        for s in parts[:-1]:
                            say(s)
                        state["buffer"] = parts[-1]
                if state["buffer"].strip():
                    say(state["buffer"])
                    state["buffer"] = ""
        except Exception as e:
            print(f"  [LLM] 流式输出中断（已说部分保留）: {e}")

        line = _clean(state["full"])
        if not state["full"]:
            try:
                history.pop()  # LLM 彻底失败：撤掉这条输入，避免历史污染
            except Exception:
                pass
        else:
            history.append({"role": "assistant", "content": line or "……"})

        try:
            if state["spoken"] == 0:
                say(line or "……")  # 整段不足一句 / LLM 失败：一次性说出占位
            if emitter:
                emitter.finish()  # 阻塞到语音播完
        except Exception as e:
            # 单次播放失败不再永久哑掉：重建该角色发声链路，下一句自动恢复
            print(f"  [TTS] 本句播放失败({e})，重建发声链路后下一句重试")
            try:
                self.emitters[speaker].abort()
            except Exception:
                pass
            try:
                self.emitters[speaker] = self._make_emitter(speaker)
            except Exception as e2:
                print(f"  [TTS] 重建失败({e2})，本场退化为纯文字")
                self.tts_ok = False
        if self.stage:
            self.stage.speaking(speaker, False)
        self.arbiter.mark_done(output.output_id)
        return line

    # ---- 发声（非流式定长文本：试音等场景用；走真实仲裁）----
    def speak(self, speaker: str, text: str) -> bool:
        output = self.arbiter.request_start(speaker=speaker, source="test",
                                            policy=POLICY_QUEUE)
        if output is None:
            print(f"  [仲裁] {speaker} 排队等待发言权")
            return False
        print(f"  {speaker}: {text}")
        if self.stage:
            self.stage.speaking(speaker, True)
            self.stage.subtitle(speaker, text)
        if self.tts_ok:
            try:
                emitter = self.emitters[speaker]
                for sentence in [s for s in _SENT_SPLIT.split(text) if s.strip()]:
                    emitter.feed(sentence)
                emitter.finish()  # 阻塞到语音播完
            except Exception as e:
                # 单次播放失败不再永久哑掉：重建该角色发声链路，下一句自动恢复
                print(f"  [TTS] 本句播放失败({e})，重建发声链路后下一句重试")
                try:
                    self.emitters[speaker].abort()
                except Exception:
                    pass
                try:
                    self.emitters[speaker] = self._make_emitter(speaker)
                except Exception as e2:
                    print(f"  [TTS] 重建失败({e2})，本场退化为纯文字")
                    self.tts_ok = False
        if self.stage:
            self.stage.speaking(speaker, False)
        self.arbiter.mark_done(output.output_id)
        return True

    def run(self, turns: int, interactive: bool, wait_idle: bool = False):
        self.state.transition_to(State.OPENING)
        self.state.transition_to(State.CHATTING)
        self.scheduler.reset_rotation("Lumi")
        partner_last = {"Lumi": "", "Nox": ""}
        print("\n=== 双 AI 语音电台开播 ==="
              + ("\n（输入弹幕后回车发送；@ Lumi 或 Nox 指定回答；直接回车=自聊；exit 退出）\n"
                 if interactive else "\n（自动模式）\n"))
        try:
            for turn in range(turns):
                viewer_msgs = []
                if interactive:
                    try:
                        line = input("弹幕> ").strip()
                    except EOFError:
                        line = ""
                    if line.lower() in ("exit", "quit", "退出"):
                        break
                    if line:
                        who = line.split("：", 1)[0].split(":", 1)[0] or "观众"
                        self.scheduler.enqueue_input(f"弹幕：{line}",
                                                     source="danmaku", label=who)
                        viewer_msgs = self.scheduler.pop_all_inputs(max_items=8)
                else:
                    viewer_msgs = self.scheduler.pop_all_inputs(max_items=8)
                    if not viewer_msgs and wait_idle:
                        # 网页弹幕模式：没人发言就挂起等待，不触发 AI 自聊
                        while not viewer_msgs:
                            time.sleep(0.5)
                            viewer_msgs = self.scheduler.pop_all_inputs(max_items=8)

                last_text = viewer_msgs[-1]["text"] if viewer_msgs else None
                speaker = self.scheduler.pick_speaker(last_text)
                if viewer_msgs:
                    print(f"  >> 弹幕路由给 {speaker}")
                line = self.ask_and_speak(speaker, viewer_msgs, partner_last[speaker])
                if line:
                    for other in ("Lumi", "Nox"):
                        if other != speaker:
                            partner_last[other] = f"[{speaker} 说] {line}"
                self.scheduler.advance_from(speaker)
                time.sleep(0.2)
        except KeyboardInterrupt:
            print("\n(收到中断)")
        finally:
            self.state.transition_to(State.ENDING)
            self.state.transition_to(State.IDLE)
            if self.stage:
                self.stage.speaking("Lumi", False)
                self.stage.speaking("Nox", False)
                self.stage.clear()
            if self.enable_tts and hasattr(self, "_pa"):
                try:
                    self._pa.terminate()
                except Exception:
                    pass
            print("=== 下播 ===")


# ======= 网页弹幕聊天页（--web-chat）=======

CHAT_PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lumi &amp; Nox 电台</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font-family:"Microsoft YaHei",system-ui,sans-serif; background:#0b0a08;
         color:#eee; height:100vh; display:flex; flex-direction:column; overflow:hidden; }
  header { padding:12px 18px; border-bottom:1px solid #2a2418; font-weight:bold;
           letter-spacing:1px; color:#F3D37A; flex:none; display:flex;
           justify-content:space-between; align-items:center; gap:10px; }
  header small { color:#8a8064; font-weight:normal; margin-left:10px; font-size:12px; }
  main { flex:1; min-height:0; display:grid;
         grid-template-columns:minmax(0,1.4fr) minmax(300px,1fr); gap:10px; padding:10px; }
  #stagewrap { min-height:0; border:1px solid #2a2418; border-radius:12px; overflow:hidden;
               background:radial-gradient(ellipse at 50% 72%, #191408 0%, #0b0a08 78%); }
  #stageframe { width:100%; height:100%; border:0; display:block; }
  #chatcol { display:flex; flex-direction:column; min-height:0;
             border:1px solid #2a2418; border-radius:12px; background:#0e0d0a; }
  #log { flex:1; overflow-y:auto; padding:14px 16px; display:flex;
         flex-direction:column; gap:10px; }
  .msg { max-width:88%; padding:9px 13px; border-radius:12px; line-height:1.55;
         white-space:pre-wrap; word-break:break-word; }
  .lumi { align-self:flex-start; background:#241d0e; border:1px solid #4a3c17; color:#F3D37A; }
  .nox  { align-self:flex-start; background:#101c22; border:1px solid #1f4450; color:#9fd8e8; }
  .me   { align-self:flex-end;  background:#1d2413; border:1px solid #3c4d22; color:#cfe3a8; }
  .sys  { align-self:center; background:none; color:#6f6a58; font-size:12px; }
  .name { font-size:11px; opacity:.65; margin-bottom:2px; }
  form { display:flex; gap:8px; padding:10px 12px 6px; border-top:1px solid #2a2418; flex:none; }
  input { flex:1; background:#15130d; border:1px solid #3a3423; color:#eee;
          border-radius:8px; padding:10px 12px; font-size:14px; outline:none; }
  input:focus { border-color:#F3D37A; }
  button { background:#F3D37A; border:none; color:#1a1508; font-weight:bold;
           border-radius:8px; padding:0 22px; cursor:pointer; }
  .hint { padding:6px 14px 10px; color:#6f6a58; font-size:12px; flex:none; }
  .hint a { color:#8a8064; }
  #btnSettings { background:none; border:1px solid #3a3423; color:#8a8064; font-size:12px;
                 border-radius:6px; padding:3px 12px; cursor:pointer; font-weight:normal;
                 letter-spacing:0; flex:none; }
  #btnSettings:hover { color:#F3D37A; border-color:#F3D37A; }
  #overlay, #cloneOverlay, #modelOverlay { position:fixed; inset:0; background:rgba(0,0,0,.62); display:none;
             align-items:center; justify-content:center; z-index:50; }
  #overlay.open, #cloneOverlay.open, #modelOverlay.open { display:flex; }
  #panel, #clonePanel, #modelPanel { width:min(660px, 94vw); max-height:88vh; overflow-y:auto; background:#12100b;
           border:1px solid #3a3423; border-radius:14px; padding:16px 20px 18px; }
  #panel h2 { margin:2px 0 10px; font-size:16px; color:#F3D37A; }
  #panel h2 small { color:#6f6a58; font-weight:normal; font-size:12px; margin-left:8px; }
  #panel fieldset { border:1px solid #2a2418; border-radius:10px; margin:10px 0;
                    padding:8px 12px 12px; min-width:0; }
  #panel legend { font-size:13px; color:#F3D37A; padding:0 6px; }
  .frow { display:flex; gap:10px; margin:8px 0; align-items:center; flex-wrap:wrap; }
  .frow > label { width:86px; flex:none; font-size:13px; color:#9a9378; text-align:right; }
  .frow input[type=text], .frow input[type=password], .frow input[type=number],
  .frow select, .frow textarea {
    flex:1; min-width:0; background:#15130d; border:1px solid #3a3423; color:#eee;
    border-radius:8px; padding:8px 10px; font-size:13px; outline:none; font-family:inherit; }
  .frow input:focus, .frow textarea:focus, .frow select:focus { border-color:#F3D37A; }
  .frow textarea { min-height:76px; resize:vertical; line-height:1.5; }
  .frow .note { font-size:12px; color:#6f6a58; flex:1; min-width:120px; }
  .keystate { font-size:12px; color:#6f6a58; flex:none; }
  .mini { background:none; border:1px solid #3a3423; color:#8a8064; font-size:12px;
          border-radius:6px; padding:4px 12px; cursor:pointer; flex:none; }
  .mini:hover { color:#F3D37A; border-color:#F3D37A; }
  .mini:disabled { opacity:.5; cursor:wait; }
  .fbtn { background:#F3D37A; border:none; color:#1a1508; font-weight:bold; border-radius:8px;
          padding:8px 18px; cursor:pointer; }
  .fbtn:disabled { opacity:.6; }
  .fbtn.ghost { background:none; border:1px solid #3a3423; color:#8a8064; font-weight:normal; }
  .fbtn.ghost:hover { color:#F3D37A; border-color:#F3D37A; }
  .foot { display:flex; justify-content:space-between; gap:10px; margin-top:14px;
          align-items:center; }
  #obsurl { width:100%; background:#15130d; border:1px solid #3a3423; color:#8a8064;
            border-radius:6px; padding:6px 8px; font-size:12px; }
  #toast { position:fixed; left:50%; top:12%; transform:translateX(-50%); background:#1d2413;
           border:1px solid #3c4d22; color:#cfe3a8; padding:8px 16px; border-radius:10px;
           font-size:13px; opacity:0; transition:opacity .25s; pointer-events:none; z-index:60;
           max-width:80vw; }
  #toast.show { opacity:1; }
  #fixaudio { background:none; border:1px solid #3a3423; color:#8a8064; font-size:12px;
              border-radius:6px; padding:2px 10px; cursor:pointer; margin-left:8px; }
  #fixaudio:hover { color:#F3D37A; border-color:#F3D37A; }
  @media (max-width: 860px) {
    main { grid-template-columns: 1fr; grid-template-rows: 42vh minmax(0,1fr); }
  }
</style>
</head>
<body>
<header><span>Lumi &amp; Nox 电台 · 舞台 <small id="wsstate">连接中…</small></span>
  <button type="button" id="btnSettings">⚙ 设置</button></header>
<main>
  <div id="stagewrap"><iframe id="stageframe"></iframe></div>
  <section id="chatcol">
    <div id="log"><div class="sys">弹幕已就绪 —— 说点什么吧；开头写「Lumi：」或「Nox：」可指定谁回答</div></div>
    <form id="f"><input id="t" autocomplete="off" placeholder="输入弹幕，回车发送…"><button>发送</button></form>
    <div class="hint">语音从本机扬声器播出；独立舞台页（供 OBS）：
      <a href="http://127.0.0.1:__WEB_PORT__/frontend/" target="_blank" id="footerStage">/frontend/</a>
      <button type="button" id="fixaudio">🔊 无声？重置发声</button></div>
  </section>
</main>
<script>
const log = document.getElementById("log");
const MAX_BUBBLES = 200;
function bubble(cls, name, text){
  const d = document.createElement("div"); d.className = "msg " + cls;
  if (name) { const n = document.createElement("div"); n.className = "name";
              n.textContent = name; d.appendChild(n); }
  d.appendChild(document.createTextNode(text));
  log.appendChild(d);
  while (log.children.length > MAX_BUBBLES) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
  return d;
}
let ws, lastBubbleRef = null;
function connect(){
  ws = new WebSocket("ws://127.0.0.1:__WS_PORT__/ws");
  ws.onopen  = () => document.getElementById("wsstate").textContent = "● 舞台已连接";
  ws.onclose = () => { document.getElementById("wsstate").textContent = "○ 舞台断开，3s 后重连…";
                       setTimeout(connect, 3000); };
  ws.onmessage = (ev) => { try { const m = JSON.parse(ev.data);
    if (m.type === "subtitle") {
      // 同一次发言的后续句子（more）拼进同一条气泡，避免刷屏
      if (m.more && lastBubbleRef && lastBubbleRef.speaker === m.speaker) {
        lastBubbleRef.node.appendChild(document.createTextNode(m.delta || m.text));
        log.scrollTop = log.scrollHeight;
      } else {
        const node = bubble(m.speaker === "Lumi" ? "lumi" : "nox", m.speaker, m.text);
        lastBubbleRef = { speaker: m.speaker, node };
      }
    } else if (m.type === "sys") {
      bubble("sys", "", m.text);
    }
  } catch (e) {} };
}
connect();
document.getElementById("f").addEventListener("submit", async (e) => {
  e.preventDefault();
  const t = document.getElementById("t"); const text = t.value.trim(); if (!text) return;
  bubble("me", "我", text); t.value = "";
  try {
    const r = await fetch("/danmaku", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
    if (!r.ok) bubble("sys", "", "发送失败 HTTP " + r.status);
  } catch (err) { bubble("sys", "", "发送失败：" + err); }
});
document.getElementById("fixaudio").addEventListener("click", async () => {
  try {
    const r = await fetch("/audio-reset", { method: "POST" });
    const j = await r.json();
    bubble("sys", "", j.ok ? "发声链路已重置，再发条弹幕试试" : "重置未生效（TTS 当前不可用）");
  } catch (err) { bubble("sys", "", "重置请求失败：" + err); }
});
</script>

<div id="overlay">
  <div id="panel">
    <h2>设置<small>保存后即时生效，无需重启</small></h2>
    <fieldset>
      <legend>🧠 大脑（LLM · OpenAI 兼容接口）</legend>
      <div class="frow"><label>接口地址</label><input type="text" id="s_llm_base"
           placeholder="https://api.example.com/v1"></div>
      <div class="frow"><label>API Key</label><input type="password" id="s_llm_key">
        <span class="keystate" id="s_llm_keystate"></span></div>
      <div class="frow"><label>模型名</label><input type="text" id="s_llm_model"
           placeholder="如 doubao-seed-2-0-lite-260428"></div>
      <div class="frow"><label>温度</label>
        <input type="number" id="s_llm_temp" min="0" max="2" step="0.1" style="flex:none;width:96px">
        <span class="note">越高回复越放飞，0.2 ~ 1.0 常用</span></div>
    </fieldset>
    <fieldset>
      <legend>🔊 声音（MiMo TTS）</legend>
      <div class="frow"><label>接口地址</label><input type="text" id="s_tts_base"
           placeholder="https://api.xiaomimimo.com/v1"></div>
      <div class="frow"><label>API Key</label><input type="password" id="s_tts_key">
        <span class="keystate" id="s_tts_keystate"></span></div>
      <div class="frow"><label>TTS 模型</label><input type="text" id="s_tts_model"
           placeholder="mimo-v2.5-tts"></div>
      <div class="frow"><label>Lumi 音色</label><input type="text" id="s_voice_lumi" list="voiceList">
        <button type="button" class="mini" id="btnTestLumi">试音</button>
        <button type="button" class="mini" id="btnCloneLumi">克隆</button></div>
      <div class="frow"><label>Nox 音色</label><input type="text" id="s_voice_nox" list="voiceList">
        <button type="button" class="mini" id="btnTestNox">试音</button>
        <button type="button" class="mini" id="btnCloneNox">克隆</button></div>
      <div class="frow"><label>音量</label>
        <input type="range" id="s_vol" min="0" max="2" step="0.1" style="flex:1">
        <span class="note" id="s_vol_val" style="flex:none;width:36px;text-align:right">100%</span></div>
      <datalist id="voiceList"></datalist>
      <div class="frow"><span class="note">音色可选预置：冰糖 / 茉莉 / 苏打 / 白桦 / Mia / Chloe / Milo / Dean / mimo_default，也可直接填平台支持的其它音色名。点「克隆」可上传一段录音，用 MiMo 音色复刻定制专属音色（clone:名字 形式保存）。</span></div>
    </fieldset>
    <fieldset>
      <legend>🎭 人设（想改性格改这里）</legend>
      <div class="frow"><label>Lumi</label><textarea id="s_per_lumi"></textarea></div>
      <div class="frow"><label>Nox</label><textarea id="s_per_nox"></textarea></div>
      <div class="frow"><span class="note">保存后下一句话开始生效；对话历史不会清空，如需彻底重新开始请点左下角「清空对话历史」。</span></div>
    </fieldset>
    <fieldset>
      <legend>🖼️ 舞台</legend>
      <div class="frow"><label>字幕</label>
        <label style="width:auto;text-align:left;display:flex;align-items:center;gap:6px">
          <input type="checkbox" id="s_sub" style="width:auto;flex:none">
          舞台页底部显示说话字幕</label></div>
      <div class="frow"><label>Lumi 形象</label><select id="s_model_lumi"></select></div>
      <div class="frow"><label>Nox 形象</label><select id="s_model_nox"></select></div>
      <div class="frow"><label>皮套库</label>
        <button type="button" class="mini" id="btnModelImport">导入皮套 zip…</button>
        <span class="note">支持 Cubism 4 模型（含 .model3.json 的 zip 包）；
          也可手动把模型文件夹放进 live2d/ 目录，下拉框会自动发现。</span></div>
      <div class="frow"><label>动作幅度</label>
        <input type="range" id="s_amp" min="0.5" max="2" step="0.1" style="flex:1">
        <span class="note" id="s_amp_val" style="flex:none;width:36px;text-align:right">1.5×</span></div>
      <div class="frow"><label>OBS 地址</label><input type="text" id="obsurl" readonly>
        <span class="note">开直播时在 OBS 添加「浏览器源」粘贴此地址（透明背景，字幕常开）</span></div>
    </fieldset>
    <div class="foot">
      <button type="button" class="fbtn ghost" id="btnClearHist">清空对话历史</button>
      <span style="display:flex;gap:10px">
        <button type="button" class="fbtn ghost" id="btnCancel">取消</button>
        <button type="button" class="fbtn" id="btnSave">保存并应用</button>
      </span>
    </div>
  </div>
</div>
<div id="toast"></div>

<div id="cloneOverlay">
  <div id="clonePanel">
    <h2>克隆音色<small id="cloneTarget">→ Lumi</small></h2>
    <fieldset>
      <legend>参考音频</legend>
      <div class="frow"><label>音色名称</label><input type="text" id="cloneName"
           maxlength="40" placeholder="如：我的音色"></div>
      <div class="frow"><label>音频文件</label><input type="file" id="cloneFile"
           accept=".mp3,.wav,audio/mpeg,audio/wav"></div>
      <div class="frow"><span class="note" id="cloneFileInfo">未选择文件</span></div>
      <div class="frow"><span class="note">要求：mp3 或 wav，清晰人声，建议 10~30 秒、
        无背景音乐、无混响；文件不超过 10MB。上传后自动登记到克隆音色库、应用为该角色
        音色并试音。MiMo 音色复刻（mimo-v2.5-tts-voiceclone）当前限时免费。</span></div>
    </fieldset>
    <fieldset>
      <legend>已有克隆音色</legend>
      <div id="cloneList"></div>
    </fieldset>
    <div class="foot">
      <button type="button" class="fbtn ghost" id="btnCloneCancel">取消</button>
      <button type="button" class="fbtn" id="btnCloneUpload">上传并应用</button>
    </div>
  </div>
</div>

<div id="modelOverlay">
  <div id="modelPanel">
    <h2>导入皮套<small>Live2D 模型 zip 包</small></h2>
    <fieldset>
      <legend>模型包</legend>
      <div class="frow"><label>皮套名称</label><input type="text" id="modelName"
           maxlength="40" placeholder="留空则用 zip 文件名"></div>
      <div class="frow"><label>zip 文件</label><input type="file" id="modelFile"
           accept=".zip,application/zip,application/x-zip-compressed"></div>
      <div class="frow"><span class="note" id="modelFileInfo">未选择文件</span></div>
      <div class="frow"><label>应用到</label>
        <select id="modelApplyTo" style="flex:none;width:190px">
          <option value="">仅入库，稍后手动选</option>
          <option value="lumi">立即应用为 Lumi 形象</option>
          <option value="nox">立即应用为 Nox 形象</option>
        </select></div>
      <div class="frow"><span class="note">要求：Cubism 4 模型的 zip 压缩包（内含
        *.model3.json 及其 moc、贴图），不超过 200MB。导入时会校验模型文件完整性，
        缺文件的压缩包会被拒绝并自动清理，不会留半成品。Cubism 2 旧模型
        （*.model.json）暂不支持。</span></div>
    </fieldset>
    <div class="foot">
      <button type="button" class="fbtn ghost" id="btnModelCancel">取消</button>
      <button type="button" class="fbtn" id="btnModelUpload">导入</button>
    </div>
  </div>
</div>

<script>
// ===== 设置面板 + 舞台 iframe 装配 =====
const stageframe = document.getElementById("stageframe");
const overlay = document.getElementById("overlay");
const WEB_PORT = "__WEB_PORT__";
let curSettings = null;

function toast(msg){
  const t = document.getElementById("toast");
  t.textContent = msg; t.classList.add("show");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 2800);
}
const v = id => document.getElementById(id).value.trim();

function stageSrc(s, withBg){
  const p = new URLSearchParams();
  p.set("subtitle", s.stage.subtitle ? "1" : "0");
  if (s.stage.lumi_model) p.set("lumi", s.stage.lumi_model);
  if (s.stage.nox_model) p.set("nox", s.stage.nox_model);
  p.set("amp", s.stage.amplitude || 1.5);
  if (withBg) p.set("bg", "1");   // 内嵌 iframe 铺深色底；OBS 地址不带 bg，保持透明
  p.set("v", Date.now());
  return `http://127.0.0.1:${WEB_PORT}/frontend/?` + p.toString();
}
function applyStageSrc(s){ stageframe.src = stageSrc(s, true); }

async function api(path, opts){
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}

async function initStage(){
  try { curSettings = await api("/settings"); }
  catch (e) { curSettings = { stage: { subtitle: false, lumi_model: "", nox_model: "" } }; }
  applyStageSrc(curSettings);
  const fl = document.getElementById("footerStage");
  if (fl && curSettings) fl.href = stageSrc(curSettings, false);
}

function fillSelect(sel, models, current){
  sel.innerHTML = "";
  const def = document.createElement("option");
  def.value = ""; def.textContent = "默认（miku / 樱花miku）";
  sel.appendChild(def);
  for (const m of models){
    const o = document.createElement("option");
    o.value = m.path; o.textContent = m.label;
    sel.appendChild(o);
  }
  sel.value = current || "";
  if (sel.selectedIndex < 0) sel.value = "";  // 当前值不在列表（文件被删）时回默认
}

async function openSettings(){
  overlay.classList.add("open");
  try {
    const [ml, s] = await Promise.all([api("/models"), api("/settings")]);
    curSettings = s;
    const vl = document.getElementById("voiceList");
    vl.innerHTML = (ml.voices || []).map(n => `<option value="${n}">`).join("")
      + Object.keys(s.clones || {}).map(n => `<option value="clone:${n}">`).join("");
    document.getElementById("s_llm_base").value = s.llm.base_url;
    document.getElementById("s_llm_key").value = "";
    document.getElementById("s_llm_key").placeholder = s.llm.api_key_set ? "已配置 —— 留空保持不变" : "未配置";
    document.getElementById("s_llm_keystate").textContent = s.llm.api_key_set ? "已配置" : "未配置";
    document.getElementById("s_llm_model").value = s.llm.model;
    document.getElementById("s_llm_temp").value = s.llm.temperature;
    document.getElementById("s_tts_base").value = s.tts.base_url;
    document.getElementById("s_tts_key").value = "";
    document.getElementById("s_tts_key").placeholder = s.tts.api_key_set ? "已配置 —— 留空保持不变" : "未配置";
    document.getElementById("s_tts_keystate").textContent = s.tts.api_key_set ? "已配置" : "未配置";
    document.getElementById("s_tts_model").value = s.tts.model;
    document.getElementById("s_voice_lumi").value = s.tts.lumi_voice;
    document.getElementById("s_voice_nox").value = s.tts.nox_voice;
    document.getElementById("s_vol").value = s.tts.volume ?? 1;
    document.getElementById("s_vol_val").textContent =
      Math.round((s.tts.volume ?? 1) * 100) + "%";
    document.getElementById("s_per_lumi").value = s.persona.lumi;
    document.getElementById("s_per_nox").value = s.persona.nox;
    document.getElementById("s_sub").checked = !!s.stage.subtitle;
    fillSelect(document.getElementById("s_model_lumi"), ml.models, s.stage.lumi_model);
    fillSelect(document.getElementById("s_model_nox"), ml.models, s.stage.nox_model);
    document.getElementById("s_amp").value = s.stage.amplitude || 1.5;
    document.getElementById("s_amp_val").textContent = (s.stage.amplitude || 1.5) + "×";
    document.getElementById("obsurl").value = stageSrc(s, false);
  } catch (e) { toast("读取设置失败：" + e.message); }
}

// 动作幅度滑杆：拖动时实时刷新数值标签
document.getElementById("s_amp").addEventListener("input",
  e => document.getElementById("s_amp_val").textContent = e.target.value + "×");

function collectSettings(){
  return {
    llm: { base_url: v("s_llm_base"), api_key: v("s_llm_key"), model: v("s_llm_model"),
           temperature: parseFloat(document.getElementById("s_llm_temp").value) || 0.8 },
    tts: { base_url: v("s_tts_base"), api_key: v("s_tts_key"), model: v("s_tts_model"),
           lumi_voice: v("s_voice_lumi"), nox_voice: v("s_voice_nox"),
           volume: (() => { const x = parseFloat(document.getElementById("s_vol").value);
                            return Number.isFinite(x) ? x : 1; })() },
    persona: { lumi: document.getElementById("s_per_lumi").value,
               nox: document.getElementById("s_per_nox").value },
    stage: { subtitle: document.getElementById("s_sub").checked,
             lumi_model: v("s_model_lumi"), nox_model: v("s_model_nox"),
             amplitude: parseFloat(document.getElementById("s_amp").value) || 1.5 },
  };
}

document.getElementById("btnSettings").addEventListener("click", openSettings);
document.getElementById("btnCancel").addEventListener("click", () => overlay.classList.remove("open"));
overlay.addEventListener("click", e => { if (e.target === overlay) overlay.classList.remove("open"); });

document.getElementById("btnSave").addEventListener("click", async () => {
  const before = curSettings ? { ...curSettings.stage } : null;
  const body = collectSettings();
  const btn = document.getElementById("btnSave");
  btn.disabled = true;
  try {
    await api("/settings", { method: "POST", headers: { "Content-Type": "application/json" },
                             body: JSON.stringify(body) });
    curSettings = { ...(curSettings || {}), stage: body.stage };
    const changed = before && (before.subtitle !== body.stage.subtitle ||
                    before.lumi_model !== body.stage.lumi_model ||
                    before.nox_model !== body.stage.nox_model);
    if (changed) applyStageSrc(curSettings);
    document.getElementById("obsurl").value = stageSrc(curSettings, false);
    toast("已保存并生效" + (changed ? "（舞台已按新形象重载）" : ""));
    overlay.classList.remove("open");
  } catch (e) { toast("保存失败：" + e.message); }
  finally { btn.disabled = false; }
});

async function testVoice(speaker, btn){
  if (btn.disabled) return;
  btn.disabled = true; const old = btn.textContent; btn.textContent = "合成中…";
  try {
    const r = await api("/test-voice", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ speaker, tts: collectSettings().tts }) });
    toast(r.ok ? `${speaker} 试音已播放` : `${speaker} 试音失败：TTS 不可用（检查 key / 模型名）`);
  } catch (e) { toast("试音失败：" + e.message); }
  finally { btn.disabled = false; btn.textContent = old; }
}
document.getElementById("btnTestLumi").addEventListener("click", e => testVoice("Lumi", e.target));
document.getElementById("btnTestNox").addEventListener("click", e => testVoice("Nox", e.target));

document.getElementById("btnClearHist").addEventListener("click", async () => {
  try {
    await api("/history-clear", { method: "POST" });
    toast("AI 对话记忆已清空（屏上已有气泡保留）");
  } catch (e) { toast("清空失败：" + e.message); }
});

// ===== 克隆音色 =====
const cloneOverlay = document.getElementById("cloneOverlay");
const CLONE_MIMES = ["audio/mpeg", "audio/mp3", "audio/wav"];
let cloneTarget = null, cloneData = null;

function openClone(speaker){
  cloneTarget = speaker; cloneData = null;
  document.getElementById("cloneTarget").textContent = "→ " + speaker;
  document.getElementById("cloneName").value = "我的音色";
  document.getElementById("cloneFile").value = "";
  document.getElementById("cloneFileInfo").textContent = "未选择文件";
  const btn = document.getElementById("btnCloneUpload");
  btn.disabled = false; btn.textContent = "上传并应用";
  cloneOverlay.classList.add("open");
  renderClones();
}
document.getElementById("btnCloneLumi").addEventListener("click", () => openClone("Lumi"));
document.getElementById("btnCloneNox").addEventListener("click", () => openClone("Nox"));
document.getElementById("btnCloneCancel").addEventListener("click",
  () => cloneOverlay.classList.remove("open"));
cloneOverlay.addEventListener("click",
  e => { if (e.target === cloneOverlay) cloneOverlay.classList.remove("open"); });

document.getElementById("cloneFile").addEventListener("change", ev => {
  cloneData = null;
  const f = ev.target.files && ev.target.files[0];
  const info = document.getElementById("cloneFileInfo");
  if (!f) { info.textContent = "未选择文件"; return; }
  const ext = (f.name.split(".").pop() || "").toLowerCase();
  let mime = (f.type || "").toLowerCase();
  if (!CLONE_MIMES.includes(mime))
    mime = ext === "mp3" ? "audio/mpeg" : ext === "wav" ? "audio/wav" : "";
  if (!mime) { info.textContent = "不支持的格式（仅 mp3 / wav）"; return; }
  if (f.size > 10 * 1024 * 1024) { info.textContent = "文件超过 10MB 上限"; return; }
  const r = new FileReader();
  r.onload = () => {
    cloneData = { dataUri: r.result, mime };
    info.textContent = `已选择：${f.name}（${(f.size / 1024).toFixed(0)}KB · ${mime}）`;
  };
  r.onerror = () => info.textContent = "文件读取失败";
  r.readAsDataURL(f);
});

document.getElementById("btnCloneUpload").addEventListener("click", async () => {
  const btn = document.getElementById("btnCloneUpload");
  const name = document.getElementById("cloneName").value.trim();
  if (!name) { toast("请先填写音色名称"); return; }
  if (!cloneData) { toast("请先选择参考音频文件（mp3 / wav）"); return; }
  btn.disabled = true; btn.textContent = "上传中…";
  try {
    await api("/voice-clone", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, mime: cloneData.mime,
        audioDataUri: cloneData.dataUri, applyTo: cloneTarget.toLowerCase() }) });
    const voiceVal = "clone:" + name;
    (cloneTarget === "Lumi" ? document.getElementById("s_voice_lumi")
                            : document.getElementById("s_voice_nox")).value = voiceVal;
    const vl = document.getElementById("voiceList");
    if (![...vl.options].some(o => o.value === voiceVal))
      vl.appendChild(new Option("", voiceVal));
    cloneOverlay.classList.remove("open");
    toast(`音色「${name}」已应用为 ${cloneTarget} 的音色，正在试音…`);
    testVoice(cloneTarget, cloneTarget === "Lumi"
      ? document.getElementById("btnTestLumi") : document.getElementById("btnTestNox"));
  } catch (e) { toast("克隆失败：" + e.message); }
  finally { btn.disabled = false; btn.textContent = "上传并应用"; }
});

// ===== 克隆音色管理（列表 / 应用 / 删除）=====
async function renderClones(){
  const box = document.getElementById("cloneList");
  try {
    const s = await api("/settings");
    const names = Object.keys(s.clones || {});
    if (!names.length) {
      box.innerHTML = '<div class="frow"><span class="note">暂无克隆音色 —— 用上面表单上传一段录音创建</span></div>';
      return;
    }
    box.innerHTML = "";
    for (const name of names) {
      const row = document.createElement("div");
      row.className = "frow";
      const label = document.createElement("span");
      label.className = "note"; label.style.flex = "1";
      label.textContent = name + "（" + (s.clones[name].mime === "audio/wav" ? "wav" : "mp3") + "）";
      row.appendChild(label);
      const mk = (txt, fn, title) => {
        const b = document.createElement("button");
        b.type = "button"; b.className = "mini"; b.textContent = txt; b.title = title;
        b.addEventListener("click", () => fn(b));
        row.appendChild(b);
      };
      mk("→Lumi", b => applyClone(name, "lumi", b), "应用为 Lumi 音色");
      mk("→Nox", b => applyClone(name, "nox", b), "应用为 Nox 音色");
      mk("删除", b => deleteClone(name, b), "删除此音色");
      box.appendChild(row);
    }
  } catch (e) {
    box.innerHTML = '<div class="frow"><span class="note">克隆音色列表读取失败</span></div>';
  }
}

async function applyClone(name, target, btn){
  btn.disabled = true;
  try {
    await api("/settings", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tts: target === "lumi"
        ? { lumi_voice: "clone:" + name } : { nox_voice: "clone:" + name } }) });
    (target === "lumi" ? document.getElementById("s_voice_lumi")
                       : document.getElementById("s_voice_nox")).value = "clone:" + name;
    toast(`音色「${name}」已应用为 ${target === "lumi" ? "Lumi" : "Nox"} 的音色`);
  } catch (e) { toast("应用失败：" + e.message); }
  finally { btn.disabled = false; }
}

async function deleteClone(name, btn){
  if (btn.disabled) return;
  btn.disabled = true; btn.textContent = "删除中…";
  try {
    const r = await api("/voice-clone-delete", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    const vl = document.getElementById("voiceList");
    [...vl.options].filter(o => o.value === "clone:" + name).forEach(o => o.remove());
    for (const inputId of ["s_voice_lumi", "s_voice_nox"]) {
      const input = document.getElementById(inputId);
      if (input.value === "clone:" + name) input.value = "";
    }
    toast(`音色「${name}」已删除`
      + ((r.reset || []).length ? "，使用中的角色已恢复默认音色" : ""));
    renderClones();
  } catch (e) { toast("删除失败：" + e.message); btn.disabled = false; btn.textContent = "删除"; }
}

// ===== 导入皮套（上传 Live2D 模型 zip）=====
const modelOverlay = document.getElementById("modelOverlay");
let modelData = null;

function openModelImport(){
  modelData = null;
  document.getElementById("modelName").value = "";
  document.getElementById("modelFile").value = "";
  document.getElementById("modelFileInfo").textContent = "未选择文件";
  document.getElementById("modelApplyTo").value = "";
  const btn = document.getElementById("btnModelUpload");
  btn.disabled = false; btn.textContent = "导入";
  modelOverlay.classList.add("open");
}
document.getElementById("btnModelImport").addEventListener("click", openModelImport);
document.getElementById("btnModelCancel").addEventListener("click",
  () => modelOverlay.classList.remove("open"));
modelOverlay.addEventListener("click",
  e => { if (e.target === modelOverlay) modelOverlay.classList.remove("open"); });

document.getElementById("modelFile").addEventListener("change", ev => {
  modelData = null;
  const f = ev.target.files && ev.target.files[0];
  const info = document.getElementById("modelFileInfo");
  if (!f) { info.textContent = "未选择文件"; return; }
  if (f.size > 200 * 1024 * 1024) { info.textContent = "文件超过 200MB 上限"; return; }
  const nameInput = document.getElementById("modelName");
  if (!nameInput.value.trim())
    nameInput.value = f.name.replace(/\\.zip$/i, "").slice(0, 40);
  const r = new FileReader();
  r.onload = () => {
    // 浏览器给 .zip 的 mime 不统一（可能是 x-zip-compressed/空），统一改写成
    // application/zip，后端只认这一种
    modelData = { dataUri: String(r.result).replace(/^data:[^;]*;/, "data:application/zip;") };
    info.textContent = `已选择：${f.name}（${(f.size / 1024 / 1024).toFixed(1)}MB）`;
  };
  r.onerror = () => info.textContent = "文件读取失败";
  r.readAsDataURL(f);
});

async function refreshModelSelects(){
  const ml = await api("/models");
  const selL = document.getElementById("s_model_lumi"),
        selN = document.getElementById("s_model_nox");
  fillSelect(selL, ml.models, selL.value);
  fillSelect(selN, ml.models, selN.value);
  return ml;
}

document.getElementById("btnModelUpload").addEventListener("click", async () => {
  const btn = document.getElementById("btnModelUpload");
  const name = document.getElementById("modelName").value.trim();
  const applyTo = document.getElementById("modelApplyTo").value;
  if (!modelData) { toast("请先选择皮套 zip 文件"); return; }
  btn.disabled = true; btn.textContent = "导入中…";
  try {
    const r = await api("/model-upload", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, zipDataUri: modelData.dataUri }) });
    await refreshModelSelects();   // 重新扫描，新皮套进入两个形象下拉框
    const first = (r.models && r.models[0] || {}).path || "";
    if (applyTo) {
      (applyTo === "lumi" ? document.getElementById("s_model_lumi")
                          : document.getElementById("s_model_nox")).value = first;
      modelOverlay.classList.remove("open");
      // 复用保存链路：写盘 + 重载舞台 iframe，新形象立即可见
      document.getElementById("btnSave").click();
    } else {
      modelOverlay.classList.remove("open");
      toast(`皮套「${name || r.dir}」已入库（${r.models.length} 个模型），可在形象下拉框选择`);
    }
  } catch (e) { toast("导入失败：" + e.message); }
  finally { btn.disabled = false; btn.textContent = "导入"; }
});

// 音量滑杆数值标签
document.getElementById("s_vol").addEventListener("input",
  e => document.getElementById("s_vol_val").textContent =
    Math.round(e.target.value * 100) + "%");

initStage();
</script>
</body>
</html>
"""


class _ChatHandler(http.server.BaseHTTPRequestHandler):
    """网页弹幕：GET / 返回聊天页；POST /danmaku 入队调度器；GET /healthz 探活。

    设置面板：GET /settings 读有效配置（key 只回 api_key_set）；POST /settings
    保存并热应用（LLM/温度/TTS 端点即时替换，音色重建发声链路，人设下一句生效）；
    GET /models 扫描 live2d 静态根下的 model3.json 供舞台形象下拉框；
    POST /model-upload 导入皮套 zip（安全解压进 live2d/ 并校验模型完整性）；
    POST /test-voice 让指定角色说一句试音；POST /history-clear 清空对话历史。
    """
    scheduler = None
    ws_port = 8768
    web_port = 8000
    reset_cb = None
    show = None
    web_root = None

    def log_message(self, *args):
        pass

    def _send_json(self, code, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = (CHAT_PAGE.replace("__WS_PORT__", str(self.ws_port))
                             .replace("__WEB_PORT__", str(self.web_port))
                             .encode("utf-8"))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/healthz":
            self._send_json(200, {"ok": True})
        elif self.path == "/settings":
            view = show_settings.public_view()
            # 人设回填"当前生效值"：首次保存会把默认人设固化进文件，之后所见即所存
            view["persona"]["lumi"] = PERSONAS["Lumi"]
            view["persona"]["nox"] = PERSONAS["Nox"]
            self._send_json(200, view)
        elif self.path == "/models":
            self._send_json(200, {"models": show_settings.scan_models(self.web_root),
                                  "voices": show_settings.MIMO_VOICES})
        else:
            self.send_error(404)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length).decode("utf-8", "replace") or "{}")

    def do_POST(self):
        if self.path == "/audio-reset":
            ok = bool(self.reset_cb()) if self.reset_cb else False
            self._send_json(200, {"ok": ok})
            return
        if self.path == "/settings":
            try:
                show_settings.save(self._read_json_body())
                note = show_settings.apply_runtime()
                show_settings.apply_personas(PERSONAS)
                # 动作幅度等舞台配置实时下发给舞台前端（免重载生效）
                if self.show and self.show.stage:
                    self.show.stage.broadcast({"type": "config", "amp":
                        show_settings.get()["stage"].get("amplitude", 1.5)})
                # 音色/TTS 端点变了：重建发声链路，下一句话就用新配置
                if self.show and self.show.tts_ok:
                    self.show._reset_voice()
                print(f"[设置] 已保存并应用: {note}")
                self._send_json(200, {"ok": True, "applied": note})
            except Exception as e:
                self._send_json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            return
        if self.path == "/voice-clone":
            # 上传参考音频 → 存入克隆音色库 → 可选立即应用到角色音色。
            # MiMo 音色复刻没有"注册音色"接口：参考音频在每次合成时内联传入，
            # 因此这里只负责保存音频 + 登记 clone:名字，发声时再内联。
            try:
                data = self._read_json_body()
            except Exception:
                self._send_json(400, {"ok": False, "error": "bad json"})
                return
            name = str(data.get("name", "")).strip()
            mime = str(data.get("mime", "")).strip().lower()
            uri = str(data.get("audioDataUri", ""))
            apply_to = str(data.get("applyTo", "")).strip().lower()
            if not name or len(name) > 40:
                self._send_json(400, {"ok": False, "error": "音色名称需为 1~40 个字符"})
                return
            if mime not in show_settings.CLONE_MIMES:
                self._send_json(400, {"ok": False, "error": "仅支持 mp3 / wav 音频"})
                return
            head = "data:" + mime + ";base64,"
            if not uri.startswith(head):
                self._send_json(400, {"ok": False, "error": "音频数据格式不对"})
                return
            try:
                audio = base64.b64decode(uri[len(head):], validate=True)
            except Exception:
                self._send_json(400, {"ok": False, "error": "base64 解码失败"})
                return
            if not audio:
                self._send_json(400, {"ok": False, "error": "音频内容为空"})
                return
            if len(audio) > 10 * 1024 * 1024:
                self._send_json(400, {"ok": False, "error": "音频超过 10MB 上限"})
                return
            audio, mime = _prepare_clone_audio(audio, mime)
            show_settings.CLONES_DIR.mkdir(exist_ok=True)
            fname = "voice_%d%s" % (int(time.time() * 1000),
                                    show_settings.CLONE_MIMES[mime])
            (show_settings.CLONES_DIR / fname).write_bytes(audio)
            show_settings.add_clone(name, "cloned_voices/" + fname, mime)
            voice_value = show_settings.CLONE_PREFIX + name
            applied = False
            if apply_to in ("lumi", "nox"):
                key = "lumi_voice" if apply_to == "lumi" else "nox_voice"
                show_settings.save({"tts": {key: voice_value}})
                show_settings.apply_runtime()
                if self.show and self.show.tts_ok:
                    self.show._reset_voice()
                applied = True
            print(f"[音色克隆] 已登记音色「{name}」（{len(audio) // 1024}KB）"
                  + ("，已应用为角色音色" if applied else ""))
            self._send_json(200, {"ok": True, "name": name,
                                  "voice": voice_value, "applied": applied})
            return
        if self.path == "/voice-clone-delete":
            # 删除克隆音色；若正被角色使用，先恢复默认音色（回落 .env 预置）
            try:
                data = self._read_json_body()
            except Exception:
                self._send_json(400, {"ok": False, "error": "bad json"})
                return
            name = str(data.get("name", "")).strip()
            clone = show_settings.get_clone(name)
            if not clone:
                self._send_json(404, {"ok": False, "error": "音色不存在"})
                return
            in_use = []
            for key, spk in (("lumi_voice", "Lumi"), ("nox_voice", "Nox")):
                cur = show_settings.get()["tts"].get(key, "")
                if show_settings.is_clone_voice(cur) and \
                        show_settings.clone_name(cur) == name:
                    show_settings.save({"tts": {key: ""}})
                    in_use.append(spk)
            try:
                (show_settings.CLONES_DIR / Path(clone["file"]).name).unlink()
            except OSError:
                pass
            show_settings.delete_clone(name)
            show_settings.apply_runtime()
            if in_use and self.show and self.show.tts_ok:
                self.show._reset_voice()
            print(f"[音色克隆] 已删除「{name}」"
                  + (f"（{','.join(in_use)} 已恢复默认音色）" if in_use else ""))
            self._send_json(200, {"ok": True, "reset": in_use})
            return
        if self.path == "/model-upload":
            # 上传 Live2D 皮套 zip → 安全解压进 live2d/<名称>/ 并校验模型
            # 完整性（moc/贴图齐全），成功后 /models 扫描即可发现（无需登记）。
            try:
                data = self._read_json_body()
            except Exception:
                self._send_json(400, {"ok": False, "error": "bad json"})
                return
            name = str(data.get("name", "")).strip()
            uri = str(data.get("zipDataUri", ""))
            head = None
            for h in ("data:application/zip;base64,",
                      "data:application/x-zip-compressed;base64,"):
                if uri.startswith(h):
                    head = h
                    break
            if not head:
                self._send_json(400, {"ok": False, "error": "数据格式不对（需 zip 的 data URI）"})
                return
            try:
                payload = base64.b64decode(uri[len(head):], validate=True)
            except Exception:
                self._send_json(400, {"ok": False, "error": "base64 解码失败"})
                return
            try:
                result = show_settings.import_model_zip(payload, name, self.web_root)
            except ValueError as e:
                self._send_json(400, {"ok": False, "error": str(e)})
                return
            except Exception as e:
                self._send_json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
                return
            print(f"[皮套导入] 已入库 {result['dir']}"
                  f"（{len(result['models'])} 个模型）")
            self._send_json(200, {"ok": True, **result})
            return
        if self.path == "/test-voice":
            speaker = "Lumi"
            try:
                body = self._read_json_body()
                speaker = "Nox" if body.get("speaker") == "Nox" else "Lumi"
            except Exception:
                body = {}
            # 面板上刚改还没保存的音色/端点先落盘应用，再试音 —— 听到的就是所见配置
            if isinstance(body.get("tts"), dict):
                try:
                    show_settings.save({"tts": body["tts"]})
                    show_settings.apply_runtime()
                    if self.show and self.show.tts_ok:
                        self.show._reset_voice()
                except Exception as e:
                    self._send_json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
                    return
            if self.show:
                self.show.speak(speaker, "大家好，这是一次试音，听到就说明语音正常！")
            self._send_json(200, {"ok": bool(self.show and self.show.tts_ok),
                                  "speaker": speaker})
            return
        if self.path == "/history-clear":
            if self.show:
                self.show.history = {name: [] for name in ("Lumi", "Nox")}
            self._send_json(200, {"ok": True})
            return
        if self.path != "/danmaku":
            self.send_error(404)
            return
        try:
            data = self._read_json_body()
        except Exception:
            self._send_json(400, {"ok": False, "error": "bad json"})
            return
        text = str(data.get("text", "")).strip()
        if not text:
            self._send_json(400, {"ok": False, "error": "empty"})
            return
        first = text.split("：", 1)[0].split(":", 1)[0].strip()
        who = first if first.upper() in ("LUMI", "NOX") else "观众"
        self.scheduler.enqueue_input(f"弹幕：{text}", source="danmaku", label=who)
        self._send_json(200, {"ok": True})


def _start_chat_server(show, chat_port, ws_port, web_port, web_root, reset_cb=None):
    _ChatHandler.show = show
    _ChatHandler.scheduler = show.scheduler
    _ChatHandler.ws_port = ws_port
    _ChatHandler.web_port = web_port
    _ChatHandler.web_root = web_root
    _ChatHandler.reset_cb = reset_cb
    try:
        httpd = _ShowHTTPServer(("127.0.0.1", chat_port), _ChatHandler)
    except OSError as e:
        print(f"[网页弹幕] 聊天页端口 {chat_port} 被占用——很可能已有一个节目在运行，"
              f"本实例弹幕页不可用。({e})")
        return
    threading.Thread(target=httpd.serve_forever, daemon=True, name="chat-web").start()
    print(f"[网页弹幕] 聊天页: http://127.0.0.1:{chat_port}/ （浏览器打开即可和 Lumi/Nox 聊天）")


def main():
    ap = argparse.ArgumentParser(description="双 AI 语音电台（本地部署启动器）")
    ap.add_argument("--turns", type=int, default=1000, help="最多聊多少轮")
    ap.add_argument("--no-tts", action="store_true", help="只出字幕不出声")
    ap.add_argument("--no-stage", action="store_true",
                    help="不启动 Live2D 舞台（WS + 静态服务）")
    ap.add_argument("--ws-port", type=int, default=8768, help="舞台 WS 端口")
    ap.add_argument("--web-port", type=int, default=8000, help="舞台静态服务端口")
    ap.add_argument("--web-chat", action="store_true",
                    help="启用网页弹幕聊天页（浏览器打字，机器人语音回答）")
    ap.add_argument("--chat-port", type=int, default=8001, help="网页弹幕聊天页端口")
    args = ap.parse_args()

    stage = None
    web_root = Path(__file__).resolve().parent.parent / "live2d"
    if not args.no_stage and (web_root / "frontend" / "index.html").exists():
        stage = Stage(web_root, ws_port=args.ws_port, web_port=args.web_port)
        print(f"[舞台] Live2D 前端已就绪: http://127.0.0.1:{args.web_port}/frontend/ "
              f"（OBS 浏览器源加载该地址）")
    elif not args.no_stage:
        print("[舞台] 未找到 live2d/frontend/，跳过舞台服务")

    show = Show(enable_tts=not args.no_tts, stage=stage)
    if args.web_chat:
        _start_chat_server(show, args.chat_port, args.ws_port,
                           args.web_port, web_root, reset_cb=show._reset_voice)
        show.run(turns=args.turns, interactive=False, wait_idle=True)
    else:
        show.run(turns=args.turns,
                 interactive=sys.stdin.isatty() or args.turns < 900)


if __name__ == "__main__":
    main()
