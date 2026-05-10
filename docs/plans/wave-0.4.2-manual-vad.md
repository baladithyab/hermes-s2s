# Wave 0.4.2 — Manual VAD (Discord-driven activity signals)

**Status**: Plan
**Author**: deep work loop, 2026-05-10
**Targets**: hermes-s2s v0.4.2

## Vision

After v0.4.1 the realtime bridge delivers audio in both directions, but **first response after VC join takes ~48 seconds**. Subsequent turns also have multi-second delays.

Root cause confirmed by bridge stats and Gemini Live docs: with `automaticActivityDetection.disabled=False` (current config), the Gemini server runs its own VAD on the audio stream. But **Discord client-side VAD chokes off RTP transmission entirely during silence** — Gemini sees the stream just stop, and only commits a turn after its internal grace timeout.

We fix this by **disabling Gemini's server-side VAD and driving turn boundaries from Discord packet flow**. Discord's client VAD is reliable: packets arrive when user talks, stop when they don't. We watch the buffer for ≥800ms silence per active speaker and emit explicit `activityEnd`. This is the canonical "manual VAD" pattern documented at https://ai.google.dev/gemini-api/docs/live-guide#disable-vad.

## Success Criteria

1. **Latency**: First user utterance after VC join → first audible response in **< 3 seconds** (was ~48s).
2. **Subsequent turns**: utterance end → response start in **< 1.5s** typical.
3. **Robustness**: silence-detection-only — no audio content analysis, no false-positives during pauses-mid-sentence.
4. **Non-regression**: cascaded mode unaffected; OpenAI realtime backend unaffected (its VAD already works fine because OpenAI uses turn-detection that respects silence packets).
5. **Tests**: 100% green (current 203/203). New tests cover silence-detector and activity-emit paths.

## Non-goals

- Adding push-to-talk UX. The signal source is Discord packet flow, automatic.
- Tuning Gemini's `silenceDurationMs` (orthogonal — that's for AAD-enabled mode which we're disabling).
- Changing OpenAI realtime backend behavior.
- Moving the VAD threshold to user-configurable (we'll start with hard-coded 800ms; can configurize in 0.4.3 if needed).

## Wire format reference (verified from ai.google.dev + js-genai)

```json
// Setup (sent once at connect)
{"setup": {"realtimeInputConfig": {"automaticActivityDetection": {"disabled": true}, "activityHandling": "START_OF_ACTIVITY_INTERRUPTS"}, ...}}

// Per-turn lifecycle
{"realtimeInput": {"activityStart": {}}}
{"realtimeInput": {"audio": {"data": "<b64>", "mimeType": "audio/pcm;rate=16000"}}}
{"realtimeInput": {"audio": {"data": "<b64>", "mimeType": "audio/pcm;rate=16000"}}}
... more audio chunks ...
{"realtimeInput": {"activityEnd": {}}}
```

`ActivityStart` and `ActivityEnd` are empty objects (Gemini API: "This type has no fields"). They MUST bracket every utterance — Gemini won't process audio outside an active activity window when AAD is disabled.

## Architecture changes

### 1. Backend Protocol (`providers/realtime/__init__.py`)

Add two new methods to `RealtimeBackend` Protocol and `_BaseRealtimeBackend`:

```python
async def send_activity_start(self) -> None: ...
async def send_activity_end(self) -> None: ...
```

Backwards compat note: existing `interrupt()` already does start+end as a manual barge-in; that path is **not** what we're using here — `interrupt()` brackets a no-op. The new methods are separate, single-direction signals.

### 2. Gemini backend (`providers/realtime/gemini_live.py`)

- Flip `automaticActivityDetection: {"disabled": True}` in `_build_setup`.
- **Keep** `activityHandling: "START_OF_ACTIVITY_INTERRUPTS"` (still meaningful for barge-in even with manual VAD).
- Add `send_activity_start()` and `send_activity_end()` methods (one-line WS sends).
- Refactor `interrupt()` to call them in sequence (eliminates duplication).

### 3. OpenAI realtime backend (`providers/realtime/openai_realtime.py`)

OpenAI Realtime uses `turn_detection` config (different shape). **Implement no-op stubs** for `send_activity_start/end` so the bridge can call uniformly. Document why: OpenAI's `server_vad` works fine over Discord because OpenAI's VAD doesn't time-out on stream pauses the way Gemini's does.

### 4. Audio bridge (`_internal/audio_bridge.py`) — the heart of the change

Add a per-user **silence detector** that fires `activity_end` after a configurable gap.

```python
# New state in RealtimeAudioBridge.__init__
self._activity_open: bool = False         # is there an unclosed activityStart?
self._last_frame_time: float = 0.0        # monotonic clock of last input frame
self._silence_gap_s: float = 0.8          # fire activity_end after this gap
self._silence_watchdog_task: Optional[asyncio.Task] = None
```

Modify `_pump_input` (line 523) — on every successful `pop_input`:
1. If `not self._activity_open`: call `await self.backend.send_activity_start()`, set `_activity_open=True`.
2. Update `_last_frame_time = monotonic()`.
3. Send audio chunk as before.

Add new `_silence_watchdog` coroutine — runs every 100ms:
1. If `_activity_open` AND `(monotonic() - _last_frame_time) >= _silence_gap_s`:
   - `await self.backend.send_activity_end()`
   - `_activity_open = False`
   - Log structured event.

Add task to `_bridge_loop` (line 456): spawn watchdog alongside in/out pumps + stats.

### 5. Stats heartbeat additions

Add to `stats()` dict:
- `activity_open` (bool)
- `activity_starts_sent` (counter)
- `activity_ends_sent` (counter)
- `time_since_last_frame_s` (float)

This lets us debug live what the watchdog is doing.

### 6. Edge cases to handle

- **Bridge close while activity open**: in `stop()`, if `_activity_open`, emit `activity_end` before closing backend so server flushes (best-effort, swallow errors).
- **Backend reconnect (Gemini session resumption)**: after reconnect, reset `_activity_open=False` so the next frame opens fresh.
- **Multiple users speaking** (rare in our use — single-allowed-user mode): treat all input as a single stream — Discord's bot already mixes them at the buffer layer. No per-user activity tracking needed.
- **Gemini streaming response while user is mid-utterance** (barge-in): `START_OF_ACTIVITY_INTERRUPTS` handles it server-side; we just keep streaming user audio; activity_end fires when they actually stop.

## Test plan

`tests/test_audio_bridge_vad.py` (new file, ~6 tests):

1. `test_first_frame_emits_activity_start` — push one frame, assert `send_activity_start` called once.
2. `test_subsequent_frames_no_extra_starts` — push 10 frames close together, assert `send_activity_start` called once total.
3. `test_silence_gap_emits_activity_end` — push frame, advance fake clock 1s, assert `send_activity_end` called.
4. `test_resume_after_silence` — frame, gap, frame again → start/end/start sequence.
5. `test_close_while_active_emits_end` — frame open, close bridge, assert end was sent.
6. `test_stats_reflect_activity_state` — stats dict has expected counters.

Use `monotonic` injection (or freezegun pattern) so tests don't sleep 800ms.

`tests/test_gemini_live_setup.py`:

7. `test_setup_disables_aad` — verify `_build_setup` returns `disabled: True`.
8. `test_send_activity_start_sends_correct_frame` — mock WS, call method, assert exact JSON.
9. `test_send_activity_end_sends_correct_frame` — same.

`tests/test_openai_realtime_activity_noop.py`:

10. `test_openai_activity_methods_are_noops` — both methods exist, are awaitable, do nothing.

## Risks

- **R1: Gemini doesn't accept manual VAD on `gemini-3.1-flash-live-preview`**. Mitigation: live-test immediately after first commit; fallback to AAD-enabled with aggressive `silenceDurationMs: 200` (Option B from earlier diagnosis).
- **R2: Watchdog races with Gemini reconnect**. Mitigation: hold `_activity_open` reset under the same lock as backend swap (the existing `_lock` in `RealtimeAudioBridge` should suffice; verify).
- **R3: Discord buffers >800ms of user speech as single push** (unlikely — receiver pushes per ~20ms RTP packet). Mitigation: check `_last_frame_time` updates per pop, not per push; we update inside `_pump_input` after `pop_input` so pumping latency doesn't lie.

## Implementation order

1. Backend protocol + `_BaseRealtimeBackend` stubs (5 min).
2. Gemini `send_activity_start`/`send_activity_end` + AAD flip (10 min).
3. OpenAI no-op stubs (5 min).
4. Bridge silence detector + pump integration + watchdog task (30 min).
5. Stats additions (5 min).
6. Tests (45 min).
7. Local pytest green.
8. Bump to `0.4.2`, commit, tag, push.
9. Reinstall in Hermes venv, restart gateway, live VC test.

Estimated total: **~90 minutes** to v0.4.2 in production.
