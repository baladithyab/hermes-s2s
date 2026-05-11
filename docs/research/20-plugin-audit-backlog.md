# 20 — Plugin Audit Backlog (v0.4.1 → v0.6+)

**Date:** 2026-05-10  **Scope:** entire `hermes_s2s` package (~7 385 LOC across 29 modules). **Method:** file-by-file read of the 15 largest files; `pytest --collect-only` (213 tests, all green); cross-check against ADRs 0005-0014, research 10-17, and the v0.4.2 manual-VAD plan.

Legend — **Effort:** S ≤ ½ day, M ≤ 2 days, L ≥ 3 days. **File refs** are `path:line`.

---

## P0 — Ship in v0.4.2 (correctness, safety, blocking debt)

| # | Title | Ref | Problem | Fix | E |
|---|---|---|---|---|---|
| 1 | **`_pump_input` silently drops resample failures** | `_internal/audio_bridge.py:669-671` | Bare `except` logs & `continue`; a persistent resample error (e.g. scipy OOM) burns CPU on poll loop with no user-visible signal. | Track `resample_fail_count` in `stats()`; surface in heartbeat; after N in a window, emit `error` event so dispatcher can restart pump. | S |
| 2 | **OpenAI Realtime has no `transcript_final` role** | `providers/realtime/openai_realtime.py:215-219, 231-238` | Events emit `{"text": ...}` but no `role` — audio_bridge sink only forwards if `role` present → user-side mirror drops all OpenAI transcripts. | Add `"role": "assistant"` to every `transcript_partial/final` payload; add `role="user"` pathway via `conversation.item.input_audio_transcription.completed` event (currently swallowed, line 246-248). | M |
| 3 | **OpenAI `transcript_final` emits both on `audio_transcript.done` AND `response.done`** | `openai_realtime.py:215, 231` | Two finals per turn → mirror rate-limiter burns tokens; UI shows duplicate "end of turn" chips. | Keep `audio_transcript.done` as the authoritative final; treat `response.done` as an internal usage event only (no mirror event). | S |
| 4 | **`get_active_bridge` is a global singleton — fails on multi-guild** | `audio_bridge.py:68-86` | Single process-wide slot; a second `/voice join` on another guild overwrites the first bridge's registry entry → `s2s_status` reports wrong stats, leave-cleanup may close the wrong session. | Key registry on `(guild_id, channel_id)`; update `s2s_status` + `_attach_realtime_to_voice_client` call-sites. | M |
| 5 | **`TranscriptMirror.schedule_send` uses deprecated `asyncio.get_event_loop()`** | `voice/transcript.py:88-96` | Py 3.12 raises `DeprecationWarning`; Py 3.14 will raise `RuntimeError` in threads without a loop — the Discord player thread. | Use `asyncio.get_running_loop()` in a try/except and fall back to adapter loop; collapse branch. | S |
| 6 | **`_pending_first_msg` never initialised in `__init__`** | `providers/realtime/gemini_live.py:98, 273` | Attribute only set in `connect()`; any code path touching `recv_events` before `connect` hits `AttributeError`. | Initialise `self._pending_first_msg = None` in `__init__`. | S |
| 7 | **`SUPPORTED_HERMES_RANGE = ("0.0.0", "999.0.0")`** | `_internal/discord_bridge.py:74` | Version gate is a no-op; any breaking core refactor will ship silently broken. | Set lower bound to earliest Hermes commit you've smoke-tested against; tighten upper after each upstream tag. CI job that cross-clones Hermes and runs the wrap-smoke test. | S |
| 8 | **Monkey-patched `_on_packet` leaks `_prev_lens` across joins** | `discord_bridge.py:1110, 1136` | Dict accumulates per-SSRC state for the process lifetime — users who join many calls leak memory. | Move `_prev_lens` onto the `voice_receiver` instance; clean up in leave-path alongside other markers (1336-1346). | S |
| 9 | **`inject_tool_result` OpenAI path: no dedup on `response.create`** | `openai_realtime.py:291-301` | Two parallel tool-calls each call `response.create` → OpenAI returns 400 "response already in progress". | Serialize via a second `asyncio.Lock` OR use `response.create` only once the LAST pending tool_result is injected (audio_bridge already has `_tool_seq_cond`; plumb an "is_last" flag). | M |
| 10 | **Doc says "session_resumed" event; code emits type='error'** | `openai_realtime.py:14-15 vs 256-259` | Docstring contract and wire are out-of-sync; consumer that filters by `type` misses the cap event. | Emit a dedicated `type="session_cap"` event; update docstring; add test asserting the type. | S |
| 11 | **No test for `_wrap_runner_handle_voice_channel_join` success branch** | `discord_bridge.py:425-543` | 120-line monkey-patch with no coverage beyond "wrap is installed"; regression risk on every Hermes bump. | Add a fixture that fakes `AIAgentRunner` + `event.source` and asserts (a) success mutates `event.source.thread_id`, (b) failure returns unchanged. | M |
| 12 | **Magic number 50 for input queue** | `audio_bridge.py:54` | ~1 s of buffering — fine for realtime, but not tuned for s2s-server mode which pushes larger chunks. | Make `input_queue_max` config-driven via `s2s.audio.input_queue_max`; keep 50 as default. | S |

---

## P1 — Next minor, v0.5.0

| # | Title | Ref | Problem | Fix | E |
|---|---|---|---|---|---|
| 13 | **`discord_bridge.py` is 1 400 LOC / ≥ 8 responsibilities** | `_internal/discord_bridge.py` (entire) | Resolve-params, version gate, monkey-patches (3 sites), capability rollback, thread resolve, bridge attach, frame-callback shim, leave-cleanup, stub pipeline — all in one module. | Split: `discord_patches.py` (monkey-patches), `attach.py` (bridge wiring), `thread_rewire.py` (runner wrap), `version_gate.py`. Target: no file > 500 LOC. | L |
| 14 | **Duplicated thread-resolution logic** | `discord_bridge.py:602-692` vs `:438-540` | Adapter-level and runner-level wrappers each ~90 LOC of near-identical code; changes must be made twice. | Extract shared helper `_apply_thread_resolution(adapter, event, channel)`; both wraps call it. | M |
| 15 | **`_translate_server_msg` is a 90-line `elif` chain** | `gemini_live.py:291-384` | Hard to follow, untested for `goAway`, `usageMetadata`. | Dispatch table `{key: handler}`; one method per top-level Gemini message type; table is unit-testable. | M |
| 16 | **Provider parity gap: Gemini lacks `interrupt(item_id)`** | `gemini_live.py:441-451` vs `openai_realtime.py:330-343` | OpenAI supports surgical truncation; Gemini `interrupt()` just brackets empty activity. | Add `Gemini` truncation via `clientContent{turnComplete:false}` + silence bracket; document semantic differences in ADR-0015. | M |
| 17 | **Provider parity gap: Gemini has no `session_cap` equivalent** | `gemini_live.py:278-289` | WS exceptions yielded as generic `error` — no structured reason, so caller can't auto-reconnect. | Inspect `websockets.exceptions.ConnectionClosed.code`; map 1000/1001 → `session_end`, server `goAway` → `session_cap`. | S |
| 18 | **Configuration sprawl** | `realtime_options.{gemini_live, openai, voice, system_prompt, language_code, model, api_key_env…}` | 6+ keys per provider, nested + flat both supported, wizard writes nested but unwrap happens in 3 places (`gemini_live.py:487`, `openai_realtime.py:372`, `discord_bridge.py:111`). | Single `ProviderConfig` dataclass with `from_dict(cfg)` classmethod; unwrap once at config-load time. Deprecate flat keys with a migrator warning. | M |
| 19 | **No structured logging** | plugin-wide | All `logger.info("hermes-s2s: %s=%s", ...)` — impossible to grep with a log-aggregator. | Adopt `logger.info("event", extra={"kind": "bridge_attach", "guild_id": ..., ...})`; add a JSON formatter helper. Gate on `HERMES_S2S_STRUCTURED_LOGS`. | M |
| 20 | **No metrics export** | plugin-wide | `bridge.stats()` exists but only emitted via heartbeat logs — no Prometheus/OTel. | Add optional OTel meter: `frames_emitted_total`, `dropped_input_total`, `tool_call_duration_seconds` (histogram). Zero-cost if `opentelemetry-api` not installed. | M |
| 21 | **Interruption / barge-in is backend-only** | `audio_bridge.py` | User speaking over ARIA doesn't clear the BridgeBuffer's 20-ms output queue → ~300 ms of stale audio plays before stop. | On `activity_start` (new utterance), call `self.buffer.clear_output()` + `voice_client.stop()` + restart queued source. | M |
| 22 | **No push-to-talk support** | n/a | Gemini manual-VAD watchdog is silence-based only; users in noisy environments cause constant activity_start. | Wire a `/ptt` slash + keybind: bridge exposes `press()`/`release()` that manually brackets activity. | M |
| 23 | **`voice/factory.py` `_build_realtime` is 150 lines with 4 config-resolution branches** | `voice/factory.py:243-396` | Test path vs production path vs registry-missing vs config-missing all interwoven. | Extract `_resolve_backend_from_spec`, `_resolve_prompt_and_voice`, `_eager_build_bridge`. | M |
| 24 | **Thread display name is `"@user"`** | `voice/transcript.py:170-175` | Placeholder renders every user as `@user` — useless in multi-user VCs. | Thread `event.source.user_display_name` through bridge; use `mention` on the adapter. | S |
| 25 | **No integration test for OpenAI `inject_tool_result` dual-event** | `tests/test_openai_realtime.py` | The doc-critical "must send two events" contract has no assertion. | Add `test_inject_tool_result_sends_conversation_item_then_response_create`. | S |
| 26 | **No fuzz/property test for `BridgeBuffer`** | `tests/test_audio_bridge.py` | Input/output thread-safety is asserted by a single handcrafted scenario. | Add `hypothesis`-based test: arbitrary interleaved push_input/pop_input/push_output/read_frame → invariants (no leaks, size bounds, frame alignment). | M |
| 27 | **Transcript export** | n/a (new) | Users ask for a "save this conversation" button — currently only live mirror. | `/transcript export` slash: pulls last N messages from the mirrored thread, formats markdown, DMs to caller. | M |
| 28 | **Voice activity LED indicator** | n/a | Opaque: no signal the bot is "listening" vs "thinking" vs "speaking". | Maintain `bot.presence` (`activity=Game("🎙 listening")` / `🤖 thinking` / `🔊 speaking`) driven by bridge state; throttle to 1 update/sec. | S |
| 29 | **No recording opt-in** | n/a | Users can't record a call for review (privacy-aware). | Add `/record start` that writes mirror + 48k PCM to `<HERMES_HOME>/recordings/<guild>-<ts>.{jsonl,wav}`; consent-gate via opt-in flag in config. | L |
| 30 | **Unit: `_translate_tools` accepts only `{"name"}` dicts — no shape validation** | `gemini_live.py:43-72` | KeyError on malformed tool manifest (no `name`) — fails late on first tool_call. | Validate at `connect()`: `name` is str, `parameters` is dict if present; raise early. | S |

---

## P2 — v0.6+ (architecture, scale, UX polish)

| # | Title | Ref | Problem | Fix | E |
|---|---|---|---|---|---|
| 31 | **Migrate off monkey-patches to upstream hooks (ADR-0012 target)** | `discord_bridge.py` entire | 3 monkey-patch sites + 1 socket-listener swap are fragile per-Hermes-version. | Land upstream PRs: `ctx.register_voice_pipeline_factory`, `VoiceReceiver.set_frame_callback`, `adapter.on_voice_join(pre_snapshot_hook)`. Remove corresponding local patches behind an env feature-flag. | L |
| 32 | **Multi-language detection** | `gemini_live.py:161-191` | Hardcoded `Respond exclusively in {X}` prefix overrides Gemini's auto-detect; users have to manually pick language per channel. | Optional `language_code="auto"` → omit the anchor prefix + set `automaticLanguageDetection=True`; expose via `/s2s lang:<code>`. | M |
| 33 | **Voice cloning / custom voice support** | provider factories | Hardcoded `_DEFAULT_VOICE = "alloy"` / `"Aoede"`; no way to use OpenAI's `voice_id` (cloned voices) or Gemini's reference-audio conditioning. | Accept `voice: {"id": "...", "reference_audio_path": "..."}` in config; route to provider-appropriate API. | L |
| 34 | **Pluggable resampler** | `audio/resample.py` | Single scipy backend, no cheaper path for realtime 48→16 k (the hot case). | Fast path: polyphase with cached FIR coeffs (no scipy, ~5× faster at 48→16). Keep scipy as fallback. | M |
| 35 | **No circuit-breaker on WS reconnect** | `gemini_live.py:479-483`, `openai_realtime.py` | `reconnect` loops on transient errors with no backoff → user sees "connecting…" flapping. | Exponential backoff (1 s → 30 s cap) + max-attempts; surface as `session_cap` after N. | M |
| 36 | **Meta-command grammar is English-only** | `voice/meta.py` | Regex tokens `"new session"`, `"stop speaking"` don't match `"nueva sesión"`. | i18n map `{lang_code: {verb: [patterns]}}`; loaded from YAML. | M |
| 37 | **`_handle_capability_error_rollback` has a 4-deep nested try** | `discord_bridge.py:144-215` | Unreadable; 3 of the 4 `except Exception` are `# pragma: no cover`. | Use `contextlib.suppress` where truly defensive; split into `_disconnect_vc`, `_notify_user` helpers. | S |
| 38 | **`BridgeBuffer.read_frame` may allocate 3840 bytes per silence frame** | `audio_bridge.py:34, 231` | `SILENCE_FRAME` is module-level but `return SILENCE_FRAME` at line 231 is fine — however `self._output_frames.pop(0)` is O(n). | Use `collections.deque` for `_output_frames`; O(1) popleft. | S |
| 39 | **Audio-chunk memory churn** | `audio_bridge.py:210-217, 709-719` | Every out-chunk goes `bytes → bytearray.extend → bytes(slice) → resample → bytes`. On a 10-min call that's ~MB of garbage. | Use `memoryview` + reuse a single output bytearray; benchmark first. | M |
| 40 | **No smoke test on real WS endpoints** | `scripts/smoke_realtime.py` | Present but not run in CI; drift between docs and Gemini/OpenAI live schema goes unnoticed. | Nightly CI job with secrets from `GEMINI_API_KEY_CI` / `OPENAI_API_KEY_CI`; skip on PRs from forks. | M |
| 41 | **Doctor doesn't probe mic permissions / opus codec** | `hermes_s2s/doctor.py` | Plug-and-play goal (ADR-0009) promises a clean diag; currently only checks env vars. | Add `doctor voice` subcommand: scipy present, websockets version, discord.py opus loaded, ffmpeg on PATH, audio input queue smoke. | M |
| 42 | **`s2s_status` LLM tool surfaces bridge stats — but nothing else** | `voice/meta_tools.py`, `audio_bridge.py:72` | Operator debugging has no one-shot snapshot of active session + backend + config. | Extend `s2s_status` to include effective mode, provider, model, connected-since, thread mirror target. | S |
| 43 | **No retry on transcript mirror send failures** | `voice/transcript.py:248-255` | `channel.send` fail (network blip) → transcript line lost forever. | Re-queue with bounded retries (3×, exp backoff); only drop after. | S |
| 44 | **ADR-0008 §2 filler audio hardcoded** | `tool_bridge.py:257` | `"let me check on that"` is the only phrase; lands awkwardly in non-English sessions. | Phrase table keyed on session language; also make config-overridable (`s2s.tools.filler_phrase`). | S |

---

## P3 — Nice-to-have / polish

| # | Title | Ref | Problem | Fix | E |
|---|---|---|---|---|---|
| 45 | **Remove unused `prev_in` in heartbeat** | `audio_bridge.py:588, 616` | Assigned but never read after initialisation. | Delete. | S |
| 46 | **Docstrings reference private file lines in Hermes core** | `discord_bridge.py:33-44` | e.g. `discord.py:376` — line numbers bitrot on upstream refactors. | Replace with stable anchors (method names + "at time of writing"). | S |
| 47 | **No ADR for v0.4.2 manual-VAD decision** | `docs/plans/wave-0.4.2-manual-vad.md` exists; no ADR | Plan doc documents the WHAT; no ADR for WHY-over-AAD (design record). | Promote to ADR-0015. | S |
| 48 | **`VoiceMode.normalize` re-raises `ValueError`, slash just echoes the exception text** | `voice/slash.py:428-432` | User sees `"'foobar' is not a VoiceMode"` — not friendly. | Catch and render the suggestion list (`Did you mean: cascaded, pipeline, realtime, s2s-server?`). | S |
| 49 | **No `README.md` badge for test / coverage** | `README.md` | Users can't see pass/fail at a glance. | Add CI status + PyPI version badges. | S |
| 50 | **`_voice_pipeline_factory` is a pure stub** | `discord_bridge.py:1366-1400` | Dead code; confusing to new contributors (looks like "the real implementation"). | Gate behind `HERMES_S2S_NATIVE_HOOK=1`; document "placeholder until upstream lands"; log WARNING if invoked. | S |

---

## Coverage gaps observed (from `pytest --collect-only`)

Present: audio bridge (13), audio VAD (10), resample (5), bridge integration (4), CLI shims (9), config loader (4), config unwrap (7), discord bridge wrap (7), doctor, meta, migrate, gemini, openai, s2s-server provider, slash, smoke, threads, tool bridge, tool export, voice modes (25).

**Missing:**

- End-to-end tool-call ordering with 3+ parallel tools (currently only 1-in-flight covered).
- `TranscriptMirror` overflow warn-throttling (line `transcript.py:209-223`).
- Runner-level thread patch success/failure branches (see P0 #11).
- `S2SModeOverrideStore` cross-process flock semantics (the atomic-rename path is only sanity-tested).
- OpenAI `inject_tool_result` dual-event contract (P1 #25).
- Resampler NaN / inf handling on pathological input.

---

## Suggested order for v0.4.2 cut

1. #6, #5, #8, #10, #45 — one-line defensive fixes, < ½ day batched.
2. #1, #2, #3, #9 — correctness bugs with visible user impact.
3. #11, #25 — test debt where the code is already right but under-fenced.
4. #12 — config knob that unblocks s2s-server-mode users.
5. #4 — multi-guild registry (must land before any cloud/multi-tenant pitch).

Total P0 effort estimate: **~4–5 engineer-days**.

---

*End of audit. 50 items, prioritized. Next step: triage with the maintainer, strike anything out of scope for 0.4.2, open issues for remaining P0s.*
