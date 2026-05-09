# Wave 0.3.1 review — Gemini (Google) perspective

Commit: `1f10c55 feat(0.3.1): audio bridge actually moves audio + tool-call bridging`
Reviewer focus: edge cases, smoke-harness fidelity, coverage gaps.
Verdict: **GO, with two P1 follow-ups for 0.3.2** (details below). Nothing here blocks v0.3.1 ship.

---

## CONFIRMED — works as claimed

1. **BridgeBuffer fractional-remainder is correct.** `push_output` (audio_bridge.py:140-160)
   extends a single bytearray, slices off whole `FRAME_BYTES` chunks, and leaves
   the tail in `_output_remainder`. A 100-byte chunk just sits in the remainder
   until enough subsequent bytes arrive — no silent drop, no click injection.
   `test_push_output_slices_with_remainder_hold` exercises the 2.5+0.5=3 frame
   case explicitly. **No "remainder stuck forever" bug** at steady state: any
   further audio from the backend completes the frame. See ISSUES §1 for the
   one exotic case (backend falls silent mid-frame).

2. **Underflow returns silence, not EOF.** `read_frame()` (line 162-172) always
   returns `SILENCE_FRAME` (3840 zero bytes) on an empty queue and increments
   `_underflows`. This matches research/07's guidance that returning `b""` would
   terminate discord.py playback permanently.

3. **Input queue drop-oldest is correct-by-contract.** `push_input`
   (line 92-111) drops the oldest slot then inserts the fresh chunk under
   `queue.Full`, which is the right policy for live conversation (freshest
   audio wins).

4. **Tool-call soft/hard timeout races are implemented correctly.**
   `_run_with_timeouts` (tool_bridge.py:90-128) uses `asyncio.shield` around the
   tool task for the soft wait (so soft-timeout cancellation doesn't kill the
   tool), then a plain `wait_for` for the remainder to the hard deadline, then
   a real `.cancel()` on hard timeout. `NotImplementedError` on
   `send_filler_audio` is swallowed so stub backends don't blow up the flow.
   The four relevant tests (`test_happy_path`, `test_soft_timeout`,
   `test_hard_timeout`, `test_tool_exception`) all assert the right envelope
   shapes. ADR-0008 spec is respected for the single-call path.

5. **Lifecycle hygiene.** `RealtimeAudioBridge.close()` (line 281-313) cancels
   the supervisor and children, awaits them, closes the backend, and is
   idempotent. `test_bridge_start_close_leaves_no_tasks` asserts zero leaked
   tasks. `pop_input` deliberately avoids `run_in_executor(queue.get)` to keep
   the shutdown path promptly cancellable (documented at line 116-124) — this
   is exactly the right call; the Anthropic review may glance past it.

6. **Smoke harness uses ONLY the public API.** `scripts/smoke_realtime.py`
   imports `hermes_s2s.audio.resample.resample_pcm`, `.providers.register_builtin_realtime_providers`,
   and `.registry.resolve_realtime`. **Zero imports from `hermes_s2s._internal`.**
   It wires `resolve_realtime → connect → send_audio_chunk → recv_events`
   directly, which is the same surface the production bridge calls. What it
   does NOT exercise: BridgeBuffer, RealtimeAudioBridge, QueuedPCMSource,
   discord_bridge, HermesToolBridge. It is a **backend smoke test**, not an
   end-to-end bridge smoke test — see ISSUES §3.

7. **Backend-rate resolution.** `_resolve_backend_rates` correctly prefers
   explicit attrs, falls back to the name map, then module defaults. The
   `_backend_type_name` helper normalizes `gemini_live` ↔ `gemini-live`.

---

## ISSUES — worth fixing (none block 0.3.1)

### P1-G1 — Drop-oldest backpressure is silent to operators
`_dropped_input` is a counter, but `push_input` (audio_bridge.py:92-111)
**emits no log line, no metric, and nothing polls the counter**. Under sustained
load the audio silently degrades and the user has no signal. The property
`dropped_input` is exposed on BridgeBuffer but nobody reads it.

**Recommendation (0.3.2):**
- Log a `warning` once every N drops (e.g. every 25 in a 1s window) from
  `push_input` itself. Cheap: one modulo check, no extra thread.
- Same for `underflows` on the output side (currently also silent).
- Consider exposing both via a `bridge.stats()` dict the Hermes status page
  could read.

### P1-G2 — `_on_packet` shim diff is racy if VoiceReceiver mutates buffers off-thread
`_install_frame_callback._wrapped_on_packet` (discord_bridge.py:437-470)
snapshots `len(buffers[ssrc])` **before** calling the original `_on_packet`,
then diffs the new length afterward and slices `buf[prev:]`.

Failure modes the Anthropic reviewer may miss:
1. **`check_silence()` trims the buffer between snapshot and diff.** Hermes's
   own silence detector (discord.py:414 per the spike notes) runs on a
   separate polling path. If it clears `_buffers[ssrc]` after `_orig(data)`
   but before we read `len(buf)` back, our diff goes negative — the guard at
   line 459 (`if len(buf) <= prev: continue`) suppresses the callback and
   **the frame that was just decoded is lost silently**.
2. **Late-arriving Opus packets for an SSRC we saw once, then stopped
   tracking.** After a user stops talking, the next packet can arrive up to
   a minute later. `_prev_lens` (line 435) keeps growing unbounded across
   SSRCs until the receiver instance dies. Minor memory leak; not a
   correctness issue.
3. **Two SSRCs colliding on a dict iteration.** We `list(buffers.items())`,
   which is safe, but if `_buffers` is a `defaultdict` being written to
   from the decode path, we can briefly observe a new key with `len==0`
   and emit a zero-byte frame via `cb(user_id, b"")`. The callback chain
   tolerates this (BridgeBuffer.push_input on 0 bytes is a no-op at the
   queue), so not a crash, but worth a `if not pcm_frame: continue` guard.

**Recommendation:**
- Add a lock **inside the VoiceReceiver** that we acquire around
  `orig + diff`. Since we're monkey-patching, we can stash our own
  `threading.Lock()` on `_rcv._hermes_s2s_lock` and hold it for both the
  snapshot and the diff. This makes the "check_silence races us" scenario
  impossible.
- Guard against `pcm_frame == b""` before invoking cb.
- Prune `_prev_lens` of SSRCs no longer in `buffers`.

This is "real but rare" in my judgment. Not a 0.3.1 blocker — the monkey-patch
path is behind an opt-in env flag (`HERMES_S2S_MONKEYPATCH_DISCORD=1`) and
the window is microseconds. Fix in 0.3.2.

### P2-G3 — Tool-call "parallel by call_id" is per-call-only, NOT concurrent
ADR-0008 states tools run "parallel by call_id". The impl is more subtle:
`handle_tool_call` (tool_bridge.py:52-72) is per-invocation; it wraps one
`_run_with_timeouts` in one task and tracks it in `_inflight[call_id]`. **There
is no `asyncio.gather` over multiple tool calls anywhere.**

Whether this is "parallel" depends on the caller. In the current code,
`_dispatch_event` in audio_bridge.py:437 **awaits** `tool_bridge.handle_tool_call`
inline in the output-pump loop. That means:

- If the backend emits two `tool_call` events back-to-back in `recv_events()`,
  **the second one blocks on the first** (serialized by the pump's `async for`).
- Only *within* a single call is there concurrency (the tool task runs while
  the soft/hard timers tick).

Gemini Live in particular can emit multiple parallel function-call parts in
one `toolCall` message; OpenAI Realtime less so. So for Gemini this is
probably an issue; for OpenAI it's rare.

**Recommendation (0.3.2):** in `_dispatch_event`, for `etype == "tool_call"`,
use `asyncio.create_task(tool_bridge.handle_tool_call(...))` and store it on
the bridge for later cancellation — don't `await` it inline. This is a small
change (~5 lines) and makes ADR-0008's claim actually true.

### P2-G4 — Smoke harness only tests the backend, not the bridge
`scripts/smoke_realtime.py` is great for validating "does Gemini/OpenAI accept
our audio and send some back" — that's what it claims to do, and it does it.
But the README Status and the commit message hint at "audio actually flows",
which a user might read as "the whole pipeline works". The smoke harness does
NOT exercise:
- BridgeBuffer push/pop under realistic timing
- 48k↔backend-rate resampling for 20ms discord frames
- QueuedPCMSource pulling frames while push_output runs
- The monkey-patched `_on_packet` shim
- Tool-call soft/hard timeout with a real backend

Companion plan `scripts/smoke_discord.md` is a manual checklist, which is
fine, but there's no automated integration test in `tests/`.

**Recommendation (0.3.2):** add a `test_integration_bridge.py` that:
  1. Builds a `FakeBackend` with a scripted async-iter of audio_chunk events
     at realistic 240B/40ms cadence.
  2. Instantiates `RealtimeAudioBridge`, starts it, feeds 100 Discord-sized
     input frames via `on_user_frame`, reads back 50 frames from
     `buffer.read_frame()`, asserts timing budget and no underflows.
  3. Same but inject a `tool_call` event mid-stream and assert filler audio +
     `inject_tool_result` are both called.

Today this tier of coverage doesn't exist; unit tests are solid but the
full-wiring case is verified only manually.

---

## QUESTIONS — not blockers, but worth an answer

1. **`recv_events` stall handling.** If the backend `async for event in events`
   loop hangs (e.g. provider keeps the WebSocket open with no frames),
   `_pump_output` sits there forever. `close()` cancels it, so shutdown is
   fine. But during a live call, there's no keepalive / watchdog —
   is that intentional? Gemini Live does send periodic `go_away` / session
   events; OpenAI sends heartbeats. If the provider genuinely silences the
   stream, the user hears 3840-byte silence frames forever and nobody knows.
   Worth a 30s no-event watchdog in 0.3.2.

2. **Late-arriving Opus after `close()`.** If the bridge is closing while the
   Discord player thread is mid-`read_frame()`, we're fine (lock-guarded).
   But if a user sends audio into `bridge.on_user_frame` after `close()` has
   been called, `push_input` happily queues it into a buffer nobody drains.
   Low-stakes memory leak bounded by `INPUT_QUEUE_MAX=50` (~200KB worst case).
   Worth a `self._closed` check in `on_user_frame` to drop post-close frames.

3. **`_BACKEND_RATE_DEFAULTS` — Gemini Live output rate.** The map has
   `gemini-live: (16000, 24000)`. Gemini Live docs say **output is 24 kHz**,
   so this is right. But the README mentions "gemini 2.5 flash native audio"
   which advertises variable output rates; is 24k really always true? A
   log line at bridge start showing the resolved rates would help users
   diagnose pitch/speed issues in the field.

4. **QueuedPCMSource cache is process-global.** `_cached_cls` in
   discord_audio.py:18 caches the built class. If two different discord
   versions are somehow loaded (unlikely in practice, but possible in a
   reload scenario), the cache survives. Minor concern.

---

## Test-coverage gap summary

| Area | Unit test | Integration test |
|---|---|---|
| BridgeBuffer slicing/remainder | ✓ | — |
| Drop-oldest backpressure | ✓ | — |
| Bridge start/close lifecycle | ✓ | — |
| Input resample arg shape | ✓ | — |
| 250ms output chunk → 12 frames | ✓ (mocked resample) | — |
| on_user_frame thread safety | ✓ | — |
| Tool bridge happy/soft/hard/exc/trunc/cancel | ✓ (6 tests) | — |
| discord_bridge strategy selection | ✓ | — |
| `_attach_realtime_to_voice_client` wiring | ✓ (with stubs) | — |
| `_install_frame_callback` shim | — | — |
| Full BridgeBuffer + RealtimeAudioBridge + FakeBackend flow | — | — |
| Tool call concurrency (2+ in-flight) | — | — |
| Recv-events stall / keepalive | — | — |
| Smoke with real Gemini/OpenAI | — | manual only (script) |

**The most valuable missing test is the full-flow integration test.**
Second most valuable is a unit test for `_install_frame_callback` — it's
the bridge's load-bearing shim and has zero direct coverage (the
discord_bridge tests mock it out).

---

## Verdict: GO for v0.3.1

The bridge is correct for the happy path. The fractional-remainder, underflow,
and lifecycle semantics match research/09 and research/07. Tool-call timeout
machinery is solid. The smoke harness does what it claims (backend validation
via public API). Known limitations are documented.

The issues I found are real but either (a) gated behind an opt-in env flag
(monkey-patch races), (b) observability gaps rather than correctness bugs
(silent drops), or (c) gaps in what's tested automatically vs manually. Ship
0.3.1; file four tickets for 0.3.2 covering G1–G4 above.
