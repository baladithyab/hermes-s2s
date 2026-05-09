# Wave 0.3.2 Cross-Family Review — KIMI

**Reviewer family:** KIMI (0.3.1 P0 finder: `bridge.start` spawning pumps without
awaiting `backend.connect`).
**Commit under review:** `548c859`.
**Focus:** silent-failure traps + contract violations.

0.3.2 is *much* tighter than 0.3.1. The structural work (filler-audio
implementation, doctor, wizard profiles, integration test, stats) all landed.
The remaining issues are ALL silent-failure traps around the edges — the
feature works when everything is green, but fails quietly in predictable
ways on real user machines.

---

## P0 — Ship-blockers

### P0-1. README's "30-second install" step 1 fails for every fresh user.

**`README.md:29`** claims `pip install 'hermes-s2s[all]'` as the first command.
**`docs/ROADMAP.md:42`** explicitly lists `- [ ] PyPI publish (pip install
hermes-s2s)` as uncompleted, and **`README.md:130`** confirms
`- [ ] PyPI publish` is still open for 0.3.2.

A fresh user running the 4-command happy path hits
`ERROR: No matching distribution found for hermes-s2s[all]` at step 1 and
never reaches steps 2-4. `hermes plugins enable hermes-s2s` cannot discover
an entry-point for a package that isn't installed. This is the wave's
headline acceptance criterion (`#8`, `#9` in the plan — "30-second install
section" / "install profile matrix") and it is wrong on first contact.

**Fix options (pick one before tagging 0.3.2):**
1. Actually publish to PyPI (the ROADMAP's intent); OR
2. Rewrite the 30-second block to `pip install
   'git+https://github.com/baladithyab/hermes-s2s.git#egg=hermes-s2s[all]'`
   and call out the PyPI-pending status inline.

Option 2 is zero-risk and honest. Option 1 is the real fix but requires
release mechanics outside the 0.3.2 diff. Either way, **do not ship the
current README claim unchanged** — the doctor is useless if the user
can't install the package containing it.

---

## P1 — Must-fix before next wave

### P1-1. Doctor + `_detect_missing` remediation hints point to extras that don't exist.

`pyproject.toml:28-44` defines extras: `moonshine`, `kokoro`, `audio`,
`realtime`, `server-client`, `local-all`, `all`, `dev`. There is NO
`local-stt` or `local-tts` extra.

Every remediation hint for local STT/TTS points to a non-existent extra:
- **`hermes_s2s/doctor.py:134`**: `"pip install hermes-s2s[local-stt]"`
- **`hermes_s2s/doctor.py:135`**: `"pip install hermes-s2s[local-tts]"`
- **`hermes_s2s/cli.py:212`**: `"moonshine_onnx (pip install hermes-s2s[local-stt])"`
- **`hermes_s2s/cli.py:215`**: `"kokoro (pip install hermes-s2s[local-tts])"`

A user whose doctor output says "moonshine_onnx NOT installed — pip install
hermes-s2s[local-stt]" copies that line, runs it, and pip silently prints
`WARNING: hermes-s2s 0.3.2 does not provide the extra 'local-stt'` — then
proceeds with a no-op install. The remediation actively misleads. Fix: use
`[moonshine]` / `[kokoro]` / `[local-all]` per the actual pyproject.

### P1-2. `_detect_missing` has no awareness of realtime profiles.

**`hermes_s2s/cli.py:208-225`** only checks moonshine/kokoro/ffmpeg/ELEVENLABS/GROQ.
The three new realtime profiles (realtime-gemini, realtime-openai,
realtime-openai-mini) are NOT in any branch. A user who runs
`pip install hermes-s2s` (no extras) then `hermes s2s setup --profile
realtime-gemini` sees zero "missing deps" warnings from `_detect_missing`
at **cli.py:330-334**, gets a clean-looking config write, and only discovers
`ModuleNotFoundError: websockets` when `/voice join` fails at runtime.

`_print_realtime_checklist` (cli.py:103-127) also doesn't check
`websockets` — only `discord`, api keys, and Discord env vars. websockets
is the one Python dep that MUST be present for the profile the user just
selected.

Fix: add `websockets` check to `_detect_missing` for realtime profiles, OR
add a line to `_print_realtime_checklist` that mirrors the doctor's
python_deps websockets check.

### P1-3. Doctor probe passes Gemini voice to OpenAI backends.

**`hermes_s2s/doctor.py:347-353`**:
```python
await backend.connect(
    "pre-flight doctor probe (read-only)",
    getattr(cfg, "realtime_options", {}).get("voice") or "Aoede",
    [],
)
```

The `or "Aoede"` fallback is Gemini-specific. If `cfg.realtime_options` has
no `voice` set (e.g. user edited config.yaml partially) and provider is
`gpt-realtime`, the backend receives `voice="Aoede"` which is not a valid
OpenAI voice. OpenAI's Realtime server will either reject with a
session.updated error or (worse) silently fall back to `alloy`. Either
way, the probe is testing something different from what production
traffic would send.

Fix: branch the default by provider — `"Aoede"` for gemini-live, `"alloy"`
for openai. Better: fail the probe cleanly with
`"no voice configured, cannot probe"`.

### P1-4. OpenAI `send_filler_audio` docstring lies about out-of-band semantics.

**`hermes_s2s/providers/realtime/openai_realtime.py:303-321`** docstring
says:

> Sends a `response.create` event with an `instructions` override... Per
> the OpenAI Realtime docs, `response.create` lets the client spawn an
> **out-of-band** model response...

The sent JSON is:
```python
{"type": "response.create",
 "response": {"instructions": f"Briefly say: {text}",
              "modalities": ["audio"]}}
```

Out-of-band mode requires `response.conversation: "none"`. This isn't
set. The request goes on the default conversation and, per OpenAI's
docs, will error with `conversation_already_has_active_response` whenever
the model was mid-response when the tool_bridge soft-timeout fired (i.e.,
precisely the common case: model said "sure, looking that up…" then
produced a tool_call while still generating audio).

Current behavior: `tool_bridge.py:113-116` catches and logs at WARNING;
filler silently never plays. Docstring promises the opposite ("out-of-band").

Fix options:
1. Add `"conversation": "none"` to the response block (actually
   out-of-band; matches docstring).
2. Rewrite the docstring to match reality ("in-band; will fail if a
   response is already active — caller should await interrupt() first").

Either way, ADD a test that drives `send_filler_audio` mid-response and
asserts the observed behavior (it's currently untested — see P2-3).

### P1-5. Gemini `send_filler_audio` interrupts the in-flight turn with no documentation or test.

**`hermes_s2s/providers/realtime/gemini_live.py:379-402`**. A
`BidiGenerateContentClientContent` with `turnComplete: true` sent while
the model is mid-audio-turn causes Gemini Live to interrupt its own
current audio generation and start a new user turn. This is the exact
semantics you want MOST of the time (old audio is now stale after 5s),
but the docstring claims the model "continues with whatever it was
doing" afterwards. It does not — the interrupted turn's remaining audio
is dropped; the model only resumes when `inject_tool_result` arrives.

The 0.3.1 review noted this is consistent with "filler is a stopgap",
but the 0.3.2 docstring promotes it as if it is cleanly insertable.
Plus: `test_send_filler_audio_emits_text_content` (tests/test_gemini_live.py:243-276)
only verifies shape, never the speaking/queue/interrupt distinction.

Fix: rewrite the docstring to call out the interrupt semantics explicitly,
matching the "Filler audio is fire-and-forget... if the tool result lands
mid-phrase, the filler is NOT interrupted" callout in README.md:135.
Add a test that sends filler twice back-to-back and asserts (at the mock
WS level) that both frames went out — verifying we didn't accidentally
add a guard. No test currently establishes the contract.

---

## P2 — Polish / follow-up waves

### P2-1. BridgeBuffer debounced warn can skip the 100-boundary.

**`hermes_s2s/_internal/audio_bridge.py:96-137`**. Two increment sites
(L109 and L126) each guarded by `if dropped_input % 100 == 0`. In the
race path where `queue.Full` → `get_nowait` succeeds (drops #1) → retry
`put_nowait` also throws `queue.Full` (drops #2) — all in one call —
the counter jumps by 2. If it was at 99 before the call it becomes 101.
`101 % 100 == 1`. No warning is logged. The "every 100 drops you get
a warning" contract is silently violated at exactly the transition
points operators care about.

`tests/test_audio_bridge.py:362-378` uses `input_max=1` in a single
thread, which always takes the first branch only (increment by 1) and
therefore never exercises the race. The "jumps from 99 to 105" case is
untested.

Fix: replace the modulo guard with a threshold-crossing check using a
`_next_warn_at` counter:

```python
if self._dropped_input >= self._next_warn_at:
    logger.warning(...)
    self._next_warn_at = (
        (self._dropped_input // 100) + 1
    ) * 100
```

This fires at the first push that crosses each boundary regardless of
stride, and naturally deduplicates. Plus: add a test that manually
bumps `_dropped_input` by 5 to cross the 100-mark and asserts exactly
one warning fires.

### P2-2. Wizard's realtime checklist prints but cannot gate.

**`hermes_s2s/cli.py:336-339`** calls `_print_realtime_checklist(profile)`
before writing config (good, the ordering is correct against dry_run and
against the file I/O at L361-377). BUT the checklist has no prompt — it
prints `[✗] GEMINI_API_KEY — ...` and then proceeds to write
`s2s.mode=realtime` + `realtime.provider=gemini-live` + `.env`
monkey-patch flag regardless. Non-interactive (CI, a script) this is
fine. Interactive, a user who sees three ✗ marks and ctrl-Cs is fine.
But a user who just *doesn't notice the lines* gets an irreversible
config write to `~/.hermes/config.yaml` for a mode that cannot possibly
work.

At minimum: if stdin is a TTY, any `[✗]` mark should trigger
`input("continue anyway? [y/N] ")` before writing. In non-TTY, proceed
silently (CI friendliness). The test
`test_setup_realtime_warns_on_missing_api_key` at
`tests/test_setup_wizard.py:219-230` uses `dry_run=True` and never
asserts the write was actually gated, so this regression is free today.

### P2-3. Filler-audio tests don't cover the SPEAKING state.

Both `tests/test_gemini_live.py:243-276` and
`tests/test_openai_realtime.py:292-320` send filler in a cleanly-idle
state and only assert message shape. The actual production context
(model is mid-audio-response when `HermesToolBridge._run_with_timeouts`
fires it at tool_bridge.py:112) is untested. See P1-4 and P1-5 — the
behavior in this state is different per backend and different from the
docstrings.

### P2-4. Doctor's overall_status logic is sound; exit code could be sharper.

**`hermes_s2s/doctor.py:483-491`**. `fail > warn > pass`, ignoring skip —
correct. Edge case "all warns, no fail" → `overall = warn` → cli exits 0
(cli.py:412 `sys.exit(0 if overall_status != "fail" else 1)`). This
matches the plan's acceptance criterion #4 ("exits 0 on green, 1 on any
error") under a literal reading of "error = fail". But a user whose
readiness check has 8 warnings gets an exit code that looks like "all
good" to any CI step using `hermes s2s doctor && start_bot`. Consider
`--strict` flag that treats warns as fail-equivalent; out-of-scope for
0.3.2 polish.

### P2-5. Doctor probe uses an arbitrary system_prompt, not the configured one.

**`hermes_s2s/doctor.py:349`** passes the literal
`"pre-flight doctor probe (read-only)"`. This tests WS connectivity +
auth + model-access, but does not verify that the *actual configured*
system_prompt is accepted by the backend (e.g. too long, malformed
templating). Acceptable for 0.3.2 — probing auth is the high-value
check — but worth a note in INSTALL.md that the probe is narrower than
the live run.

---

## What's good

- **Wiring end-to-end is REAL.** `tool_bridge.py:112` invokes
  `backend.send_filler_audio("let me check on that")` with a proper
  best-effort exception catch at L113-116. My worry from the task brief
  ("is the integration actually wired?") was unfounded — the hookup is
  preserved from 0.3.1 and the new concrete implementations plug straight
  into it. Filler audio is not dead code.
- **Doctor's probe path correctly uses `resolve_realtime`** (doctor.py:326-338)
  so any backend registered via the plugin registry (including future ones)
  will be probed, not just the two hardcoded backends. Lazy imports of
  websockets via the registry — probe doesn't drag the realtime extra at
  module load. Clean.
- **Doctor's api_keys check is provider-aware** (doctor.py:205-207) — only
  fails on a missing key when that provider is the configured one, warns
  otherwise. Exactly right.
- **Idempotent `.env` monkeypatch append** (cli.py:131-144) uses the same
  marker-line pattern as the existing `HERMES_LOCAL_STT_COMMAND` write.
  Test at `tests/test_setup_wizard.py:209-216` confirms double-run yields
  one line.
- **`stats()` on both BridgeBuffer and RealtimeAudioBridge** expose a clean
  dict with every counter a user would want for a `/s2s status` slash
  command or structured log event. Aggregation in
  `RealtimeAudioBridge.stats()` (audio_bridge.py:395-405) delegates
  properly and augments without clobbering.
- **Integration test exists** (`tests/test_bridge_integration.py`, 235
  lines, FakeBackend + real RealtimeAudioBridge). This is the first wave
  where a pump-level regression would be caught at CI.
- **ADR trail is tight.** Plan → BACKLOG → ADR-0008/0009 → diff all line
  up. Reviewer onboarding overhead is minimal.
- **0.3.1 P0 stays fixed.** `bridge.start` still awaits `backend.connect`
  before spawning pumps (audio_bridge.py — not touched by this diff).

---

## Summary

**1 P0 (README install fails fresh), 5 P1s (silent-failure traps at edges),
5 P2s (polish).** P0 is release coordination, not code. P1 order of impact:
P1-1 (doctor hints to phantom extras) → P1-2 (no websockets check in
realtime wizard) → P1-4/P1-5 (filler docstrings vs actual backend
semantics) → P1-3 (Gemini-voice default leaks into OpenAI probe). With
P0-1 fixed, ship it.
