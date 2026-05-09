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

Three steps to get local Moonshine STT + Kokoro TTS flowing through Hermes voice mode:

```bash
# 1. Install (with the local-all extra — pulls moonshine_onnx + kokoro)
pip install "hermes-s2s[local-all]"
# or, from source:
git clone https://github.com/baladithyab/hermes-s2s ~/.hermes/plugins/hermes-s2s
cd ~/.hermes/plugins/hermes-s2s && pip install -e ".[local-all]"

# 2. Enable the plugin in Hermes
hermes plugins enable hermes-s2s

# 3. Pick a profile (interactive wizard — writes the right config for you)
hermes s2s setup
# tip: `hermes s2s setup --profile local-all --dry-run` to preview the diff
```

Then start Hermes normally, enable voice (`/voice on` in CLI, or join a Discord VC with the voice bridge configured), and talk.

> **What this changes in your Hermes config**
>
> `hermes s2s setup` doesn't fork Hermes — it writes standard Hermes command-provider entries that invoke the `hermes-s2s-tts` / `hermes-s2s-stt` console scripts shipped by this package. See [ADR-0004](docs/adrs/0004-command-provider-interception.md) for the integration rationale.
>
> ```yaml
> # ~/.hermes/config.yaml (excerpt, written by `hermes s2s setup`)
> tts:
>   provider: hermes-s2s-kokoro
>   providers:
>     hermes-s2s-kokoro:
>       type: command
>       command: "hermes-s2s-tts --provider kokoro --voice af_heart --output {output_path} --text-file {input_path}"
>       output_format: wav
> ```
>
> ```bash
> # ~/.hermes/.env (appended by `hermes s2s setup`)
> HERMES_LOCAL_STT_COMMAND='hermes-s2s-stt --provider moonshine --model tiny --input {input_path} --output {output_path}'
> ```

For the full mechanics of each path, worked examples, and troubleshooting, see [docs/HOWTO-VOICE-MODE.md](docs/HOWTO-VOICE-MODE.md).

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

**0.3.1 (current):** realtime audio actually flows through Discord. With `HERMES_S2S_MONKEYPATCH_DISCORD=1` + `s2s.mode: realtime`, `/voice join` bridges a Discord VC end-to-end through Gemini Live or OpenAI Realtime — incoming 48 kHz stereo Opus is decoded, resampled, and streamed to the backend; responses come back as 20 ms / 3 840-byte frames into `discord.AudioSource`. Tool calls round-trip through Hermes's dispatcher with 5 s soft / 30 s hard timeouts. See [docs/HOWTO-REALTIME-DISCORD.md](docs/HOWTO-REALTIME-DISCORD.md) to set it up and [scripts/smoke_realtime.py](scripts/smoke_realtime.py) to validate your API keys standalone.

- [x] Project scaffold + plugin manifest
- [x] Config schema + provider registry interfaces
- [x] Moonshine STT provider (local) — ported from Hermes feat/aria-voice
- [x] Kokoro TTS provider (local) — ported from Hermes feat/aria-voice
- [x] `hermes-s2s-tts` / `hermes-s2s-stt` CLI shims (0.2.0)
- [x] `hermes s2s setup` interactive wizard (0.2.0)
- [x] `s2s-server` provider — HTTP REST (0.2.0), WebSocket pipeline (0.3.0)
- [x] **Realtime backends: Gemini Live, GPT-4o Realtime wire protocol** (0.3.0)
- [x] **Discord audio bridge actually moves frames** (0.3.1 — see ADR-0007)
- [x] **Tool-call bridging with timeouts** (0.3.1 — see ADR-0008)
- [ ] Multi-user VC mixing (0.4.0)
- [ ] Per-backend filler-audio impl during slow tool calls (0.3.2)
- [ ] Daemon mode to eliminate per-call model-load cost (0.3.x)
- [ ] Telegram realtime duplex (0.4.0)
- [ ] PyPI publish

> **Known issues in 0.3.1:**
>
> - **Single-user only.** Discord's `AudioSink.write` fires per-user-per-frame; 0.3.1 picks the first active speaker and ignores the rest. Multi-party mixing is scoped to 0.4.0.
> - **Filler audio is a stub.** `send_filler_audio` on both Gemini Live and OpenAI Realtime backends raises `NotImplementedError` — the tool bridge catches it and carries on, so users get longer silent gaps on slow tool calls instead of a "let me check on that" placeholder. Real per-backend filler triggers land in 0.3.2.
> - **Realtime requires the monkey-patch flag.** `HERMES_S2S_MONKEYPATCH_DISCORD=1` must be set explicitly; without it the bridge is a no-op and Hermes's built-in cascaded voice keeps running (so 0.3.0 users don't regress).

See `docs/ROADMAP.md` for the milestone breakdown.

## Compatibility

- **Hermes Agent**: ≥ 0.X (verify against your installed version)
- **Python**: 3.11+
- **OS**: Linux, WSL2, macOS — Windows native untested
- **GPU**: Optional. CUDA for Moonshine/Kokoro acceleration; both work on CPU.

## License

Apache-2.0. Bundled provider code (Moonshine, Kokoro, etc.) keeps its own upstream licenses.
