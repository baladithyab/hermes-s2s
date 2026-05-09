# hermes-s2s — Configurable Speech-to-Speech for Hermes Agent

> A standalone Hermes plugin that gives Discord, Telegram, and CLI voice mode the **full S2S backend matrix**: native realtime models (Gemini Live, GPT-4o Realtime), cloud cascaded pipelines, hybrid local+cloud, and fully-local pipelines (your own Moonshine+vLLM+Kokoro server). Mix and match per-stage. Configurable per-call.

## What this is

Hermes ships built-in voice mode with a fixed cascaded STT → LLM → TTS pipeline and a small set of providers. `hermes-s2s` extends that with:

- **Realtime duplex backends** — Gemini Live, GPT-4o Realtime / gpt-4o-mini-realtime
- **Per-stage configurability** — local STT + cloud LLM + local TTS, any combination
- **Local pipeline integration** — wraps your own [streaming-speech-to-speech](https://huggingface.co/spaces/Codeseys/streaming-speech-to-speech-demo) (or compatible) server as a one-shot duplex backend
- **Provider registry** — Moonshine, Kokoro, faster-whisper, ElevenLabs, OpenAI, Groq, and any custom CLI-wrapped provider
- **Modes** — `cascaded` (STT-then-LLM-then-TTS), `realtime` (native duplex WS), `s2s-server` (full-pipeline external server)

## Why a plugin instead of a fork

Hermes built-in voice mode is great for the common case. This plugin is for power users who want:

- Sub-300ms latency via realtime models or local vLLM pipelines
- Strict privacy (everything local) or strict cost control (free local + cheap cloud LLM)
- Per-call or per-channel backend switching
- Their own infrastructure (HuggingFace S2S server, local GPU rig) plugged in

Forking Hermes for this would diverge the codebase. A plugin opts users in cleanly.

## 30-second install

```bash
pip install 'hermes-s2s[all] @ git+https://github.com/baladithyab/hermes-s2s.git@v0.3.2'
hermes plugins enable hermes-s2s
hermes s2s setup --profile realtime-gemini
hermes s2s doctor
```

The doctor command tells you what's missing (API keys, system deps).
Once it's all green, restart `hermes gateway` and `/voice join` in any
Discord VC the bot has access to.

### Other profiles

| Profile | What you get | $/30min |
|---|---|---|
| realtime-gemini | Gemini Live half-cascade | $0.06 |
| realtime-openai | gpt-realtime premium | $1.44 |
| realtime-openai-mini | gpt-realtime-mini | $0.45 |
| local-all | Moonshine + Kokoro (cascaded) | electricity only |

Full profile matrix + per-OS system deps: [docs/INSTALL.md](https://github.com/baladithyab/hermes-s2s/blob/main/docs/INSTALL.md).
End-to-end voice-mode walkthrough + troubleshooting: [docs/HOWTO-VOICE-MODE.md](https://github.com/baladithyab/hermes-s2s/blob/main/docs/HOWTO-VOICE-MODE.md).

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

**0.3.2 (current):** plug-and-play install + `hermes s2s doctor` diagnostic + backend filler audio. `hermes s2s setup --profile realtime-gemini` writes the full realtime config + monkey-patch flag in one shot; `hermes s2s doctor` runs a readiness check over config, Python deps, system deps, API keys, and an optional backend-WS probe. Backend filler audio (`send_filler_audio`) now lands for both Gemini Live and OpenAI Realtime so slow tool calls no longer produce dead air. See [docs/INSTALL.md](https://github.com/baladithyab/hermes-s2s/blob/main/docs/INSTALL.md) for the install profile matrix and [docs/HOWTO-VOICE-MODE.md](https://github.com/baladithyab/hermes-s2s/blob/main/docs/HOWTO-VOICE-MODE.md#diagnosing-problems-with-hermes-s2s-doctor) for the doctor walkthrough.

**0.3.1:** realtime audio actually flows through Discord. With `HERMES_S2S_MONKEYPATCH_DISCORD=1` + `s2s.mode: realtime`, `/voice join` bridges a Discord VC end-to-end through Gemini Live or OpenAI Realtime — incoming 48 kHz stereo Opus is decoded, resampled, and streamed to the backend; responses come back as 20 ms / 3 840-byte frames into `discord.AudioSource`. Tool calls round-trip through Hermes's dispatcher with 5 s soft / 30 s hard timeouts. See [docs/HOWTO-REALTIME-DISCORD.md](docs/HOWTO-REALTIME-DISCORD.md) to set it up and [scripts/smoke_realtime.py](scripts/smoke_realtime.py) to validate your API keys standalone.

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
- [x] **Plug-and-play install + `hermes s2s doctor`** (0.3.2 — see ADR-0009)
- [x] **Per-backend filler-audio implementation** (0.3.2)
- [ ] Multi-user VC mixing (0.4.0)
- [ ] Filler-audio cancellation when the tool result arrives early (0.4.0)
- [ ] Daemon mode to eliminate per-call model-load cost (0.3.x)
- [ ] Telegram realtime duplex (0.4.0)
- [ ] PyPI publish

> **Known issues in 0.3.2:**
>
> - **Single-user only.** Discord's `AudioSink.write` fires per-user-per-frame; the bridge picks the first active speaker and ignores the rest. Multi-party mixing is scoped to 0.4.0.
> - **Filler audio is fire-and-forget.** `send_filler_audio("let me check on that")` now plays, but if the tool result lands mid-phrase, the filler is NOT interrupted — you'll hear both. Cancellation is scoped to 0.4.0.
> - **Realtime requires the monkey-patch flag.** `HERMES_S2S_MONKEYPATCH_DISCORD=1` must be set explicitly; the `realtime-*` setup profiles append this automatically, but if you're editing `.env` by hand you still need the line.

See `docs/ROADMAP.md` for the milestone breakdown.

## Compatibility

- **Hermes Agent**: ≥ 0.X (verify against your installed version)
- **Python**: 3.11+
- **OS**: Linux, WSL2, macOS — Windows native untested
- **GPU**: Optional. CUDA for Moonshine/Kokoro acceleration; both work on CPU.

## License

Apache-2.0. Bundled provider code (Moonshine, Kokoro, etc.) keeps its own upstream licenses.
