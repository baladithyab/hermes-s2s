# Backlog — 0.3.1

> Goal: make the Discord realtime bridge actually move audio. Today (0.3.0) it logs a warning and the built-in Hermes voice stays active. Wave plan 0.3.0 explicitly deferred this because it requires a live Hermes + Discord harness to verify. 0.3.1 closes that gap.

## Atomic items

### B1: Incoming audio sink (Discord → realtime backend)
- [ ] **B1-A** — `hermes_s2s/_internal/audio_bridge.py`: a `RealtimeAudioBridge` class that owns one realtime backend session and two queues (in: PCM from Discord, out: PCM to Discord).
- [ ] **B1-B** — Drop-in `discord.AudioSink` subclass in `_internal/discord_audio.py` that consumes Discord's per-user 20ms Opus frames, decodes via the `discord.opus` decoder, and pushes 48k stereo s16le PCM into the bridge's input queue.
- [ ] **B1-C** — Resampling layer: bridge reads from input queue, resamples 48k stereo → 16k mono (Gemini) or 24k mono (OpenAI) via `hermes_s2s.audio.resample`, sends to `backend.send_audio_chunk()`.
- [ ] **B1-D** — Backpressure: drop oldest frames if queue fills, log a counter (helps surface "user is talking faster than the network can ship").

### B2: Outgoing audio source (realtime backend → Discord)
- [ ] **B2-A** — `hermes_s2s/_internal/discord_audio.py::QueuedPCMSource` subclass of `discord.AudioSource` that pulls 20ms PCM frames from the bridge's output queue. Yields silence when the queue is empty (Discord requires steady frame cadence).
- [ ] **B2-B** — Bridge reads `backend.recv_events()`, maps `audio_chunk` events: resample backend's PCM (16k Gemini / 24k OpenAI mono) → 48k stereo, chunk into 20ms `frames` (3840 bytes each at 48kHz s16le stereo), push into output queue.
- [ ] **B2-C** — Source is wired into `voice_client.play(source)` with a stop-on-bridge-close callback.

### B3: Tool-call bridging
- [ ] **B3-A** — Bridge listens for `tool_call` events from `recv_events()`. On call, invoke Hermes's tool dispatcher via the existing `s2s_tools.dispatch_tool` (or however the plugin context exposes it).
- [ ] **B3-B** — Tool result → `backend.inject_tool_result(call_id, result)`.
- [ ] **B3-C** — Timeout policy: if a tool runs >5s, push a single "let me check on that" filler audio chunk into the output queue (synthesized once, cached) so the model's user-perceived latency stays bounded. If tool runs >30s, abort with an error injected.

### B4: Wire audio_bridge into discord_bridge
- [ ] **B4-A** — Replace the stub log in `_internal/discord_bridge.py::_attach_realtime_to_voice_client` with a real `RealtimeAudioBridge` instantiation.
- [ ] **B4-B** — Backend resolution from config: read `s2s.realtime.provider` and `s2s.realtime.<provider>.*` opts, call `resolve_realtime(provider, opts)`, hand the backend to the bridge.
- [ ] **B4-C** — Lifecycle: bridge starts on `/voice join` (when monkey-patch fires), stops on `/voice leave` (or VoiceClient disconnect). Cleanup must be idempotent — `bridge.close()` callable multiple times.

### B5: Smoke harness
- [ ] **B5-A** — `scripts/smoke_realtime.py`: standalone script that connects to a real backend (Gemini Live or OpenAI Realtime, gated on env keys), sends a 1-second tone or pre-recorded WAV, confirms it gets audio back. Useful for "does my GEMINI_API_KEY actually work" validation. Not run in CI.
- [ ] **B5-B** — `scripts/smoke_discord.py`: outline-only — actually exercising Discord requires a bot token, a server, and a VC. Document the manual test plan.
- [ ] **B5-C** — `tests/test_audio_bridge.py`: unit tests with mocked backend + mocked Discord audio. Cover: bridge lifecycle, backpressure, tool-call round-trip, filler audio injection.

### B6: Documentation
- [ ] **B6-A** — `docs/HOWTO-REALTIME-DISCORD.md` (NEW): end-to-end setup for the realtime path. Bot setup, env keys, monkey-patch flag, expected latency, known issues.
- [ ] **B6-B** — Update README Status section: 0.3.0 = bridge plumbing; 0.3.1 = audio actually flows.
- [ ] **B6-C** — Update SKILL.md with the realtime troubleshooting section.

## Risks

- **Discord opus decoder availability**: `discord.opus` requires `libopus` system lib. Document, fail loudly with install instructions.
- **Per-user vs mixed audio**: Discord's `AudioSink.write` fires per-user-per-frame. For a single-user realtime conversation this is fine; for a multi-party VC we'd need to either pick one user or mix. **Decision: 0.3.1 supports single-user only, document the limitation.**
- **Frame timing precision**: Discord expects exactly 20ms cadence. If backend audio chunks arrive at irregular sizes (Gemini delivers ~250ms chunks, OpenAI ~50ms), we need to slice and queue. The QueuedPCMSource yielding silence on empty handles the underflow case.
- **Tool-call latency**: filler-audio TTS itself takes ~200ms. Pre-synthesize once at bridge start (single sample like "let me check on that") and cache the PCM bytes.
- **Cleanup ordering**: backend WS close vs Discord disconnect vs queue draining — get this wrong and you leak async tasks. Need a single `bridge.close()` that cancels everything in the right order.

## Out of scope for 0.3.1 (deferred to 0.4.0)

- Multi-party VC mixing
- Telegram realtime
- Voice cloning custom voices
- True barge-in for cascaded mode
- s2s-server full-pipeline WS client
