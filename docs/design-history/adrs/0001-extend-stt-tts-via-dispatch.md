# ADR-0001: Extend STT/TTS via existing if/elif dispatch (do not introduce a provider plugin abstraction)

**Status:** proposed
**Date:** 2026-05-09
**Deciders:** Codeseys (user), ARIA-orchestrator

## Context

Hermes already supports many STT (`local`, `local_command`, `groq`, `openai`, `mistral`, `xai`) and TTS (`edge`, `elevenlabs`, `openai`, `xai`, `minimax`, `mistral`, `gemini`, `neutts`, `kittentts`, `piper`) providers. We want to add Moonshine STT and Kokoro TTS to make the local-pipeline story competitive without forcing users to run a separate server.

Two extension paths exist:

**Option A — Match the existing pattern.** Add `_transcribe_moonshine()` and `_generate_kokoro()` functions in `tools/transcription_tools.py` and `tools/tts_tool.py`, hook them into the dispatch chains in `transcribe_audio()` and `text_to_speech_tool()`, add the names to `BUILTIN_TTS_PROVIDERS`, update `hermes_cli/setup.py` discovery.

**Option B — Introduce a `VoiceProviderPlugin` ABC.** Create a formal plugin type (parallel to memory/context-engine/image-gen) that voice providers must implement. Refactor existing providers to fit the ABC.

## Decision

**Option A.** Match the existing pattern.

## Rationale

1. **Refactoring a working system carries risk for no immediate user value.** The existing STT and TTS dispatch is battle-tested across 16 providers with extensive test coverage (`test_transcription_tools.py` 1348 LOC, `test_tts_command_providers.py` 500 LOC). Refactoring everything to fit a new ABC would risk regressions in providers users already depend on (ElevenLabs, OpenAI, Edge TTS, Groq).

2. **A `type: command` escape hatch already exists.** Users who want zero-Python integration can wire any STT/TTS CLI via `tts.providers.<name>: { type: command, command: "..." }` (and `HERMES_LOCAL_STT_COMMAND` for STT). For genuinely third-party providers, this is the right answer. For first-class native providers like Moonshine and Kokoro that benefit from in-process model caching and Python API access, native dispatch is appropriate.

3. **The if/elif chain is not a problem in practice.** Adding two more branches to a 9-branch dispatch is not architectural debt — it's the right shape for the cardinality involved.

4. **A future "VoiceProvider ABC" decision can still happen.** If/when realtime backends (ADR-0002) prove that a uniform abstraction is needed, we can do that refactor at a later wave with all the realtime-mode design pressure available, instead of speculatively now.

## Implementation shape

### Moonshine STT (`tools/transcription_tools.py`)

```python
# After the other _transcribe_* functions (around line 700):
def _transcribe_moonshine(file_path: str, model_name: str) -> Dict[str, Any]:
    """Transcribe using Moonshine (UsefulSensors, local, GPU/CPU)."""
    global _moonshine_model_cache
    # Lazy import — moonshine-onnx is optional
    try:
        import moonshine_onnx  # or 'moonshine' (non-onnx)
    except ImportError:
        return {"success": False, "transcript": "",
                "error": "moonshine-onnx not installed. pip install moonshine-onnx"}
    # ... model load with cache, .transcribe(), return dict
```

Add to dispatch in `transcribe_audio()`:
```python
if provider == "moonshine":
    moonshine_cfg = stt_config.get("moonshine", {})
    model_name = model or moonshine_cfg.get("model", "tiny")  # or "base"
    return _transcribe_moonshine(file_path, model_name)
```

Update `_get_provider()` doc-string to list moonshine as a recognized value.

### Kokoro TTS (`tools/tts_tool.py`)

Pattern-match on `_generate_piper_tts` (line 1402) — identical shape:
- Lazy import (`from kokoro import KPipeline` — this is already what user's v6 uses).
- Module-level cache `_kokoro_pipeline_cache`.
- Read `tts_config["kokoro"]` for `voice` (default `af_heart`), `lang_code` (default `'a'` for American English).
- Synthesize, concatenate audio chunks, write WAV.
- ffmpeg-convert to mp3/ogg if requested.

Add `"kokoro"` to `BUILTIN_TTS_PROVIDERS` frozenset.

Add dispatch branch in `text_to_speech_tool()` near the other built-ins.

### Wizard / docs

Update `hermes_cli/setup.py` STT and TTS provider lists, `hermes_cli/tools_config.py` `TOOL_CATEGORIES`, `website/docs/user-guide/features/voice-mode.md` provider tables, `website/docs/user-guide/configuration.md` config block examples.

## Consequences

**Positive:**
- Zero risk to existing 16 providers.
- Clean upstream-PR shape (additive, follows established pattern).
- Users get Moonshine + Kokoro with no new abstractions to learn.
- Tests can be parallel-style alongside `test_tts_piper.py`.

**Negative:**
- The if/elif chains grow by one branch each. Mitigation: cardinality is fine.
- Future realtime mode will need a different abstraction (see ADR-0002).

## Alternatives considered

- **VoiceProviderPlugin ABC + plugin-directory layout** — rejected, see rationale #1.
- **Wire Kokoro via `type: command`** — possible but loses Python streaming generator support; users running v6 already import KPipeline. A native provider gives us streaming + the same imports v6 uses.
- **Wire Moonshine via `HERMES_LOCAL_STT_COMMAND`** — possible but Moonshine's value proposition (sub-100ms inference) is best realized with a long-lived Python process holding the model in GPU memory; per-call subprocess startup destroys that.
