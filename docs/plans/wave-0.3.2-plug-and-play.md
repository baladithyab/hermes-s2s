# Wave plan: 0.3.2 — Plug-and-play install + diagnostic

> ADRs: 0009. Backlog: BACKLOG-0.3.2.md.
> Effort: ~1 day across 4 parallel subagents.
> Acceptance: from `pip install hermes-s2s[all]` to "audio in a Discord VC" is 3 commands. `hermes s2s doctor` diagnoses every common failure mode.

## Goal

Make the install + configure path bulletproof. Three new pieces:
1. Backend filler-audio implementations (Gemini Live + OpenAI Realtime)
2. Realtime wizard profiles + monkey-patch auto-config
3. `hermes s2s doctor` pre-flight + `s2s_doctor` LLM tool
4. Plus: integration test, README/INSTALL overhaul, backpressure visibility

## Acceptance criteria

1. `gemini_live.py::send_filler_audio("text")` sends a `BidiGenerateContentClientContent` with the text; mock-WS test verifies the JSON shape.
2. `openai_realtime.py::send_filler_audio("text")` sends `response.create` with instructions override; mock-WS test verifies.
3. `hermes s2s setup --profile realtime-gemini` writes the full config + .env idempotently. Tests verify.
4. `hermes s2s doctor` runs all checks, exits 0 on green, 1 on any error. `--json` produces machine-readable output. `--no-probe` skips WS probe.
5. `s2s_doctor` tool callable by the LLM; returns structured JSON.
6. Integration test wires a FakeBackend + RealtimeAudioBridge + BridgeBuffer end-to-end without leaks.
7. Backpressure: `BridgeBuffer` debounced WARNING log every 100 drops; `bridge.stats()` exposes counters.
8. README has a "30-second install" section at the top.
9. `docs/INSTALL.md` has the install profile matrix.
10. Existing 60 tests still pass; ~15 new tests added.
11. Version bumped to 0.3.2.

## File ownership

| Subagent | Owns | Touches | Reads |
|---|---|---|---|
| **G1: Backend filler audio** | `hermes_s2s/providers/realtime/gemini_live.py` (extend), `hermes_s2s/providers/realtime/openai_realtime.py` (extend), `tests/test_gemini_live.py` + `tests/test_openai_realtime.py` (extend) | none | research/05, ADR-0008 |
| **G2: Wizard profiles** | `hermes_s2s/cli.py` (extend cmd_setup), `tests/test_setup_wizard.py` (extend) | none | ADR-0009 |
| **G3: Doctor + s2s_doctor tool + stats** | `hermes_s2s/doctor.py` (NEW), `hermes_s2s/cli.py` (add `doctor` subcommand), `hermes_s2s/schemas.py` + `hermes_s2s/tools.py` (add s2s_doctor), `hermes_s2s/_internal/audio_bridge.py` (add stats() + debounced log), `tests/test_doctor.py` (NEW), `tests/test_audio_bridge.py` (extend) | none | ADR-0009 |
| **G4: Integration test + docs + version** | `tests/test_bridge_integration.py` (NEW), `README.md` (rewrite Quick Start + add 30-second install), `docs/INSTALL.md` (NEW), `docs/HOWTO-VOICE-MODE.md` (extend with doctor mention), `pyproject.toml` (version bump), `hermes_s2s/__init__.py` (version bump), `tests/test_smoke.py` (version assertion update) | none | all 0.3.2 work |

G1, G2, G3, G4 are all independent. G2 and G3 both touch `cli.py` but in different functions — G2 owns `cmd_setup` + setup_argparse setup-subparser; G3 owns the new `doctor` subparser + dispatch case. Clean split via grep.

## Subagent prompts

### G1 — Backend filler audio

> Implement `send_filler_audio(text: str)` for both realtime backends per ADR-0008.
>
> 1. `hermes_s2s/providers/realtime/gemini_live.py`: add `async def send_filler_audio(self, text: str) -> None`. Sends a `BidiGenerateContentClientContent` JSON message with `clientContent.turns: [{role: "user", parts: [{text: text}]}], turnComplete: true`. The model speaks the text once. Per Gemini Live docs (research/05).
>
> 2. `hermes_s2s/providers/realtime/openai_realtime.py`: add `async def send_filler_audio(self, text: str) -> None`. Sends a `response.create` message with `response: {instructions: f"Briefly say: {text}", modalities: ["audio"]}`. Per OpenAI Realtime docs.
>
> 3. Tests:
>    - `tests/test_gemini_live.py`: add 1 test `test_send_filler_audio_emits_text_content` using the mock_ws_server fixture. Backend.connect, then send_filler_audio("hello"); assert the server received a JSON message with the right shape (`clientContent.turns[0].parts[0].text == "hello"` or whatever Gemini expects per research/05).
>    - `tests/test_openai_realtime.py`: add 1 test `test_send_filler_audio_emits_response_create`. Same pattern; assert `response.create` with `instructions` containing the text.
>
> CONSTRAINTS: don't break existing 8 gemini tests + 4 openai tests. Lazy-import websockets. ~30 LOC per backend method + ~50 LOC per test.
>
> Verify: cd /mnt/e/CS/github/hermes-s2s && python3 -m pytest tests/test_gemini_live.py tests/test_openai_realtime.py -q. Then full suite.
>
> REPORT: 5-line summary. Files + LOC, exact JSON shape used per backend, test count.

### G2 — Wizard realtime profiles

> Extend `hermes_s2s/cli.py::cmd_setup` per ADR-0009 §1.
>
> 1. Add 3 profiles to the profile dispatch:
>    - `realtime-gemini`: writes s2s.mode=realtime, realtime.provider=gemini-live, realtime.gemini_live.{model: "gemini-live-2.5-flash", voice: "Aoede", system_prompt: "You are a helpful voice assistant. Respond briefly."}.
>    - `realtime-openai`: same shape with provider=gpt-realtime, openai.{model: "gpt-realtime", voice: "alloy"}.
>    - `realtime-openai-mini`: provider=gpt-realtime-mini, openai.{model: "gpt-realtime-mini", voice: "alloy"}.
>
> 2. For ANY realtime profile, append `HERMES_S2S_MONKEYPATCH_DISCORD=1` to ~/.hermes/.env (using the existing marker-line pattern from 0.2.0). Idempotent.
>
> 3. Pre-write checks (warnings, not errors):
>    - GEMINI_API_KEY set? (only required for realtime-gemini)
>    - OPENAI_API_KEY set? (only required for realtime-openai*)
>    - DISCORD_BOT_TOKEN visible in env or .env? (always required for VC)
>    - DISCORD_ALLOWED_USERS set?
>    - python -c "import discord" works? (Hermes voice extra)
>    Print `[ ]` (missing) / `[✓]` (set) / `[✗]` (will fail) per check, with remediation links inline.
>
> 4. Update --profile choices argparse list to include the 3 new profiles.
>
> 5. Tests in `tests/test_setup_wizard.py`:
>    - `test_setup_realtime_gemini_writes_full_config`: --profile realtime-gemini --dry-run; assert dict has s2s.mode=realtime + realtime.provider=gemini-live + realtime.gemini_live.* present.
>    - `test_setup_realtime_appends_monkeypatch_env`: write to a tmp .env path; assert HERMES_S2S_MONKEYPATCH_DISCORD=1 line present after run.
>    - `test_setup_realtime_warns_on_missing_api_key`: monkeypatch GEMINI_API_KEY out of env; capture stdout; assert warning text contains "GEMINI_API_KEY" + remediation URL.
>    - `test_setup_realtime_idempotent_env_append`: run twice; assert .env has only ONE HERMES_S2S_MONKEYPATCH_DISCORD line.
>
> CONSTRAINTS: don't break existing 4 wizard tests. ~80 LOC additions to cli.py + ~80 LOC tests. Use the same yaml.safe_dump + deep-merge pattern as the existing profiles.
>
> Verify: cd /mnt/e/CS/github/hermes-s2s && python3 -m pytest tests/test_setup_wizard.py -q. Then full suite.
>
> REPORT: 5-line summary.

### G3 — Doctor + s2s_doctor tool + stats

> Implement `hermes s2s doctor` per ADR-0009 §2 + §3 + bridge stats per BACKLOG F5.
>
> 1. `hermes_s2s/doctor.py` (NEW, ~250 LOC):
>    - `def run_doctor(probe: bool = True) -> dict`: returns `{"checks": [{"name": str, "status": "pass"|"warn"|"fail", "message": str, "remediation": str}], "overall_status": "pass"|"warn"|"fail"}`.
>    - Check functions, organized by category: configuration, python_deps, system_deps, api_keys, hermes_integration, backend_connectivity.
>    - Each check fn returns a dict in the schema above. The runner aggregates.
>    - Backend connectivity probe: opens WS via the configured realtime backend's connect(), waits for first server event or 5s timeout, closes. Catches any error and reports as "fail" with the error message.
>    - `def format_human(report: dict) -> str`: pretty-prints the readiness check with the box-drawing UI from ADR-0009 §2.
>    - `def format_json(report: dict) -> str`: json.dumps the report.
>
> 2. `hermes_s2s/cli.py`: add doctor subparser:
>    - `--json` flag (default False)
>    - `--no-probe` flag (default False; default behavior is to probe)
>    - dispatch to `cmd_doctor(args)` which calls run_doctor + format_*.
>
> 3. `hermes_s2s/schemas.py`: add `S2S_DOCTOR` per ADR-0009 §3.
>
> 4. `hermes_s2s/tools.py`: add `def s2s_doctor(args, **kwargs) -> str`: parses args["probe"] (default True), calls run_doctor, returns format_json.
>
> 5. `hermes_s2s/__init__.py::register(ctx)`: register the new tool via `ctx.register_tool(name="s2s_doctor", toolset="s2s", schema=s2s_schemas.S2S_DOCTOR, handler=s2s_tools.s2s_doctor)`.
>
> 6. `hermes_s2s/_internal/audio_bridge.py::BridgeBuffer`:
>    - Add `dropped_input_warn_threshold = 100` class constant.
>    - When `dropped_input` crosses a multiple of 100, log a WARNING: "hermes-s2s: dropped %d input frames (network/processing slow)".
>    - Add method `def stats() -> dict` returning `{"dropped_input": int, "dropped_output": int, "queue_depth_in": int, "queue_depth_out": int, "frames_emitted": int, "frames_underflow": int}`. Add counters for `frames_emitted` and `frames_underflow` if not present.
>
> 7. `hermes_s2s/_internal/audio_bridge.py::RealtimeAudioBridge`:
>    - Add `def stats() -> dict` returning bridge-level stats (delegate to buffer + add own counters).
>
> 8. Tests:
>    - `tests/test_doctor.py` (NEW): 6 tests:
>      a. run_doctor with no realtime config → reports cascaded mode + relevant checks
>      b. run_doctor with realtime-gemini config + no GEMINI_API_KEY → fails on api_keys check
>      c. format_human produces a string containing all check names
>      d. format_json produces parseable JSON
>      e. probe=False skips the backend connectivity check
>      f. probe=True with GEMINI_API_KEY set + mocked websockets.connect → connectivity check passes
>    - `tests/test_audio_bridge.py` extend with:
>      g. test_buffer_logs_warning_every_100_drops
>      h. test_buffer_stats_returns_expected_keys
>      i. test_bridge_stats_aggregates_buffer
>
> CONSTRAINTS: lazy-import websockets in the connectivity probe. Don't actually call real Gemini/OpenAI APIs in tests. Total ~250 + 50 + 30 LOC + ~250 LOC tests. Pretty-printer can use plain `print` formatting (no rich/click dep).
>
> Verify: cd /mnt/e/CS/github/hermes-s2s && python3 -m pytest tests/ -q.
>
> REPORT: 5-line summary including how many checks the doctor runs in total and which categories.

### G4 — Integration test + docs + version bump

> Five deliverables:
>
> 1. `tests/test_bridge_integration.py` (NEW): in-process end-to-end test wiring everything. Build a FakeBackend class (yields scripted RealtimeEvent audio_chunk events from a list), a real RealtimeAudioBridge (NOT mocked), feed input PCM via on_user_frame, assert: backend.send_audio_chunk was called with resampled PCM at the right rate; output queue was populated; read_frame produces the expected silenced/audio frames; close() leaves no asyncio tasks running. ~150 LOC, 3-4 tests.
>
> 2. README.md: rewrite the top section. Add a "30-second install" callout BEFORE the architecture overview:
>    ```markdown
>    ## 30-second install
>    
>    ```bash
>    pip install 'hermes-s2s[all]'
>    hermes plugins enable hermes-s2s
>    hermes s2s setup --profile realtime-gemini
>    hermes s2s doctor
>    ```
>    
>    Then add `GEMINI_API_KEY=...` to `~/.hermes/.env` (or any other API key the doctor flagged), restart `hermes gateway`, and `/voice join` in any Discord VC the bot has access to.
>    ```
>    Keep the rest of the README mostly intact; just update the Status section to 0.3.2 = plug-and-play.
>
> 3. `docs/INSTALL.md` (NEW, ~100 lines): install profile matrix.
>    | Profile | What you get | Install |
>    |---|---|---|
>    | core | basic plugin, command-provider STT/TTS shims (0.2.0) | `pip install hermes-s2s` |
>    | local-all | + Moonshine STT + Kokoro TTS + scipy resampling | `pip install 'hermes-s2s[local-all]'` |
>    | realtime | + Gemini Live + OpenAI Realtime backends | `pip install 'hermes-s2s[realtime]'` |
>    | server-client | + s2s-server WS client (0.4.0) | `pip install 'hermes-s2s[server-client]'` |
>    | all | everything above | `pip install 'hermes-s2s[all]'` |
>    Plus system deps section (libopus, ffmpeg, espeak-ng) + per-OS install commands.
>
> 4. `docs/HOWTO-VOICE-MODE.md` extend: add a section "Diagnosing problems with `hermes s2s doctor`" after the troubleshooting block. Show example output + how to interpret each check.
>
> 5. Version bumps:
>    - pyproject.toml: 0.3.1 → 0.3.2
>    - hermes_s2s/__init__.py __version__: 0.3.1 → 0.3.2
>    - tests/test_smoke.py: any version assertion → 0.3.2
>
> CONSTRAINTS: README "30-second install" must fit in <15 lines (no scrollbar). Don't promise features that aren't shipping in 0.3.2. Cite GitHub URLs not local paths. Lazy-import scipy in the integration test (skip if missing).
>
> REPORT: 5-line summary.

## Concurrency rules

- 4 subagents in one batch. G2 and G3 both touch cli.py but disjoint functions; orchestrator commits combined diff at end.
- File-ownership table is hard. No subagent edits files outside its column.

## Phase 7 + 8

Combined into one cross-family review (~3 reviewers). Tagging proceeds after intersection-must-fix items resolved.

## Risks

- **Doctor probe leaks money on real backends**: ~$0.0001 per probe. Document; default to `--no-probe` if env var `HERMES_S2S_DOCTOR_NO_PROBE_DEFAULT=1` is set; for now default is probe-on.
- **G2 wizard prompts can deadlock in non-TTY**: existing wizard already uses `input()` which raises EOFError. The `--profile` flag bypasses prompts entirely. Document.
- **Filler-audio Gemini message shape uncertainty**: research/05 documented the basic shape but not specifically the "say this text and continue" pattern. F1-A will need to verify against the live API in the smoke harness.
