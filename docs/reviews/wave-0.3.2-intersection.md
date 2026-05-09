# 0.3.2 Phase 8 — Cross-family intersection

## Reviewers
- Claude (`docs/reviews/wave-0.3.2-claude.md`, 236 lines)
- Gemini (`docs/reviews/wave-0.3.2-gemini.md`, 262 lines)
- Kimi   (`docs/reviews/wave-0.3.2-kimi.md`,  297 lines)

## P0 — must fix before tag

### P0-1 (Claude+Gemini+Kimi triple-flag) — `s2s_doctor` LLM tool crashes from gateway

`hermes_s2s/tools.py::s2s_doctor` calls `run_doctor()` which calls `asyncio.run(_probe_backend_async(...))` at `doctor.py:433`. When invoked from Hermes gateway (always in a running event loop), `asyncio.run` raises `RuntimeError: asyncio.run() cannot be called from a running event loop`. Probe coroutine leaks (RuntimeWarning).

**Fix**: detect running loop in `_probe_backend_async` dispatcher; use `asyncio.get_running_loop()` + `loop.run_until_complete` is wrong too — instead, make `run_doctor` accept an optional `_in_async_context` mode where the probe is awaited from the caller, OR run the probe in a thread via `asyncio.to_thread`/dedicated executor when called from a running loop.

**Cleanest fix**: split `run_doctor` into a sync core + async probe. `s2s_doctor` tool handler becomes async (Hermes tool registry supports async handlers). CLI wrapper stays sync via `asyncio.run`.

### P0-2 (Claude+Kimi) — OpenAI filler audio missing `conversation: "none"`

`hermes_s2s/providers/realtime/openai_realtime.py::send_filler_audio` sends `response.create` with `instructions` override but WITHOUT `conversation: "none"`. The actual use case (mid-response tool timeout) requires out-of-band response that doesn't pollute history. Per OpenAI Realtime docs, GA `gpt-realtime` requires `conversation: "none"` for OOB responses. Without it, the response either pollutes history OR gets rejected during in-flight tool calls. Test rubber-stamps the buggy shape.

**Fix**:
```python
{
  "type": "response.create",
  "response": {
    "conversation": "none",
    "output_modalities": ["audio"],   # GA name; "modalities" is legacy
    "instructions": f"Briefly say: {text}",
  }
}
```
Update test to assert `conversation == "none"` and `output_modalities` (or assert presence of either field for GA-compat).

### P0-3 (Claude) — `plugin.yaml` still 0.1.0

`plugin.yaml` has `version: 0.1.0` while pyproject + `__init__` are 0.3.2. Violates acceptance criterion #11.

**Fix**: bump `plugin.yaml` to 0.3.2.

### P0-4 (Kimi) — README "30-second install" `pip install hermes-s2s[all]` doesn't work yet

Package isn't published to PyPI. Fresh user runs the literal command and gets `ERROR: Could not find a version`. ROADMAP confirms PyPI publish is unchecked.

**Fix options**:
- (a) Publish to PyPI now (out of scope for 0.3.2).
- (b) Change README to `pip install 'hermes-s2s[all] @ git+https://github.com/baladithyab/hermes-s2s.git'` for the 30-second install.
- (c) Document `git clone + pip install -e '.[all]'` as the install path until PyPI lands.

Going with (b) — single command, no clone needed, works today. Bonus: pin to `@v0.3.2` tag once we ship.

## P1 — fix before tag

### P1-1 (Claude+Gemini+Kimi) — Doctor probe voice resolution buggy + uses wrong field shape

`doctor.py:350` does `cfg.realtime_options.get("voice")` but voice is nested under `gemini_live`/`openai`. Plus defaults to `"Aoede"` even for OpenAI where it's an invalid voice name. Probe uses unrepresentative connection params.

**Fix**: read voice from the right nested location based on active provider. If missing, use provider-appropriate defaults (Aoede for Gemini, alloy for OpenAI).

### P1-2 (Claude) — `bridge.stats()` not surfaced through `s2s_status`

G3 built `BridgeBuffer.stats()` and `RealtimeAudioBridge.stats()` but `s2s_status` tool wasn't extended to call them. Plumbing complete but disconnected from users/LLM.

**Fix**: extend `s2s_status` to include bridge stats when an active bridge exists. Probe `ctx` for active bridge or use a registry pattern.

### P1-3 (Kimi) — Phantom extras `[local-stt]`/`[local-tts]` in remediation hints

`doctor.py:134-137` + `cli.py:212,215` hint at extras `local-stt`/`local-tts` that don't exist in `pyproject.toml`. Real names are `moonshine`, `kokoro`.

**Fix**: replace remediation strings with the real extras names.

### P1-4 (Kimi) — `_detect_missing` doesn't check realtime extras

User without `[realtime]` extra installed runs `hermes s2s setup --profile realtime-gemini` → wizard writes config cleanly → at runtime `ModuleNotFoundError`. Wizard should warn.

**Fix**: in the realtime profile pre-write checklist, check `find_spec('websockets')`. Already implicitly covered in doctor but needs to be in wizard too.

### P1-5 (Gemini) — `test_setup_realtime_warns_on_missing_api_key` is a false positive

Test asserts on a substring printed regardless of env var state. Removing `monkeypatch.delenv` still passes.

**Fix**: assert on the `[✗]` marker prefix specifically when env var is missing, OR assert on the warning-color emission, OR diff the output between set/unset cases.

### P1-6 (Gemini+Kimi soft P2) — Doctor probe doesn't wait for first server event

Per the plan: "wait up to 5s for first event or just-connected state". Current impl just awaits `connect()` return. Missing real readiness check.

**Fix**: after `await backend.connect(...)`, do `await asyncio.wait_for(anext(backend.events()), timeout=2.0)` and treat the timeout as a soft warning ("connected but no events received"), not a fail.

## P2 — defer to 0.3.3 unless trivial

- (Claude P2) Hardcoded 5s probe timeout — make a CLI flag.
- (Claude P2) Brittle `==` set assertion in stats test — use `>=` superset.
- (Claude P2) GitHub URL mismatch `baladithyab` vs `codeseys` — verify with grep, fix if mismatched.
- (Gemini P2) `ctypes.util.find_library` side-effects — minor, ignore.
- (Kimi P2) BridgeBuffer modulo debounce skips boundary on multi-drop race — fix with `>=` ranges, trivial.
- (Kimi P2) Wizard checklist prints `[✗]` but still writes config — by design (--dry-run gates).

**Decision**: address P2 items 3 and 5 in this fix wave (trivial). Defer rest to 0.3.3.

## Fix-wave commit plan

Single subagent does all P0 + P1 + listed-P2s in one commit. Reviewers stay; we just intersect.
