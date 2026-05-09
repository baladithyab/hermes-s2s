# Wave 0.3.0 — Cross-Family Cold Review (Gemini-family reviewer)

**Commit:** `b5c9c11` — "feat(0.3.0): realtime backends + audio resample + discord bridge"
**Scope:** R1 audio resample, R2-A Gemini Live, R2-B OpenAI Realtime, R3 Discord bridge.
**Stats:** 11 files, +2125 / -37. 40 pass, 4 skip (scipy-gated), 0 fail.
**Reviewer lens:** test-coverage edge cases, 30-min cap lossiness, Discord failure modes.
**Verdict:** ⚠️ **CONDITIONAL GO** — tag 0.3.0 is acceptable *only after* the
Discord-bridge silent-mute bug (ISSUE-1) is fixed. All other findings are
non-blocking.

---

## ✅ CONFIRMED

### Mock WS tests actually exercise the production code path
Spot-checked the four OpenAI tests and the four Gemini tests:

- `OpenAIRealtimeBackend.connect()` calls `websockets.connect(url, additional_headers=...)`
  (`openai_realtime.py:117`) against the fixture's real `websockets.serve` endpoint
  (`conftest.py:183`). The session-update JSON is produced by the production
  `_send_json` path (`openai_realtime.py:85-91`) and captured on the wire by
  the fixture's tee'd `ws.recv` (`conftest.py:147-152`).
- `test_happy_connect_and_audio_flow` (`test_openai_realtime.py:101-116`) then
  decodes the **raw frames** the server observed and re-parses them as JSON —
  i.e. we assert against what the real backend emitted, not against a stub.
- `test_tool_call_round_trip_sends_output_and_response_create`
  (`test_openai_realtime.py:169-192`) drives `backend.inject_tool_result()`
  and verifies **both** `conversation.item.create` and `response.create` land
  on the wire, *in the correct order* (the OpenAI-specific gotcha). This is
  the highest-value assertion in the suite.
- Gemini tests use `mock_ws_server()` scripted mode + `add_proactive` /
  `add_reply`, and drive `GeminiLiveBackend.connect()` through the real
  `websockets.connect(url)` (`gemini_live.py` connect path). Setup frame,
  tool-call round-trip (`test_gemini_live.py:134-151`), and
  `sessionResumption.handle` echo on reconnect (`:180-186`) are all observed
  on the wire.

**No short-circuits.** No test mocks `websockets.connect` itself. The backends'
real send/recv loops and JSON codecs run end-to-end.

### Fixture handles async cancellation + test failure cleanup
`conftest.py:173-192` — `pytest_asyncio.fixture` wraps `yield server` in a
`try/finally` that calls `ws_server.close()` and `await
asyncio.wait_for(ws_server.wait_closed(), timeout=2.0)`. The `timeout=2.0`
upper-bounds cleanup so a stuck server cannot hang the whole suite; any
timeout is swallowed (`:191-192`). Test body exceptions propagate after
cleanup — correct.

One minor note: per-connection handler tasks spawned inside
`_dispatch` (`conftest.py:132-170`) do their own `try/except Exception: pass`
at `:169-170` so a client that disconnects abnormally during a scripted
reply will not raise into the fixture. Good.

### `ws_server.headers` capture is correct, and Authorization validation works
`conftest.py:93-106` probes `ws.request.headers` (websockets v12+) first,
falling back to `ws.request_headers` (older). `raw_items()` is preferred so
duplicate headers aren't silently dropped. The headers are captured **once per
connection**, so only the *first* connect's headers are retained — fine for
the single-shot tests.

`test_happy_connect_and_audio_flow:100-103` asserts `authorization == "Bearer
sk-test-123"` and `openai-beta == "realtime=v1"` case-insensitively (via
`hdrs_lower`). That matches the backend's `openai_realtime.py:110-113`
exactly. Gemini passes auth via a URL query param (`?key=...`), not headers,
so the absence of a header assertion in Gemini tests is correct.

### 30-min cap reconnect *behavior* is wired correctly (but see ISSUE-2)
`openai_realtime.py:249-274` handles two close paths — raised
`ConnectionClosed` and the `async for` iterator exiting silently on a clean
close. Both surface `RealtimeEvent(type="error",
payload={"reason":"session_cap"})`. `test_session_cap_close_surfaces_error_and_reconnect_works`
(`test_openai_realtime.py:197-253`) exercises the clean-close path
(`ws.close(code=1000)`), asserts the `session_cap` event, and then reconnects
via a **new** `OpenAIRealtimeBackend` instance. Second connection succeeds;
server-side counter proves two distinct handshakes.

### Gemini `_translate_tools` unit test + session-resumption reconnect
`test_gemini_live.py:213-237` covers the JSON-schema-to-`functionDeclarations`
transform in isolation (good — separates translator from I/O).
`test_session_resumption` (`:159-188`) verifies the handle is persisted
across `close()` and re-sent on `connect()` — the happy-path reconnect story.

### Discord bridge strategy selection + idempotency
`test_discord_bridge.py` covers: (a) no-env + no-hook → no-op log, (b)
env-only → `join_voice_channel` wrapped with the `_BRIDGE_WRAPPED_MARKER`
sentinel, (c) native hook wins over env, (d) idempotent double-install.
Fake `gateway.platforms.discord` is injected via `sys.modules`
(`test_discord_bridge.py:68-84`) — avoids a real discord.py dep. Good.

---

## ❌ ISSUES

### ISSUE-1 🔴 **BLOCKER** — Discord bridge pauses Hermes audio path BEFORE the stub, causing silent mute in realtime mode

**File:** `hermes_s2s/_internal/discord_bridge.py:243-257`

In `_install_bridge_on_adapter`, when `s2s.mode == "realtime"` and a
`VoiceReceiver` exists on the adapter, the code does this sequence:

```python
# L243-250
receiver.pause()        # ← Hermes's STT/TTS path is now OFF
# L252-257
logger.info("hermes-s2s: would bridge audio here ...")   # ← stub; does nothing
```

The report's "logs would bridge audio here" description undersells the
severity. It is **not** a no-op. It is an **active regression** versus
`mode != "realtime"`:

| config | default (no env) | env + `mode='text'` | env + `mode='realtime'` (this release) |
|---|---|---|---|
| User joins VC, speaks | STT→text→TTS works | STT→text→TTS works | **bot is silent, no reply** |

The user will reasonably think "realtime mode works" because the bot joined
the channel, the log says "would bridge audio here (backend=openai-realtime)",
and *no error is raised*. In reality, the receiver is paused and no audio is
being piped to the backend. Any user who follows README/HOWTO, sets
`HERMES_S2S_MONKEYPATCH_DISCORD=1`, and flips `mode: realtime` hits a
completely mute bot.

Two acceptable fixes — either is sufficient for the 0.3.0 tag:
1. **(Preferred)** Don't pause the receiver until the real bridge is wired.
   Move the `receiver.pause()` call into the 0.3.1 audio-loop path so the
   0.3.0 stub is truly no-op.
2. Gate the whole `_install_bridge_on_adapter` body on an additional
   `HERMES_S2S_REALTIME_BRIDGE_STUB_OK=1` env var, and log **WARNING** (not
   INFO) when the stub fires: "realtime bridge is a stub; audio is NOT
   flowing. Unset s2s.mode=realtime to use the default STT/TTS path."

Either way the HOWTO should say "0.3.0 ships setup plumbing only; do not flip
`mode: realtime` on a production Discord bot before 0.3.1."

### ISSUE-2 🟡 **NON-BLOCKER** — OpenAI 30-min cap lossiness is documented in source + ADR, but NOT in the user-facing HOWTO

`docs/HOWTO-VOICE-MODE.md` contains **zero** mentions of "30-min", "session
cap", or "lossy reconnect" (grepped the whole `docs/` tree). The warning is
in:
- `hermes_s2s/providers/realtime/openai_realtime.py:13-18` (source docstring)
- `docs/design-history/adrs/0002-realtime-bridge-abstraction.md:72`
- `docs/design-history/research/{02,05}-*.md`

End users reading HOWTO-VOICE-MODE will not see it. The implementation itself
is **not** silently accepting conversation loss — it surfaces a distinct
`error` event with `reason='session_cap'` (`openai_realtime.py:256-258`,
`:268-273`) that a caller can act on. But the *default* caller behavior (not
shipped in 0.3.0) isn't specified anywhere. A single paragraph in the HOWTO
would close this.

Suggested doc text: *"OpenAI Realtime has a hard 30-minute cap per WS. When
hit, the backend emits an `error` event with `reason='session_cap'`.
Reconnection is **lossy**: OpenAI does not expose a resumption handle, so the
model's in-session memory of the conversation is not restored. Re-sending
`session.update` restores the system prompt and tools, but conversation
history must be replayed via `conversation.item.create` if continuity
matters. Gemini Live does not have this limitation — it uses server-side
`sessionResumption.handle`."*

### ISSUE-3 🟢 **COSMETIC** — `recv_events()` emits `type="error"` for session_cap, but the docstring on `:167-174` calls it a `session_resumed` event
Minor doc/code drift. The payload shape and emitted `type` are correct
(`error` with `reason='session_cap'`); only the docstring is stale. Safe
one-line fix.

---

## ❓ QUESTIONS

### Q1 — What happens on a network blip mid-call (WS dies but Discord VC stays up)?
Once `_install_bridge_on_adapter` is real (0.3.1), the backend WS can die
mid-conversation while the Discord `VoiceClient` remains connected. The
current 0.3.0 code surfaces `session_cap` only on server-side clean close.
For network errors (ECONNRESET, TLS abort, keepalive timeout), the
`except closed_exc` branch at `openai_realtime.py:249` catches it, but the
*caller* — which doesn't exist yet in 0.3.0 — must decide whether to
reconnect, tell the user "one sec, reconnecting…", or leave VC. No test
exercises the mid-stream abnormal-close path (as opposed to the clean
`ws.close(code=1000)` at `test_openai_realtime.py:209`). **Recommended
test for 0.3.1:** simulate `await ws.close(code=1011)` after already
emitting some audio deltas, assert the backend surfaces an error and doesn't
yield partial/corrupt chunks.

### Q2 — Does Discord VC stay alive in a broken state?
Yes, by design. `DiscordAdapter.join_voice_channel` (wrapped at
`discord_bridge.py:135-146`) succeeds/fails independently of any subsequent
bridge install failure — `_install_bridge_on_adapter` has broad `except
Exception` and only logs. That's the right call for "don't let a bridge bug
break VC join", but it means once the real audio loop ships, a backend WS
death will leave the user in a voice channel with a dead bot until they
manually disconnect. **Recommended for 0.3.1:** on terminal backend error,
either (a) try one reconnect, or (b) leave the VC automatically with a
user-visible message in the text channel.

### Q3 — Test coverage gap: `send_audio_chunk` with a non-24k sample rate when scipy *is* available
`test_openai_realtime.py:91` passes `sample_rate=24_000` so the resample
branch at `openai_realtime.py:151-162` is never exercised in CI. Gemini tests
pass `sample_rate=16000` (matching native rate, same skip). Combined with the
scipy-gated audio-resample tests skipping in CI, the actual wiring
`backend.send_audio_chunk(..., sample_rate=48000)` → `resample_pcm` is
**never** exercised by a passing test. Add one CI job with `[audio]` installed
that exercises the 48k-in path for both backends.

### Q4 — `mock_ws_server.headers` only captures the first connection
`conftest.py:93-106` overwrites `self.headers` on every connect, but the
current tests only look at it once. If a future reconnect test wants to
assert headers on the *second* handshake, this needs a `headers_history:
list[dict]`. Not a bug today; a small ergonomic note.

### Q5 — `_capture_headers` uses broad `except Exception` that could hide genuine fixture bugs
`conftest.py:105-106` swallows any exception from header probing. If
websockets changes the `ws.request` attribute name again, tests will silently
report `headers == {}` and the authorization assertions will fail with a
confusing "no 'authorization' key" error instead of a "header-probe broken"
error. Consider logging a warning in the `except` branch.

---

## 📋 Coverage gap summary (edge cases the Anthropic reviewer may not flag)

1. **No test for OpenAI abnormal close** (network error vs clean 30-min
   close). Only `code=1000` is exercised.
2. **No test for mid-stream backend error event** while audio deltas are
   arriving. `test_error_event_surfaces` (Gemini) sends error as the *first*
   message, which is the easy case.
3. **No test that `send_audio_chunk` is thread-safe across concurrent
   iteration of `recv_events`** — `_send_lock` exists
   (`openai_realtime.py:88-91`) but is untested. Realtime voice inherently
   runs send and recv concurrently.
4. **No test that `interrupt()` is safe to call on an already-closed WS.**
   Backend raises `RuntimeError` if `self._ws is None`
   (`openai_realtime.py:305-306`); a post-close `interrupt` from a racing
   coroutine will raise instead of no-op. May be intentional; worth
   confirming.
5. **No test for Gemini `inject_tool_result` with a non-string result** —
   Gemini wraps `{"result": result}` unconditionally
   (the test asserts `fr["response"] == {"result": "result"}`), unlike
   OpenAI which `json.dumps` non-strings. Different contracts between the
   two backends; document or unify.
6. **Audio-resample round-trip test** (`test_round_trip_440hz`...) is nice,
   but only tests a pure sine. Real speech has broadband content and
   aliasing from `resample_poly` could manifest differently. Not
   blocking; could add a "white-noise spectral flatness preserved within
   3 dB" test post-0.3.0.
7. **No test for `SUPPORTED_HERMES_RANGE` upper-bound rejection.**
   `test_discord_bridge.py` only tests the happy-path version gate.

---

## 🏷 Go / No-Go for v0.3.0 tag

**CONDITIONAL GO.**

- **Block the tag** until ISSUE-1 (Discord bridge silent-mute in realtime
  mode) is fixed. It is a regression against 0.2.x behavior for any user
  who follows the documented enablement flow.
- **Ship the tag** with ISSUE-2 (HOWTO docs for 30-min cap) tracked as a
  0.3.1 doc task — it's a doc gap, not a correctness bug, and the source
  + ADR cover it.
- ISSUE-3 is cosmetic; fix whenever.

Once ISSUE-1 is addressed (a 5-line diff moving `receiver.pause()` out of
the 0.3.0 stub path), this review is a clean GO. The realtime-backend
implementations are solid, the mock fixture is well-designed, and the test
suite — while it has the gaps above — exercises the production code paths
for real.
