# Wave 1 Plan — Moonshine STT + Kokoro TTS providers

> Status: ready to execute
> ADR refs: ADR-0001 (extend via dispatch)
> Estimated effort: 1 day across 4 parallel subagents
> Branch: feat/aria-voice (off baladithyab/hermes-agent main)
> Target: clean upstream PR to NousResearch/hermes-agent

## Goal

Add `moonshine` as a 7th STT provider and `kokoro` as an 11th TTS provider to Hermes, matching the established `if/elif` dispatch pattern. No new abstractions. No breaking changes. Drop-in additive.

## Acceptance criteria (executable)

1. `tts.provider: kokoro` in `~/.hermes/config.yaml` produces a playable mp3/ogg from the existing `text_to_speech` tool with no other config required (defaults: `voice: af_heart`, `lang_code: a`).
2. `stt.provider: moonshine` in `~/.hermes/config.yaml` transcribes a 1-2s WAV via `transcribe_audio` and returns success=True with non-empty transcript.
3. New tests pass: `tests/tools/test_tts_kokoro.py` and `tests/tools/test_transcription_moonshine.py` — each ≥3 cases (happy, missing dep, fallback) using mocks; no real model download in CI.
4. Existing tests do NOT regress: `pytest tests/tools/test_transcription_tools.py tests/tools/test_tts_*` clean.
5. `hermes_cli/setup.py` interactive wizard lists `moonshine` (STT) and `kokoro` (TTS) under their respective categories.
6. Doc pages updated: `website/docs/user-guide/features/voice-mode.md` provider tables, `website/docs/guides/use-voice-mode-with-hermes.md` examples reference the new providers, `website/docs/user-guide/configuration.md` has config-block examples.
7. `pyproject.toml` adds optional extras: `moonshine = ["moonshine-onnx"]` and `kokoro = ["kokoro>=0.7"]` (or whatever the actual current pkg is — verify pre-execution). The `voice` extra includes both.

## File ownership (for parallel subagents)

| Subagent | Owns | Touches | Reads only |
|---|---|---|---|
| **W1-A: Moonshine impl** | `tools/transcription_tools.py` (additions), `tests/tools/test_transcription_moonshine.py` (new) | `pyproject.toml` (extra) | existing `_transcribe_local`, `_transcribe_groq` for pattern |
| **W1-B: Kokoro impl** | `tools/tts_tool.py` (additions), `tests/tools/test_tts_kokoro.py` (new) | `pyproject.toml` (extra) | existing `_generate_piper_tts` for pattern |
| **W1-C: Wizard + config** | `hermes_cli/setup.py`, `hermes_cli/tools_config.py` | none | provider lists |
| **W1-D: Docs** | `website/docs/user-guide/features/voice-mode.md`, `website/docs/guides/use-voice-mode-with-hermes.md`, `website/docs/user-guide/configuration.md` | none | Hermes voice-mode docs structure |

**Conflicts:** W1-A and W1-B both touch `pyproject.toml` (different extras). To avoid a merge race, W1-A owns the dependency block edit; W1-B's prompt instructs it to skip that file and let W1-A handle it.

## Pre-execution verification (orchestrator does this before dispatching)

The pkg names matter — bad assumption here will sink the wave.

**Moonshine package:** Verified via the v6 pipeline: `from transformers import AutoProcessor, MoonshineForConditionalGeneration` works (transformers >= 4.43). There's also `useful-moonshine` and `moonshine-onnx`. Use `moonshine-onnx` as the canonical install — it's the smallest dep, doesn't require torch. **HF model:** `UsefulSensors/moonshine-tiny` (27M) and `UsefulSensors/moonshine-base` (61M).

**Kokoro package:** Verified via v6: `from kokoro import KPipeline; KPipeline(lang_code='a')`. Pkg: `kokoro` on PyPI (Apache-2.0, requires espeak-ng for phonemizer — already a Hermes voice-mode system dep for NeuTTS). **HF model:** `hexgrad/Kokoro-82M`. Default voice `af_heart` (Hexgrad-trained American Female).

Both confirmed against user's `https://github.com/codeseys/streaming-speech-to-speech (local checkout) pipelines/v6_final.py` lines 130-170 — exact import shape and usage.

## Subagent prompts (sketches — actual prompts in the dispatch call)

### W1-A — Moonshine STT

> Add a Moonshine STT provider to Hermes following ADR-0001. Read the existing `_transcribe_local` function in `tools/transcription_tools.py` (lines 390-447) for the pattern. Add `_transcribe_moonshine()` after `_transcribe_xai`, hook it into the dispatch in `transcribe_audio()` (line 789-868), update `_get_provider()` doc-string. Use `moonshine-onnx` package (`MoonshineOnnxModel`); fall back to transformers (`MoonshineForConditionalGeneration`) if onnx is unavailable. Cache the model module-level keyed by model size + device. Read input via soundfile + scipy.signal.resample to 16kHz mono. Return `{success, transcript, provider, error?}` shaped exactly like `_transcribe_local`. Add tests at `tests/tools/test_transcription_moonshine.py` with happy-path (mock model), missing-package (raise ImportError), bad audio (return error dict). Update `pyproject.toml`: add `moonshine = ["moonshine-onnx>=0.2"]` extra (verify version on PyPI), include in `voice` extra. Do NOT touch `tools/tts_tool.py` or anything outside the listed files.

### W1-B — Kokoro TTS

> Add a Kokoro TTS provider to Hermes following ADR-0001. Read `_generate_piper_tts` (`tools/tts_tool.py` line 1402-1477) for the exact pattern to match — it's the closest precedent. Add `_generate_kokoro_tts()` after `_generate_kittentts`, add `"kokoro"` to `BUILTIN_TTS_PROVIDERS` frozenset (line 316). Hook into dispatch in `text_to_speech_tool()`. Use `from kokoro import KPipeline`; cache `_kokoro_pipeline_cache` keyed by `(lang_code, voice)`. Read `tts_config["kokoro"]` for `voice` (default `"af_heart"`), `lang_code` (default `"a"`), `speed` (default `1.0`). Iterate `KPipeline(text, voice, ...)` collecting numpy audio chunks; concatenate; write 24kHz WAV via soundfile; ffmpeg-convert to mp3/ogg/etc. if requested (mirror `_generate_piper_tts` ffmpeg pattern). Tests at `tests/tools/test_tts_kokoro.py`: happy (mock pipeline), missing pkg (raise), basic config validation. Do NOT touch `pyproject.toml` (W1-A owns it). Do NOT touch `tools/transcription_tools.py`.

### W1-C — Wizard + config integration

> Add `moonshine` to STT provider lists and `kokoro` to TTS provider lists in `hermes_cli/setup.py` (in the providers list) and `hermes_cli/tools_config.py` (in `TOOL_CATEGORIES`). Search for where `piper` and `neutts` are listed and add `kokoro` alongside them (Kokoro is a peer of those). Search for where `local`, `groq`, `openai`, `mistral` STT providers are listed and add `moonshine` (Moonshine is a peer of `local` — both are local STT). Read the surrounding code so the new entries match the existing dict/list shape exactly. Do NOT touch tools/, tests/, docs/.

### W1-D — Documentation

> Update Hermes voice-mode docs. Read `website/docs/user-guide/features/voice-mode.md` and `website/docs/guides/use-voice-mode-with-hermes.md` first to understand structure. In voice-mode.md: add `moonshine` row to the STT provider comparison table; add `kokoro` row to the TTS provider comparison table; mention `pip install hermes-agent[voice]` covers both. In use-voice-mode-with-hermes.md: add a "Provider recommendations — Kokoro" subsection in TTS recommendations (Apache-2.0, 82M, fast, sounds great); add Moonshine to STT recommendations (faster than faster-whisper-base, MIT, GPU-friendly). In `website/docs/user-guide/configuration.md`: add config-block examples for both. Do NOT touch source code.

## Concurrency rules

- Each subagent gets its own task in a single `delegate_task(tasks=[...])` batch, runs in parallel.
- Concurrency cap: 4. Matches `delegation.max_concurrent_children` default (3) — bumped to 4 for this wave because tasks are well-isolated. If config rejects 4, run W1-D last as a follow-up.
- File-ownership table is hard. No subagent reads or writes a file outside its column.
- All subagents commit their work via the orchestrator at end of wave (single squash-friendly commit).

## Phase 7 (concurrent review during execution)

Spawn alongside the 4 execution subagents:

- **R1: cold-review provider impl** — model: `google/gemini-3-pro-preview`. Read the W1-A and W1-B diffs cold (no rationale), report security/correctness/style mismatch vs. existing providers. Specifically check: lazy import shape, error dict shape, model-cache eviction safety, ffmpeg fallback path.
- **R2: doc accuracy** — model: `openai/gpt-5`. Read the W1-D diff cold, verify provider tables align with what W1-A/W1-B actually expose (config keys, env vars, defaults).

## Phase 8 (cross-family final review, after Wave 1 commit)

3 reviewers, different families. Each reads the entire Wave 1 commit cold and reports CONFIRMED / ISSUES (severity) / QUESTIONS:

1. `anthropic/claude-opus-4.7` (control)
2. `moonshotai/kimi-k2-thinking`
3. `deepseek/deepseek-v3.2-exp` (or whatever DeepSeek slug Hermes config has — verify before passing — fall back to `nvidia/nemotron-3-super-120b-a12b`).

Per memory pitfalls: probe model availability with a 1-line `delegate_task(model=…, goal="echo hello")` before relying on cross-family routing.

## Risks

- **Pkg version drift:** moonshine-onnx and kokoro both move fast. Pin minor versions, document upgrade path.
- **espeak-ng on Windows:** Kokoro's phonemizer needs espeak-ng. Hermes docs already note this for NeuTTS — reuse the messaging.
- **Moonshine 16kHz resample cost:** scipy.signal.resample is ~3-5ms per second of audio at 48k→16k. Acceptable for a 30s clip, irrelevant for short voice messages. Document.
- **CUDA optional:** Both work CPU-only. Wave 1 ships CPU+GPU; the GPU path uses `device_map="auto"` for transformers fallback or the onnx CUDAExecutionProvider for moonshine-onnx. Tests stay on CPU.

## Rollback

Single feature branch. If Phase 8 review surfaces blockers, hard-reset to bd27436b0 and rebuild from this plan. No production traffic depends on these providers; risk surface is limited to opt-in users.
