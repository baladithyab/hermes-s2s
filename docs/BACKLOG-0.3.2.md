# Backlog — 0.3.2

> Goal: make hermes-s2s plug-and-play. From `pip install` to "AI voice in Discord VC" should be 3 commands and 30 seconds, with clear errors at every step that would otherwise fail.

## Atomic items

### F1: Backend filler audio implementations
- [ ] **F1-A** — `gemini_live.py::send_filler_audio(text)`: send a `BidiGenerateContentClientContent` with `text` parts (the Pipecat pattern). Model speaks the text once, then continues normally on `inject_tool_result`.
- [ ] **F1-B** — `openai_realtime.py::send_filler_audio(text)`: send `response.create` with `instructions` override telling the model to say the text. Per ADR-0008.
- [ ] **F1-C** — Tests: each backend test_*.py adds 1 test asserting the right WS message shape goes out on `send_filler_audio("test")`. Mock WS server captures + asserts.

### F2: Plug-and-play wizard realtime profiles
- [ ] **F2-A** — Add 3 new wizard profiles to `hermes_s2s/cli.py::cmd_setup`: `realtime-gemini` (cheapest cloud), `realtime-openai` (premium voice), `realtime-openai-mini` (mid-tier). Each profile writes the `s2s.mode: realtime` block + `realtime.provider` + `realtime.<provider>.*` opts.
- [ ] **F2-B** — Wizard auto-detects + sets `HERMES_S2S_MONKEYPATCH_DISCORD=1` in `.env` for any realtime profile (with explicit user confirmation since it's a monkey-patch). Idempotent.
- [ ] **F2-C** — Wizard checks for `DISCORD_BOT_TOKEN` + `DISCORD_ALLOWED_USERS` before declaring success; warns user if missing with a one-liner pointing at the Hermes Discord setup doc.
- [ ] **F2-D** — Tests: extend `test_setup_wizard.py` for the 3 new profiles; verify .env writes; verify warning fires on missing DISCORD_*.

### F3: `hermes s2s doctor` pre-flight check
- [ ] **F3-A** — New CLI subcommand `hermes s2s doctor` that runs a comprehensive readiness check:
  - Active s2s.mode + provider config (read config.yaml)
  - Required env vars set (GEMINI_API_KEY / OPENAI_API_KEY based on provider)
  - Optional env vars (HERMES_S2S_MONKEYPATCH_DISCORD for realtime+Discord)
  - Python deps installed (moonshine_onnx, kokoro, scipy, websockets, discord)
  - System deps (ffmpeg, libopus, espeak-ng for kokoro)
  - Hermes-side: DISCORD_BOT_TOKEN, DISCORD_ALLOWED_USERS visible in env
  - Backend connect probe (5s timeout) — actually opens the WS for realtime backends and closes immediately. Gated on env keys present.
  - Reports each check with ✓ / ✗ / ⚠ + specific remediation instruction
- [ ] **F3-B** — JSON output mode (`--json`) so the LLM can call `s2s_doctor` as a tool and parse the result.
- [ ] **F3-C** — New tool schema `s2s_doctor` exposed to the model so users can ask "is my voice setup working" and get an authoritative answer.

### F4: Integration test (BridgeBuffer + RealtimeAudioBridge + FakeBackend full-flow)
- [ ] **F4-A** — `tests/test_bridge_integration.py`: in-process end-to-end test that wires a fake `RealtimeBackend` (yields scripted audio_chunk events) + `RealtimeAudioBridge` + `BridgeBuffer` together. Asserts: input PCM flows through to backend, backend audio reaches the buffer at the right rate/format, fractional remainder behavior holds across multiple backend chunks, close() is clean.
- [ ] **F4-B** — Stress test: drive 100 frames of input + 5 backend chunks, verify zero leaked tasks and bounded queue depths.

### F5: Backpressure visibility (Gemini P1-G1 from 0.3.1 review)
- [ ] **F5-A** — When `BridgeBuffer.dropped_input` increments, log a WARNING every 100 drops (debounced — don't spam every drop).
- [ ] **F5-B** — Expose `bridge.stats()` returning `{dropped_input, dropped_output, queue_depth_in, queue_depth_out, frames_emitted, frames_underflow}` so `s2s_status` tool can report it live.
- [ ] **F5-C** — `s2s_status` tool extended to include bridge stats when an active bridge exists.

### F6: README + INSTALL overhaul
- [ ] **F6-A** — README "30-second install" section at the top — copy/paste 3 commands, no scrollbar.
- [ ] **F6-B** — `docs/INSTALL.md`: matrix of install profiles (`pip install hermes-s2s[realtime]` vs `[local-all]` vs `[all]`) with what each unlocks.
- [ ] **F6-C** — Document `hermes s2s doctor` in HOWTO + README.
- [ ] **F6-D** — A "What can go wrong" troubleshooting section with the 10 most likely failure modes and one-liner fixes.

## Out of scope for 0.3.2

- Multi-party VC mixing (0.4.0)
- Telegram realtime (0.4.0)
- Tool list passed to backend at connect (currently empty list; needs Hermes tool registry integration)
- Filler audio cancellation when result arrives mid-speech (0.4.0)

## Risks

- **Filler audio implementations**: each backend's "say this text once" mechanism is non-obvious. F1-A and F1-B need careful WS message shaping. Tests must verify the exact JSON the model receives.
- **Wizard `.env` edits**: idempotency is hard to get right. F2 must use a marker-line pattern (already established for HERMES_LOCAL_STT_COMMAND in 0.2.0).
- **Doctor check WS probe**: opens a real WS to Gemini/OpenAI. Costs ~$0.0001 per probe. Document; gate on `--probe` flag if user wants to skip. Default = probe.
- **Plug-and-play assumption**: the user still needs Discord bot setup (token, allowed users, intents). hermes-s2s doesn't replace that. Doctor surfaces it; install docs reference the Hermes Discord setup guide.
