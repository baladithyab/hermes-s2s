# Wave plan: 0.2.0 — Hermes voice integration via command-provider shims

> ADRs: 0004 (interception strategy), 0005 (audio resampling not used in 0.2.0).
> Effort: ~1 day across 4 parallel subagents.
> Acceptance: `tts.provider: hermes-s2s-kokoro` and `HERMES_LOCAL_STT_COMMAND=hermes-s2s-stt ...` work end-to-end in Hermes voice mode.

## Goal

Ship CLI shims (`hermes-s2s-stt`, `hermes-s2s-tts`) that Hermes invokes via its command-provider mechanism. After 0.2.0:
- User runs `pip install hermes-s2s[local-all]`.
- User runs `hermes plugins enable hermes-s2s` then `hermes s2s setup`.
- The setup command writes the matching config block.
- User runs `hermes` and types `/voice on` (or joins a Discord VC); voice flows through Moonshine STT and Kokoro TTS without forking Hermes.

## Acceptance criteria

1. `pip install -e .` registers `hermes-s2s-tts` and `hermes-s2s-stt` console_scripts on PATH.
2. `hermes-s2s-tts --provider kokoro --text-file /tmp/in.txt --output /tmp/out.wav --voice af_heart` writes a valid 24kHz WAV.
3. `hermes-s2s-stt --provider moonshine --input /tmp/in.wav --output /tmp/out.txt` writes a transcript text file.
4. `hermes s2s setup` interactive command produces a working config.yaml block.
5. Smoke test: a Hermes session with `tts.provider: hermes-s2s-kokoro` (command type) calls `/voice tts` with a 1-line message and receives audio. (Manual smoke; tested by user, not in CI.)
6. Existing 8 smoke tests in `tests/test_smoke.py` still pass; 6 new tests cover the shims.

## File ownership

| Subagent | Owns | Touches | Reads |
|---|---|---|---|
| **W2-A: Shims** | `hermes_s2s/cli_shims/{__init__.py,tts_shim.py,stt_shim.py}` (new), `tests/test_cli_shims.py` (new) | `pyproject.toml` (`[project.scripts]`) | existing `providers/stt/moonshine.py`, `providers/tts/kokoro.py` |
| **W2-B: Setup wizard** | `hermes_s2s/cli.py` (extend with `setup` subcommand) | none | `config/__init__.py`, ADR-0004 examples |
| **W2-C: Provider polish** | `hermes_s2s/providers/stt/s2s_server.py`, `hermes_s2s/providers/tts/s2s_server.py` (URL-scheme dispatch: http vs ws) | none | existing impls |
| **W2-D: Docs** | `README.md` (add Quick Start "via Hermes voice mode" section), `docs/HOWTO-VOICE-MODE.md` (new) | none | ADR-0004 |

W2-A and W2-B are sequential within a single subagent if we hit the parallelism cap; otherwise parallel. W2-C and W2-D are independent of A/B.

## Subagent prompts

### W2-A — CLI shims

> Create CLI shims in `hermes_s2s/cli_shims/` that Hermes can invoke via its command-provider mechanism. ADR-0004 (`docs/adrs/0004-command-provider-interception.md`) is the spec.
>
> Create:
> 1. `hermes_s2s/cli_shims/__init__.py` (one-line module docstring)
> 2. `hermes_s2s/cli_shims/tts_shim.py` with `def main():` argparse handler:
>    - flags: `--provider {kokoro|s2s-server}`, `--text` (inline) or `--text-file PATH`, `--output PATH`, `--voice STR`, `--lang-code STR`, `--speed FLOAT`, `--endpoint URL` (for s2s-server)
>    - logic: read text → resolve provider via `hermes_s2s.registry.resolve_tts(provider, opts)` → `provider.synthesize(text, output)` → exit 0 on success, non-zero with stderr on error
> 3. `hermes_s2s/cli_shims/stt_shim.py` with `def main():` argparse handler:
>    - flags: `--provider {moonshine|s2s-server}`, `--input PATH`, `--output PATH`, `--model {tiny|base}`, `--device {cuda|cpu}`, `--endpoint URL`
>    - logic: resolve provider → `provider.transcribe(input)` → write transcript to `output` path (Hermes's `HERMES_LOCAL_STT_COMMAND` reads back a text file matching the audio basename, but check ADR-0004 §"open questions" — exact contract is TBD; for safety, support both `--output FOO.txt` and writing to a `.txt` next to the audio)
> 4. `tests/test_cli_shims.py` with 6 tests using subprocess.run + mocked providers:
>    - tts_shim happy path (kokoro mock yields audio chunks)
>    - tts_shim missing-input error
>    - stt_shim happy path (moonshine mock returns "hello")
>    - stt_shim missing-input error
>    - tts_shim invalid-provider error
>    - stt_shim s2s-server endpoint error (connection refused)
>
> Update `pyproject.toml` `[project.scripts]`:
> ```toml
> [project.scripts]
> hermes-s2s-tts = "hermes_s2s.cli_shims.tts_shim:main"
> hermes-s2s-stt = "hermes_s2s.cli_shims.stt_shim:main"
> ```
>
> CONSTRAINTS: don't load heavy deps (kokoro, moonshine_onnx) at import time — only when `provider.synthesize/transcribe` is called. Each shim total < 100 lines. Tests use `monkeypatch` to stub the providers, no real model downloads.
>
> Verify: `cd /mnt/e/CS/github/hermes-s2s && pip install -e . --quiet` (skip if pip unavailable; run `python -m pytest tests/ -q` to confirm 8 existing + 6 new tests pass).
>
> REPORT: 5-line summary, files touched + LOC, any decisions, test result count.

### W2-B — `hermes s2s setup` interactive wizard

> Extend `hermes_s2s/cli.py` with a `setup` subcommand. Goal: walk a user through choosing a profile and writing the matching config blocks.
>
> Add to `setup_argparse`:
> ```python
> p_setup = subs.add_parser("setup", help="Interactively configure Hermes voice mode for hermes-s2s")
> p_setup.add_argument("--profile", choices=["local-all", "hybrid-privacy", "cloud-cheap", "s2s-server", "custom"], default=None, help="Skip interactive prompt and pick a profile")
> p_setup.add_argument("--dry-run", action="store_true", help="Print the config that would be written; don't modify files")
> ```
>
> Add `def cmd_setup(args)`:
> 1. If `--profile` not given, prompt with a numbered menu (use plain `input()`; no rich/click deps).
> 2. For each profile, build the right `tts:` and `stt:` blocks targeting `hermes-s2s-tts` / `hermes-s2s-stt`. ADR-0004 has the exact YAML.
> 3. Detect installed deps:
>    - `moonshine_onnx`: `importlib.util.find_spec`
>    - `kokoro`: same
>    - `ffmpeg`: `shutil.which("ffmpeg")`
>    - `GEMINI_API_KEY` / `OPENAI_API_KEY` env vars
>    Surface a "Missing: …" warning if a chosen profile needs a missing dep.
> 4. If not `--dry-run`: read `~/.hermes/config.yaml`, merge new keys (preserve existing), write back. Use `pyyaml` (already a dep).
> 5. If profile needs `HERMES_LOCAL_STT_COMMAND`: append to `~/.hermes/.env` (idempotent — check if line already present).
> 6. Print a "Next steps" block with the exact `/voice on` / restart instructions.
>
> Tests: `tests/test_setup_wizard.py` with 4 tests:
>    - `--profile local-all --dry-run` produces the expected dict
>    - `--profile hybrid-privacy` writes the right blocks to a tmp_path
>    - missing-dep warning surfaces
>    - existing config keys preserved (don't clobber unrelated tts settings)
>
> CONSTRAINTS: don't import heavy deps. Use `tmp_path` fixtures for write tests. Total < 200 lines including tests.
>
> REPORT: 5-line summary.

### W2-C — Provider polish (URL-scheme dispatch for s2s-server)

> Update `hermes_s2s/providers/stt/s2s_server.py` and `hermes_s2s/providers/tts/s2s_server.py` so they auto-detect HTTP vs WebSocket endpoints and route accordingly.
>
> Current state: both providers assume HTTP (`/asr` and `/tts` REST endpoints). Future: 0.2.x stretch will add WebSocket pipeline support but for 0.2.0 we just need the provider to fail gracefully on `ws://` endpoints with a clear message pointing to the planned `/asr`/`/tts` REST endpoints in upstream streaming-speech-to-speech.
>
> For each provider:
> 1. In `__init__`, parse the endpoint scheme. If `ws://` or `wss://`, log a warning: "s2s-server STT/TTS in 0.2.0 requires HTTP endpoints (/asr, /tts). WebSocket pipeline mode coming in 0.3.0 — see github.com/baladithyab/hermes-s2s ROADMAP.md".
> 2. If `http://` or `https://`, work as today.
> 3. Add a `health_check()` method that hits the endpoint root with a timeout and returns `(ok: bool, message: str)`.
> 4. If `s2s.s2s_server.fallback_to: pipeline` is set in config and the call fails (connection refused, 5xx), the provider should raise a specific `S2SServerUnavailable` exception (defined in this module) so the caller can decide to fall through. Don't actually do the fallback in the provider; the routing layer (Hermes side, via the config) handles it.
>
> Tests: extend or create `tests/test_s2s_server_provider.py` with 4 tests using `httpx.MockTransport`:
>    - HTTP endpoint happy path
>    - WebSocket endpoint warns and rejects
>    - Connection refused → `S2SServerUnavailable`
>    - `health_check` happy + sad paths
>
> CONSTRAINTS: don't import websockets; that's 0.3.0. Total ~80 LOC across both providers + tests.
>
> REPORT: 5-line summary.

### W2-D — README + HOWTO docs

> Update `README.md` and create `docs/HOWTO-VOICE-MODE.md`.
>
> README updates:
> - Replace the existing "Quick start" section with a 3-step recipe targeting Hermes voice mode (not raw Python API):
>   1. `pip install hermes-s2s[local-all]`
>   2. `hermes plugins enable hermes-s2s`
>   3. `hermes s2s setup` (pick `local-all` profile)
>   Then: `hermes` → `/voice on` → speak.
> - Add a "What this changes in your Hermes config" callout showing the YAML that `hermes s2s setup` writes.
> - Update the Status section: 0.2.0 = "command-provider integration via shims; works in Hermes voice mode without forking".
>
> New `docs/HOWTO-VOICE-MODE.md` (~150 lines):
> - Section 1: How Hermes voice mode finds providers (cite ADR-0004 briefly).
> - Section 2: The `tts.providers.<name>: type: command` mechanism with a worked example.
> - Section 3: The `HERMES_LOCAL_STT_COMMAND` env var with a worked example.
> - Section 4: Troubleshooting — "voice mode joins VC but doesn't hear me" / "TTS produces silence" / "shims hang".
> - Section 5: Daemon mode (0.2.1 preview, opt-in via `--daemon` flag once shipped).
>
> CONSTRAINTS: Match existing README markdown style. No aspirational claims about realtime / Discord seam (that's 0.3.0). Cite GitHub URLs not local paths.
>
> REPORT: 5-line summary.

## Concurrency rules

- 4 subagents in one batch (`delegation.max_concurrent_children` is 3 by default — bump request to 4, if rejected, run W2-D last as a follow-up).
- File-ownership table is hard. No subagent writes files outside its column.
- W2-A owns `pyproject.toml` `[project.scripts]` edit; W2-B does not touch it.

## Phase 7 (concurrent review)

- **R1 cold review:** `openai/gpt-5` reads the W2-A diff cold, checks for: shim error handling, exit codes, race conditions in subprocess invocation, model-loading-on-import (must not), test mock realism.
- **R2 doc-vs-code:** `google/gemini-3-pro-preview` reads README + HOWTO + ADR-0004 + actual shim flag definitions cold, reports any drift between what docs claim and what shims accept.

## Phase 8 (cross-family final review, after Wave 2 commit)

3 reviewers, different families. CONFIRMED / ISSUES / QUESTIONS in <300 lines each.
- `anthropic/claude-opus-4.7` (control)
- `moonshotai/kimi-k2-thinking`
- `google/gemini-3-pro-preview`

Per memory pitfalls: probe each model's availability with a 1-line `delegate_task(model=…, goal="echo hello")` before dispatching the full review.
