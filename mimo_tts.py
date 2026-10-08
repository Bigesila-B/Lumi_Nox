"""小米 MiMo TTS（mimo-v2.5-tts 系列）流式合成器 —— cosyvoice_tts.IndependentSynth 的等价替换。

走 MiMo 开放平台的 OpenAI 兼容 chat/completions 协议：stream=true 时 SSE 返回，
音频在 choices[].delta.audio.data，为 base64 PCM 分块（实测 24000Hz / 16bit /
单声道，与 CosyVoice PCM_24000HZ_MONO_16BIT 相同，播放链路可原样复用）。

协议差异：CosyVoice 是持续 websocket、可往同一会话不断追加文本；MiMo 一次请求
只合成请求里给定的固定文本。因此 feed(sentence) 每句发一次流式请求，解码出的
PCM 按序写进同一个播放队列（顺序由队列保证），句间停顿由播放缓冲抹平。

对外生命周期与 IndependentSynth 完全一致：
  open(voice_id, model, cable_index) → feed(sentence)... → finish() / abort()

依赖由 init() 注入（pyaudio / AEC 参考缓冲 / 监听设备 / 日志），避免与 lumi_tts
循环 import。密钥只从 .env / 环境变量读取（MIMO_TTS_BASE_URL / MIMO_TTS_API_KEY）。
"""

import base64
import json
import os
import queue
import threading
import time
from threading import Lock

import numpy as np
import pyaudiowpatch as pyaudio
import requests
from scipy.signal import resample_poly
from dotenv import load_dotenv

from url_guard import validate_public_http_url

# MiMo 流式 PCM 为裸帧、无头，采样率固定按此值播放（2026-08 用 wav 格式实测确认）。
SAMPLE_RATE = 24000

# 调试探针（MIMO_TTS_DEBUG=1 开启）：追踪流开关/合成/播放字节量/写入阻塞，
# 用于定位"聊几句就没声"一类问题。
_DEBUG = os.environ.get("MIMO_TTS_DEBUG", "") == "1"

# 播放音量（0~2，1=原始电平）：由设置面板实时调整，按块缩放 PCM
_VOLUME = 1.0


def set_volume(volume):
    global _VOLUME
    _VOLUME = max(0.0, min(2.0, float(volume)))


def _dbg(msg):
    if _DEBUG:
        _log_fn(f"[MiMo·调试] {msg}")

# ======= 注入依赖（由 init() 设置）=======
_pa_instance = None
_monitor_device_index = None
_ref_buffer = None  # AEC 参考缓冲（= lumi_tts.tts_ref_buffer）
_log_fn = lambda msg: print(msg, flush=True)

_BASE_URL = ""
_API_KEY = ""


def init(*, pa_instance, ref_buffer=None,
         monitor_device_index=None, log_fn=None):
    """由 lumi_tts.init 转调一次，注入合成器需要的外部依赖。"""
    global _pa_instance, _monitor_device_index, _ref_buffer, _log_fn
    global _BASE_URL, _API_KEY
    _pa_instance = pa_instance
    _ref_buffer = ref_buffer
    _monitor_device_index = monitor_device_index
    if log_fn:
        _log_fn = log_fn
    load_dotenv()
    _BASE_URL = os.environ.get("MIMO_TTS_BASE_URL", "").rstrip("/")
    _API_KEY = os.environ.get("MIMO_TTS_API_KEY", "")
    if _API_KEY:
        # 发请求前校验：仅 http/https 且 host 为公网（见 url_guard）。
        validate_public_http_url(_BASE_URL)


class MimoSynth:
    """MiMo 流式合成器。open 建播放链路；feed 每句发一次 SSE 请求，PCM 按序入
    播放队列；finish 等全部合成并播完；abort 打断在途请求并丢弃未播帧。"""

    def __init__(self):
        self._cable_index = None
        self._model = ""
        self._voice = ""
        self._audio_stream = None
        self._audio_write_lock = Lock()
        self._audio_state = {"closed": False}
        self._monitor_stream = None
        self._monitor_queue: queue.Queue = queue.Queue()
        self._monitor_thread = None
        self._playback_queue: queue.Queue = queue.Queue()
        self._playback_thread = None
        self._synth_queue: queue.Queue = queue.Queue()  # 待合成句子；None = 结束
        self._synth_thread = None
        self._inflight_response = None  # 供 abort 打断在途 HTTP
        self._inflight_lock = Lock()
        self._audio_chunk_count = 0
        self._synth_bytes = 0
        self._written_bytes = 0
        self._opened = False
        self._aborted = False
        self.on_error = None  # callable(message)：本句最终合成失败时回调

    # ---- 生命周期 ----
    def open(self, voice_id, model, cable_index):
        if not _API_KEY:
            raise RuntimeError("未配置 MIMO_TTS_API_KEY，无法使用 MiMo TTS（在 .env 填写后重试）")
        validate_public_http_url(_BASE_URL)
        self._model = model or "mimo-v2.5-tts"
        self._voice = voice_id or "mimo_default"
        self._cable_index = cable_index
        self._audio_chunk_count = 0
        self._synth_bytes = 0
        self._written_bytes = 0
        self._audio_state = {"closed": False}
        self._aborted = False

        _audio_fpb = 4800  # 200ms @24kHz，撑过句间空档避免 underrun
        self._audio_stream = None
        if cable_index is not None:
            try:
                self._audio_stream = _pa_instance.open(
                    format=pyaudio.paInt16, channels=1, rate=SAMPLE_RATE, output=True,
                    output_device_index=cable_index, frames_per_buffer=_audio_fpb,
                )
            except Exception as e:
                _log_fn(f"[MiMo] 声卡 index={cable_index} 不可用({e})，回退默认输出")
                self._audio_stream = None
        if self._audio_stream is None:
            self._audio_stream = _pa_instance.open(
                format=pyaudio.paInt16, channels=1, rate=SAMPLE_RATE, output=True,
                frames_per_buffer=_audio_fpb,
            )

        if _monitor_device_index is not None and cable_index is not None:
            try:
                self._monitor_stream = _pa_instance.open(
                    format=pyaudio.paInt16, channels=1, rate=SAMPLE_RATE, output=True,
                    output_device_index=_monitor_device_index,
                    frames_per_buffer=_audio_fpb,
                )
                self._monitor_thread = threading.Thread(
                    target=self._monitor_writer, daemon=True)
                self._monitor_thread.start()
            except Exception as e:
                _log_fn(f"[MiMo·监听] 无法打开监听设备: {e}")
                self._monitor_stream = None

        self._playback_thread = threading.Thread(
            target=self._playback_worker, daemon=True, name="mimo-playback")
        self._synth_thread = threading.Thread(
            target=self._synth_worker, daemon=True, name="mimo-synth")
        self._playback_thread.start()
        self._synth_thread.start()
        self._opened = True
        _dbg(f"流已开 voice={self._voice[:24]} model={self._model} "
             f"device={cable_index if cable_index is not None else '默认'}")

    def feed(self, text):
        if self._opened and text and text.strip():
            self._synth_queue.put(text.strip())

    def finish(self):
        if not self._opened:
            return
        self._synth_queue.put(None)  # 哨兵：合成线程收尾
        self._synth_thread.join(timeout=180)
        if self._audio_chunk_count == 0:
            _log_fn("[MiMo·无音频] 未收到任何音频帧")
        self._cleanup(interrupted=False)

    def abort(self):
        if not self._opened:
            return
        self._aborted = True
        with self._inflight_lock:
            if self._inflight_response is not None:
                try:
                    self._inflight_response.close()
                except Exception:
                    pass
        self._cleanup(interrupted=True)

    # ---- 内部 ----
    def _synth_worker(self):
        while True:
            sentence = self._synth_queue.get()
            if sentence is None or self._aborted:
                break
            try:
                self._synthesize_one(sentence)
            except Exception as e:
                _log_fn(f"[MiMo·合成异常] {e}")

    def _synthesize_one(self, sentence: str):
        """单句流式合成：SSE 逐块取 base64 PCM，解码后按序入播放队列。

        429/5xx 自动退避重试（免费档限流敏感）；只有在本句一个音频块都没
        播出去时才重试，避免中途重试造成音频重复。
        """
        for attempt in range(3):
            fed_before = self._audio_chunk_count
            try:
                self._request_and_stream(sentence)
                return
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else 0
                partial = self._audio_chunk_count > fed_before
                if status in (429, 500, 502, 503, 504) and not partial and attempt < 2:
                    wait = 3 * (attempt + 1)
                    _log_fn(f"[MiMo] 合成被限流/服务错误({status})，{wait}s 后重试本句")
                    time.sleep(wait)
                    continue
                _log_fn(f"[MiMo·合成异常] HTTP {status}: {e}")
                self._emit_error(f"合成失败(HTTP {status})，本句跳过")
                return
            except Exception as e:
                _log_fn(f"[MiMo·合成异常] {e}")
                self._emit_error(f"合成失败：{e}")
                return

    def _emit_error(self, message: str):
        """本句最终失败：日志 + 回调（my_show 用来在聊天页提示用户）。"""
        if callable(self.on_error):
            try:
                self.on_error(message)
            except Exception:
                pass

    def _request_and_stream(self, sentence: str):
        """发起一次流式合成请求并把音频块送入播放队列（抛错交给上层重试）。"""
        if self._aborted:
            return
        payload = {
            "model": self._model,
            "messages": [{"role": "assistant", "content": sentence}],
            "audio": {"format": "pcm", "voice": self._voice},
            "stream": True,
        }
        resp = requests.post(
            f"{_BASE_URL}/chat/completions",
            headers={"api-key": _API_KEY, "Content-Type": "application/json"},
            json=payload, stream=True, timeout=(10, 120),
        )
        with self._inflight_lock:
            self._inflight_response = resp
        try:
            resp.raise_for_status()
            for line in resp.iter_lines(decode_unicode=True):
                if self._aborted:
                    break
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                if not choices:  # SSE 首/尾帧（role、usage）没有 choices
                    continue
                audio = (choices[0].get("delta") or {}).get("audio") or {}
                b64 = audio.get("data")
                if b64:
                    self._audio_chunk_count += 1
                    self._synth_bytes += len(b64) // 4 * 3
                    self._playback_queue.put(base64.b64decode(b64))
        finally:
            with self._inflight_lock:
                self._inflight_response = None
            resp.close()
        _dbg(f"合成完成: {sentence[:12]}… 块={self._audio_chunk_count} "
             f"累计={self._synth_bytes // 1024}KB")

    def _playback_worker(self):
        while True:
            item = self._playback_queue.get()
            if item is None:
                break
            try:
                with self._audio_write_lock:
                    if self._audio_state["closed"]:
                        break
                    if _VOLUME != 1.0:
                        samples = np.frombuffer(item, dtype=np.int16)
                        item = np.clip(samples.astype(np.float32) * _VOLUME,
                                       -32768, 32767).astype(np.int16).tobytes()
                    t0 = time.time()
                    self._audio_stream.write(item)
                    took = time.time() - t0
                    self._written_bytes += len(item)
                    if _DEBUG and took > 2:
                        _dbg(f"写入阻塞 {took:.1f}s（累计写入 "
                             f"{self._written_bytes // 1024}KB）")
                if self._monitor_stream:
                    self._monitor_queue.put_nowait(item)
            except Exception as e:
                _log_fn(f"[MiMo·播放异常] {e}")
                _dbg(f"播放线程因异常退出: {e}")
                break
            if _ref_buffer is not None:
                samples = np.frombuffer(item, dtype=np.int16)
                ref16 = resample_poly(samples, up=2, down=3).astype(np.int16)
                _ref_buffer.extend(ref16)
        if _DEBUG:
            _dbg(f"播放线程退出（累计写入 {self._written_bytes // 1024}KB，"
                 f"队列残留约 {self._playback_queue.qsize()} 块）")

    def _monitor_writer(self):
        while True:
            data = self._monitor_queue.get()
            if data is None:
                break
            try:
                self._monitor_stream.write(data)
            except Exception:
                break

    def _cleanup(self, interrupted: bool):
        if interrupted:
            while not self._synth_queue.empty():
                try:
                    self._synth_queue.get_nowait()
                except queue.Empty:
                    break
            self._synth_queue.put(None)
            self._synth_thread.join(timeout=3)
            while not self._playback_queue.empty():
                try:
                    self._playback_queue.get_nowait()
                except queue.Empty:
                    break
            self._playback_queue.put(None)
            self._playback_thread.join(timeout=3)
        else:
            self._playback_queue.put(None)
            self._playback_thread.join(timeout=60)
        with self._audio_write_lock:
            if not self._audio_state["closed"]:
                try:
                    self._audio_stream.stop_stream()
                except Exception:
                    pass
                try:
                    self._audio_stream.close()
                except Exception:
                    pass
                self._audio_state["closed"] = True
        if self._monitor_stream:
            self._monitor_queue.put(None)
            if self._monitor_thread:
                self._monitor_thread.join(timeout=2)
            try:
                self._monitor_stream.stop_stream()
            except Exception:
                pass
            try:
                self._monitor_stream.close()
            except Exception:
                pass
        self._opened = False
        _dbg(f"清理完成 interrupted={interrupted} "
             f"写入总量={self._written_bytes // 1024}KB")
