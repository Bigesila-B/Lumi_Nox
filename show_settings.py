"""show_settings.py — 直播间设置中心（本地部署补充，非仓库原生物）。

把"能在网页设置面板里调"的配置集中到一个 JSON 文件（show_settings.json，
与本模块同目录），分四组：

  llm     大脑：OpenAI 兼容端点 base_url / api_key / model / temperature
  tts     声音：MiMo TTS base_url / api_key / model，以及 Lumi / Nox 的音色名
  persona 人设：Lumi / Nox 的 system prompt 文本
  stage   舞台：字幕开关、两个角色的 Live2D 模型路径（相对 live2d 静态根）

生效机制（对应 my_show 的三种接线点）：
- 启动：my_show 在导入 fast_brain / mimo_tts **之前**调用 load_and_apply_env()，
  把文件里非空项写进 os.environ。后续模块里的 load_dotenv() 默认不覆盖已存在的
  环境变量，因此"设置文件值 > .env 值"自然成立，原有读取路径一行不用改。
- 运行中保存：apply_runtime() 直接替换 fast_brain 的 custom 客户端/模型全局与
  openai 档温度、mimo_tts 的端点全局；音色改动走环境变量 + 重建发声链路
  （my_show.Show._reset_voice），全部免重启。人设走 apply_personas 原地覆盖。
- 展示：public_view() 给前端回有效值，但 api_key 永不回传明文，只有 api_key_set。

JSON 里存了 API key，属于密钥文件：已加入 .gitignore，不要提交/外发。
"""

import base64
import io
import json
import os
import re
import shutil
import time
import zipfile
from pathlib import Path

SETTINGS_FILE = Path(__file__).resolve().parent / "show_settings.json"

# MiMo 开放平台预置音色（2026-08 实测可用列表，.env 注释同源）。
MIMO_VOICES = ["mimo_default", "冰糖", "茉莉", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean"]

# 音色复刻（MiMo-V2.5-TTS-VoiceClone）：参考音频在每次合成请求里内联传入
# （audio.voice = data:<mime>;base64,<音频>），没有独立的"注册音色"接口。
CLONE_MODEL = "mimo-v2.5-tts-voiceclone"
CLONE_PREFIX = "clone:"
CLONES_DIR = SETTINGS_FILE.parent / "cloned_voices"
CLONE_MIMES = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav"}

# 空字符串 = 不覆盖，回落到 .env / 代码默认；temperature 有代码默认 0.8。
DEFAULTS = {
    "llm": {"base_url": "", "api_key": "", "model": "", "temperature": 0.8},
    "tts": {"base_url": "", "api_key": "", "model": "", "lumi_voice": "", "nox_voice": "",
            "volume": 1.0},
    "persona": {"lumi": "", "nox": ""},
    "stage": {"subtitle": True, "lumi_model": "", "nox_model": "", "amplitude": 1.5},
}

_settings: dict = {}


def _deep_copy_defaults() -> dict:
    return json.loads(json.dumps(DEFAULTS))


def load() -> dict:
    """读 JSON 并合并进默认结构。文件缺失/损坏一律退回默认，不阻断开播。"""
    global _settings
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except Exception as e:
        print(f"[设置] {SETTINGS_FILE.name} 读取失败({e})，本次使用默认设置")
        data = {}
    merged = _deep_copy_defaults()
    merged["clones"] = {}
    if isinstance(data, dict):
        for group, fields in DEFAULTS.items():
            src = data.get(group)
            if not isinstance(src, dict):
                continue
            for key, default in fields.items():
                if key not in src:
                    continue
                val = src[key]
                if isinstance(default, bool):
                    merged[group][key] = bool(val)
                elif isinstance(default, float):
                    try:
                        merged[group][key] = min(2.0, max(0.0, float(val)))
                    except (TypeError, ValueError):
                        pass
                elif val not in (None, ""):
                    merged[group][key] = str(val)
        # 克隆音色库（独立于 DEFAULTS 的字典组）：name -> {file, mime}
        clones = data.get("clones")
        if isinstance(clones, dict):
            merged["clones"] = {}
            for name, c in clones.items():
                if isinstance(c, dict) and isinstance(c.get("file"), str) \
                        and isinstance(c.get("mime"), str):
                    merged["clones"][str(name)] = {"file": c["file"], "mime": c["mime"]}
    _settings = merged
    return _settings


def get() -> dict:
    return _settings


def _apply_env() -> None:
    """把非空设置写进环境变量（先于 fast_brain/mimo_tts 导入时调用才完整生效）。"""
    llm, tts = _settings["llm"], _settings["tts"]
    if llm["base_url"]:
        os.environ["CUSTOM_LLM_BASE_URL"] = llm["base_url"]
    if llm["api_key"]:
        os.environ["CUSTOM_LLM_API_KEY"] = llm["api_key"]
    if llm["model"]:
        os.environ["CUSTOM_LLM_MODEL"] = llm["model"]
    if tts["base_url"]:
        os.environ["MIMO_TTS_BASE_URL"] = tts["base_url"]
    if tts["api_key"]:
        os.environ["MIMO_TTS_API_KEY"] = tts["api_key"]
    if tts["model"]:
        # 两个角色共用同一个 TTS 模型名（voice_registry 按 CHARACTER_*_VOICE_MODEL 读取）
        os.environ["CHARACTER_A_VOICE_MODEL"] = tts["model"]
        os.environ["CHARACTER_B_VOICE_MODEL"] = tts["model"]
    if tts["lumi_voice"]:
        os.environ["CHARACTER_A_VOICE_ID"] = tts["lumi_voice"]
    if tts["nox_voice"]:
        os.environ["CHARACTER_B_VOICE_ID"] = tts["nox_voice"]


def load_and_apply_env() -> dict:
    load()
    _apply_env()
    return _settings


def apply_personas(personas: dict) -> None:
    """文件里的人设（非空才生效）原地覆盖进 my_show.PERSONAS。"""
    for key, name in (("lumi", "Lumi"), ("nox", "Nox")):
        text = _settings["persona"].get(key, "")
        if text.strip():
            personas[name] = text


def save(new_settings: dict) -> dict:
    """合并保存到 JSON。api_key 传空 = 保持文件里已有值，防前端回显丢 key。"""
    if isinstance(new_settings, dict):
        for group, fields in DEFAULTS.items():
            src = new_settings.get(group)
            if not isinstance(src, dict):
                continue
            for key in fields:
                if key not in src:
                    continue
                val = src[key]
                if key.endswith("api_key") and not str(val).strip():
                    continue
                if isinstance(fields[key], bool):
                    _settings[group][key] = bool(val)
                elif isinstance(fields[key], float):
                    try:
                        _settings[group][key] = min(2.0, max(0.0, float(val)))
                    except (TypeError, ValueError):
                        pass
                elif val is not None:
                    _settings[group][key] = str(val)
    SETTINGS_FILE.write_text(
        json.dumps(_settings, ensure_ascii=False, indent=2), encoding="utf-8")
    _apply_env()
    return _settings


def apply_runtime() -> dict:
    """运行中热更新：LLM 客户端/模型/温度、MiMo TTS 端点。返回生效情况说明。"""
    note = {}
    # --- LLM：用当前环境变量重建 custom 客户端，替换 fast_brain 的模块级全局 ---
    try:
        import fast_brain
        from openai import OpenAI
        from url_guard import validate_public_http_url

        base = os.environ.get("CUSTOM_LLM_BASE_URL", "")
        key = os.environ.get("CUSTOM_LLM_API_KEY", "")
        model = os.environ.get("CUSTOM_LLM_MODEL", "")
        if key and model:
            validate_public_http_url(base)
            client = OpenAI(api_key=key, base_url=base)
            fast_brain._custom_client = client
            fast_brain.LLM_MODELS["custom"] = (f"Custom ({model})", client, model, "openai")
            fast_brain._current_model_key = "custom"
            fast_brain.llm_client = client
            fast_brain.LLM_MODEL = model
            note["llm"] = f"{model} @ {base}"
        fast_brain._BRAND_PARAMS["openai"]["temperature"] = float(_settings["llm"]["temperature"])
        note["temperature"] = float(_settings["llm"]["temperature"])
    except Exception as e:
        note["llm_error"] = f"{type(e).__name__}: {e}"
    # --- TTS：更新 mimo_tts 端点全局（音色在环境变量里，由重建发声链路读取）---
    try:
        import mimo_tts
        mimo_tts._BASE_URL = os.environ.get("MIMO_TTS_BASE_URL", "").rstrip("/")
        mimo_tts._API_KEY = os.environ.get("MIMO_TTS_API_KEY", "")
        try:
            mimo_tts.set_volume(float(_settings["tts"].get("volume", 1.0)))
            note["volume"] = float(_settings["tts"].get("volume", 1.0))
        except Exception:
            pass
        note["tts"] = "ok" if mimo_tts._API_KEY else "未配置 key，语音不可用"
    except Exception as e:
        note["tts_error"] = f"{type(e).__name__}: {e}"
    return note


def public_view() -> dict:
    """给前端的只读视图：有效值（文件为空回落 env），api_key 只给 api_key_set。"""
    llm, tts = _settings["llm"], _settings["tts"]
    return {
        "llm": {
            "base_url": llm["base_url"] or os.environ.get("CUSTOM_LLM_BASE_URL", ""),
            "api_key_set": bool(llm["api_key"] or os.environ.get("CUSTOM_LLM_API_KEY")),
            "model": llm["model"] or os.environ.get("CUSTOM_LLM_MODEL", ""),
            "temperature": llm["temperature"],
        },
        "tts": {
            "base_url": tts["base_url"] or os.environ.get("MIMO_TTS_BASE_URL", ""),
            "api_key_set": bool(tts["api_key"] or os.environ.get("MIMO_TTS_API_KEY")),
            "model": tts["model"] or os.environ.get("CHARACTER_A_VOICE_MODEL", "mimo-v2.5-tts"),
            "lumi_voice": tts["lumi_voice"] or os.environ.get("CHARACTER_A_VOICE_ID", ""),
            "nox_voice": tts["nox_voice"] or os.environ.get("CHARACTER_B_VOICE_ID", ""),
            "volume": tts["volume"],
        },
        "persona": dict(_settings["persona"]),
        "stage": dict(_settings["stage"]),
        "clones": {name: {"mime": c["mime"], "file": c["file"]}
                   for name, c in _settings.get("clones", {}).items()},
    }


# ======= 克隆音色库 =======

def is_clone_voice(voice) -> bool:
    """音色值是否指向克隆音色（形如 clone:名字）。"""
    return isinstance(voice, str) and voice.startswith(CLONE_PREFIX)


def clone_name(voice) -> str:
    return voice[len(CLONE_PREFIX):] if is_clone_voice(voice) else ""


def list_clones() -> dict:
    return dict(_settings.get("clones", {}))


def get_clone(name: str):
    return _settings.get("clones", {}).get(name)


def add_clone(name: str, file: str, mime: str) -> None:
    """登记克隆音色（音频文件已由调用方写入 CLONES_DIR）并落盘。"""
    _settings.setdefault("clones", {})[name] = {"file": file, "mime": mime}
    SETTINGS_FILE.write_text(
        json.dumps(_settings, ensure_ascii=False, indent=2), encoding="utf-8")


def delete_clone(name: str) -> bool:
    """从克隆音色库移除并落盘。返回是否存在。音频文件由调用方负责删除。"""
    clones = _settings.get("clones", {})
    if name not in clones:
        return False
    del clones[name]
    SETTINGS_FILE.write_text(
        json.dumps(_settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def clone_data_uri(clone: dict) -> str:
    """把克隆音色的参考音频读成 data URI（MiMo voiceclone 每次合成都要内联传）。"""
    path = SETTINGS_FILE.parent / clone["file"]
    b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return f"data:{clone['mime']};base64,{b64}"


def scan_models(web_root: Path) -> list:
    """扫描 live2d 静态根下所有 model3.json，返回前端下拉框选项。path 为
    站内绝对路径（如 /miku/miku.model3.json），前端经 URLSearchParams 编码后
    以 ?lumi= / ?nox= 传给舞台页（舞台页 Q.get 会自动解码一次）。"""
    models = []
    if not web_root or not Path(web_root).exists():
        return models
    web_root = Path(web_root)
    for p in sorted(web_root.rglob("*.model3.json")):
        rel = p.relative_to(web_root)
        if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
            continue  # 跳过隐藏目录/缓存目录
        models.append({
            "path": "/" + rel.as_posix(),
            "label": f"{rel.parts[0] if len(rel.parts) > 1 else ''}/{p.name}".lstrip("/"),
        })
    return models


# ======= 导入皮套（上传 Live2D 模型 zip）=======

MODEL_ZIP_LIMIT = 200 * 1024 * 1024
# 目录名只放行安全字符：分隔符/盘符冒号/通配符/控制字符等一律替换成下划线
_MODEL_DIR_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _inside_root(root: Path, name: str) -> Path:
    """把「我们自己拼出来的」单段名字落成 root 内的路径（程序内部使用，
    不接触压缩包内容；压缩包成员一律走 zf.extract 的标准库安全解压）。"""
    norm = os.path.normpath(name.replace("\\", "/"))
    if not norm or os.path.isabs(norm) or os.path.splitdrive(norm)[0] \
            or norm.split(os.sep)[-1] == ".." or ".." in Path(norm).parts:
        raise ValueError(f"不安全路径：{name}")
    target = root / norm
    root_real = os.path.realpath(str(root))
    target_real = os.path.realpath(str(target))
    if os.path.commonpath([target_real, root_real]) != root_real:
        raise ValueError(f"不安全路径：{name}")
    return target


def _model3_problem(model3_path: Path, web_root: Path) -> str:
    """检查 model3.json 引用的 moc / 贴图是否齐全（只读存在性检查）。
    返回问题描述，齐全返回 ""。"""
    try:
        ref = json.loads(model3_path.read_text(encoding="utf-8-sig")) \
            .get("FileReferences") or {}
    except Exception as e:
        return f"model3.json 无法解析（{e}）"
    base = model3_path.parent
    try:
        moc = ref.get("Moc")
        if not moc or not _inside_root(base, str(moc)).exists():
            return f"缺少模型本体 {moc or '(FileReferences.Moc 缺失)'}"
        for tex in ref.get("Textures") or []:
            if not _inside_root(base, str(tex)).exists():
                return f"缺少贴图 {tex}"
    except ValueError:
        return "清单引用了模型目录之外的文件"
    return ""


def _model_label(rel_from_web_root: Path) -> str:
    return (f"{rel_from_web_root.parts[0]}/" if len(rel_from_web_root.parts) > 1 else "") \
        + rel_from_web_root.name


def import_model_zip(zip_bytes: bytes, name: str, web_root) -> dict:
    """把上传的 Live2D 模型 zip 解压进 live2d/<名称>/，校验后返回发现的模型。

    流程：zip 合法性 → 成员名显式审计（拒绝绝对路径/盘符/..）+ 解压总量上限 →
    用 zf.extract 标准库安全解压（其内部还会剥离盘符与 .. 成分，双保险）→
    若整个包只有一个顶层目录则去掉这层冗余包装 → 至少一个 Cubism 4 模型
    （*.model3.json）且其 moc/贴图齐全。失败即整体回滚（不留半成品），
    原因以 ValueError(中文) 抛出。
    """
    if not zip_bytes:
        raise ValueError("压缩包内容为空")
    if len(zip_bytes) > MODEL_ZIP_LIMIT:
        raise ValueError("压缩包超过 200MB 上限")
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise ValueError("不是有效的 zip 压缩包（皮套需打包为 .zip 上传，rar 暂不支持）")

    web_root = Path(web_root)
    if not web_root.exists():
        raise ValueError(f"live2d 静态目录不存在：{web_root}")

    dir_name = _MODEL_DIR_BAD.sub("_", str(name or "")).strip(" .")[:60] \
        or "imported_model"
    dest = _inside_root(web_root, dir_name)
    if dest.exists():  # 重名不覆盖，追加时间戳
        dir_name = f"{dir_name}_{time.strftime('%Y%m%d_%H%M%S')}"
        dest = _inside_root(web_root, dir_name)

    tmp = _inside_root(web_root, f".import_tmp_{time.strftime('%Y%m%d_%H%M%S')}")
    tmp.mkdir(parents=True)
    ok = False
    try:
        total = 0
        tops = set()
        for info in zf.infolist():
            rel = info.filename.replace("\\", "/")
            if info.is_dir() or not rel or rel.endswith("/"):
                continue
            parts = [p for p in rel.split("/") if p]
            if not parts or parts[0] in ("", "/", "..") \
                    or any(p == ".." for p in parts):
                raise ValueError(f"压缩包内含不安全路径：{info.filename}")
            if parts[0].startswith(".") or parts[0] == "__MACOSX" \
                    or any(p == ".DS_Store" for p in parts):
                continue  # macOS 打包垃圾 / 隐藏文件
            total += info.file_size
            if total > MODEL_ZIP_LIMIT:
                raise ValueError("解压后体积超过 200MB 上限")
            tops.add(parts[0])
            try:
                # 标准库安全解压：内部剥离盘符/绝对前缀/.. 成分；上面的显式
                # 审计让恶意包直接报错，而不是被静默改名后入库
                zf.extract(info, str(tmp))
            except OSError as e:
                raise ValueError(f"解压成员失败：{info.filename}（{e}）")
        # 常见打包习惯：所有文件都在一个同名顶层文件夹里 —— 去掉这层冗余
        content = tmp / sorted(tops)[0] if len(tops) == 1 and (tmp / sorted(tops)[0]).is_dir() \
            else tmp
        models = sorted(content.rglob("*.model3.json"))
        if not models:
            raise ValueError("压缩包里没有 Cubism 4 模型（*.model3.json）——"
                             "Cubism 2 旧模型（*.model.json）暂不支持")
        for m in models:
            problem = _model3_problem(m, web_root)
            if problem:
                raise ValueError(f"{m.relative_to(content)}：{problem}")
        content.rename(dest)
        ok = True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if not ok:
            shutil.rmtree(dest, ignore_errors=True)
    found = []
    for m in sorted(dest.rglob("*.model3.json")):
        rel = m.relative_to(web_root)
        found.append({"path": "/" + rel.as_posix(), "label": _model_label(rel)})
    return {"dir": dir_name, "models": found}
