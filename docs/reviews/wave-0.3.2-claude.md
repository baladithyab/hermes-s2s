# Cross-family review: hermes-s2s 0.3.2 (commit 548c859) — CLAUDE

Review scope: G1 filler audio, G2 wizard realtime profiles, G3 doctor + bridge
stats, G4 integration test + install docs. Ran `pytest -q` locally against the
commit → **83 passed, 1 skipped**. That is table-stakes; the wave plan's
acceptance criteria go further and that's where this review lives.

## P0 blockers

### P0-1 — OpenAI filler creates a conversation-polluting, possibly-rejected response
**Where:** `hermes_s2s/providers/realtime/openai_realtime.py:318-326`
**What's wrong:** `send_filler_audio` emits

```json
{"type": "response.create",
 "response": {"instructions": "Briefly say: <text>",
              "modalities": ["audio"]}}
```

Two concrete bugs against the canonical OpenAI Realtime docs
(https://platform.openai.com/docs/guides/realtime-conversations, §
out-of-band responses):

1. **Missing `"conversation": "none"`.** ADR-0008 §2 calls this filler on
   soft-timeout *during* an in-flight tool call. Per OpenAI docs, a
   concurrent `response.create` is only legal if it's out-of-band, which
   requires `conversation: "none"`. Without it the filler (a) may be
   rejected by the server while a pending tool response exists, and (b)
   becomes a permanent `assistant` message in the conversation history
   containing the filler text — future turns will see "Briefly say:
   thinking" as a real model utterance. The docstring literally calls the
   pattern "out-of-band" but the wire frame is *not* out-of-band.
2. **`modalities` vs `output_modalities`.** The GA `gpt-realtime` model
   (which is what the new wizard profile writes — see cli.py:67-74) uses
   `output_modalities` in `response.create.response`. `modalities` is the
   legacy `gpt-4o-realtime-preview` field. Some installs will silently
   fall back to text-only output for the filler, defeating the whole
   point (ADR-0008 was supposed to cover the silent gap with audio).

**Fix:**
```python
await self._send_json({
    "type": "response.create",
    "response": {
        "conversation": "none",
        "output_modalities": ["audio"],
        "instructions": f"Briefly say: {text}",
        "metadata": {"hermes_s2s_filler": "1"},
    },
})
```
And update `tests/test_openai_realtime.py::test_send_filler_audio_emits_response_create`
to assert `conversation == "none"` and `output_modalities == ["audio"]`.
The current test is a rubber-stamp of the buggy shape.

### P0-2 — `s2s_doctor` LLM tool will crash inside a running event loop
**Where:** `hermes_s2s/doctor.py:433` (`asyncio.run(...)` inside
`_backend_connectivity_checks`) and `hermes_s2s/tools.py:126-133`
(`s2s_doctor` is a **sync** handler registered via `ctx.register_tool`).
**What's wrong:** When the LLM invokes `s2s_doctor` from within the
gateway — which is the advertised use case in schemas.py:38-43 ("Use
when the user asks 'is my voice setup working'") — Hermes is already
running an asyncio event loop. `asyncio.run()` raises
`RuntimeError: asyncio.run() cannot be called from a running event loop`.
The tool returns an error string instead of a report, and the feature is
dead for its primary LLM-facing use case. Tests pass only because they
call `run_doctor()` from pytest sync context.
**Fix:** Detect running loop and use `asyncio.get_event_loop().run_until_complete`
is not safe either. The clean fix is two-layer:
```python
def _run_probe_sync(cfg):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_probe_backend_async(cfg, timeout=5.0))
    # Running loop: delegate to a thread with its own loop.
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(
            lambda: asyncio.run(_probe_backend_async(cfg, timeout=5.0))
        ).result(timeout=8.0)
```
And add a test that runs `run_doctor(probe=True)` from inside
`asyncio.run(...)` with a mocked backend. Currently zero coverage of this
path — which is the ONLY path that matters for `s2s_doctor` as an LLM tool.

### P0-3 — `plugin.yaml` version drift
**Where:** `plugin.yaml:2` — `version: 0.1.0` while pyproject + `__init__.py`
are at 0.3.2.
**What's wrong:** Hermes's plugin loader may display / gate on this
version string. Two separate sources of truth have silently diverged
since 0.1.x. Users running `hermes plugins list` or similar will see
0.1.0, and the wave plan acceptance item #11 ("Version bumped to 0.3.2")
is objectively not met everywhere. G4 missed this file.
**Fix:** bump `plugin.yaml:2` to `0.3.2` and add an assertion in
`tests/test_smoke.py` that parses `plugin.yaml` and checks the version
matches `hermes_s2s.__version__`.

## P1 must-fix-before-tag

### P1-1 — Doctor probe passes the wrong `voice` argument
**Where:** `hermes_s2s/doctor.py:350`:
`getattr(cfg, "realtime_options", {}).get("voice") or "Aoede"`
**What's wrong:** `realtime_options` is the full realtime dict
`{"provider": "gemini-live", "gemini_live": {"voice": "Aoede"}}` (see
`config/__init__.py:69`). `.get("voice")` at the top level is always
`None`, so the probe always connects with hard-coded `"Aoede"`. Harmless
for Gemini (Aoede is a real voice). On an OpenAI probe, Aoede is a
**Gemini** voice name and the backend may reject it (voice validation
happens server-side).
**Fix:** resolve voice from the provider-specific sub-dict, e.g.:
```python
opts = getattr(cfg, "realtime_options", {}) or {}
provider = (opts.get("provider") or "").lower()
subkey = "openai" if "openai" in provider else "gemini_live"
voice = (opts.get(subkey, {}) or {}).get("voice") or (
    "alloy" if "openai" in provider else "Aoede"
)
```

### P1-2 — `bridge.stats()` exists but nothing exposes it to users or the LLM
**Where:** `hermes_s2s/tools.py:36-71` (`s2s_status`) vs
`hermes_s2s/_internal/audio_bridge.py:392-405` (new `bridge.stats()`).
**What's wrong:** G3 wired stats into the bridge but never exposed them
through the existing diagnostic surface. `s2s_status` — the tool users
and the LLM already know about — is unchanged. So when the debounced
"dropped 100 frames" WARNING fires in production logs, the user has no
in-product way to confirm it's still climbing. The wave plan explicitly
says "backpressure visibility"; the plumbing stops one hop short of the
consumer. Not a P0 because the log line exists, but it ships a half-done
feature.
**Fix:** in `s2s_status`, if there is a live bridge instance (track one
globally or pass through `kwargs`), add a `"bridge_stats": bridge.stats()`
key to the output dict. At minimum, extend the `s2s_doctor` JSON report
with a `"runtime"` category that reports current bridge stats when a
bridge is active.

### P1-3 — Doctor WS probe close can leak a WebSocket on slow shutdown
**Where:** `hermes_s2s/doctor.py:377-380`.
**What's wrong:** On the happy path the probe does
`await asyncio.wait_for(backend.close(), timeout=2.0)` and swallows any
exception. If `close()` exceeds 2 s (network hang, TLS shutdown stall),
the underlying websocket is abandoned — the `TimeoutError` is caught,
the coroutine is cancelled but `self._ws` in the backend lives on. Not
catastrophic for a one-shot CLI but meaningful when `s2s_doctor` is
called repeatedly from the LLM (each call can leak one WS per slow
shutdown). `_probe_backend_async` also doesn't `backend._ws = None` or
otherwise guarantee reference is dropped.
**Fix:** after `asyncio.wait_for(close(), 2.0)` in both branches, do a
hard reference drop: `backend._ws = None` inside a try/except, and log a
debug if the close timed out so it's observable.

### P1-4 — `hermes plugins enable` precondition not documented in the 30-second path
**Where:** `README.md:26-38`.
**What's wrong:** The 4-command happy path assumes `hermes plugins enable`
resolves `hermes-s2s` from pip-installed packages. True in practice
because command 1 (`pip install`) happened first. But the README says
nothing about what happens when:
- User is in a venv Hermes doesn't know about → `hermes plugins enable`
  exits nonzero with no plugin found.
- User has Hermes installed globally but hermes-s2s in a venv → same.
- User installed into `~/.hermes/plugins/` by hand (the other supported
  path per the 0.2.0 wave) → `hermes plugins enable` output is different.
The 30-second install is the marketing promise; the moment it breaks,
the whole plug-and-play narrative collapses. INSTALL.md §Common errors
doesn't cover this either.
**Fix:** add one sentence to the 30-second block: "Both commands must
run in the same Python environment Hermes uses" and add a troubleshooting
entry to INSTALL.md for "plugin not found after `hermes plugins enable`".

### P1-5 — `send_filler_audio` on Gemini: correct shape, untested under concurrent audio send
**Where:** `hermes_s2s/providers/realtime/gemini_live.py:393-401`; test at
`tests/test_gemini_live.py:238-275`.
**What's wrong:** Shape is correct per ai.google.dev/gemini-api/docs/live-guide
(`clientContent.turns[*].parts[*].text` + `turnComplete: true`) —
verified against the canonical docs. But: the docstring says
"fire-and-forget", and the WS `send` is not wrapped in any lock. The
OpenAI backend has `_send_lock` (openai_realtime.py:65, 85-91); the
Gemini backend has no equivalent. `websockets` library permits
concurrent `send` calls but interleaves frames arbitrarily; the
realistic case is filler being interleaved mid-audio-chunk on a second
line of the same WS. Usually harmless but a lurking bug.
**Fix:** add an `asyncio.Lock` in `GeminiLiveBackend.__init__` and guard
`_ws.send` in `send_audio_chunk`, `inject_tool_result`,
`send_filler_audio`, `interrupt` with it. Mirror the OpenAI pattern
exactly.

## P2 should-fix-soon

- **doctor.py:258** — `HERMES_S2S_MONKEYPATCH_DISCORD` read from
  `os.environ` at doctor-time, not from `~/.hermes/.env`. A user who
  ran `hermes s2s setup --profile realtime-gemini` 30 s ago and did NOT
  restart their shell will see a false `⚠` here even though the setup
  wizard just wrote the line. Consider parsing the .env file too, or
  documenting the shell-restart requirement at the check-failure site.
- **doctor.py:433** — probe runs with a hard-coded 5 s timeout and no
  way for CI to tune it. Add a `timeout` kwarg to `run_doctor`.
- **audio_bridge.py (stats test)** — `test_buffer_stats_returns_expected_keys`
  asserts `set(s.keys()) == {…}` with `==`; future additive fields will
  break the test. Use `>=` (subset).
- **INSTALL.md:109-112** — the `InvalidStatusCode` name was deprecated in
  `websockets` 13+ in favor of `InvalidStatus`. Error text users will
  actually see may not match the doc.
- **README.md:48** — the docs link uses `baladithyab/hermes-s2s` while
  other locations (e.g., `plugin.yaml:6`) point at `codeseys/hermes-s2s`.
  One of these will 404 once the repo is canonicalized.
- **cli.py:109-113** — `_print_realtime_checklist` tells users to
  `pip install hermes-agent[messaging]` to get `discord`. Worth
  cross-checking that `[messaging]` is the current Hermes extra name.

## What's good

- **Filler-audio Gemini shape is correct.** Cross-checked against
  ai.google.dev/gemini-api/docs/live-guide (Python + JS examples both
  show `sendClientContent({turns, turnComplete})` which serializes to
  exactly the wire frame here). Sending from a raw WS without the SDK
  is the risky path; the shape nails it.
- **`_append_monkeypatch_env` idempotency is genuinely bulletproof.**
  Marker-line pattern + double check (`_MONKEYPATCH_MARKER in existing
  or _MONKEYPATCH_LINE in existing`) means the user can run setup 10
  times and the .env stays clean. The test
  `test_setup_realtime_idempotent_env_append` confirms it. Nothing to
  add here.
- **Doctor categorization + format_human layout is a real UX upgrade.**
  Six clearly-named categories, every check carries a `remediation`
  string, isatty-gated ANSI keeps CI logs clean. This is the kind of
  thing that takes a support-ticket volume from 10/week to 1/week.
- **Integration test (`test_bridge_integration.py`) is the right test.**
  It catches what unit tests miss — real resample path, real bridge
  loop, task-leak check on close. This was the missing piece from 0.3.1.

---

*Reviewed by Claude family. Recommend: block tag on P0-1, P0-2, P0-3;
 address P1s within the 0.3.2 window before announcing; batch P2s into
 0.3.3 housekeeping.*
