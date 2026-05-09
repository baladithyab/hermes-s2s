# Wave 0.3.1 Review — Anthropic (Claude) Cross-Family

Commit: `1f10c55` — audio bridge + tool bridge + discord frame shim.
Scope: async-cleanup correctness, monkey-patch `_buffers` diff safety, `queue.Queue ↔ asyncio` boundary.
Verdict line at bottom.

---

## CONFIRMED (looks correct)

- `BridgeBuffer.push_input` (`audio_bridge.py:92-111`) is genuinely thread-safe: `queue.Queue.put_nowait`/`get_nowait` are atomic under the GIL, drop-oldest race at L101-L111 is benign (duplicate drop increments `_dropped_input` rather than losing data).
- `pop_input` (`audio_bridge.py:113-129`) correctly avoids `run_in_executor(queue.get)` and the author explains *why* (executor thread would wedge on blocking `get`, no cancel path). Comment at L117-124 is the right call. 5 ms poll is cheap.
- `push_output`/`read_frame` (`audio_bridge.py:140-172`) guard the bytearray + frame list with `threading.Lock`. Fractional-remainder retention matches research/09. `read_frame` never returns `b""` (returns `SILENCE_FRAME`), matching the Discord contract.
- `QueuedPCMSource` factory via `__new__` + lazy discord import (`discord_audio.py:58-68`) — clean, lets module load in CI w/o discord.py.
- `HermesToolBridge.cancel_all` (`tool_bridge.py:74-79`) **is** idempotent and race-free *on a single event loop*: both `handle_tool_call` and `cancel_all` are coroutines on the same loop; `_inflight` dict mutation cannot interleave between awaits in a way that strands a task.
- `tool_bridge.py:97-98`: `asyncio.shield(tool_task)` wrapped in `wait_for` is the correct soft-timeout pattern — shield swallows the outer cancel so `tool_task` survives soft-timeout; hard-timeout path at L108-L114 cancels+awaits properly.
- `_wrap_leave_voice_channel` (`discord_bridge.py:483-532`) wraps once via `_S2S_LEAVE_WRAPPED_MARKER`, pops bridge from dict before awaiting `close()` — can't double-close from a second leave call.
- `_install_frame_callback` idempotence via `_FRAME_CB_INSTALLED_MARKER` (`discord_bridge.py:409-414`): re-install swaps the callback slot instead of stacking wrappers. Correct — prevents O(n) `_on_packet` chains on rejoin.

---

## ISSUES

### P0
None.

### P1

1. **First-frame-per-new-SSRC is silently dropped** — `discord_bridge.py:437-464`.
   - On a packet for a brand-new SSRC (user starts speaking for the first time), the pre-snapshot `before` dict (L441-447) is taken *before* `_orig(data)` runs, so if this SSRC key didn't exist in `_buffers` yet it won't be in `before`. After `_orig`, the buffer now exists and has `N` bytes. The fallback chain at L457 is `before.get(ssrc, _prev_lens.get(ssrc, len(buf)))` → resolves to `len(buf)` → `len(buf) <= prev` (L459) → `continue`. **First frame of every new speaker is dropped.**
   - Fix: fallback should be `0`, not `len(buf)`, when the SSRC isn't in either dict. This is the common case at first-utterance.

2. **Hermes-loop vs VC-loop cross-loop `close()`** — `discord_bridge.py:357` + `audio_bridge.py:281-314`.
   - `bridge.start()` is scheduled via `asyncio.run_coroutine_threadsafe` onto `voice_client.loop`. The bridge's `_task` and its children live on that loop.
   - `leave_voice_channel_wrapped` (`discord_bridge.py:498-526`) awaits `bridge.close()` from whichever loop invoked `leave_voice_channel`. If that's the same loop as `voice_client.loop` (typical discord.py setup), fine. If Hermes ever runs its own loop separate from the VC loop, `self._task.cancel()` (L290) operates on a task owned by a different loop — undefined behaviour / RuntimeError.
   - Fix: in `close()`, detect `self._task.get_loop() is not running_loop`, and if so use `asyncio.run_coroutine_threadsafe(self._async_close_inner(), self._task.get_loop()).result()` or similar. At minimum document the single-loop assumption.

3. **`send_filler_audio` exceptions orphan the tool task** — `tool_bridge.py:102-105`, `L127-L128`.
   - Only `NotImplementedError` is caught around `backend.send_filler_audio`. Any other exception (network error, backend disconnected) escapes to the outer `except Exception:` at L127 which returns a JSON error envelope **but `tool_task` keeps running** — no cancel at L127, only at the explicit `CancelledError` branch L124.
   - Fix: broaden the inner catch to `except Exception` (log at debug) *or* cancel `tool_task` in the outer exception branch before returning the envelope.

### P2

4. **Dead `if/else` branch in `_pump_input`** — `audio_bridge.py:355-371`. Both branches call `resample_pcm(...)` with identical arguments. Looks like an incomplete short-circuit (same-rate path was intended to skip resample and just do channel downmix). Either remove the dead branch or implement the fast-path. Low risk, just noise/confusion.

5. **`_bridge_loop` children-assignment race** — `audio_bridge.py:323-325`.
   - Tasks are created at L323-L324, assigned to `self._children` at L325. If `close()` fires between L324 and L325 (e.g., concurrent `run_coroutine_threadsafe(start)` + immediate cancel), L296-L303 iterates an empty `_children`. The supervisor task at L289 will still be cancelled, which cancels `_bridge_loop`, whose `finally` at L333-L341 cleans up — so children are cleaned eventually. Technically safe via the `finally`, but fragile. Consider assigning `_children` *before* creating tasks or populating it inside the loop.

6. **`run_coroutine_threadsafe` future is discarded** — `discord_bridge.py:357`.
   - If `bridge.start()` raises (e.g., `asyncio.create_task` fails on a closed loop), the exception is lost. Attach `.add_done_callback(_log_if_error)` to surface the failure.

7. **`_pump_output` swallows backend exceptions without re-raising to supervisor** — `audio_bridge.py:397-403`.
   - If `backend.recv_events()` iterator raises (e.g., websocket closed), the output pump exits cleanly. Input pump keeps trying to `send_audio_chunk` on a dead backend, logging one exception per 20 ms. Supervisor gathers with `return_exceptions=True` (L327) so it doesn't notice. Either: (a) cancel the input pump when output pump exits unexpectedly, or (b) set `_stop_event` so input pump exits too. `_stop_event` is currently written at L287 but never read.

8. **`_prev_lens` dict mutated without lock** — `discord_bridge.py:435, 458`.
   - In practice discord.py processes UDP packets on a single receiver thread, so no true concurrency. Document the assumption. If discord.py ever switches to a thread-pool receiver, this becomes a P1 data race.

9. **`_buffers[ssrc]` unbounded growth** — the shim diffs lengths but doesn't trim. `_buffers` is owned by Hermes's VoiceReceiver; it's flushed on silence detection. If silence-detection is disabled or never fires, RAM grows per-user per-call. Not our bug (Hermes's) but our shim's correctness depends on Hermes's flushing cadence — document that dependency in ADR-0007.

10. **Backend-rate auto-detect by substring match** — `audio_bridge.py:213-219`. `if key in name` on `_BACKEND_RATE_DEFAULTS` keys matches `"gemini-live"` inside a hypothetical `"my-gemini-live-proxy"` — probably fine, but `for key, …: return` means first-hit wins by dict-insertion order. Deterministic on 3.7+, still brittle. Prefer exact match with a fallback.

11. **`_install_via_monkey_patch` re-attach on reconnect** — `discord_bridge.py:165-173`. If Hermes reconnects (creates a *new* `VoiceReceiver` per `join_voice_channel` call) our shim gets installed fresh each time — correct. **But**: if Hermes ever caches/reuses a receiver across channel joins, our `_install_frame_callback` at L409-414 correctly swaps the callback slot *without* re-wrapping `_on_packet`, so the old `_hermes_s2s_frame_cb` attribute points at the stale bridge's `on_user_frame`. Closed bridge's `push_input` still gets called → no-op effectively (closed queue is fine), but `_prev_lens` from the old shim closure persists. Benign but worth documenting.

---

## QUESTIONS

- Is `discord.py`'s packet-receive actually single-threaded? Confirm before declaring (8) benign. Search in `/home/codeseys/.hermes/hermes-agent/gateway/platforms/discord.py` for `receive_audio_packet` threading model.
- Does Hermes use one asyncio loop for both its main event loop and `voice_client.loop`? If yes, (2) is moot in practice. Add an assertion.
- Why does `recv_events` typing (`Protocol: async def recv_events(self) -> AsyncIterator`) return an iterator from an `async def`? Either make it `def recv_events(self) -> AsyncIterator` or have the protocol be an async-gen. `_pump_output:389` awaits nothing to get the iterator — `self.backend.recv_events()` returns a coroutine per the Protocol but is iterated as an async iterator at L398. This works if implementers declare it `def` or make it an async-generator, which contradicts the Protocol. Cosmetic but confusing.
- Should `cancel_all` be called on bridge `close()`? Currently `RealtimeAudioBridge.close()` does NOT call `tool_bridge.cancel_all()` — in-flight tool calls remain pending, their tasks live on until GC, and `backend.inject_tool_result` calls a closed backend. Deliberate or oversight?

---

## Copy-paste / research-lift concerns

- The identical `if/else` branches at `audio_bridge.py:355-371` look like a research/09 snippet where the same-rate fast path was supposed to skip `resample_pcm` (e.g., `if src_rate==dst_rate: just downmix`), but was pasted twice with the same body. Very likely copy-paste-without-think.
- `_BRIDGE_WRAPPED_MARKER` + `_S2S_LEAVE_WRAPPED_MARKER` + `_FRAME_CB_INSTALLED_MARKER`: three separate idempotence markers for closely-related wraps. Works, but three strings with three spellings invites drift. Minor.
- The SPIKE header at `discord_bridge.py:21-49` cites line numbers in `gateway/platforms/discord.py` — those will rot. Mark them as "as of Hermes ≤SUPPORTED_HERMES_RANGE upper bound" or accept they'll go stale.

---

## GO / NO-GO for v0.3.1 tag

**CONDITIONAL-GO.**

The three P1 issues are real but narrow in blast radius:
- #1 (first-frame drop) is a quality-of-service issue, not a crash — drops ~20 ms once per new speaker. Tag-able with a follow-up patch.
- #2 (cross-loop close) likely never fires in the Hermes single-loop topology.
- #3 (filler-audio exception orphan) matters only when `send_filler_audio` raises non-`NotImplementedError`, which on the 0.3.1 stub backends it does not.

Recommend: tag v0.3.1 **if** a NEWS/KNOWN-ISSUES entry covers #1 and #3, and a 0.3.1.post1 or 0.3.2 patch lands the `0`-fallback fix for the SSRC diff (one-line change). Otherwise, gate v0.3.1 on fixing #1 — it's trivial and the only user-visible defect.
