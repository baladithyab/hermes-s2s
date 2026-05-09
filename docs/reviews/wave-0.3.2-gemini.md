# 0.3.2 cross-family review — Gemini

Reviewer: Gemini family. Commit under review: `548c859`. Diff base: `dbfb4ec`.
Lens: test rigor, async hazards, argparse hygiene, lazy imports, error-path UX,
back-compat, integration-test fidelity.

---

## P0 — must fix before tag

### P0-1. `s2s_doctor` LLM tool is broken when called from the gateway (running loop)

`hermes_s2s/tools.py::s2s_doctor` → `run_doctor(probe=True)` →
`_backend_connectivity_checks` → **`asyncio.run(_probe_backend_async(...))`**
at `doctor.py:433`.

`asyncio.run` raises `RuntimeError: asyncio.run() cannot be called from a
running event loop` whenever the handler is invoked from an async context —
which is exactly what happens when the LLM calls the tool via the Hermes
gateway. Reproduced locally:

```
$ python3 -c "...run_doctor(probe=True) from inside asyncio.run(...)"
/.../doctor.py:435: RuntimeWarning: coroutine '_probe_backend_async' was never awaited
OK fail
```

Three bad consequences:

1. The probe check silently reports **"Probe dispatcher raised: asyncio.run() ..."**
   — actionable-looking but completely misleading (user's setup is fine).
2. The coroutine is never awaited → leak + `RuntimeWarning` on every call.
3. The `overall_status` becomes `fail` even when the backend is reachable.

Acceptance criterion #5 (`s2s_doctor tool callable by the LLM; returns
structured JSON`) is not actually met in the gateway path. Fix: detect a
running loop in `_backend_connectivity_checks` and either dispatch via
`asyncio.run_coroutine_threadsafe` into a worker thread, expose an
`async run_doctor()` variant and make `s2s_doctor` an async handler, or
default `probe=False` when `asyncio.get_running_loop()` succeeds.

There is **no test that covers this path** — `test_run_doctor_with_probe_mocked_websocket`
is called from sync scope, so it happily hits the nested-loop-free branch.
Add a test that runs `s2s_doctor({"probe": True})` inside an `asyncio.run(...)`
and asserts it does not crash / does not report a spurious failure.

---

## P1 — should fix this wave

### P1-1. `test_setup_realtime_warns_on_missing_api_key` does not test the warning

```python
def test_setup_realtime_warns_on_missing_api_key(...):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    ...
    assert "GEMINI_API_KEY" in out
    assert "aistudio.google.com" in out
```

`_print_realtime_checklist` **always** prints the line
`"[...] GEMINI_API_KEY — https://aistudio.google.com/apikey"` — the only
thing that varies with the env var is the `[✓]` vs `[✗]` glyph. Removing the
`delenv` call leaves the test passing. It asserts nothing about the actual
warning semantics. Either assert on `"[✗]"` preceding `GEMINI_API_KEY`, or
assert stderr contains a WARNING-level message, not that some substring
appears in stdout.

### P1-2. Probe skips the "first server event" gate required by the plan

Plan §Acceptance-4: *"opens WS via the configured realtime backend's connect(),
**waits for first server event** or 5s timeout, closes."* The implementation
in `_probe_backend_async` only `await`s `backend.connect(...)` and then closes.
For the Gemini backend `connect()` returns as soon as the `setupComplete` is
received (good), but for OpenAI it returns after `session.update` is sent —
which can succeed even if the API key is later rejected by an async error
frame. The probe's "pass" therefore under-reports real credential failures.

Trivially fixed: after `connect`, `anext(backend.recv_events().__aiter__())` with
a 2 s wait_for, or scan for a `RealtimeEvent(type="error")` as well. The
mocked-backend test (`test_run_doctor_with_probe_mocked_websocket`) would
need to script that event — right now it mocks `connect` as `AsyncMock` and
never touches `recv_events`.

### P1-3. `test_full_flow_input_output` is lenient about resampler output

The test checks `rate == 16000` (✓ good catch) and `600 <= len(chunk) <= 680`,
but never verifies the *content* is a faithful mixdown. A resampler that
returned the right number of zeroed bytes would pass. Because the input is
non-silent `b"\x10\x00\xf0\xff"` × N, it's cheap to assert
`chunk != b"\x00" * len(chunk)` — this catches stereo→mono average bugs
where a naively subtracted L/R pair cancels to zero.

### P1-4. Integration-test fake backend is not a faithful `RealtimeBackend`

`FakeBackend` in `tests/test_bridge_integration.py` omits properties that the
bridge consults at instantiation: `input_sample_rate`, `output_sample_rate`.
It works today only because `_resolve_backend_rates` falls back to the
NAME-based dict (`"gemini-live"`). If a future refactor makes explicit rate
attributes required the tests still pass against the stale Protocol. Either:

- assert `isinstance(FakeBackend(), hermes_s2s.providers.realtime.RealtimeBackend)`
  at the top of the module (runtime-checkable Protocol), or
- add `input_sample_rate = 16000` / `output_sample_rate = 24000` class attrs
  and *assert* the bridge picked them up via `bridge._backend_input_rate`.

Also `recv_events` is defined as `async def ... -> AsyncIterator[...]` but
its body is an `async def` generator; the real Protocol spec says it is
an async generator function. Fine in duck terms, subtle when someone types
annotations strictly.

### P1-5. Back-compat: realtime-profile wizard silently coexists with pre-0.3.2 cascaded state

Running `hermes s2s setup --profile realtime-gemini` on a config that has
`tts.provider` + `stt.provider` from a previous `local-all` run does
**deep-merge** a new `s2s.mode=realtime` block *on top of* those. The result
is a config with both `tts:` / `stt:` sections *and* `s2s.mode=realtime`. The
runtime currently ignores the stale sections (by mode-switch) — but the new
wizard never warns the user that they have dead config, and a subsequent
switch back to cascaded will pick up whatever stale provider is there. A
one-line `print("note: existing cascaded tts/stt blocks left untouched")`
when switching to realtime would close the UX gap.

No test covers this migration scenario. `test_existing_config_keys_preserved`
only covers local-all + extra keys — it should be mirrored for a realtime
profile over an existing cascaded config.

### P1-6. `test_backpressure_under_load` is race-prone

The test pushes 200 frames from a thread into an `input_max=10` queue while
`_pump_input` is actively draining with `resample_pcm` as its only time sink.
On a fast box with scipy resample <0.3 ms, the pump can drain ~100 frames in
the 200-push window; the threshold `dropped >= 190` will then flake. Either
pause the pump during burst (e.g. stop the bridge loop before pushing), cap
with `time.sleep(0)` per N pushes, or lower the assertion to
`dropped >= 150`. Not a theoretical concern — I watched it succeed in 200 ms
end-to-end, which is within variance distance of the threshold.

---

## P2 — nice to fix

### P2-1. Remediation copy: `discord.py` install hint is inconsistent

`doctor.py` line 138: `"discord", "pip install discord.py", "Discord voice bridge"`.
The rest of hermes-s2s docs consistently route through
`pip install 'hermes-agent[messaging]'` (which pulls `discord.py[voice]` plus
PyNaCl/opus). Suggesting bare `pip install discord.py` is correct but
under-sells the real dependency shape — users who follow it get a discord
import that then fails on missing PyNaCl for voice. Suggest
`pip install 'hermes-agent[messaging]'`.

### P2-2. Lazy-import claim: `ctypes.util.find_library("opus")` triggers a DL-open

`_system_dep_checks` calls `ctypes.util.find_library("opus")`. On Linux this
shells out to `/sbin/ldconfig -p` or, on some glibc builds, `dlopen`s libopus
probe-style. It's cheap but it's NOT "only standard library at load time" in
the strict sense of avoiding third-party side-effects. Cosmetic — the
comment at the top of `doctor.py` says "standard library at load time" which
is still true. No action needed, just flagging that the function call is
heavier than the docstring suggests.

### P2-3. `format_human` color codes leak into captured test output

`format_human` checks `sys.stdout.isatty()` and drops ANSI if not. Good.
But `test_format_human_includes_all_categories` runs under pytest where
stdout is captured (not a TTY), so colors are stripped — fine. If a future
refactor moves the TTY check behind `NO_COLOR` env var or similar, the
existing assertions `"Configuration" in out` still work. Note: the glyphs
✓ / ⚠ / ✗ are Unicode; on Windows CMD without UTF-8 console they may mojibake.
Consider a `--ascii` flag or respect `NO_COLOR`.

### P2-4. Doctor probe uses "Aoede" as default voice even for OpenAI

```python
getattr(cfg, "realtime_options", {}).get("voice") or "Aoede"
```

"Aoede" is a Gemini voice. Passing it to OpenAI `connect(voice="Aoede")` may
trigger a server-side rejection. Default a provider-appropriate voice or
pass `None` and let the backend use its own default.

### P2-5. `run_doctor()` swallows `load_config` exceptions into a single FAIL

Line 463-474: any exception from `load_config()` produces a single
`load_config` FAIL check and short-circuits the report. But `load_config`
already returns `S2SConfig.from_dict({})` on parse errors — so the only way
to hit the `except` is if `~/.hermes/config.yaml` is a symlink loop or the
`import` itself fails (e.g. `yaml` missing). That path is untested. Minor.

### P2-6. `asyncio.wait_for(backend.close(), timeout=2.0)` after success path

`_probe_backend_async` line 378 is fine, but on timeout the cleanup calls
`await backend.close()` **without** a timeout, so a hung close can stall the
probe for arbitrarily long (the outer 5 s timeout has already fired; we are
now in the `except`). Wrap those two cleanup awaits in `asyncio.wait_for`
too.

---

## What's good

- **Argparse discipline is clean.** G2 and G3 carved disjoint parsers:
  `setup` owns `--profile/--dry-run/--config-path`, `doctor` owns
  `--json/--no-probe`. No flag collision, help text is consistent, and the
  inline `# ----- G3 -----` banner-comments make the merge obvious.
- **Module-level lazy imports in doctor.py are genuinely clean.** Top-level
  imports are stdlib only (`asyncio`, `ctypes.util`, `importlib.util`, etc.).
  `websockets`, `scipy`, `discord`, backend classes, and even `load_config`
  are all behind function-local imports. `importlib.util.find_spec` is used
  for dep probes (never actually imports the target). I grepped for
  top-level imports of the heavy deps — none.
- **Back-compat preserved on the tool schema side.** `s2s_status`,
  `s2s_set_mode`, `s2s_test_pipeline` handlers and schemas are untouched.
  `S2S_DOCTOR` is additive. Existing 5 profiles (`local-all`,
  `hybrid-privacy`, `cloud-cheap`, `s2s-server`, `custom`) retain their
  exact config shape — `_profile_blocks` was not touched; realtime profiles
  go through a separate `_realtime_s2s_block` factory. `test_local_all_*`,
  `test_hybrid_privacy_*`, `test_existing_config_keys_preserved` all still
  green.
- **Filler-audio tests actually verify the JSON shape.** Both
  `test_send_filler_audio_emits_text_content` (Gemini) and
  `test_send_filler_audio_emits_response_create` (OpenAI) parse the sent
  frames and assert on deeply-nested keys (`clientContent.turns[0].parts[0].text`,
  `response.modalities`, `instructions` substring). Much better than the
  typical "was it called" assertion. Gemini test also verifies the
  not-connected guard raises `RuntimeError`.
- **Bridge stats + debounced log are tested with real overflow pressure.**
  `test_buffer_logs_warning_every_100_drops` uses `input_max=1` + 250 pushes
  and asserts on both the 100-threshold *and* 200-threshold log message,
  which catches off-by-one errors in the modulo check.
- **Integration test catches real behavior.** `test_close_leaves_no_leaked_tasks`
  is the right assertion (`asyncio.all_tasks() - tasks_before - {current}`)
  and actually exercises the pump-cancellation path.
  `test_backend_audio_chunks_get_resampled_to_48k_stereo` scripts a 24k mono
  chunk and verifies **4+ frames of 3840 B each** land in the buffer — that
  proves the 24k→48k + mono→stereo + 20 ms framing pipeline is real, not
  stubbed. This is what the plan asked for.
- **Doctor remediation URLs are current.** `aistudio.google.com/apikey`,
  `platform.openai.com/api-keys`, and
  `hermes-agent.nousresearch.com/docs/user-guide/messaging/discord` all
  resolve to live, on-topic pages (I checked). The plan's acceptance
  criterion for "actionable error message" is met on the
  non-probe checks.
- **Version bump is consistent.** `pyproject.toml` 0.3.2, `__init__.py`
  0.3.2, `test_smoke.py` updated together. No drift.

---

## Summary

Ship-blocker: **P0-1** (s2s_doctor LLM tool crashes/leaks under realtime
probe from the gateway). That single bug guts acceptance criterion #5.
The plan's promised UX — "an LLM can call s2s_doctor and get back
structured JSON" — is not delivered in practice, and there is no test
that catches it.

Everything else is P1/P2 polish. The lazy-import discipline and the
filler-audio tests are notably good — best test-quality in the wave. The
integration test is the right shape but has one race and one
fake-Protocol-drift concern. The wizard's "warning" test is a
false-positive test and should be rewritten before merge.
