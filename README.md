# hermes-s2s — Configurable Speech-to-Speech for Hermes Agent

> A standalone Hermes plugin that gives Discord, Telegram, and CLI voice mode the **full S2S backend matrix**: native realtime models (Gemini Live, GPT-4o Realtime), cloud cascaded pipelines, hybrid local+cloud, and fully-local pipelines (your own Moonshine+vLLM+Kokoro server). Mix and match per-stage. Configurable per-call.

## What this is

Hermes ships built-in voice mode with a fixed cascaded STT → LLM → TTS pipeline and a small set of providers. `hermes-s2s` extends that with:

- **Realtime duplex backends** — Gemini Live, GPT-4o Realtime / gpt-4o-mini-realtime
- **Per-stage configurability** — local STT + cloud LLM + local TTS, any combination
- **Local pipeline integration** — wraps your own [streaming-speech-to-speech](https://github.com/codeseys/streaming-speech-to-speech) (or compatible) server as a one-shot duplex backend
- **Provider registry** — Moonshine, Kokoro, faster-whisper, ElevenLabs, OpenAI, Groq, and any custom CLI-wrapped provider
- **Modes** — `cascaded` (STT-then-LLM-then-TTS), `realtime` (native duplex WS), `s2s-server` (full-pipeline external server)

## Why a plugin instead of a fork

Hermes built-in voice mode is great for the common case. This plugin is for power users who want:

- Sub-300ms latency via realtime models or local vLLM pipelines
- Strict privacy (everything local) or strict cost control (free local + cheap cloud LLM)
- Per-call or per-channel backend switching
- Their own infrastructure (HuggingFace S2S server, local GPU rig) plugged in

Forking Hermes for this would diverge the codebase. A plugin opts users in cleanly.

## Quick start

```bash
# 1. Install the plugin
pip install hermes-s2s              # PyPI (once published)
# or, from source:
git clone https://github.com/codeseys/hermes-s2s ~/.hermes/plugins/hermes-s2s
cd ~/.hermes/plugins/hermes-s2s && pip install -e .

# 2. Enable it
hermes plugins enable hermes-s2s

# 3. Pick a profile (interactive)
hermes s2s setup
# Or edit ~/.hermes/config.yaml directly:
```

```yaml
# ~/.hermes/config.yaml
s2s:
  mode: cascaded                # cascaded | realtime | s2s-server
  pipeline:
    stt:
      provider: moonshine       # moonshine | local | groq | openai | mistral | xai | s2s-server
      moonshine:
        model: tiny             # tiny (27M) | base (61M)
        device: cuda            # cuda | cpu
    tts:
      provider: kokoro          # kokoro | edge | elevenlabs | openai | piper | neutts | s2s-server
      kokoro:
        voice: af_heart
        lang_code: a            # a (American English), b (British), j (Japanese), z (Chinese), ...
  realtime:
    provider: gemini-live       # gemini-live | gpt-realtime | gpt-realtime-mini
    gemini_live:
      model: gemini-live-2.5-flash       # half-cascade ($0.06 / 30 min)
      voice: Aoede
    openai:
      model: gpt-realtime-mini           # ($0.45 / 30 min) — or gpt-realtime ($1.44)
      voice: cedar
  s2s_server:
    endpoint: ws://localhost:8000/ws
    health_url: http://localhost:8000/health
    auto_launch: false                    # true: hermes spawns the server
    fallback_to: pipeline                 # if server down, fall through to cascaded
```

## Architecture

Three top-level modes, six provider categories:

```
                          ┌─ s2s.mode: cascaded ──────────────┐
                          │   STT → AIAgent (Hermes LLM) → TTS │
                          │   Each stage independently         │
                          │   configurable, mix local/cloud    │
                          └────────────────────────────────────┘
                          ┌─ s2s.mode: realtime ──────────────┐
 Discord/Telegram/CLI ─▶  │   Discord-Opus ↔ Realtime WS       │
        Voice              │   (Gemini Live / OpenAI Realtime)  │
                          │   Tool calls bridged to Hermes     │
                          └────────────────────────────────────┘
                          ┌─ s2s.mode: s2s-server ────────────┐
                          │   WS to your own server            │
                          │   (full pipeline lives there)      │
                          │   Lowest latency on local hardware │
                          └────────────────────────────────────┘
```

## Backend matrix

| Mode | STT | LLM | TTS | Latency | $/30min | When |
|------|-----|-----|-----|---------|---------|------|
| cascaded | groq | (Hermes) | edge | ~1.5s | ~$0.05 | Cheap cloud, default |
| cascaded | local | (Hermes) | elevenlabs | ~1.2s | ~$0.10 | Privacy-in, premium voice-out |
| cascaded | moonshine | (Hermes) | kokoro | ~0.6s | $0 | Fully local, free |
| cascaded | openai | (Hermes) | openai | ~1.0s | ~$0.15 | All cloud, OpenAI ecosystem |
| realtime | — | gemini-live half-cascade | — | ~600ms | $0.06 | Cheapest realtime |
| realtime | — | gpt-realtime | — | ~700ms | $1.44 | Premium voice quality |
| realtime | — | gpt-realtime-mini | — | ~700ms | $0.45 | Mid-tier realtime |
| s2s-server | (server) | (server) | (server) | ~250ms | electricity | Local v6 pipeline |

## Status

- [x] Project scaffold + plugin manifest
- [x] Config schema + provider registry interfaces
- [x] Moonshine STT provider (local) — ported from Hermes feat/aria-voice
- [x] Kokoro TTS provider (local) — ported from Hermes feat/aria-voice
- [ ] s2s-server backend (WS client for `streaming-speech-to-speech`)
- [ ] realtime-gemini backend
- [ ] realtime-openai backend
- [ ] Discord voice integration (bridge into Hermes's Discord adapter)
- [ ] CLI voice integration
- [ ] `/s2s mode` slash command
- [ ] `hermes s2s` CLI subcommand tree
- [ ] PyPI publish

See `docs/ROADMAP.md` for milestone breakdown.

## Compatibility

- **Hermes Agent**: ≥ 0.X (verify against your installed version)
- **Python**: 3.11+
- **OS**: Linux, WSL2, macOS — Windows native untested
- **GPU**: Optional. CUDA for Moonshine/Kokoro acceleration; both work on CPU.

## License

Apache-2.0. Bundled provider code (Moonshine, Kokoro, etc.) keeps its own upstream licenses.
