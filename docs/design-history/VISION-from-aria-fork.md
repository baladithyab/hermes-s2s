# ARIA — Voice Backends for Hermes (definitive scope)

> **2026-05-09 scope-lock.** Three pivots collapsed the project into its real shape:
> 1. Hermes already ships `/voice join` + multi-provider STT/TTS (`tools/transcription_tools.py`, `tools/tts_tool.py`) → existing voice mode is the seam, not the target.
> 2. `tts.providers.<name>` with `type: command` is already a CLI escape hatch → command-wrapped providers need **no Python in core**.
> 3. User has a production local S2S pipeline at `/mnt/e/CS/HF/streaming-speech-to-speech/` (v6: 212ms best, 239ms median TTFA, Moonshine+vLLM+Kokoro). We **wire** it, not rebuild it.

## The S2S backend matrix Hermes should support

| Backend | Path | Latency | Cost (30min call) | When to use |
|---|---|---|---|---|
| **realtime-gemini** | Discord-Opus ↔ Gemini Live WS (native duplex) | ~500-700ms | $0.06 (half-cascade) – $0.36 (native-audio) | Cheap cloud, real-time barge-in |
| **realtime-openai** | Discord-Opus ↔ gpt-4o-realtime WS | ~600-900ms | $0.45 (mini) – $1.44 (gpt-realtime) | Premium voice quality, robust tool-calling |
| **cascaded-cloud** | STT-API → LLM (any) → TTS-API | 1.5-3s | varies | Existing Hermes pipeline; works today; just config |
| **cascaded-hybrid** | local STT (Moonshine/faster-whisper) → cloud LLM (OpenRouter) → cloud TTS (ElevenLabs/OpenAI) | 1-2s | ~$0.05-0.30 | Privacy on input, cloud quality on output |
| **cascaded-local** | local STT → local LLM (vLLM) → local TTS (Kokoro) | 250-600ms | electricity only | Air-gapped, zero per-minute cost; **wraps user's v6 pipeline** |
| **cascaded-mixed** | any local + any cloud combo via config | varies | varies | Power-user customization |

The five "cascaded-*" rows are all the **same code path** with different per-stage provider configs. Realtime-* are a separate code path (duplex bridge, no STT/TTS roundtrip).

## Hermes integration shape (verified via deepwiki)

**Existing extension points:**
- `tools/transcription_tools.py::_get_provider` + `_transcribe_*` — STT dispatch (if/elif chain, no ABC).
- `tools/tts_tool.py::text_to_speech_tool` + `_generate_*` — TTS dispatch.
- `tts.providers.<name>` with `type: command` — zero-Python CLI wrapper.
- `HERMES_LOCAL_STT_COMMAND` env — same for STT.
- `gateway/platforms/discord.py` — already has `/voice join`, per-user audio streams, SPEAKING-opcode SSRC mapping, echo prevention, hallucination filter.

**What's missing:**
1. **Moonshine STT provider** — add `_transcribe_moonshine()` to `tools/transcription_tools.py`.
2. **Kokoro TTS provider** — add `_generate_kokoro()` to `tools/tts_tool.py`.
3. **Realtime-mode plumbing** — new `voice.mode: "realtime" | "cascaded"` switch; bypasses STT/TTS, opens duplex WS to a realtime backend, bridges to Discord/CLI audio.
4. **Realtime backend plugins** — `realtime-gemini` and `realtime-openai`. New plugin type or just two config-driven implementations.
5. **Local-pipeline integration** — wrapper around the user's `streaming-speech-to-speech/server/app/` (or pipelines/v6_final.py) that exposes it as a Hermes voice provider. Two options:
   - **Option A:** Run S2S server as separate process (it already is a FastAPI WS server). Hermes voice mode connects as a WS client. Lowest coupling, highest re-use.
   - **Option B:** Import its components (Moonshine + vLLM + Kokoro) directly into Hermes process. Higher coupling, simpler deploy.
   - **Recommendation:** Option A. The existing repo is a real product with its own benchmarks, ADRs, demo Space. Hermes wraps it as a backend, not absorbs it.

## Backlog — all waves

### Wave 1: Cascaded-pipeline provider additions (≤1 day, clean upstream PR)

- **W1-A** Moonshine STT provider (`tools/transcription_tools.py`)
- **W1-B** Kokoro TTS provider (`tools/tts_tool.py`)
- **W1-C** Update `hermes_cli/setup.py` + `tools_config.py` for wizard discovery
- **W1-D** Docs: `website/docs/user-guide/features/tts.md` + STT section
- **W1-E** Tests: provider availability check, smoke test on a 1s clip
- **W1-F** PR upstream to NousResearch/hermes-agent

### Wave 2: Local-pipeline backend wrapper (~1-2 days)

- **W2-A** Add `s2s-local` STT provider that opens a WS to user's streaming-speech-to-speech server, sends mic audio, receives transcript+TTS in one shot. Bypasses Hermes's normal LLM call (the local pipeline already has its own LLM).
- **W2-B** Alternative split: `s2s-local-stt-only` and `s2s-local-tts-only` providers that talk to the local server for just one stage (so user can mix-and-match: local Moonshine + cloud LLM + local Kokoro).
- **W2-C** Config schema: `voice.local.endpoint`, `voice.local.health_check`, `voice.local.fallback`.
- **W2-D** Setup wizard branch: detect/install streaming-speech-to-speech repo, prompt to launch its server.

### Wave 3: Realtime duplex backends (~3-5 days, the real new work)

- **W3-A** `voice.mode: "realtime" | "cascaded"` config switch in Discord and CLI voice paths.
- **W3-B** RealtimeBackend abstraction: `connect()`, `send_audio(pcm_chunk)`, `recv_audio_stream() -> AsyncIterator`, `inject_tool_result()`, `interrupt()`, `close()`.
- **W3-C** Audio resampling layer: Discord 48kHz s16le stereo ↔ realtime backend's required format (16kHz Gemini, 24kHz OpenAI, both PCM mono).
- **W3-D** **realtime-gemini** backend (Gemini Live WS). Half-cascade default ($0.06/30min); native-audio config option.
- **W3-E** **realtime-openai** backend (gpt-realtime / gpt-realtime-mini WS). gpt-4o-mini-realtime default ($0.45/30min).
- **W3-F** Tool-call bridging: realtime backend emits function call → Hermes runs it via the existing tool dispatch → result injected back into the realtime session.
- **W3-G** Session resumption (Gemini >15min via tokens; OpenAI hard 30min cap with reconnect).
- **W3-H** Tests: mock WS server, dry-run flow, real-API smoke test gated on env keys.

### Wave 4: Polish (deferred)

- True barge-in for cascaded mode (VAD on incoming audio while TTS plays → cancel)
- Voice-aware tool verbalization (numbers→words, URLs→"sending as text", code→summary)
- Multi-party VC handling (per-user STT routing, "@voice-bot" wakeword optional)
- Persona-overlay system message for voice-mode-only response shaping (no markdown, shorter sentences)

## Open question for the user before W1 starts

The PR target. Wave 1 is a clean upstream PR — it adds two providers using the established dispatch pattern, no new abstractions, no breaking changes. Wave 2 is also probably upstream-friendly (just another set of providers). Wave 3 introduces a new top-level mode and the abstraction for it; that's bigger and might want to land as a plugin in this fork first, then upstream once proven.

I'm proceeding under: **W1 → upstream PR; W2 → upstream PR with feature-flag; W3 → fork-first, upstream-after-proof**. If you want different, say so.

## Skipped / not doing

- ~~Building a separate ARIA daemon~~ — Hermes voice mode is the runtime.
- ~~discord.py voice gateway research~~ — Hermes already wraps it; deferred research files (`docs/research/01-discord-voice.md`) kept for reference only.
- ~~"ARIA persona" Wave 3 item~~ — punted to Wave 4 polish; users can already set personas via existing skin/persona config.
- ~~Telegram voice channels~~ — Telegram voice notes already work; group-call participation is out of scope.
