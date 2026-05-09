# Backlog — 0.2.0 + 0.3.0

> Status: living doc. Items move from `[ ]` → `[~]` (in flight) → `[x]` (done).
> Each item names its owner file path so parallel subagents have crisp boundaries.

## 0.2.0 — Hermes voice integration (no fork required)

Goal: routes Hermes built-in voice mode (`/voice on`, Discord `/voice join`, Telegram voice notes) through hermes-s2s providers when configured. Pure-plugin; zero changes to NousResearch/hermes-agent core.

### W1: pre_tool_call hook routing

- [ ] **W1-A** — `hermes_s2s/hooks.py`: implement `route_voice_calls(tool_name, args, task_id, **kw)` that intercepts `transcribe_audio` and `text_to_speech` calls when `s2s.mode != cascaded` OR when `s2s.pipeline.{stt,tts}.provider` names a non-Hermes-builtin (e.g. `moonshine`, `kokoro`, `s2s-server`).
- [ ] **W1-B** — Wire the hook in `__init__.py::register(ctx)` via `ctx.register_hook("pre_tool_call", ...)`.
- [ ] **W1-C** — Result-injection mechanism: pre-tool hook can't return a result directly in Hermes (it's observer-only). Instead, **register replacement tools with the same names that delegate to our providers**. Investigate via deepwiki whether `ctx.register_tool(name="transcribe_audio", ...)` last-write-wins or collides; if collides, fall back to a wrapper-tool pattern (`s2s_transcribe_audio` + a `pre_llm_call` system-message overlay teaching the model to use it).
- [ ] **W1-D** — Tests: route a transcribe_audio call through the hook, assert moonshine's `transcribe()` was hit, not faster-whisper's.

### W2: streaming-speech-to-speech server WS client

- [ ] **W2-A** — `hermes_s2s/_internal/ws_client.py`: thin async WS client matching the protocol from https://github.com/codeseys/streaming-speech-to-speech (`turn_start` / `turn_end` / `audio_chunk` binary / `asr_partial` / `asr_result` / `tts_chunk` JSON+binary). Handshake `?version=1` query param.
- [ ] **W2-B** — Upgrade `providers/pipeline/s2s_server.py::S2SServerPipeline.converse()` from STUB → working, using the WS client. Async iterator yields `{type, data, sample_rate}` events.
- [ ] **W2-C** — Health check + auto-launch helper in `hermes_s2s/_internal/server_lifecycle.py`. `auto_launch=true` runs the server as a child process, captures logs to `~/.hermes/logs/s2s-server.log`.
- [ ] **W2-D** — `hermes s2s serve` CLI subcommand: detect `STREAMING_S2S_PATH` env or `~/streaming-speech-to-speech/`, run `python -m server.app.main`.

### W3: Stage-only s2s-server provider polish

- [ ] **W3-A** — Add `/asr`-only and `/tts`-only HTTP endpoints to `streaming-speech-to-speech` (upstream PR there, not in this repo).
- [ ] **W3-B** — Update `providers/stt/s2s_server.py` and `providers/tts/s2s_server.py` to handle both single-shot HTTP and the WS pipeline protocol (auto-detect by URL scheme: `http://` → REST, `ws://` → use full pipeline).
- [ ] **W3-C** — Fallback chain config: if `s2s.s2s_server.fallback_to: pipeline` and the server is unreachable, transparently fall through to local Moonshine + Hermes LLM + Kokoro.

## 0.3.0 — Realtime backends + Discord integration

Goal: `s2s.mode: realtime` works end-to-end in Discord VC with both Gemini Live and gpt-4o-realtime backends, including tool-calling round-trips.

### R1: Audio resampling utility

- [ ] **R1-A** — `hermes_s2s/audio/resample.py`: `resample_pcm(audio_bytes, src_rate, dst_rate, src_channels, dst_channels=1, src_dtype="s16le", dst_dtype="s16le") -> bytes`. Use `scipy.signal.resample_poly` with rational gcd (matches v6 pattern). Handle stereo → mono mixdown explicitly.
- [ ] **R1-B** — Tests: 48kHz s16le stereo (Discord) → 16kHz mono float32 (Gemini Live in) round-trip; 24kHz mono float32 (Gemini out) → 48kHz s16le stereo (Discord). Spectral integrity check (peak detection within 1 bin).

### R2: RealtimeBackend protocol — concrete impls

- [ ] **R2-A** — `providers/realtime/__init__.py`: lock the `RealtimeBackend` Protocol shape and `RealtimeEvent` tagged union (already drafted as stub).
- [ ] **R2-B** — `providers/realtime/gemini_live.py`: full impl. WS to `wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=...`. Inbound: PCM16 16kHz. Outbound: PCM16 24kHz. Emit `transcript_partial` / `audio_chunk` / `tool_call` / `error`. Handle session-resumption tokens.
- [ ] **R2-C** — `providers/realtime/openai_realtime.py`: full impl. WS to `wss://api.openai.com/v1/realtime?model=...`. PCM16 24kHz both ways. Server-VAD events. Hard 30-min cap → tear down + reconnect with replay (lossy; document).
- [ ] **R2-D** — Tool-call bridge: realtime backend `tool_call` event → caller invokes Hermes's tool dispatcher → result via `inject_tool_result(call_id, result)`. Caller keeps a registry of in-flight tool calls.
- [ ] **R2-E** — Tests: mock WS server, dry-run flow (connect, send 1s of silence, receive a synthesized greeting, interrupt, close).

### R3: Discord voice integration

- [ ] **R3-A** — `hermes_s2s/_internal/discord_bridge.py`: hook into Hermes's existing `gateway/platforms/discord.py` voice handler via the plugin's `on_session_start` hook. Inspect platform; if Discord VC and `s2s.mode == realtime`, replace the Hermes-default audio sink/source with a hermes-s2s pair that bridges Discord-Opus ↔ realtime backend.
- [ ] **R3-B** — Determine the actual integration seam: Hermes's voice handler is in `gateway/platforms/discord.py` — does it expose a swappable AudioSink/AudioSource? If not, this might require either (a) a small upstream PR in Hermes adding a `voice_pipeline_factory` hook, or (b) hooking deeper into discord-ext-voice-recv directly inside our plugin (parallel client). **Spike first** before committing to either path.
- [ ] **R3-C** — Slash command extension: `/voice backend gemini` / `/voice backend openai` / `/voice backend local` to pick the active s2s.realtime.provider for the call.

### R4: CLI mic integration

- [ ] **R4-A** — `hermes_s2s/_internal/cli_audio.py`: PortAudio mic loop that streams to a realtime backend (mirrors the CLI voice mode but via realtime instead of cascaded STT-then-LLM-then-TTS).
- [ ] **R4-B** — Hook into Hermes's `/voice on` slash command via `pre_llm_call`-equivalent for voice — pending discovery of whether such a hook exists.

### R5: Polish

- [ ] **R5-A** — Long-tool-call filler audio. When a tool takes >5s, inject a "let me check on that" audio chunk so the model doesn't go silent.
- [ ] **R5-B** — `s2s.realtime.cost_cap_per_session: 1.0` config. Track usage; if exceeded, force-end the session with a graceful audio outro.
- [ ] **R5-C** — Voice-mode aware response shaping (`pre_llm_call` system prompt overlay): "you're answering via voice — be concise, no markdown, no code blocks."

## Out of scope for 0.2.0 / 0.3.0

- Telegram voice channel participation (group calls, pytgcalls/TDLib)
- Voice cloning custom voices (already handled by ElevenLabs `voice_id` config)
- Multi-language realtime auto-detect
- Persona overlay beyond the simple voice-mode prompt shaping
- Full barge-in for cascaded mode (Wave 4 / 0.4.0)

## Risks captured here, not buried in code

- **Hooks-vs-fork:** if the `pre_tool_call` hook can't actually replace tool execution in Hermes (only observe), we need a different routing strategy. **Resolve in Phase 3 research before W1 starts.**
- **Discord voice swap:** Hermes voice handler may not expose a swappable factory. If so, R3 needs either an upstream PR or a parallel discord-ext-voice-recv client. **Spike in Phase 3.**
- **GPU contention:** Realtime backends are network-bound; cascaded local pipeline is GPU-bound. Not a blocker but document expectation in `s2s.mode: cascaded` recipe when user is gaming.
- **Realtime API key UX:** plugin manifest already declares optional `GEMINI_API_KEY` / `OPENAI_API_KEY`. Test that `hermes plugins install` correctly prompts for them when realtime mode is active.
