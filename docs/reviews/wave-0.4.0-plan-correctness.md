# 0.4.0 Wave Plan — Pre-Execution Correctness Review

**Reviewer:** Phase-8 subagent #3 (CORRECTNESS lens)
**Date:** 2026-05-10
**Target:** `docs/plans/wave-0.4.0-rearchitecture.md` + ADRs 0012/0013/0014 + research 14/15
**Read order followed:** plan (full) → ADR-0013 → ADR-0014 (spot) → gemini_live.py 270–385 → audio_bridge.py 600–618 → discord_bridge.py markers → research-15 §2.

## ⚠ Diversity caveat (honest header)

The route-fidelity probe was RED: the 3 "cross-family" Phase-8 reviewers all fell back to **anthropic/claude-opus-4.7**. This review is *nominally* cross-family but is in reality **Opus-on-Opus-on-Opus**, differentiated only by framing prompt (correctness vs clarity vs risk). Intersection-P0 from this triad should be trusted LESS than a true three-family intersection; union-P1 should be trusted MORE (because orthogonality comes from framing only, not training-data independence). Anything a single Opus instance missed systematically will be missed by all three. Operator should weight Kimi/Gemini sign-off on W1c, W3b, W4a manually if any P0 here is disputed.

---

## Findings (ordered by severity)

### P0-1. W3b M3.3 transcript event-type strings are factually wrong — will silently drop ALL realtime transcripts. (plan L411-414)

**Evidence.** The plan at L411-414 says:
```python
if self._transcript_sink:
    role = "user" if event.type == "transcript_partial_user" else "assistant"
    self._transcript_sink(role=role, text=event.text, final=event.final)
```
The actual `GeminiLiveBackend._translate_server_msg()` at `gemini_live.py:321-348` emits:
- `RealtimeEvent(type="transcript_partial", payload={"text": ..., "role": "assistant"|"user"})`
- `RealtimeEvent(type="transcript_final", payload={"turn_complete": True})` — **no `text` on final; no role**

The spec is wrong in **three** ways:
1. `event.type` is `"transcript_partial"` (no `_user`/`_assistant` suffix). The equality check `== "transcript_partial_user"` is **always False** ⇒ every event routes to `assistant`, mislabeling user STT and never firing for users.
2. There is no `event.text` attribute; text lives in `event.payload["text"]`. AttributeError on first transcript ⇒ the try-block at the caller (audio_bridge `_dispatch_event`) will swallow and log, dropping silently.
3. There is no `event.final`. Finality is a separate event type, and it carries `{"turn_complete": True}` with no text/role at all. The current spec will crash on the final event too.

**Remediation.** Rewrite M3.3 exactly as:
```python
if self._transcript_sink and event.type in ("transcript_partial", "transcript_final"):
    payload = event.payload or {}
    role = payload.get("role", "assistant")
    text = payload.get("text")  # may be None for transcript_final with turn_complete
    final = event.type == "transcript_final" or bool(payload.get("turn_complete"))
    if text is not None or final:
        self._transcript_sink(role=role, text=text or "", final=final)
```
Add a pytest that instantiates a fake bridge, feeds a real `RealtimeEvent(type="transcript_partial", payload={"text":"hi","role":"user"})` through `_dispatch_event`, and asserts the sink receives `role="user"`. This test would have caught the bug pre-merge.

---

### P0-2. W1b M1.5 asyncio-lifecycle guarantee is prose-only; the 0.3.1 regression fence is not mechanically enforced by the acceptance test. (plan L275-280, A1 L286)

**Evidence.** M1.5 says "awaits `backend.connect()` BEFORE spawning input/output pump tasks (regression-fence per memory…)". The acceptance test A1 is `test_realtime_session_calls_connect_before_pumps` — BUT the plan does not spell out HOW the test asserts ordering. A naive test could mock `backend.connect` as an AsyncMock and only verify it was called at all; that passes even if pumps spawned first. The 0.3.1 bug was Kimi-caught precisely because the unit test failed to guard the ordering.

Additionally, research-15 §2 uses the pattern `self._backend = await self._stack.enter_async_context(build_backend(spec))` where `__aenter__` performs connect — the plan's M1.5 does NOT lock in that idiom. An executor could legitimately do `self._backend = RealtimeBackend(spec); self._stack.callback(self._backend.close); self._pump = spawn(...); await self._backend.connect()` and pass "connect before pumps" lexically while still racing.

**Remediation.** (a) Amend M1.5 to REQUIRE: `self._backend = await self._stack.enter_async_context(build_backend(spec))` on the *first* line of `start()`'s try-block, with pump spawning strictly after. (b) Amend A1 to spell out: the test must install a `side_effect` on the mocked `backend.connect` that asserts `self._send_task is None` and `self._recv_task is None` at connect-time. An ordering bug then raises inside connect.

---

### P0-3. W2a M2.1 persistence test runs in-process only — cross-process survival not verified. (plan L341-344, A1 L357)

**Evidence.** M2.1 states `/s2s` "persists to `~/.hermes/.s2s_mode_overrides.json`". The only acceptance test is A1 `pytest tests/test_slash_command.py -q`. Python unit tests ordinarily run in one process; if the implementation writes to an in-memory dict AND opportunistically flushes, a single-process test passes even when the actual file is never written or is written to the wrong path (e.g. a Pytest-tmp homedir that is torn down).

Per the prior hermes-s2s memory entry ("tests usually run in one process and miss in-memory cache cases"), this is exactly the class of bug that escapes unit tests and surfaces only in production on the next `/voice join` after a bot restart.

**Remediation.** Add acceptance test A4: write an override via the slash handler, read the JSON file **directly with `json.load(open(path))` in the test**, assert the expected `{guild_id: {channel_id: mode}}` structure is on disk. Separately, add an integration test that constructs TWO independent `SlashState` instances in one process (simulating restart) and verifies the second instance reads the first instance's write.

---

### P1-4. MetaCommandSink regex anchors reject ordinary trailing speech. (plan L451)

**Evidence.** The new-session pattern `^(start|begin|open) (a |an )?new (session|chat|conversation)$` has a trailing `$`. The utterance "Hey ARIA, start a new session about React" fails to match at all because "about React" follows. Research-15 §3 and ADR-0014 are silent on whether grammar is closed-set or should allow trailing topic hints. Two reasonable designs exist:
- **Closed-set (current spec):** user must say exactly "start a new session". Follow-ups ignored. UX acceptable if we document.
- **Open-tail (likely user-desired):** "start a new session about X" → `/new --about="about X"` or `/new` + title auto-suggest.

Current behaviour is the worst of both: the phrase is neither meta-command nor LLM utterance (it fails regex and goes to LLM raw, which may say "okay!" but not actually trigger /new).

**Remediation (clarify):** add an explicit note to M4.1 — either (a) keep `$`, document in HOWTO that trailing text is forbidden, and add a negative test that "start a new session about X" deliberately does NOT match; OR (b) replace `$` with `(?:\s+about\s+(?P<topic>.{1,80}))?$` for /new and /branch. Recommend (b) for /new (low destruction, pleasant UX) and (a) for /compress (keep grammar tight).

---

### P1-5. W4a lacks a spoken-confirmation path for destructive meta-commands. (plan L460-465)

**Evidence.** M4.2 says "Earcon for `/new`, `/title`, `/branch`; spoken confirmation for `/compress` and `/resume`". But `/resume` was DEFERRED in the scope-refinement block (L19-21), leaving `/compress` as the only spoken-confirm path. `/new` is DESTRUCTIVE (ends the current session) but gets only an earcon. ADR-0014 L18 says matched utterances are CONSUMED (not forwarded to LLM) — so the LLM never voices an acknowledgement. User experience: user says "hey ARIA, start a new session", hears a beep, has no idea whether it worked or whether the bot was just confused.

**Remediation.** Upgrade `/new` to spoken-confirm tier ("Starting a new session.") or add an explicit earcon spec with two distinct tones (success vs confusion) and document the sound-design in HOWTO-VOICE-MODE.md. Add test `test_meta_dispatcher_emits_spoken_ack_for_new` to enforce.

---

### P1-6. W1c monkey-patch idempotency is not spelled out. (plan L316-320)

**Evidence.** The current `discord_bridge.py` uses `_BRIDGE_WRAPPED_MARKER = "__hermes_s2s_voice_bridge__"` (L73) to guard `join_voice_channel` double-wrapping, and a separate `_S2S_LEAVE_WRAPPED_MARKER` for leave. W1c M1.9 says "Replace lines 333-360 … with delegated factory call" but does not state that:
- The marker check at existing L202 must remain before the new factory-delegate wrapping.
- `setattr(..., _BRIDGE_WRAPPED_MARKER, True)` must still fire on the new wrapper.
- The factory itself must be lazily initialized (a second `register()` should reuse the existing factory, not build two).

A second `register()` call (common in test fixtures, hot-reload dev loops) could double-wrap and cause double-bridge-construction.

**Remediation.** Add to M1.9 spec: "Preserve the `_BRIDGE_WRAPPED_MARKER` guard at the head of `_install_via_monkey_patch()`; set the marker on the new wrapped function; expose `_get_or_build_factory(adapter)` that caches on `adapter._s2s_voice_factory`." Add a unit test that calls `register(ctx)` twice and asserts `DiscordAdapter.join_voice_channel` was wrapped exactly once.

---

### P1-7. W5a first-load auto-translate has no partial-state recovery spec. (plan L27-31, scope refinement)

**Evidence.** The refined W5a does auto-translation inside `config/__init__.py` on first load, writing a backup file before atomic swap. But if the process dies AFTER `.bak.0_4_<ts>` is written and BEFORE the atomic rename of the new file, the next start would see *both* `config.yaml` (unchanged) and a backup (identical). On the second start the code would think "already backed up, skipping" (if it checks bak existence) and never re-attempt, OR would create a second backup. Neither is correct.

**Remediation.** Specify the exact 3-step invariant: (1) write `config.yaml.new` next to the original; (2) fsync + rename to `config.yaml.bak.0_4_<ts>` ONLY after (1) succeeds; (3) rename `config.yaml.new` → `config.yaml` atomically. On startup, if `config.yaml.new` exists but `config.yaml` has old schema, complete the swap forward. Test with `os.rename` interposed to fail at each step.

---

### P2-8. W3a and W3b file ownership — no collision, rule respected. (plan L124-127) ✅

Verified: W3a owns `voice/threads.py` + `voice/transcript.py`; W3b owns `_internal/audio_bridge.py` + `_internal/discord_bridge.py`. They are in batch B3 but sequential (W3a first per L137). The plan's own "no two waves in the same parallel batch may write the same file" rule is satisfied because B3 is declared **sequential**, not parallel. Confirmed not an issue.

---

### P2-9. Acceptance-test `grep` audit — all flags clean. ✅

Scanned every `grep` in the acceptance blocks: L358 `grep -F '@app_commands.choices'`, L432 `grep -F '_transcript_sink'`, L514 `grep -F 'VOICE_OVERLAY_BEGIN'`, L573 `grep -F '/s2s'`, L574 `grep -F 'four modes'`, L575 `grep -F '0.4.0'`. All use `-F` (fixed-string). No PCRE features in default `grep` — no issue.

---

### P2-10. AsyncExitStack ordering in M1.5 is under-specified but correct in research-15. (plan L275-280 vs research-15 L92-104)

Research-15 §2 pseudocode registers in this order inside `start()`'s try:
1. `enter_async_context(build_backend(spec))` — backend + connect
2. `stack.callback(_detach_sink)` — audio sink detach
3. pump tasks spawned → `push_async_callback(_cancel_task, t)` per task

`aclose()` unwinds LIFO: cancel pumps → detach sink → close backend. **Correct.** The plan's M1.5 doesn't repeat this ordering, so an executor deviating from research-15 §2 is plausible. Tighten by citing research-15 L92-104 verbatim in M1.5. Not P0 because the research doc is already referenced at L247-249.

---

### P2-11. MetaCommandSink + LLM consumption semantics undocumented in plan. (plan L447-458)

ADR-0014 L18 says matched utterances ARE consumed. Plan M4.1/M4.2 doesn't restate this; an executor reading only the plan could reasonably pass-through-and-dispatch (double-fire). Fix by adding one sentence to M4.1: "Matched utterance is consumed; the STT-to-LLM forwarder sees the empty string (or nothing). See ADR-0014 §2." Also add a test `test_matched_utterance_not_forwarded_to_llm`.

---

## Verdict

**READY-WITH-MINOR-EDITS → NEEDS-REWORK on W3b.**

- **P0-1 (transcript event types) is a merge-blocker.** Executing W3b as written ships broken realtime transcripts AND a silent AttributeError storm in logs. Must be corrected in the plan BEFORE W3b subagent dispatch — not after.
- **P0-2 and P0-3 are test-discipline gaps** that an executor might accidentally fix on their own but also might not. Amending the plan to make the assertions explicit costs 20 minutes and removes a real escape path.
- P1 findings (4, 5, 6, 7) should each land as a plan-edit commit on the planning branch before W1a begins. Total effort: ~45 minutes of plan-editing; no scope change.
- P2 findings are documentation tightening; can be folded into W6a.

Recommend: one 60-minute plan-revision pass addressing P0-1/2/3 + P1-4/5/6/7, re-dispatch this same Phase-8 (or Kimi cross-check P0-1 independently since diversity was not met), THEN begin W1a.
