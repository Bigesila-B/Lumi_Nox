# -*- coding: utf-8 -*-
"""渲染「Lumi_Nox 本地部署」架构图（PNG），供 docs/LOCAL_DEPLOY.md 与 README 引用。

用 Pillow 手工绘制（无 graphviz / mermaid 工具链依赖），2x 超采样后 LANCZOS
缩回，保证文字锐利。输出覆盖同目录的 local-deploy-architecture.png：

    python docs/assets/make_architecture_diagram.py

注意：中文字形取自 Windows 的微软雅黑（msyh.ttc / msyhbd.ttc）。该字体不含
⚙（U+2699）、⟷（U+27F7）等符号，写文案时请避开，否则会渲染成方框。
"""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

W, H = 1600, 1200
S = 2  # 超采样倍数

BG = "#FFFFFF"
INK = "#0F172A"
BODY = "#334155"
MUTED = "#64748B"
ARROW = "#334155"
HOT = "#DC2626"

FONT_DIR = "C:/Windows/Fonts/"


def font(size, bold=False):
    return ImageFont.truetype(FONT_DIR + ("msyhbd.ttc" if bold else "msyh.ttc"),
                              int(size * S))


img = Image.new("RGB", (W * S, H * S), BG)
d = ImageDraw.Draw(img)


# ---------------- 基础绘制 ----------------
def rbox(x, y, w, h, fill, outline, r=12, width=1.6):
    d.rounded_rectangle([x * S, y * S, (x + w) * S, (y + h) * S],
                        radius=r * S, fill=fill, outline=outline,
                        width=max(1, int(width * S)))


def text(x, y, s, size=13.5, color=BODY, bold=False, anchor="la",
         align="left", spacing=6):
    d.multiline_text((x * S, y * S), s, font=font(size, bold), fill=color,
                     anchor=anchor, align=align, spacing=int(spacing * S))


def line(x1, y1, x2, y2, color=ARROW, width=2.0):
    d.line([x1 * S, y1 * S, x2 * S, y2 * S], fill=color,
           width=max(1, int(width * S)))


def dashes(x1, y1, x2, y2, color=HOT, width=2.0, seg=7, gap=6):
    import math
    dx, dy = x2 - x1, y2 - y1
    dist = math.hypot(dx, dy)
    if dist == 0:
        return
    ux, uy = dx / dist, dy / dist
    t = 0.0
    while t < dist:
        e = min(t + seg, dist)
        line(x1 + ux * t, y1 + uy * t, x1 + ux * e, y1 + uy * e, color, width)
        t = e + gap


def head(x1, y1, x2, y2, color=ARROW, size=8.5):
    import math
    ang = math.atan2(y2 - y1, x2 - x1)
    p = [(x2, y2),
         (x2 - size * math.cos(ang - 0.42), y2 - size * math.sin(ang - 0.42)),
         (x2 - size * math.cos(ang + 0.42), y2 - size * math.sin(ang + 0.42))]
    d.polygon([(px * S, py * S) for px, py in p], fill=color)


def arrow(x1, y1, x2, y2, color=ARROW, width=2.0, dotted=False, size=8.5):
    if dotted:
        dashes(x1, y1, x2, y2, color, width)
    else:
        line(x1, y1, x2, y2, color, width)
    head(x1, y1, x2, y2, color, size)


# ---------------- 标题 ----------------
text(48, 34, "Lumi_Nox 本地部署 · 架构与数据流", size=30, color=INK, bold=True)
text(48, 78, "★ = 本 fork 在「可本地开播」目标下新增的部署层；其余方块为上游开源核心"
             "（MIO-456/Lumi_Nox，MIT）", size=14, color=MUTED)

# 图例（右上）
lx, ly = 1120, 34
rbox(lx, ly, 432, 74, "#F8FAFC", "#E2E8F0", r=10, width=1.2)
line(lx + 18, ly + 24, lx + 78, ly + 24, ARROW, 2.4)
head(lx + 18, ly + 24, lx + 78, ly + 24, ARROW, 8)
text(lx + 88, ly + 17, "运行时流量：网页 / 弹幕 / 音频 / 事件", size=12.5, color=BODY)
dashes(lx + 18, ly + 52, lx + 78, ly + 52, HOT, 2.4)
head(lx + 18, ly + 52, lx + 78, ly + 52, HOT, 8)
text(lx + 88, ly + 45, "配置热更新：设置保存后即刻生效（免重启）", size=12.5, color=HOT)

# ---------------- 泳道 A：观众与直播软件 ----------------
AX, AY, AW, AH = 48, 136, 1504, 170
rbox(AX, AY, AW, AH, "#F5F9FF", "#BBD4F5", r=14, width=1.6)
text(AX + 20, AY + 10, "① 观众与直播软件", size=15, color="#1D4ED8", bold=True)

# A1 聊天页
a1x, a1y, a1w, a1h = 80, 172, 740, 118
rbox(a1x, a1y, a1w, a1h, "#FFFFFF", "#3B82F6", r=11)
text(a1x + 18, a1y + 11, "浏览器 · 弹幕聊天页 ★", size=16, color="#1E3A8A", bold=True)
text(a1x + 18, a1y + 38, "http://127.0.0.1:8001", size=13, color="#2563EB", bold=True)
text(a1x + 18, a1y + 62,
     "弹幕发送/显示 · 设置面板（LLM / 声音 / 人设 / 舞台）\n"
     "内嵌舞台 iframe ← 静态 :8000（bg=1 铺深色底，避免 iframe 透明合成垫白）",
     size=12.5, color=BODY, spacing=5)

# A2 OBS
a2x, a2y, a2w, a2h = 860, 172, 660, 118
rbox(a2x, a2y, a2w, a2h, "#FFFFFF", "#3B82F6", r=11)
text(a2x + 18, a2y + 11, "OBS 浏览器源（直播合成）", size=16, color="#1E3A8A", bold=True)
text(a2x + 18, a2y + 38, "http://127.0.0.1:8000/frontend/", size=13,
     color="#2563EB", bold=True)
text(a2x + 18, a2y + 62,
     "Live2D 舞台页：口型 / 眨眼 / 呼吸 / 摆动 / 表情 / 字幕\n"
     "背景真透明，供 OBS 叠加；模型与运行库自备",
     size=12.5, color=BODY, spacing=5)

# ---------------- 泳道 B：引擎进程 ----------------
BX, BY, BW, BH = 48, 375, 1504, 550
rbox(BX, BY, BW, BH, "#FFFBF0", "#F3D08A", r=14, width=1.6)
text(BX + 20, BY + 10, "② 引擎进程 — my_show.py ★（本地启动器：编排核心 + LLM + TTS + 舞台服务）",
     size=15, color="#B45309", bold=True)

# B-左：本地 HTTP
blx, bly, blw, blh = 80, 430, 360, 470
rbox(blx, bly, blw, blh, "#FFFFFF", "#E7B04A", r=11)
text(blx + 16, bly + 12, "本地 HTTP 服务 ★", size=15, color="#92400E", bold=True)
text(blx + 16, bly + 36, "127.0.0.1:8001（仅回环绑定）", size=12, color=MUTED)
text(blx + 16, bly + 62,
     "/danmaku        弹幕入队 → 调度器\n"
     "/settings       读写设置中心\n"
     "/models         扫描 Live2D 皮套\n"
     "/model-upload   导入皮套 zip\n"
     "/test-voice     角色试音\n"
     "/voice-clone    克隆音色（上传/删除）\n"
     "/audio-reset    重置发声链路\n"
     "/history-clear  清空对话历史\n"
     "/healthz        健康检查",
     size=12.5, color=BODY, spacing=7)
line(blx + 16, bly + 386, blx + blw - 16, bly + 386, "#EADFC8", 1.4)
text(blx + 16, bly + 402,
     "Windows 双实例防护：\n"
     "allow_reuse_address=False，\n"
     "端口占用直接报错而非静默同绑",
     size=12, color=MUTED, spacing=6)

# B-中：编排核心 + 语音链路（纵向主链）
bcx, bcw = 470, 570
chain = [
    ("① SpeakerScheduler — 选人", "上游编排核心：@提及优先 / 观众队列 / 角色轮转"),
    ("② SpeechOutputArbiter — 发言权仲裁", "同一时刻只有一张嘴（QUEUE / DROP / INTERRUPT）"),
    ("③ fast_brain — LLM 客户端", "OpenAI 兼容端点；stream=True 边生成边分句，首句约 0.7s 出声"),
    ("④ 发声链路 — TTS ★", "mimo_tts / cosyvoice_tts → tts_emitter → 虚拟声卡（AEC 参考缓冲）"),
]
cy = 430
for i, (t, sub) in enumerate(chain):
    h = 92
    rbox(bcx, cy, bcw, h, "#FFF8EC", "#E7B04A", r=11)
    text(bcx + 18, cy + 14, t, size=15, color="#7C2D12", bold=True)
    text(bcx + 18, cy + 44, sub, size=12.5, color=BODY)
    if i < len(chain) - 1:
        arrow(bcx + bcw / 2, cy + h + 6, bcx + bcw / 2, cy + h + 34, ARROW, 2.4)
    cy += h + 40

# B-右：Stage
brx, brw = 1070, 450
rbox(brx, 430, brw, 225, "#FFFFFF", "#E7B04A", r=11)
text(brx + 18, 442, "Stage 舞台服务 ★", size=15, color="#92400E", bold=True)
text(brx + 18, 470, "WS 127.0.0.1:8768 — 事件广播", size=13, color="#2563EB", bold=True)
text(brx + 18, 494,
     "speaking / subtitle / clear / config(amp)\n"
     "→ 驱动口型、字幕气泡、动作幅度",
     size=12.5, color=BODY, spacing=5)
text(brx + 18, 546, "静态 127.0.0.1:8000 — 服务 live2d/ 目录", size=13,
     color="#2563EB", bold=True)
text(brx + 18, 570,
     "前端页 + PIXI / Cubism 运行库 + 皮套模型\n"
     "路径：仓库上一级 ../live2d/（本仓库不含）",
     size=12.5, color=BODY, spacing=5)

rbox(brx, 675, brw, 225, "#FFFFFF", "#E7B04A", r=11)
text(brx + 18, 687, "Stage 前端关键实现（index.html）", size=15,
     color="#92400E", bold=True)
text(brx + 18, 715,
     "· 双遍加载 + 实例重建：绕开 Cubism wasm 堆扩容后\n"
     "  旧实例参数视图失效（否则除最后加载的模型外全部冻结）\n"
     "· autoUpdate:false + 自驱管线：参数写完亲自 update\n"
     "· 参数索引直写 raw.parameters.values，绕开 ID 查找\n"
     "· 精灵级呼吸/摆动保底生命感\n"
     "· 情绪关键词 → 表情（哭 / 脸红 / 比心 …）",
     size=12.5, color=BODY, spacing=7)

# ---------------- 泳道 C：设置中心 + 外部服务 ----------------
CX, CY, CW, CH = 48, 990, 1504, 160
rbox(CX, CY, CW, CH, "#F8F5FF", "#D6C6F5", r=14, width=1.6)
text(CX + 20, CY + 10, "③ 配置与外部依赖", size=15, color="#6D28D9", bold=True)

c1x, c1y, c1w, c1h = 80, 1026, 740, 106
rbox(c1x, c1y, c1w, c1h, "#FFFFFF", "#8B5CF6", r=11)
text(c1x + 18, c1y + 11, "设置中心 show_settings.py ★  +  show_settings.json", size=15,
     color="#5B21B6", bold=True)
text(c1x + 18, c1y + 38,
     "llm 端点/模型/温度 · tts 端点/音色/音量 · persona 人设 · stage 形象/字幕/幅度",
     size=12.5, color=BODY)
text(c1x + 18, c1y + 62,
     "密钥只进 JSON 文件（已 gitignore）；GET 只回 api_key_set，不回明文\n"
     "启动时写入环境变量 → 设置文件值 > .env 值；保存即热替换运行时全局",
     size=12.5, color=MUTED, spacing=5)

c2x, c2y, c2w, c2h = 860, 1026, 660, 106
rbox(c2x, c2y, c2w, c2h, "#FFFFFF", "#8B5CF6", r=11)
text(c2x + 18, c2y + 11, "外部服务 / 自备资源", size=15, color="#5B21B6", bold=True)
text(c2x + 18, c2y + 38,
     "LLM：任意 OpenAI 兼容端点（doubao ARK / DashScope / 自建）",
     size=12.5, color=BODY)
text(c2x + 18, c2y + 62,
     "TTS：MiMo 开放平台（可选 CosyVoice）；出网前经 url_guard ★ 校验公网 host\n"
     "Live2D：模型、PIXI / Cubism 运行库、虚拟声卡与 .env 密钥均需自备",
     size=12.5, color=MUTED, spacing=5)

# ---------------- 跨泳道箭头 ----------------
# A1 → 引擎 HTTP：弹幕
arrow(300, 294, 300, 424, ARROW, 2.4)
text(312, 336, "弹幕 POST /danmaku", size=12.5, color=BODY, bold=True)

# 引擎 Stage → A2：事件 + 静态页
arrow(1295, 424, 1295, 296, ARROW, 2.4)
text(1307, 336, "WS 事件 → 口型 / 字幕", size=12.5, color=BODY, bold=True)

# 引擎 → 外部服务：HTTPS API
arrow(900, 906, 900, 1020, ARROW, 2.4)
text(912, 942, "HTTPS（LLM / TTS，发请求前校验 host）", size=12.5, color=BODY, bold=True)

# 设置中心 → 引擎：热更新（点线）
arrow(560, 1020, 560, 908, HOT, 2.4, dotted=True)
text(572, 942, "保存即热更新（免重启）", size=12.5, color=HOT, bold=True)

img = img.resize((W, H), Image.LANCZOS)
out = Path(__file__).with_name("local-deploy-architecture.png")
img.save(out, "PNG", optimize=True)
print("saved:", out, img.size)
