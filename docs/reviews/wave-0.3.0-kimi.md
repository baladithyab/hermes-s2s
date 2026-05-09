# Wave 0.3.0 Cross-Family Cold Review — Kimi / Moonshot

**Commit:** `b5c9c11` — feat(0.3.0): realtime backends + audio resample + discord bridge
**Reviewer perspective:** packaging / dependency surface, error UX, version coherence, copy-pasted-from-research-doc smells.
**Scope:** *different* from Anthropic (protocol correctness) and Gemini (edge cases). This pass focuses on what the user actually sees at install time, at config time, and when things break mid-call.

---

## CONFIRMED (works as advertised)

- **Lazy imports are disciplined.** `gemini_live.py` and `openai_realtime.py` both import `websockets` *inside* `connect()` (L109 / L100) and defer `hermes_s2s.audio.resample` until a non-native sample rate is seen. Module import is clean with or without optional deps; `python -c "import hermes_s2s.providers.realtime.gemini_live"` does not fault on a no-websockets host.
- **Provider registry registration is defensive.** `providers/__init__.py::register_builtin_realtime_providers()` wraps each `register_*` call in `try/except Exception as exc: logger.debug(...)`. A missing transitive dep at factory-import time quietly drops that backend from the registry instead of taking down the plugin — matches the stated "partial environments work" goal.
- **`resample_pcm` fails loudly, not silently.** `audio/resample.py` L86-91 wraps `from scipy.signal import resample_poly` in try/except and re-raises `ImportError(_SCIPY_HINT)` with an actionable install hint (`pip install 'hermes-s2s[audio]'`). Same-rate passthrough and mono↔multi mixdown work without scipy — 2 of 5 tests pass without scipy as advertised.
- **OpenAI 30-minute cap is surfaced, not swallowed.** `recv_events` detects both abnormal `ConnectionClosed` (L249) AND clean server-initiated close (L264) and emits `RealtimeEvent(type="error", payload={"reason": "session_cap", ...})`. The caller has enough information to decide reconnect. Good.
- **Tool-result injection matches vendor quirk.** OpenAI sends both `conversation.item.create` AND `response.create` (L291-301) — the research doc's #1 gotcha is honored. Gemini sends only `toolResponse` and relies on auto-resume (L370-377) — also correct.
- **Discord bridge is a layered no-op by default.** Three-strategy install in `_internal/discord_bridge.py::install_discord_voice_bridge` with idempotency marker and version gate. Default behavior is a single info log — cannot break a user who never opted in.
- **Tests: 40 pass, 4 skip-on-no-scipy.** `pytest -q` clean locally on this box.

---

## ISSUES

### I1. Version is still `0.2.0` — must bump before tagging v0.3.0. **BLOCKER.**

`pyproject.toml:7` → `version = "0.2.0"`.
`hermes_s2s/__init__.py:24` → `__version__ = "0.2.0"`.
Commit subject is `feat(0.3.0): …` and the wave plan is `wave-0.3.0-…`. Shipping as-is means `pip install hermes-s2s==0.3.0` fails and `hermes_s2s.__version__` lies to anyone sniffing it from the status tool.

**Fix:** bump both locations to `0.3.0` *before* the tag. Add a `CHANGELOG.md` entry if one exists; none is present in this wave.

### I2. `[realtime]` extra is an empty list — install hint is misleading. **HIGH.**

`pyproject.toml` declares `realtime = []` with a comment "uses websockets + httpx (already in core deps)". That is consistent with itself, but both realtime backends raise errors advising `pip install 'hermes-s2s[realtime]'` (gemini_live.py:114) or `pip install websockets` (openai_realtime.py:105). Since `websockets>=12.0` is *already* a core `dependencies` entry, those error paths are practically dead — but if they *do* fire (broken venv, frozen image), the suggested remediation installs nothing extra. A user who follows the advice will be back at the same error.

**Options:**

1. Move `websockets>=12.0` out of core `dependencies` and into the `realtime` extra. Net effect: a smaller core footprint; users who only want the local `moonshine+kokoro` pipeline don't pull a websocket lib they'll never use. Then the existing error strings become truthful. This is the design the comments in pyproject clearly *wanted*.
2. Keep core as-is and rewrite the error messages to say "the `websockets` package is a core dependency and should already be installed; try `pip install --force-reinstall hermes-s2s`."

Option 1 is cleaner and matches the stated intent ("uses websockets + httpx"). Either is fine; shipping the current mismatch is not.

### I3. Stale `0.1.0` / `0.2.0` strings baked into user-visible text. **MEDIUM.**

- `hermes_s2s/providers/realtime/__init__.py:59` — `_BaseRealtimeBackend.connect` raises `"{self.NAME} realtime backend is a stub in this 0.1.0 release."` This error message ships in 0.3.0 and contradicts the actual release. Unreachable for `GeminiLiveBackend` (overrides) but still visible if anyone subclasses `_BaseRealtimeBackend` without overriding.
- `hermes_s2s/audio/__init__.py:1` — module docstring says `"(TBD in 0.2.0)"`. The module is now implemented; docstring is lying.
- `hermes_s2s/providers/pipeline/s2s_server.py:29, 49` — still advertises itself as `"Stub for 0.1.0"` and raises `"stub in 0.1.0"` in `NotImplementedError`. If a user configures `mode: s2s-server` in 0.3.0 they'll see a 0.1.0-dated error.
- `hermes_s2s/skills/hermes-s2s/SKILL.md:4,149,157` — YAML front-matter `version: 0.1.0`, troubleshooting paragraph says "Realtime backends are stubs in this release" (they aren't anymore), and "Wire the Discord/CLI hooks (planned, 0.2.0) to actually route through this plugin" (that's this wave's work — should be updated or removed).

**Fix:** grep-and-replace sweep before tagging. These are the clearest copy-paste-from-previous-wave-docs smell.

### I4. OpenAI docstring disagrees with its own code. **LOW, but exact case of "copy-pasted-from-research-doc".**

`openai_realtime.py` module docstring (L13-15):

> When the server closes the WS we surface a ``session_resumed`` event with ``type='error'`` …

The actual code (L256-259, L268-274) emits `RealtimeEvent(type="error", …)` — a plain `error` event, never `session_resumed`. The docstring confuses two things that the research doc listed side-by-side. Either update the docstring to say "error event with `reason='session_cap'`", or (worse) retrofit an extra `session_resumed` event. Pick one; current state misinforms readers.

### I5. `GeminiLiveBackend` inherits from `_BaseRealtimeBackend`, `OpenAIRealtimeBackend` does not. **LOW / consistency.**

Two backends, two base-class strategies. `_BaseRealtimeBackend.__init__` stores `self.config = kwargs`; `GeminiLiveBackend.__init__` also unpacks the same kwargs into named attributes (so `self.config` and `self.api_key_env` are duplicated state). `OpenAIRealtimeBackend` uses named constructor args and ignores the base. Pick one pattern and apply to both, or delete `_BaseRealtimeBackend` — it now only contributes a stale `"stub in this 0.1.0 release"` error message and zero enforcement (the `RealtimeBackend` `Protocol` already documents the contract).

### I6. Three functions over 80 lines — split before they grow further. **LOW, code smell.**

- `gemini_live.py::_translate_server_msg` — 94 lines. Fans one server frame out to an event list, but is really three sequential parsers (serverContent, toolCall, sessionResumptionUpdate). Split into `_parse_server_content`, `_parse_tool_call`, `_parse_session_resumption` and have the dispatcher call them in order.
- `openai_realtime.py::recv_events` — 110 lines. The event-type `if/elif` ladder is the obvious extraction target (`_translate_openai_event(msg) -> list[RealtimeEvent]`).
- `providers/stt/moonshine.py::transcribe` — 89 lines (pre-existing, not new in 0.3.0; noted for completeness).

Nothing here is hair-on-fire; flag for the next refactor pass.

### I7. `setup["sessionResumption"] = {}` set unconditionally. **LOW / research-doc literalism.**

`gemini_live.py:176-179`:

```python
if self._session_handle:
    setup["sessionResumption"] = {"handle": self._session_handle}
else:
    setup["sessionResumption"] = {}
```

The empty-dict branch exists because the reference docs list `sessionResumption` as a setup field. Sending `{}` asks Gemini to issue a fresh resumption handle — that IS the intent — but a naive reader sees the empty-dict assignment and assumes it was left over from a template. Either add a one-line comment ("# empty dict opts in to resumption; server returns a handle to reuse on reconnect") or collapse to always-set.

---

## QUESTIONS

1. **Should `server-client` extra also be empty?** `pyproject.toml:36` — `server-client = []`. Same logic as `realtime = []`. If the rationale is "websockets is core", fine, but if the idea is to eventually move websockets to an extra (I2), then both extras need to gain `websockets>=12.0`. Decide once.
2. **Why does `httpx` live in core?** It's only imported by `providers/stt/s2s_server.py` and `providers/tts/s2s_server.py` — stage-only s2s-server shims. A user on pure local `moonshine+kokoro` never uses it. Could move to the `server-client` extra alongside websockets for a tighter core. Out of scope for 0.3.0 but worth considering.
3. **Discord bridge with `HERMES_S2S_MONKEYPATCH_DISCORD=1` + `s2s.mode: realtime` logs "would bridge audio here".** Is this going to be prominent enough in release notes that a user won't think Discord-realtime is *done* in 0.3.0? The commit message says "Stub bridge audio loop (0.3.1 polish)" — good. But the `SKILL.md` still claims "planned, 0.2.0" and needs to say "setup plumbing only in 0.3.0; audio loop in 0.3.1" for this to be honest.
4. **OpenAI realtime `additional_headers` → `extra_headers` TypeError fallback** (L116-119). On `websockets>=14`, `extra_headers` was removed entirely — and the TypeError path there will itself raise a different error. If you want true cross-version support, catch `TypeError` *and* `DeprecationWarning` / pin `websockets>=13` in deps. Or drop the fallback: core already pins `websockets>=12` where `additional_headers` is supported.

---

## Specific-check answers

1. **No `[realtime]` extra + `s2s.mode: realtime`.** Because websockets is in *core*, the user actually gets websockets and the backend connects. If they pip-installed from a broken wheel and websockets truly is missing, they see `"gemini-live backend requires the 'websockets' package. Install with: pip install 'hermes-s2s[realtime]'"` — which is *not* actionable because `[realtime]` is empty. See I2.
2. **Provider registry registration without websockets.** Yes — succeeds. `register_builtin_realtime_providers` imports `gemini_live` and `openai_realtime` modules, neither of which imports websockets at module level. `websockets` is only touched inside `connect()`. Correct behavior.
3. **`resample_pcm` without scipy.** Fails loudly with `ImportError("scipy is required for audio resampling when src_rate != dst_rate. Install with: pip install 'hermes-s2s[audio]'")`. Actionable. ✅
4. **TODO/FIXME/XXX/HACK markers.** None found. What's there instead is "stub in 0.1.0" strings (see I3), which are arguably worse because they read like prose but are out-of-date facts.
5. **Functions over 80 lines.** Three: `_translate_server_msg` (94), OpenAI `recv_events` (110), moonshine `transcribe` (89 — pre-existing). See I6.
6. **Version 0.2.0 in pyproject.** Yes, still 0.2.0. Must bump to 0.3.0. See I1.

---

## Go / No-Go for v0.3.0

**NO-GO as-is.** Two blockers before the tag:

- **I1** — bump `pyproject.toml` and `__init__.py` to `0.3.0`. Tagging `v0.3.0` on a `0.2.0` sdist is a footgun.
- **I3** — scrub the `0.1.0` / `0.2.0` strings from `providers/realtime/__init__.py`, `audio/__init__.py`, `providers/pipeline/s2s_server.py`, and `SKILL.md`. Users *will* read these.

**GO after** those two sweeps + preferably **I2** (either move websockets to the `realtime` extra, or rewrite the install-hint error strings to match reality) and **I4** (one-line docstring correction). The rest (I5, I6, I7, all QUESTIONS) can slip to 0.3.1.

Ship-quality of the actual realtime protocol work is good: tool-result injection honors both vendor quirks, 30-minute cap surfaces cleanly, resampling is lazy and fails loud. The packaging/versioning metadata is where this wave drops the ball.
