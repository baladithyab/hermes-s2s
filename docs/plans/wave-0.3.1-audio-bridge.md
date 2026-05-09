# Wave plan: 0.3.1 — Audio bridge actually moves audio

> ADRs: 0007 (audio bridge frame-callback), 0008 (tool-call bridging).
> Research: 07-discord-audio-contract.md, 08-tool-call-bridging-patterns.md, 09-frame-timing-cadence.md.
> Effort: ~1.5 days across 4 parallel subagents.
> Acceptance: with `HERMES_S2S_MONKEYPATCH_DISCORD=1` + `s2s.mode: realtime`, joining a Discord VC bridges audio end-to-end. Smoke harness validates without a live Hermes.

## Goal

Promote the Discord bridge from "logs a warning" to "actually routes audio." Plus tool-call bridging, plus a smoke harness so we can validate without the full Hermes+Discord stack running.

## Acceptance criteria

1. `BridgeBuffer` class in `_internal/audio_bridge.py` with sync `read()` for the AudioSource thread + async `push_output()` from the asyncio bridge task. Drift-compensated, drops oldest on overflow, returns silence on underflow.
2. `RealtimeAudioBridge` class wires: incoming `(user_id, pcm)` callback → input queue → resample → backend.send_audio_chunk; backend.recv_events → audio_chunk → resample → output queue (sliced to 20ms frames).
3. `QueuedPCMSource(discord.AudioSource)` in `_internal/discord_audio.py` returns 3840-byte frames from the buffer.
4. Tool-call dispatcher in `_internal/tool_bridge.py` per ADR-0008 (5s soft / 30s hard timeout, filler audio, parallel by call_id, error injection, 4KB truncation, cancel-on-interrupt).
5. `discord_bridge.py` updated: stub log REPLACED with real `RealtimeAudioBridge` instantiation. Bridge backend is resolved from `s2s.realtime.provider`. Lifecycle hook: bridge starts on `/voice join`, closes on disconnect.
6. `scripts/smoke_realtime.py` runs end-to-end against a real Gemini Live or OpenAI Realtime backend (gated on env keys); pre-recorded WAV in → audio out.
7. `scripts/smoke_discord.md` (manual test plan): bot setup, env keys, expected behavior, known issues.
8. Tests: `tests/test_audio_bridge.py` (8+ tests with mocked backend + mocked Discord audio), `tests/test_tool_bridge.py` (6+ tests for timeout/filler/cancel paths). Existing 41 still pass.
9. README + HOWTO updates.
10. `__version__ = "0.3.1"` in pyproject.toml + `__init__.py`.

## File ownership

| Subagent | Owns | Touches | Reads |
|---|---|---|---|
| **B1: Audio bridge core** | `hermes_s2s/_internal/audio_bridge.py` (new), `hermes_s2s/_internal/discord_audio.py` (new), `tests/test_audio_bridge.py` (new) | none | ADR-0007, research/07, research/09, existing `audio/resample.py` |
| **B2: Tool bridge** | `hermes_s2s/_internal/tool_bridge.py` (new), `tests/test_tool_bridge.py` (new) | `hermes_s2s/providers/realtime/__init__.py` (add `send_filler_audio` to Protocol) | ADR-0008, research/08 |
| **B3: discord_bridge integration** | `hermes_s2s/_internal/discord_bridge.py` (extend `_attach_realtime_to_voice_client`), `tests/test_discord_bridge.py` (extend) | none | existing discord_bridge.py, ADR-0007 |
| **B4: Smoke harness + docs** | `scripts/smoke_realtime.py` (new), `scripts/smoke_discord.md` (new), `docs/HOWTO-REALTIME-DISCORD.md` (new), `README.md` (Status section), `pyproject.toml` (version bump), `hermes_s2s/__init__.py` (version bump) | none | all ADRs |

B1 and B2 are independent. B3 depends on both B1 and B2 — but the Protocol for `RealtimeAudioBridge` is small enough that B3 can develop against a stub class, then swap when B1 lands. B4 is independent of all three.

## Concurrency rules

- 4 subagents in parallel batch (test if `delegation.max_concurrent_children` accepts 4; if not, B4 last).
- File ownership is hard. No subagent edits a file outside its column.
- B2 owns the Protocol addition (`send_filler_audio`). B1 and B3 may *read* the Protocol but not edit.

## Subagent prompts (sketches)

### B1 — Audio bridge core

> Implement `RealtimeAudioBridge` + `BridgeBuffer` + `QueuedPCMSource` per ADR-0007 (`docs/adrs/0007-audio-bridge-frame-callback.md`) and research/09 (`docs/design-history/research/09-frame-timing-cadence.md`).
>
> Three new files:
>
> 1. `hermes_s2s/_internal/audio_bridge.py`:
>    - `class BridgeBuffer`: `__init__(input_max=50, output_max=50)`. `push_input(user_id, pcm_bytes)` from receive callback (called from a thread; thread-safe). Async `pop_input() -> tuple[int, bytes]`. `push_output(pcm_bytes)` slices fractional remainder per research/09 (hold remainder bytearray, no zero-padding). Sync `read_frame() -> bytes` for AudioSource thread; returns 3840-byte silence when empty.
>    - `class RealtimeAudioBridge`: owns the BridgeBuffer + a backend instance + an asyncio task. `start()` opens backend connection, spawns the bridge loop. `on_user_frame(user_id, pcm)` is the receive callback (sync; thread-safe push_input). `bridge_loop()` reads input queue, resamples to backend rate, sends; reads backend recv_events, resamples to 48k stereo, push_output. `close()` cancels everything idempotently.
>    - Resampling via `hermes_s2s.audio.resample.resample_pcm`. Lazy-import scipy as the resample module already does.
>
> 2. `hermes_s2s/_internal/discord_audio.py`:
>    - `class QueuedPCMSource(discord.AudioSource)`: subclasses discord.AudioSource. `read() -> bytes` calls `self.buffer.read_frame()`. `is_opus() -> False`. `cleanup()` is no-op. Lazy-import discord at instantiation, not at module load.
>
> 3. `tests/test_audio_bridge.py`: 8 tests using mock backend (asyncio Mock) and mock buffer:
>    a. BridgeBuffer push_input + pop_input round-trip
>    b. BridgeBuffer drops oldest on input overflow (counter increments)
>    c. BridgeBuffer push_output slices 8000-byte chunk into 20ms frames + holds remainder
>    d. BridgeBuffer read_frame returns silence on underflow
>    e. RealtimeAudioBridge start + close lifecycle (no leaked tasks)
>    f. Bridge resamples 48k stereo → 16k mono on input (mocked resample, verify it was called with right rates)
>    g. Bridge slices 250ms backend chunk into 12.5 frames + holds half-frame
>    h. on_user_frame is thread-safe (call from threading.Thread, verify no exception)
>
> CONSTRAINTS: ~250 LOC bridge + ~80 LOC discord_audio + ~200 LOC tests. Lazy-import discord and scipy. Use `queue.Queue` (sync) for the AudioSource ↔ asyncio boundary, NOT `asyncio.Queue`. Use `asyncio.Queue` for everything within the asyncio side.
>
> Verify: cd /mnt/e/CS/github/hermes-s2s && python3 -m pytest tests/ -q. Expect 49+ tests pass (41 existing + 8 new).
>
> REPORT: 5-line summary.

### B2 — Tool bridge

> Implement `HermesToolBridge` per ADR-0008 (`docs/adrs/0008-tool-call-bridging.md`).
>
> 1. `hermes_s2s/_internal/tool_bridge.py`:
>    - `class HermesToolBridge`: `__init__(dispatch_tool, soft_timeout=5.0, hard_timeout=30.0, result_max_bytes=4096)`. The `dispatch_tool` is a callable `(name, args) -> str | dict` provided by the bridge owner (typically `ctx.dispatch_tool` from Hermes plugin context).
>    - `async handle_tool_call(backend, call_id, name, args) -> str`: runs the dispatch in a task, races against soft+hard timeouts, fires backend.send_filler_audio() on soft timeout, returns the result string to inject back.
>    - `async cancel_all()`: cancels all in-flight tool tasks. Idempotent.
>    - `_truncate(s)`: caps at result_max_bytes UTF-8 with `"\n\n[result truncated; original length: N bytes]"` suffix.
>    - `_serialize(result)`: dict→JSON, str passthrough, other→str().
>    - Error injection format per ADR-0008: `{"error": "...", "retryable": bool}`.
>
> 2. `hermes_s2s/providers/realtime/__init__.py`: add `async def send_filler_audio(self, text: str) -> None` to the `RealtimeBackend` Protocol. Update `_BaseRealtimeBackend.send_filler_audio` to raise `NotImplementedError("base stub")`. Each concrete backend (gemini_live.py, openai_realtime.py) gets a stub method that raises NotImplementedError too — full impl is in 0.3.2 polish since it requires backend-specific filler-trigger logic. The bridge will catch NotImplementedError gracefully.
>
> 3. `tests/test_tool_bridge.py`: 6 tests with mocked dispatch + mocked backend:
>    a. Happy path: tool finishes in 0.1s, no filler, result returned
>    b. Soft timeout: tool takes 5.5s, filler triggered, result returned
>    c. Hard timeout: tool takes 31s, cancelled, error result returned
>    d. Tool raises exception: error result returned
>    e. Result truncation: tool returns 5KB string, truncated to 4KB + suffix
>    f. cancel_all: in-flight task cancelled cleanly
>
> CONSTRAINTS: ~150 LOC bridge + ~150 LOC tests. asyncio.shield for soft-timeout pattern. Don't actually sleep 30s in tests — use small soft/hard values like 0.05/0.2 and `await asyncio.sleep(0.1)` for "slow" tools.
>
> Verify: cd /mnt/e/CS/github/hermes-s2s && python3 -m pytest tests/ -q.
>
> REPORT: 5-line summary.

### B3 — Wire bridges into discord_bridge.py

> Replace the stub log + premature receiver.pause() in `hermes_s2s/_internal/discord_bridge.py::_attach_realtime_to_voice_client` with a real `RealtimeAudioBridge` instantiation per ADR-0007.
>
> The function currently logs a warning and stops. Replace with:
>
> ```python
> from hermes_s2s.config import load_config
> from hermes_s2s.registry import resolve_realtime
> from hermes_s2s._internal.audio_bridge import RealtimeAudioBridge
> from hermes_s2s._internal.discord_audio import QueuedPCMSource
> from hermes_s2s._internal.tool_bridge import HermesToolBridge
>
> def _attach_realtime_to_voice_client(adapter, voice_client, voice_receiver, ctx):
>     cfg = load_config()
>     if cfg.mode != "realtime":
>         return  # cascaded mode — leave Hermes built-in voice alone
>
>     # Resolve backend from config
>     backend = resolve_realtime(cfg.realtime_provider, cfg.realtime_options)
>
>     # Tool bridge wired to Hermes dispatch
>     tool_bridge = HermesToolBridge(dispatch_tool=ctx.dispatch_tool)
>
>     # Audio bridge owns the backend + buffer
>     bridge = RealtimeAudioBridge(backend=backend, tool_bridge=tool_bridge)
>
>     # Hook into VoiceReceiver: install our frame callback
>     # Mechanism: monkey-patch _decoder.decode callsite at discord.py:376
>     # (or use existing voice_receiver.set_frame_callback if Hermes already exposes it).
>     # See ADR-0007.
>     _install_frame_callback(voice_receiver, bridge.on_user_frame)
>
>     # Outgoing: replace whatever Hermes plays with our QueuedPCMSource
>     source = QueuedPCMSource(bridge.buffer)
>     if voice_client.is_playing():
>         voice_client.stop()
>     voice_client.play(source)
>
>     # Start bridge loop
>     asyncio.run_coroutine_threadsafe(bridge.start(), voice_client.loop)
>
>     # Track for cleanup
>     adapter._s2s_bridges = getattr(adapter, "_s2s_bridges", {})
>     adapter._s2s_bridges[voice_client.guild.id] = bridge
> ```
>
> SPIKE FIRST: read /home/codeseys/.hermes/hermes-agent/gateway/platforms/discord.py around line 376 (the Opus decode site per research/07) and confirm whether Hermes's VoiceReceiver has a `set_frame_callback` method or whether we need to install one via monkey-patch. Document the actual injection point.
>
> Implement `_install_frame_callback(voice_receiver, callback)`. If `set_frame_callback` exists, use it. Else: monkey-patch the receiver's decoded-frame handler.
>
> Also wire the cleanup path on `/voice leave` — wrap `DiscordAdapter.leave_voice_channel` to call `bridge.close()` for the matching guild.
>
> Extend `tests/test_discord_bridge.py` with 3 more tests:
> a. Bridge instantiation when monkey-patch + s2s.mode == realtime (mock everything)
> b. Bridge close on leave_voice_channel
> c. Bridge NOT instantiated when s2s.mode == cascaded (don't disturb built-in voice)
>
> CONSTRAINTS: don't actually run discord.py in tests. Mock `gateway.platforms.discord` via sys.modules. ~120 LOC additions. Do not break existing 4 discord_bridge tests.
>
> Verify: cd /mnt/e/CS/github/hermes-s2s && python3 -m pytest tests/ -q.
>
> REPORT: 5-line summary including spike findings about VoiceReceiver's frame-callback availability.

### B4 — Smoke harness + docs + version bump

> Three deliverables:
>
> 1. `scripts/smoke_realtime.py`: standalone Python script (NOT a test). Argparse: `--provider {gemini-live|gpt-realtime}`, `--input WAV` (default a synthesized 1s tone if omitted), `--output WAV` (records the response). Reads env (GEMINI_API_KEY or OPENAI_API_KEY), instantiates the backend, sends audio, collects 5s of response, writes to output, prints timing summary. Exit 0 on success, 1 on any error. ~150 LOC.
>
> 2. `scripts/smoke_discord.md`: a manual test plan markdown doc. Sections:
>    - Prerequisites (Discord bot token, allowed user, voice channel)
>    - Setup (env vars, config.yaml block)
>    - Smoke steps (1-7 step-by-step checklist)
>    - Expected output (what the user sees in the bot's console + Discord)
>    - Known issues / troubleshooting
>    - "How to report bugs" section pointing at the GitHub issues
>
> 3. `docs/HOWTO-REALTIME-DISCORD.md`: end-to-end setup doc, ~120 lines. Covers: install, env keys, monkey-patch flag, config recipes for gemini-live and gpt-realtime, latency expectations, known limitations (single-user only in 0.3.1), troubleshooting.
>
> 4. `README.md`: update Status section to reflect 0.3.1 = audio actually flows.
>
> 5. `pyproject.toml`: bump `version = "0.3.0"` → `"0.3.1"`. `hermes_s2s/__init__.py`: bump `__version__`. `tests/test_smoke.py`: update the version assertion test.
>
> CONSTRAINTS: smoke scripts use only the public API of hermes-s2s (resolve_realtime, audio.resample). Don't import internals. Don't promise live audio in the docs unless 0.3.1 actually delivers it.
>
> REPORT: 5-line summary.

## Phase 7 + 8

Combined into one cross-family review since the changes are concentrated:

- **Reviewer 1** (Anthropic): protocol correctness, resource leaks (asyncio task cleanup, queue.Queue thread safety)
- **Reviewer 2** (Gemini): edge cases (backpressure, buffer underflow under jitter, concurrent tool calls)
- **Reviewer 3** (Kimi): packaging, error UX, version coherence, code smells

Same go/no-go gate before tagging v0.3.1.

## Risks

- **Hermes VoiceReceiver doesn't expose set_frame_callback**: B3 spike confirms; if absent we install via monkey-patch on the receiver class.
- **AudioSource thread synchronization**: queue.Queue is the standard answer but easy to misuse. B1 tests must verify thread-safety explicitly.
- **Smoke harness can't be fully automated**: Discord side requires manual validation. Document loudly that the smoke script proves backend connectivity, not Discord integration.
- **Filler-audio API unimplemented**: B2's `send_filler_audio` raises NotImplementedError. Tool bridge catches it gracefully — no filler is fired, just direct injection. Acceptable for 0.3.1; full impl in 0.3.2.
