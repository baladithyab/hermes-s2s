# Wave 0.3.0 — Phase-8 Cross-Family Review (Anthropic)

**Reviewer:** Claude (Anthropic family)
**Commit:** `b5c9c11`
**Scope:** audio/resample, gemini_live, openai_realtime, discord_bridge
**Focus:** protocol correctness vs research/05, resource leaks, monkey-patch safety

---

## CONFIRMED ✓

### Gemini Live (`hermes_s2s/providers/realtime/gemini_live.py`)
- Connect URL `GEMINI_LIVE_URL` L36–40 matches research/05 §1.1 verbatim.
- Setup frame L151–180 includes every required field per research/05 §1.2:
  `model`, `generationConfig`, `systemInstruction`, `realtimeInputConfig`,
  `inputAudioTranscription`, `outputAudioTranscription`,
  `contextWindowCompression`, `sessionResumption`. Auto-prefixes `models/` at L155.
- Audio framing L226: `realtimeInput.audio.{data,mimeType}` base64 JSON text frame — matches §1.3.
- Tool translation L43–72 round-trips cleanly: Hermes `{name,desc,parameters}` → Gemini `functionDeclarations[]`. Test `test_translate_tools_shape` covers. Pass-through of uppercase-OPENAPI and lowercase-JSON-schema types is correct.
- `inject_tool_result` L351–377 sends `toolResponse.functionResponses[{id,response}]` with matching `id`, JSON-decoded response — matches §1.5.
- Session resumption: handle captured on `sessionResumptionUpdate` at L329–343, re-sent at L176–177 on reconnect. Test `test_session_resumption` proves round-trip.
- `close()` L182–190 nulls `_ws` before `await ws.close()` — defensive against double-close.
- `recv_events` L249–251 yields final error event on exception only when `not self._closed` — no spurious errors from client-initiated close.

### OpenAI Realtime (`hermes_s2s/providers/realtime/openai_realtime.py`)
- Connect URL template L38: `wss://api.openai.com/v1/realtime?model={model}` — matches research/05 §2.1.
- Headers L110–113: `Authorization: Bearer …` + `OpenAI-Beta: realtime=v1`. Backward-compat fallback from `additional_headers` → `extra_headers` (websockets v11 vs v12) at L116–119. ✓
- `session.update` L127–138 has `model`, `instructions`, `voice`, `input_audio_format=pcm16`, `output_audio_format=pcm16`, `tools`, `tool_choice=auto`.
- Audio framing L165: `input_audio_buffer.append` base64 — matches §2.3.
- **`inject_tool_result` L278–301 sends BOTH** `conversation.item.create` (function_call_output, `output` coerced to str) AND `response.create`, in that order. The OpenAI gotcha is handled. Test `test_tool_call_round_trip_sends_output_and_response_create` asserts ordering.
- `interrupt()` L303–316 sends `response.cancel` → `output_audio_buffer.clear` → `conversation.item.truncate` with `item_id`, `content_index=0`, `audio_end_ms`. Ordering asserted in test L279–282.
- 30-min cap handling L249–274: distinguishes client-initiated close (`_client_initiated_close`) from server-initiated (abnormal or clean 1000). Surfaces `error` event with `reason=session_cap`. Test simulates via `ws.close(code=1000)` and confirms reconnect works on a fresh backend instance. ✓
- `_send_lock` serializes concurrent writes — no interleaved JSON frames between `inject_tool_result`'s two sends.

### Resource leaks
- No background `asyncio.create_task` in either backend. `recv_events` is a generator driven by the caller. No leaked tasks.
- Gemini `reconnect()` (L393–397) calls `close()` then `connect()`; on the same instance the old `_ws` is closed and replaced — no dangling WS.
- OpenAI has no `reconnect()` helper (intentional — lossy per §2.7). Caller constructs a fresh instance, confirmed by test L242–250.
- `_send_lock` (OpenAI) is rebuilt on every `connect()` L121. Same for `_client_initiated_close` reset at L123.

### Discord bridge (`hermes_s2s/_internal/discord_bridge.py`)
- No-op default L83–88: without env var and without `ctx.register_voice_pipeline_factory`, only logs an info line. Verified by `test_install_is_no_op_when_no_env_and_no_native_hook`.
- Env-var gate `_ENV_FLAG = "HERMES_S2S_MONKEYPATCH_DISCORD"` L40, check at L76.
- Idempotency L131–133: `_BRIDGE_WRAPPED_MARKER` sentinel on the wrapper function. On second install `original` IS the wrapper → marker detected → early return. Test `test_install_monkey_patches_discord_adapter` L101–104 asserts identity stability across two installs.
- Native hook takes precedence (L62–73) even with env var set. Test `test_native_hook_takes_precedence_over_monkey_patch` asserts.
- `_install_bridge_on_adapter` L203–257 is gated on `cfg.mode == 'realtime'` and pauses `VoiceReceiver` before the stub bridge — safe handoff, no double-transcription.
- Import-failure path L100–108: logs warning and returns, does not raise.

### Audio resample (`hermes_s2s/audio/resample.py`)
- `gcd` ratio L93–95: `g=gcd(src,dst); up=dst//g; down=src//g`. Correct: 48000↔16000 → up=1, down=3; 24000↔48000 → up=2, down=1; 48000↔24000 → up=1, down=2. Matches ADR-0005.
- `resample_poly(arr, up, down, axis=0)` L97: `axis=0` is correct for both 1-D mono and 2-D (frames,channels) — per-channel resample along frame axis.
- Mono→multi upmix L79: `np.repeat(arr[:, None], N, axis=1)` → shape (frames, N). `numpy_to_pcm` flattens with `reshape(-1)` (C-order) → correctly interleaved PCM. Test `test_gemini_live_out_24k_mono_f32_to_48k_stereo_s16le` confirms L == R (L102–103).
- Multi→mono L76: `arr.mean(axis=1)` — standard mixdown. Test L49–63 uses L=+0.5, R=−0.5 → mean=0 → `max(abs(out)) <= 1 LSB`. **Tolerance is correct** — stereo mixdown to mono followed by mono-to-stereo upmix is NOT bit-exact when channels differ, but this test does NOT exercise that round-trip; it only tests the one-way mixdown of a known L≠R signal, which is deterministic to ~1 LSB after quantization.
- Round-trip test `test_round_trip_440hz_preserves_peak_frequency` L109–130 stays mono throughout; FFT peak within one bin is an appropriate psychoacoustic-grade tolerance for resample_poly.

---

## ISSUES

### P1

1. **Version string mismatch** — `hermes_s2s/__init__.py:24` still reads `__version__ = "0.2.0"`. Commit message and plan both say 0.3.0. Must bump before tagging, otherwise pip-installed users report wrong version, and skill/plugin self-identification is wrong. **Blocker for `git tag v0.3.0`.**

2. **`SUPPORTED_HERMES_RANGE = ("0.0.0", "999.0.0")`** — `discord_bridge.py:38`. Effectively a no-op gate. The ADR-0006 intent is to detect when upstream Hermes internals move. Upper bound `999.0.0` will silently permit the patch on every future Hermes release, defeating the "clear error instead of silent broken patch" goal stated in the comment L36–37. Tighten upper bound to e.g. `"0.9.0"` (next minor after tested) or track explicit `hermes_agent.__version__` from the spike.

### P2

3. **`install_discord_voice_bridge` double-call on native hook path not idempotent** — `discord_bridge.py:62–73`. The native hook is called unconditionally on every `install_...` invocation. If Hermes's plugin loader reloads the plugin, `native_hook("discord", _voice_pipeline_factory)` runs again with no sentinel on the ctx side. This is Hermes's problem to dedupe on its registry, but the plugin should probably check / be explicit. Not blocking.

4. **OpenAI `session.update` omits `input_audio_transcription` and `turn_detection`** — `openai_realtime.py:127–138`. Server defaults are sensible (server_vad, no user transcription). But research/05 §2.2 shows both as standard fields. Without `input_audio_transcription` the backend never gets user-side transcript events — which Hermes presumably wants for logging/memory. Add at least `"input_audio_transcription": {"model": "gpt-4o-mini-transcribe"}` as a default, or expose via config.

5. **Gemini `send_audio_chunk` assumes mono input** — `gemini_live.py:195–227`. Signature takes `pcm_chunk, sample_rate` only, no `channels`. If Discord pipes 48k stereo s16le raw, `resample_pcm(..., src_channels=1, dst_channels=1)` will misinterpret interleaved L/R as 2× mono samples at double rate. Either add `channels` kwarg or document that caller MUST pre-mix to mono. (OpenAI backend has the same narrower signature L141 — same caveat.)

6. **Gemini `interrupt()` semantics** — `gemini_live.py:379–388` sends `activityStart` then immediately `activityEnd` with no audio in between. Per research/05 §1.6, those frames *bracket* the user's interrupting audio. This back-to-back pattern is a no-op signal to the server and will not cancel in-flight audio. In auto-VAD mode interrupts are handled server-side anyway, so this is only wrong for manual-VAD callers. Either document "auto-VAD only" or fix the manual path to coordinate with an audio-send window.

7. **Gemini `recv_events` swallows `interrupted: true`** — `gemini_live.py:253–346` has no handler for `serverContent.interrupted`. Research/05 §1.4 calls this out as a distinct signal. Callers cannot learn from the backend that the model was barge-in-interrupted. Add a `RealtimeEvent(type="interrupted", ...)`.

8. **OpenAI backend swallows `conversation.item.input_audio_transcription.*`** — `openai_realtime.py:246–248` intentionally drops these. If #4 is addressed, also emit user-side `transcript_partial/final` events for symmetry with Gemini (which does emit `inputTranscription` at L299–306).

9. **Gemini setup sends `"sessionResumption": {}` on first connect** — `gemini_live.py:179`. Research/05 §1.2 shows `{"handle": null}`. Gemini accepts both (empty dict = opt-in resumption with no handle), but `{"handle": null}` is the canonical form. Cosmetic.

10. **`resample_pcm` returns bytes even when `src_rate==dst_rate` and channels match but dtypes differ** — OK and desired. But when *everything* matches (the passthrough case tested at L32–40) the function still does `frombuffer → astype(float32)/32768 → clip*32768 → int16 → tobytes`, which CAN introduce rounding on s16le→s16le if the float32 normalization isn't lossless. In practice float32 has enough precision for int16 so `out == src` holds (test confirms). But a fast-path `if src_rate==dst_rate and src_channels==dst_channels and src_dtype==dst_dtype: return audio_bytes` would save work. Perf only.

---

## QUESTIONS

- Does Hermes' plugin system guarantee `register()` is called only once per process? If yes, Discord-bridge idempotency-on-reload is moot; if no, we should also sentinel the native-hook path.
- OpenAI reconnect on `session_cap`: caller is expected to construct a fresh `OpenAIRealtimeBackend`. No API to "replay conversation items" — does the Hermes orchestrator hold transcript state and replay via `conversation.item.create` on the new backend? If not, every 30 minutes the user loses context.
- Is there a Moonshine/Kokoro-side callsite that passes multi-channel audio to `send_audio_chunk` (issue #5 above)? Need to audit actual pipeline wiring.

---

## Go/No-Go: **CONDITIONAL GO** for `v0.3.0`

Must fix before tagging:
- **P1-1**: bump `__version__` to `"0.3.0"` in `hermes_s2s/__init__.py:24`.

Should fix soon (can ship as 0.3.1 patch, not a tag blocker):
- P1-2 (version-gate tightening), P2-4 (input_audio_transcription), P2-6/7 (Gemini interrupt + interrupted event).

Protocol shapes match research/05 for both backends. Monkey-patch is safe (no-op default, env-gated, idempotent). Resample math is correct. 40 tests pass, no leaked async tasks, no dangling WS. Bump the version string and ship.
