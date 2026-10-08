# Lumi_Nox

> **Two AI characters co-hosting one live show — the engine and orchestration that let them share a stage.**

*Battle-tested by a real daily livestream.*

[![CI](https://github.com/MIO-456/Lumi_Nox/actions/workflows/ci.yml/badge.svg)](https://github.com/MIO-456/Lumi_Nox/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/MIO-456/Lumi_Nox)](https://github.com/MIO-456/Lumi_Nox/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

![Two AI characters co-hosting a livestream](docs/assets/hero.png)

**▶ Watch Lumi & Nox co-host live on Bilibili: [Lumi和Nox-AI搭档](https://space.bilibili.com/544387533)**

## What it does

Lumi and Nox are two AI VTubers who host a livestream together. They hold a
real-time voice conversation with *each other*, banter, react to viewer chat,
remember their regulars across streams, and play games together — in the spirit
of the Neuro × Evil dynamic.

## Try it now (zero setup)

```bash
python main.py
```

No API keys, audio hardware or models required. `main.py` is a runnable taste of
the **real** coordination core: it drives two example characters through the real
scheduler and speech arbiter, so you can watch the turn-taking, @-mention routing
and one-voice-at-a-time logic run in your terminal in a few seconds. The LLM and
voice are swapped for tiny stand-ins here; the production engine lives in the
files below.

## How it works

- **Realtime dual-session engine** (`realtime_chat.py`, `realtime_chat_protocol.py`)
  — each character runs on its own end-to-end speech-to-speech session (doubao
  SC2.0 over websocket); audio is attributed and routed per speaker, so two
  characters can be live at the same time.
- **Turn orchestration & cross-character mirroring** (`conversation.py`,
  `speaker_scheduler.py`) — who speaks next is decided live from @-mentions,
  partner hand-offs and a prioritized viewer-chat queue; what one says is mirrored
  into the other's context as a stage note, so neither mistakes its partner's
  lines for its own.
- **Speech-output arbitration** (`speech_output_arbiter.py`) — only one voice holds
  the floor at a time (QUEUE / DROP / INTERRUPT), released only when a character's
  *audio* has truly finished, so the two never talk over each other.
- **Voice** (`lumi_tts.py`, `cosyvoice_tts.py`, `tts_emitter.py`) — streaming
  text-to-speech with voice-cloned timbres (CosyVoice), or borrowed from the
  realtime engine so both pipelines sound identical.
- **Hearing** (`lumi_asr.py`) — streaming speech recognition for live voice input.
- **Long-term memory** (`memory/`) — per-viewer and self memory in SQLite, distilled
  by an LLM, with deterministic membership facts and time decay so stale topics stop
  dominating later streams.
- **Playing games** (`games/`) — bridges that let the AIs play games as stream
  segments, making decisions and calling tools while they narrate. **Buckshot
  Roulette** (turn-based), **Terraria** (**A\* pathfinding** + a **five-layer goal
  planner** over a tModLoader mod), **Kingdom Rush** — a tower-defense AI driven by a
  **LuaJIT mod reverse-engineered into the game's LÖVE engine** (see
  [reverse-engineering notes](docs/games/kingdom-rush-reverse-engineering.md))
  — and two word games, **Wordle** and **Handle** (汉兜, a Chinese-idiom Wordle), each
  a self-contained web frontend + an entropy solver that runs in a **separate worker
  process**, so the heavy mid-game search never stalls the main loop. The Kingdom Rush
  AI now pairs its algorithmic tower-placement skeleton with a **per-wave LLM strategist**
  that reads the in-game bestiary and adapts the build to each incoming wave.
- **Fast brain** (`fast_brain.py`) — a per-character lightweight LLM for tool-driven
  decisions alongside the realtime voice chat.
- **Coordination backbone** (`event_bus.py`, `state_machine.py`) — every module talks
  through an in-process event bus anchored to one global state machine.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design.

## Repository layout

```
main.py                  # zero-setup co-hosting demo
event_bus.py             # coordination backbone
state_machine.py         # global stream state
speaker_scheduler.py     # turn selection and @-mention routing
speech_output_arbiter.py # one voice holds the floor at a time
conversation.py          # text-pipeline orchestration and history mirroring
realtime_chat*.py        # dual realtime speech-to-speech sessions
lumi_asr.py / lumi_tts.py / cosyvoice_tts.py / tts_emitter.py
memory/                  # SQLite memory, extraction, decay and deterministic facts
games/                   # one directory per game; shared word-game worker
docs/                    # architecture and game engineering notes
tests/                   # zero-dependency public-core checks
```

See [games/README.md](games/README.md) for game entry points and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the complete data flow.

## Local deployment (this fork)

The open core is the engine; this fork adds the deployment layer that makes it
runnable and operable on one machine — a launcher with a web control room, a
settings center, a MiMo TTS provider and the live stage:

```
my_show.py       # launcher: coordinator + LLM + TTS + stage/chat servers
show_settings.py # settings center: LLM / TTS / persona / stage, no restart
mimo_tts.py      # MiMo streaming TTS synth (peer of cosyvoice_tts)
url_guard.py     # pre-flight URL check: http(s) only, public host only
```

![Lumi_Nox local deployment architecture](docs/assets/local-deploy-architecture.png)

That diagram is the whole picture: viewers and OBS along the top, the engine
process in the middle (HTTP endpoints, the scheduler → arbiter → LLM → TTS chain,
and the stage service), and configuration plus external dependencies at the
bottom. Solid arrows are runtime traffic; the dashed red arrow is a settings
change taking effect without a restart.

See [docs/LOCAL_DEPLOY.md](docs/LOCAL_DEPLOY.md) for how to run it, how the
settings precedence works, voice cloning and Live2D model import, and a few
Windows-specific traps worth knowing about.

## Getting started

The coordination layer and the `main.py` demo are pure standard library. To run
the real voice engine, install the dependencies and provide your own keys:

```bash
pip install -r requirements.txt
cp .env.example .env     # then fill in your doubao / DashScope keys
```

### What you need to bring

This repo is the **open-core** of a system that runs a live show every day — the
architecture and engineering are here to read and build on, not a turnkey product.
Running the full thing needs the pieces below: some are yours to bring, some are
intentionally kept closed.

- **API keys** (only to run the live voice — the `main.py` demo needs none). The
  LLM brain takes any **OpenAI-compatible** endpoint (doubao ARK, DashScope,
  OpenAI, …). The realtime speech-to-speech voice currently uses **doubao SC2.0**
  and the streaming TTS/ASR use **DashScope** (CosyVoice / fun-asr). Put your keys
  in `.env`.
- **Your own voice IDs** — the private cloned-voice registry is not published.
  `voice_registry.py` resolves the placeholder characters from
  `CHARACTER_A_VOICE_ID` / `CHARACTER_B_VOICE_ID` and optional matching
  `*_VOICE_MODEL` variables, otherwise TTS falls back to a system voice.
- **A Live2D model** — the avatar / motion / expression layer is tied to specific
  character models and is **not** included; bring your own and wire it in.
- **The game** — the game bridge talks to a commercial game over TCP; you supply
  the game itself.
- **Personas** — `voice_config.py` ships placeholder example characters; the real
  Lumi / Nox persona prompts and worldview are intentionally closed.

Vision, drawing, the Live2D motion/expression layer, voiceprints, and the live
director/control console remain in the private production system.

## Verification

The public core keeps its smoke path free of external services:

```bash
python main.py
python -m unittest discover -s tests -v
python -m compileall -q .
```

The same checks run on every push and pull request through GitHub Actions.

## Roadmap

This is an open window into a real, running system — not a turnkey product roadmap.
Public releases are curated snapshots; the private livestream runtime may move ahead
between milestones. More subsystems open only after their privacy, licensing and
dependency boundaries have been reviewed.

## License & open-core

[MIT](LICENSE). Open-core: the engine, games and architecture are here to read and
build on; the characters' persona, IP, worldview and operations data are not.
