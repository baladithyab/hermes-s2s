# Wave 0.2.0 — Cold Review (commit dff7a95)

Reviewer: subagent, cold read of diff. Commit-message rationale intentionally deferred until after notes were formed.

Scope: `cli_shims/{tts,stt}_shim.py`, `cli.py` (setup wizard), `providers/{stt,tts}/s2s_server.py`, tests, pyproject, docs.

---

## CONFIRMED ✅

- **Lazy-import discipline holds.** `python3 -c 'import hermes_s2s.cli_shims.tts_shim, ...stt_shim'` → `sys.modules` scan for kokoro/moonshine/torch returns `[]`. Shims import only `argparse`, `sys`, `pathlib` at module load. Registry + provider registration happen inside `main()` only (`tts_shim.py:55-56`, `stt_shim.py:59-60`). Factories in `providers/tts/kokoro.py:38` and `providers/stt/moonshine.py:24` late-import the heavy deps. ✅
- **`[project.scripts]` entries wired** (`pyproject.toml:57-59`) so `hermes-s2s-tts` / `hermes-s2s-stt` land on `PATH` via `pip install`.
- **Subprocess exit-code contract** correct: both shims return `1` and write `"hermes-s2s-{tts,stt} error: <reason>"` to stderr on any exception (`tts_shim.py:63-65`, `stt_shim.py:75-77`). Tests exercise this via real subprocess in `test_cli_shims.py:74-99, 135-166`.
- **s2s-server scheme dispatch** works: `http(s)://` goes through; `ws(s)://` logs a warning at construct time and raises `S2SServerUnavailable` on use (`providers/stt/s2s_server.py:31-35, 57-60`; tts symmetric). Tested in `test_s2s_server_provider.py:53-65`.
- **`S2SServerUnavailable` dual-module definition is safe for `except RuntimeError`.** Both classes subclass `RuntimeError` directly (`providers/stt/s2s_server.py:22`, `providers/tts/s2s_server.py:21`). A caller catching `RuntimeError` catches both. A caller that imports only one and catches *that* symbol will miss the other — documented inline (`providers/stt/s2s_server.py:7-9`) but see P2 below.
- **Idempotent `.env` append** via marker line (`cli.py:33, 145-150`); tested in `test_setup_wizard.py:55-57`.
- **Deep-merge preserves unrelated config keys** (`cli.py:132-138`); tested in `test_setup_wizard.py:80-107`.
- **Health-check 2s GET /health** as promised (`providers/*/s2s_server.py:49-54`). Tested happy + sad path.

---

## ISSUES

### P0

- **None confirmed P0 from the diff alone.** The integration contract with *external* Hermes (how it substitutes `{output_path}` / where it reads STT output from) is not verifiable from this repo; see P1-A / QUESTIONS.

### P1

- **P1-A: STT `--output` vs sibling `.txt` contract is asserted, not proven.** ADR-0004 §"open questions" #2 (docs/adrs/0004-command-provider-interception.md:108) explicitly flags "Hermes reads the `.txt` output file" as unverified, and the wave plan (wave-0.2.0-command-shims.md:48) told the implementer to *support both* `--output FOO.txt` **and** writing to `<audio>.txt` next to the audio. The shipped shim does **only one**: honors `--output` if given, else writes to `Path(input).with_suffix(input.suffix + ".txt")` (`stt_shim.py:44-48`). If Hermes substitutes `{output_path}` the contract is fine; if Hermes ignores `{output_path}` and reads a sibling `.txt`, it breaks when Hermes's `{output_path}` is in a different dir (e.g., `/tmp/hermes_transcripts/xyz.txt`) while audio is at `/tmp/audio/abc.wav`. **Mitigation for safety:** also write the transcript to the audio-sibling `.txt` (cheap double-write) until the contract is empirically verified against a running Hermes.
- **P1-B: Default output-path naming is `<input>.<ext>.txt`, not `<input>.txt`.** `Path("clip.wav").with_suffix(".wav" + ".txt")` → `clip.wav.txt` (`stt_shim.py:48`). HOWTO-VOICE-MODE.md:63 ambiguously says "defaults to `<input>.txt` next to the audio" — reader will expect `clip.txt`. If Hermes expects the basename-stripped form, default-path behavior is wrong. Either change to `inp.with_suffix(".txt")` or fix the docs to be explicit.
- **P1-C: Kokoro mock stubs the wrong layer.** `test_cli_shims.py:55` monkey-patches `KokoroTTS.synthesize` wholesale, so nothing in the test suite ever exercises the `(graphemes, phonemes, audio)` unpacking at `providers/tts/kokoro.py:62`. A real-KPipeline shape regression (e.g., KPipeline adds a 4th tuple element in a new kokoro release) passes tests and breaks in prod. Add at least one test that patches `kokoro.KPipeline` with a generator yielding `("g", "p", np.zeros(240, dtype=np.float32))` tuples and lets `KokoroTTS.synthesize` run to disk.
- **P1-D: Single-quoting in `.env` is brittle.** `cli.py:147` writes `HERMES_LOCAL_STT_COMMAND='{command}'` with a hard-coded single quote. The command template `hermes-s2s-stt --provider moonshine --model tiny --output {output_path} --input {input_path}` contains no single quotes today, but any future template containing an apostrophe (or a user typing one into the `custom` prompt at `cli.py:90, 92`) will produce a malformed `.env` line. Use `shlex.quote(command)` or escape defensively.

### P2

- **P2-A: Duplicate `S2SServerUnavailable` definitions.** Two separate classes with identical names/bases (`providers/stt/s2s_server.py:22`, `providers/tts/s2s_server.py:21`). The inline comment (stt s2s_server.py:7-9) rationalizes this as "keep the provider surface flat," but it means `isinstance(exc, SttUnavail)` returns `False` for a tts-raised instance. Any future caller-side code that does `except S2SServerUnavailable` (singular import) will silently miss the other side. Recommend moving to `hermes_s2s/_internal/errors.py` (that module already exists per `pyproject.toml:65`) and re-exporting from both provider files. Low urgency because no internal caller catches it today.
- **P2-B: `hermes s2s setup` dispatch skips result-printing for the setup branch.** `cli.py:250-252` returns early from `dispatch()` after `cmd_setup(args)` so the json-pretty-print block (`cli.py:259-263`) is skipped. That's intentional (setup already prints human-readable status), but `cmd_setup` returns an `int` that `dispatch` discards — the shell exit code from `hermes s2s setup` will not reflect `cmd_setup`'s return value unless the outer dispatcher inspects it. If the parent CLI treats `func` return as exit code, fine; if not, non-zero setup failures exit 0.
- **P2-C: `cli.py` `_custom_prompt()` default-command hint is broken.** `cli.py:90` says `"TTS command for {tts_provider} [leave blank for default kokoro]"` but blank input produces **no `providers` block at all** (`cli.py:94-97` — `if tts_cmd:` gates the entire block). Blank input means "Hermes must resolve `tts_provider` natively" — which for `hermes-s2s-kokoro` Hermes cannot do (it's not a built-in Hermes provider name). User will get a silent mis-config. Either (a) drop the misleading "default kokoro" hint or (b) fall back to the `local-all` template when the user leaves it blank and accepts the default provider name.
- **P2-D: Setup wizard's `local-all` TTS command omits `--lang-code a`.** `cli.py:44-47` hardcodes `--voice af_heart --output ... --text-file ...` but skips `--lang-code`. HOWTO-VOICE-MODE.md:28 shows `--lang-code a` in the documented output. Shim default is `"a"` (`tts_shim.py:26`) so behavior is identical, but the generated config diverges from the docs → future voice-pack swaps may bite.
- **P2-E: pyproject version still `0.1.0`** (`pyproject.toml:7`) despite commit being `feat(0.2.0)`. Intentional? Release-tagging convention? Noted.
- **P2-F: s2s-server TTS `.wav` → non-wav rename without ffmpeg corrupts container.** `providers/tts/s2s_server.py:104-105`: when ffmpeg is missing and caller asked for `foo.mp3`, code does `os.rename(wav_path, output_path)` → renames a RIFF/WAV byte-stream to `foo.mp3`. Players that honor the extension will choke. Parallel to kokoro.py:94-96 (same pattern — pre-existing). Prefer logging a clear warning or failing loudly rather than silent-rename.
- **P2-G: `S2SServerTTS.synthesize` return-type inconsistency with `KokoroTTS.`** Both return `str` output-path. OK. But the shim's `provider.synthesize(text, args.output)` (`tts_shim.py:61`) discards the return value. If ffmpeg conversion changes the path, the shim is fine (it uses `args.output` for exit status), but any logging consumer loses the actual written path. Minor.
- **P2-H: `_custom_prompt()` hardcodes `output_format: wav`** (`cli.py:96`) regardless of what command the user wrote. If user writes an ogg-emitting command, config lies. Parameterize or drop.

---

## QUESTIONS ❓

1. **Does Hermes substitute `{output_path}` before exec, or is the placeholder consumed in-shim?** Shims treat `--output` as a concrete argv value (`tts_shim.py:24`, `stt_shim.py:22-26`), assuming Hermes does the substitution. If Hermes literally passes the string `{output_path}` through, the shim will try to write to a file literally named `{output_path}`. Needs a smoke test against a real Hermes.
2. **STT text-back-read mechanism.** ADR-0004:108 says Hermes reads the `.txt` — is that `{output_path}` or `<input>.txt`? P1-A above. Can this be resolved by reading Hermes source or running a live integration? Worth doing before 0.2.1.
3. **`output_format: wav` in the TTS YAML block (`cli.py:48, 78`) — is that a Hermes-parsed key or ignored?** HOWTO-VOICE-MODE.md:29 reproduces it as if meaningful. If Hermes ignores it, harmless. If Hermes uses it to format-convert *after* the shim returns, the shim's own wav-output alignment (`providers/tts/kokoro.py:77-80`, `providers/tts/s2s_server.py:85-106`) may double-convert.
4. **Setup wizard `.env` writing assumes Hermes auto-loads `~/.hermes/.env`** (HOWTO-VOICE-MODE.md:69 even notes "or let your shell auto-load it"). Does Hermes itself `dotenv`-load that file, or does the user have to source it manually? If the latter, setup should print a clearer "you must source this" next-step.
5. **Why is `make_moonshine` model_name not mapped from the CLI-friendly `tiny|base` to the canonical HF/ONNX repo name?** `stt_shim.py:27` advertises `--model {tiny|base}`; `make_moonshine` passes the raw string to `MoonshineOnnxModel(model_name="tiny")`. If that library expects `"moonshine/tiny"` or similar, this breaks at runtime. Pre-existing to this commit but exposed more broadly by the shim advertising both choices.

---

## SUMMARY

Diff is clean, tight, and mostly correct. The lazy-import discipline is real and verified. The main technical risk is the **unverified Hermes-side contract** for how `{output_path}` / sibling-`.txt` is consumed on the STT side (P1-A) — the shim makes a reasonable assumption but doesn't hedge, despite the wave plan telling it to hedge. The test suite's kokoro mock is too coarse (P1-C) — it validates glue but not the tuple-unpack contract. Everything else is polish.

Recommended before cutting 0.2.0 final:
1. Smoke-test against a running Hermes to pin down P1-A / Q1 / Q2. If unclear, hedge by dual-writing sibling `.txt` (P1-A mitigation).
2. Add a real-KPipeline-shape test (P1-C).
3. `shlex.quote` the `.env` value (P1-D).
4. Fix the misleading "default kokoro" custom-prompt hint (P2-C).
5. Bump `pyproject.toml` version to `0.2.0` (P2-E).
