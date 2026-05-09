# Wave 0.3.1 — Cross-Family Cold Review (Kimi / Moonshot)

**Commit reviewed:** `1f10c55` — *feat(0.3.1): audio bridge actually moves audio + tool-call bridging*
**Diff size:** 15 files, +2 204 / −52
**Reviewer:** third independent perspective (Kimi/Moonshot family), after Claude + Gemini.

**Verdict: 🟡 CONDITIONAL GO for `v0.3.1`.** The packaging, error envelopes, and
new test coverage are real. However **one P0 lifecycle bug** (bridge never
calls `backend.connect`) makes the happy-path demo advertised in the README
fail silently in production, and a clutch of user-visible "0.3.0" strings make
the release look un-proofread. Fix the two P0s below and this is a clean tag.

---

## ✅ CONFIRMED

### C1. Packaging compiles cleanly under every profile
* `python -m compileall hermes_s2s/` → exit 0 with **no** scipy, **no** discord,
  **no** websockets installed of the four new files.
* `python -c "import hermes_s2s._internal.{audio_bridge,tool_bridge,discord_bridge,discord_audio}"`
  imports clean; filter `sys.modules` for real third-party pulls returns `[]`.
  Lazy-import discipline is correct on all four new files.
* Full `pytest` suite: **58 passed, 4 skipped** in 2 s on an env with
  `websockets` + `discord.py` but **no scipy** (i.e. `[realtime]` only). No
  regressions from 0.3.0.
* `pyproject.toml` profile shape is coherent: `[realtime]` → websockets;
  `[local-all]` → moonshine+kokoro+audio (adds scipy); `[all]` → every extra
  including scipy and websockets. No orphaned or duplicated dependencies.

### C2. Version coherence on the shipping surface
* `pyproject.toml` → `0.3.1` ✅
* `hermes_s2s/__init__.py::__version__` → `0.3.1` ✅
* `tests/test_smoke.py` guard → asserts `== "0.3.1"` ✅
  (Unlike 0.3.0's Claude/Kimi review, there's no version-drift blocker this wave.)

### C3. Tool-bridge behaviour is close to ADR-0008
* Soft timeout (5 s) → `asyncio.shield` keeps the tool running, fires
  `backend.send_filler_audio` (with `NotImplementedError` swallowed — sane
  given README acknowledges the stub). Hard timeout (30 s) cancels cleanly
  and returns `{"error": "...", "retryable": true}`.
* Result truncation is **byte-safe** (`encoded[:N].decode(errors='ignore')`);
  no UnicodeDecodeError footgun.
* `cancel_all()` idempotent; sync + async dispatch both supported.
* 6 new tool-bridge tests + 8 audio-bridge tests + 3 discord-bridge tests green.

### C4. Error UX — smoke_realtime.py
Running with unset key is the *good* path here:
```
$ env -u GEMINI_API_KEY python3 scripts/smoke_realtime.py --provider gemini-live
error: GEMINI_API_KEY not set in environment
$ echo $?
1
```
No stack trace, exits 1, tells the user exactly which env var. ✅

### C5. BridgeBuffer correctness (the bit that actually moves audio)
* Drop-oldest input backpressure is implemented correctly, including the
  double-Full race branch.
* `read_frame()` returns `SILENCE_FRAME` (3 840 bytes) on underflow — never
  `b""`. That's the single most important invariant for discord.py's player
  thread (per research/07) and it's airtight.
* Fractional-remainder held across `push_output` calls — no mid-stream
  zero-padding clicks.
* `pop_input` uses cooperative polling instead of `run_in_executor(get)` —
  this is the **right** fix for the "executor thread wedged on blocking get
  after task cancel" shutdown-cleanliness problem. Nice.

### C6. Idempotent monkey-patching of discord internals
`_BRIDGE_WRAPPED_MARKER`, `_S2S_LEAVE_WRAPPED_MARKER`, and
`_FRAME_CB_INSTALLED_MARKER` all prevent stacked wrappers across reloads.
The `before/after` length-diff on `_buffers[ssrc]` correctly extracts only
the PCM appended by the current Opus frame. Good defensive engineering.

---

## ❌ ISSUES

### I1. **P0 — `backend.connect()` is never called.** `_attach_realtime_to_voice_client`
resolves the backend, builds the `RealtimeAudioBridge`, then calls
`bridge.start()` which launches `_pump_input` + `_pump_output`. **Nothing ever
awaits `backend.connect(system_prompt, voice, tools)`.** Consequence:

* `_pump_input` → first frame → `backend.send_audio_chunk(...)` → for
  gemini_live: `raise RuntimeError(f"{self.NAME}: not connected")`
  (`gemini_live.py:197-198`). Caught by the pump's broad `except`, logged,
  loop continues looping forever emitting the same log line per user frame.
* `_pump_output` → `backend.recv_events()` immediately raises the same
  "not connected" RuntimeError (`gemini_live.py:231-232`). Caught, pump exits.
* User sees: bot joins, silence, nothing works, log-stream full of
  "backend.send_audio_chunk failed". The README promise ("realtime audio
  actually flows through Discord") does not hold for the happy path.

This is the exact regression the Gemini reviewer warned about last wave on a
different axis (bridge-not-really-wired). **Must fix before tag.** Minimum
fix: in `_attach_realtime_to_voice_client`, before
`asyncio.run_coroutine_threadsafe(bridge.start(), loop)`, schedule a
`connect()` via the same loop, and wait for it (or defer `start()` into a
coro that does `await backend.connect(...)` first). Bonus: surface connect
errors to the user (log `.error` with the actual exception + remediation).

### I2. **P0 — User-visible "0.3.0" strings in six production files.**
All of these are reachable via configuration mistakes users will actually
make, and each one says the wrong version:

| File | Line | String |
|---|---|---|
| `hermes_s2s/providers/realtime/__init__.py` | 60 | `"is a stub in this 0.3.0 release"` — reachable by any 3rd-party subclass of `_BaseRealtimeBackend` that forgets to override `connect` |
| `hermes_s2s/providers/pipeline/s2s_server.py` | 29, 49 | `"Stub for 0.3.0"` / `"stub in 0.3.0"` — reached when a user sets `mode: s2s-server` in 0.3.1 |
| `hermes_s2s/providers/stt/s2s_server.py` | 34, 59 | `"mode is 0.3.0+"` in both warning log and `SttUnavail` exception |
| `hermes_s2s/providers/tts/s2s_server.py` | 39, 64 | same pair for TTS |
| `hermes_s2s/audio/__init__.py` | 1 | docstring `"(implemented in 0.3.0)"` |

Fix: bulk rename to `0.3.1` in the user-facing paths. For `s2s_server` stubs
(which *still* aren't implemented) the string should be "planned for a
future release" rather than naming any version. Bonus: update
`tests/test_s2s_server_provider.py:57/62/64` which currently hard-matches
`"0.3.0"` and would have to be relaxed to `"WebSocket"` or a version regex.

### I3. **P1 — `SKILL.md` version header still `0.3.0` and body contradicts 0.3.1.**
`hermes_s2s/skills/hermes-s2s/SKILL.md:4` → `version: 0.3.0`. Plus the
troubleshooting paragraph (line 149) says *"realtime Discord audio bridging
is setup-plumbing only in 0.3.0 — audio routing lands in 0.3.1"*, which is
now **misleading present-tense narrative**: this *is* 0.3.1. Pitfalls
paragraph (line 157) repeats the same claim. Users who hit this skill via
`ctx.register_skill` will read that realtime doesn't work yet, which is the
opposite of the pitch in README.md.

### I4. **P1 — Stale "0.3.1's audio loop" stub comment in discord_bridge.py.**
`discord_bridge.py:544` → `"Real implementation lands alongside 0.3.1's
audio loop."` That's now a time-travelling claim. The `_StubVoicePipeline`
is still a no-op (only reachable via the imaginary
`ctx.register_voice_pipeline_factory` upstream hook that doesn't exist yet,
so it's benign in practice). Change the comment to "Lands when the upstream
VoicePipelineFactory hook ships."

### I5. **P2 — Two long functions in `_internal/discord_bridge.py`.**
* `_attach_realtime_to_voice_client`: **124 lines** (line 259). Reasonable
  because it's literally the ADR-0007 flow in one place, but it would read
  much better split into `_build_bridge_for_vc`, `_wire_outbound`, and
  `_start_on_loop`. Ideal moment to do that split is the I1 fix (since that
  same function needs the `await backend.connect` plumbing).
* `_install_frame_callback`: **96 lines** with three branches (preferred
  path, already-installed, monkey-patch). The nested `wrapped_on_packet`
  closure is the longest part; extracting it to a module-level function
  parameterised by the receiver would cut ~30 lines and make the control
  flow legible.
None of the other new functions exceed 60 lines.

### I6. **P2 — Two branches of `_pump_input` are identical.**
`audio_bridge.py:355-371` — both the same-rate and different-rate paths call
`resample_pcm(..., dst_channels=1)` with identical args. The comment says
"Same-rate path still needs stereo→mono mixdown" but both branches already
pass `dst_channels=1`, so the `if` is dead. Collapse to one call, drop the
`if/else`. Pure dead-code.

### I7. **P2 — README Status section is mostly honest but slightly over-sells.**
The paragraph leads with *"realtime audio actually flows through Discord"*
and *"`/voice join` bridges a Discord VC end-to-end"*. Given I1 (bridge
never connects), the honest version today is *"plumbing is complete; end-to-
end connectivity requires the lifecycle fix in I1"*. Once I1 lands the copy
is accurate. The "Known issues in 0.3.1" block correctly flags single-user,
filler-audio stub, and the monkey-patch flag requirement — that's the
right tone, just under-scoped by one bug.

---

## ❓ QUESTIONS

1. **`HermesToolBridge` + `inject_tool_result`.** The bridge dispatches the
   tool and truncates the result, but nowhere does it call
   `backend.inject_tool_result(call_id, result)`. The audio-bridge dispatch
   path calls `await self.tool_bridge.handle_tool_call(backend, call_id, ...)`
   whose return value (the truncated result string) is simply dropped. Is
   the expectation that backends auto-inject during `handle_tool_call`?
   Neither the ADR-0008 summary in the docstring nor the test
   (`test_tool_bridge.py`) calls `backend.inject_tool_result`. Looks like an
   unwired loop-back.

2. **`_voice_pipeline_factory` stub.** `_StubVoicePipeline` is dead code in
   0.3.1 (no `register_voice_pipeline_factory` path in Hermes today). Keep
   as forward-compatibility scaffolding, or excise until the upstream hook
   exists? I'd lean excise to reduce review surface, but either is fine if
   the comment in I4 gets updated.

3. **Single-guild fallback in `leave_voice_channel_wrapped`.** When
   `guild_id` can't be resolved, the wrapper `popitem()`s whichever bridge
   is present. Reasonable for single-guild dev, but in a multi-guild host
   this will close the *wrong* bridge. Worth a `logger.warning` at minimum.

4. **`websockets` version pin.** `pyproject.toml` pins `websockets>=12.0`
   but the public API changed meaningfully between 12 and 15 (see
   `connect()` ctx-manager vs coroutine behaviour). Gemini/OpenAI backends
   use the legacy coroutine form — confirm that still works on 15.x? My env
   has 15.0.1 and tests green, so probably fine, but worth a quick grep.

5. **`INPUT_QUEUE_MAX = 50` (~1 s).** The `research/09` note mentions users
   in busy servers can sustain 1–2 s of input before backpressure kicks in.
   Is 1 s definitely the right headroom vs. latency tradeoff? Not blocking,
   curious about the rationale.

---

## 🏷 Go / No-Go for `v0.3.1`

**CONDITIONAL GO.** Two P0 fixes required before tag:

1. **I1** — Wire `await backend.connect(system_prompt, voice, tools)` into
   the bridge start-up in `_attach_realtime_to_voice_client`, with
   user-visible error reporting on connect failure. Ideally add a test
   that fails today: mock a backend whose `send_audio_chunk` asserts
   `connect()` was awaited first.
2. **I2** — Bulk-rename the six user-facing `0.3.0` strings in the
   production providers + audio docstring, adjust the two
   `test_s2s_server_provider.py` asserts accordingly.

**Nice-to-have before tag** (non-blocking):
* **I3** — SKILL.md version header + narrative refresh.
* **I4** — fix the "0.3.1's audio loop" stub comment.
* **I6** — remove dead `if/else` in `_pump_input`.

Once I1 + I2 are in, this is a clean tag. The underlying audio-bridge
engineering (BridgeBuffer, frame-callback shim, tool-bridge timeouts, 17
new tests) is genuinely solid work — the regressions are all at the
integration seams, not in the new primitives. Ship it with the fixes.

— Kimi / Moonshot reviewer, 2026-05-09
