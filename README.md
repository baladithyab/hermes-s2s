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

**0.2.0 (current):** command-provider integration shipping. `hermes-s2s-tts` / `hermes-s2s-stt` console scripts wire into Hermes voice mode via the built-in `type: command` TTS provider and the `HERMES_LOCAL_STT_COMMAND` env var — **no Hermes fork required**, config-only on the Hermes side. See [ADR-0004](docs/adrs/0004-command-provider-interception.md) for why this beats the original "register tools with the same name" sketch.

- [x] Project scaffold + plugin manifest
- [x] Config schema + provider registry interfaces
- [x] Moonshine STT provider (local) — ported from Hermes feat/aria-voice
- [x] Kokoro TTS provider (local) — ported from Hermes feat/aria-voice
- [x] **`hermes-s2s-tts` / `hermes-s2s-stt` CLI shims** (0.2.0)
- [x] **`hermes s2s setup` interactive wizard** (0.2.0)
- [x] **`s2s-server` provider (HTTP REST — `/asr`, `/tts`)** (0.2.0)
- [ ] Daemon mode (`hermes s2s serve --daemon`) — eliminates per-call model-load cost (0.2.1)
- [ ] `s2s-server` WebSocket pipeline mode (0.3.0)
- [ ] Realtime backends: Gemini Live, GPT-4o Realtime (0.3.0 — see ADR-0005 / ADR-0006)
- [ ] Discord voice seam (0.3.0 — see ADR-0006)
- [ ] PyPI publish

> **Known issue (0.2.0):** every voice turn pays the model-load cost because each shim invocation is a fresh Python subprocess. On a 5090 that's ~200–400ms of cold-start overhead per call. The 0.2.1 daemon mode resolves this — see [HOWTO-VOICE-MODE.md §5](docs/HOWTO-VOICE-MODE.md#5-daemon-mode-021-preview).

See `docs/ROADMAP.md` for the milestone breakdown.

## Compatibility

- **Hermes Agent**: ≥ 0.X (verify against your installed version)
- **Python**: 3.11+
- **OS**: Linux, WSL2, macOS — Windows native untested
- **GPU**: Optional. CUDA for Moonshine/Kokoro acceleration; both work on CPU.

## License

Apache-2.0. Bundled provider code (Moonshine, Kokoro, etc.) keeps its own upstream licenses.
