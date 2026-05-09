# Wave plan: 0.3.0 — Realtime backends + Discord voice bridge

> ADRs: 0002 (realtime abstraction), 0005 (audio resampling), 0006 (discord bridge).
> Research: 05-realtime-ws-protocols.md (implementation-grade WS shapes), 06-hermes-discord-voice-seam.md (bridge strategy).
> Effort: ~2-3 days across 4 parallel subagents + 1 Discord-bridge spike.
> Acceptance: `s2s.mode: realtime` + `s2s.realtime.provider: gemini-live` works end-to-end in Discord VC, including tool-calling.

## Goal

Promote the realtime backends and Discord integration from STUB to working.

After 0.3.0:
- `s2s.mode: realtime` + `gemini-live` or `gpt-realtime` produces duplex audio in Discord VC.
- Tool calls from the realtime backend route through Hermes's tool dispatcher; results inject back.
- `audio/resample.py` handles the 48k stereo Discord ↔ 16/24k mono backend conversions.
- The Discord bridge installs via monkey-patch (gated on `HERMES_S2S_MONKEYPATCH_DISCORD=1` env) until the upstream PR (ADR-0006) lands.

## Acceptance criteria

1. `from hermes_s2s.audio.resample import resample_pcm` round-trips a 1s tone 48k stereo s16le → 16k mono float32 → 48k stereo s16le with peak frequency within 1 bin of original.
2. `make_gemini_live({...})` returns a connected backend; `await backend.connect("you are a helpful voice assistant", "Aoede", [])` opens a real WS to the Live API (mocked in CI; live test gated on `GEMINI_API_KEY` env).
3. `make_openai_realtime({...})` symmetric.
4. Tool-call round trip in mock-WS test: backend emits `{type:"tool_call", payload:{call_id, name:"search", args:{q:"foo"}}}`, caller responds with `await backend.inject_tool_result(call_id, "result text")`, next event is `audio_chunk` representing the model's spoken response.
5. Discord bridge: with `HERMES_S2S_MONKEYPATCH_DISCORD=1` and a real Hermes voice setup, joining a VC with `s2s.mode: realtime` routes audio through the Gemini Live backend (smoke test, manually verified by user — not in CI).
6. Tests: `tests/test_audio_resample.py` (5 tests), `tests/test_gemini_live.py` (4 tests, mock WS), `tests/test_openai_realtime.py` (4 tests, mock WS), `tests/test_discord_bridge.py` (3 tests, mocked Hermes import). All existing tests pass.

## File ownership

| Subagent | Owns | Touches | Reads |
|---|---|---|---|
| **R1: Audio resampling** | `hermes_s2s/audio/resample.py` (replace stub), `tests/test_audio_resample.py` (new) | none | ADR-0005 |
| **R2-A: Gemini Live** | `hermes_s2s/providers/realtime/gemini_live.py` (replace stub), `tests/test_gemini_live.py` (new) | none | research/05-realtime-ws-protocols.md, ADR-0002 |
| **R2-B: OpenAI Realtime** | `hermes_s2s/providers/realtime/openai_realtime.py` (replace stub), `tests/test_openai_realtime.py` (new) | none | research/05-realtime-ws-protocols.md, ADR-0002 |
| **R3: Discord bridge spike + impl** | `hermes_s2s/_internal/discord_bridge.py` (new), `tests/test_discord_bridge.py` (new) | `hermes_s2s/__init__.py` (add bridge install in `register()`) | ADR-0006, research/06-hermes-discord-voice-seam.md |

## Subagent prompts (sketches)

### R1 — Audio resampling utility

> Replace stub `hermes_s2s/audio/__init__.py`. Create `hermes_s2s/audio/resample.py` per ADR-0005 (`docs/adrs/0005-audio-resampling.md`). Implement `resample_pcm(audio_bytes, src_rate, dst_rate, src_channels, dst_channels=1, src_dtype="s16le", dst_dtype="s16le") -> bytes`.
>
> Plus a helper `pcm_to_numpy(audio_bytes, dtype, channels) -> np.ndarray` and `numpy_to_pcm(arr, dtype) -> bytes` so callers don't have to reimplement format conversion.
>
> Tests `tests/test_audio_resample.py`: 5 cases covering (a) 48k stereo s16le → 16k mono float32 (Discord → Gemini Live in), (b) 24k mono float32 → 48k stereo s16le (Gemini Live out → Discord), (c) same-rate same-channels passthrough, (d) stereo→mono mixdown only, (e) round-trip 1s 440Hz tone preserves peak frequency within 1 FFT bin.
>
> CONSTRAINTS: scipy is in `[audio]` extra; if not installed, raise a helpful ImportError. Default to scipy.signal.resample_poly. ~150 LOC for impl + tests. No hard scipy import at module load (lazy in resample_pcm body).

### R2-A — Gemini Live backend

> Replace `hermes_s2s/providers/realtime/gemini_live.py` STUB with a working impl. Spec is in `docs/design-history/research/05-realtime-ws-protocols.md` §Gemini Live.
>
> Implement `GeminiLiveBackend` matching the `RealtimeBackend` Protocol (in `providers/realtime/__init__.py`):
> - `connect(system_prompt, voice, tools)`: open WS to `wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={GEMINI_API_KEY}`. Send `BidiGenerateContentSetup` JSON with model, voice, system_prompt, tools (translated from Hermes's tool schema dict to Gemini's function-declaration format).
> - `send_audio_chunk(pcm_chunk, sample_rate)`: send `BidiGenerateContentClientContent` with audio inline base64. Resample to 16kHz s16le mono if not already (use `hermes_s2s.audio.resample`).
> - `recv_events()`: async-iterate WS messages, yield `RealtimeEvent` for each `serverContent.modelTurn` (audio chunk, transcript), `toolCall` (function call), `sessionResumptionUpdate` (resumption token), `error`.
> - `inject_tool_result(call_id, result)`: send `BidiGenerateContentToolResponse` with `functionResponses[].id == call_id`. Gemini auto-resumes after tool response.
> - `interrupt()`: send `BidiGenerateContentClientContent.activityStart` then `activityEnd` to interrupt mid-utterance.
> - `close()`: clean WS shutdown.
> - Session resumption: on `sessionResumptionUpdate.newHandle`, store handle. On reconnect (after disconnect or timeout), pass the handle in `setup.sessionResumption.handle`.
>
> Tests `tests/test_gemini_live.py`: 4 cases using a mock WS server (use `websockets.serve` in a pytest fixture, point the backend at `ws://127.0.0.1:<port>`):
> 1. Happy connect → setup → audio in → audio out → close
> 2. Tool-call round trip (server emits toolCall, asserts client sends toolResponse with matching id)
> 3. Session resumption (server sends resumption update, client reconnects with handle)
> 4. Error event surfaces as `RealtimeEvent(type="error", ...)`
>
> CONSTRAINTS: `websockets` package is in `[realtime]` extra; lazy-import. ~250 LOC backend + ~150 LOC tests. Use `httpx` only if the API requires it (it doesn't for the WS path). Do NOT call the real API in tests.

### R2-B — OpenAI Realtime backend

> Replace `hermes_s2s/providers/realtime/openai_realtime.py` STUB. Spec is in `research/05-realtime-ws-protocols.md` §OpenAI Realtime.
>
> Implement `OpenAIRealtimeBackend` matching `RealtimeBackend`:
> - `connect`: WS to `wss://api.openai.com/v1/realtime?model={model}`, headers `Authorization: Bearer {OPENAI_API_KEY}`, `OpenAI-Beta: realtime=v1`. Send `session.update` with voice, instructions (system_prompt), tools, input/output audio format (PCM16 24kHz).
> - `send_audio_chunk`: send `input_audio_buffer.append` with base64-encoded PCM16 24kHz. Resample if input rate differs.
> - `recv_events`: yield events for `response.audio.delta` (audio chunk), `response.audio_transcript.delta` (transcript partial), `response.function_call_arguments.done` (tool call complete), `response.done` (end-of-turn with usage), `error`.
> - `inject_tool_result(call_id, result)`: send `conversation.item.create` with type `function_call_output` + `call_id` + `output`, THEN send `response.create` to make model continue (this is the OpenAI gotcha — Gemini auto-resumes; OpenAI doesn't).
> - `interrupt`: send `response.cancel` + `output_audio_buffer.clear` + `conversation.item.truncate`.
> - `close`: clean shutdown.
> - Lifecycle: on 30-min hard-cap or disconnect, attempt reconnect with replay of session config. Conversation history is NOT preserved server-side; document the lossiness.
>
> Tests `tests/test_openai_realtime.py`: 4 cases with mock WS server:
> 1. Happy connect → session.update → audio → audio out → close
> 2. Tool-call round trip (server emits function_call_arguments.done, client sends function_call_output + response.create)
> 3. 30-min cap simulation: server closes WS, client reconnects with same session
> 4. Error event surfaces
>
> CONSTRAINTS: same as R2-A. Use the SAME mock-WS fixture pattern as R2-A — extract it to `tests/conftest.py` if both subagents need it (R2-A and R2-B should coordinate on the fixture). Both share the audio resample utility from R1. ~250 + 150 LOC.

### R3 — Discord bridge (monkey-patch path)

> SPIKE FIRST (10 min): read `gateway/platforms/discord.py` from a Hermes install (or the upstream repo) to confirm the exact method signature and class name of `VoiceReceiver` / `DiscordAdapter.join_voice_channel`. Document any divergences from research/06-hermes-discord-voice-seam.md in your report.
>
> Then implement `hermes_s2s/_internal/discord_bridge.py` per ADR-0006:
> - `def install_discord_voice_bridge(ctx) -> None`: top-level entry. Detects strategy:
>   - Check for native `voice_pipeline_factory` hook on `ctx` (future-proof; today it doesn't exist).
>   - Else if `os.environ.get("HERMES_S2S_MONKEYPATCH_DISCORD") == "1"`: install monkey-patch.
>   - Else: log info "hermes-s2s realtime in Discord disabled — set HERMES_S2S_MONKEYPATCH_DISCORD=1 or wait for upstream PR".
> - Monkey-patch impl: import `gateway.platforms.discord`. If module not importable, log warning and bail. Wrap `DiscordAdapter.join_voice_channel`:
>   - After original method completes (returns the connected `VoiceClient`), check `s2s.mode` config. If `realtime`, swap the receiver/source with our bridge.
>   - Bridge audio: incoming Opus/PCM frames → `realtime_backend.send_audio_chunk()`. Backend events → `audio_chunk` → encode to PCM16 24kHz → push to a `discord.PCMVolumeTransformer` queue source.
> - Wire `install_discord_voice_bridge(ctx)` into `hermes_s2s/__init__.py::register(ctx)` (add a single line).
>
> Tests `tests/test_discord_bridge.py`: 3 cases:
> 1. No env var, no native hook: install logs info and is a no-op
> 2. Env var set, mocked `gateway.platforms.discord` module: install patches `DiscordAdapter.join_voice_channel`
> 3. Native `voice_pipeline_factory` exists on a mock ctx: install uses it instead of monkey-patch
>
> CONSTRAINTS: do NOT actually run discord.py in tests. Mock the entire `gateway.platforms.discord` module via `sys.modules` injection. Mark live integration as a manual smoke test in the report. ~200 LOC bridge + ~120 LOC tests.

## Concurrency

- 4 subagents in one batch (the orchestrator is hard-capped at 3 — split into 2 batches if needed: batch 1 = R1 + R2-A, batch 2 = R2-B + R3). R2-A and R2-B share the conftest mock WS fixture, so let R2-A land first or have both write to a shared fixture file under coordination.
- Decision: run as 4 parallel children (max_concurrent_children may need bump to 4) since the file ownership is fully disjoint and tests/conftest.py would need both R2-A and R2-B to write to it. Have R2-A own conftest.py; R2-B reads it.

## Phase 7 (concurrent review)

- **R1-rev**: cold review of audio/resample.py + tests. Check FFT round-trip math, lazy-import discipline.
- **R2-rev**: cold review of both realtime backends. Check WS message-shape correctness against research/05-realtime-ws-protocols.md, tool-call round-trip semantics (especially the OpenAI `response.create` requirement), session resumption.
- **R3-rev**: cold review of discord_bridge.py. Check monkey-patch safety (version-detection), no-op-on-missing-env behavior, fallback logic.

3 reviewers, different families per the model-roster skill.

## Phase 8 (cross-family final)

After 0.3.0 commits land. 3 reviewers, separate families:
1. `anthropic/claude-opus-4.7`
2. `google/gemini-3-pro-preview`
3. `moonshotai/kimi-k2-thinking`

Required intersection findings → must-fix before tag.

## Risks

- **Mock WS fixture quality**: if the mock doesn't match real WS framing, tests pass but production breaks. Mitigation: include 1 manual-smoke test stub gated on `GEMINI_API_KEY` / `OPENAI_API_KEY` env that does a real 5-second round trip when the keys are present.
- **Discord monkey-patch fragility**: Hermes refactors break us. Mitigation: pin to a Hermes minor-version range, error loudly on mismatch, document upstream PR status in error message.
- **Tool-call latency**: realtime APIs expect tool results within 5-15s. Long Hermes tools (web search, file ops) may exceed. Mitigation: caller-side timeout + filler-audio (deferred to 0.4.0).
- **Audio resampling cost**: `resample_poly` allocates intermediate buffers. For 20ms frames @ 48kHz, ~1ms per call. Acceptable.

## Out of scope for 0.3.0 (deferred to 0.4.0)

- Long-tool filler audio
- Voice-mode response shaping (system prompt overlay)
- Multi-party VC handling
- True barge-in for cascaded mode
- s2s-server full-pipeline WS client (still STUB)
