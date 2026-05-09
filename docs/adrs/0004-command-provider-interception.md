# ADR-0004: Use Hermes's command-provider mechanism + a CLI shim for voice-mode interception

**Status:** accepted
**Date:** 2026-05-09
**Supersedes:** the "use `ctx.register_tool` to override `transcribe_audio`/`text_to_speech`" sketch in ADR-0001's Wave 1 plan. ADR-0001's high-level recommendation (extend, don't refactor) still holds.
**Deciders:** Codeseys, ARIA-orchestrator
**Driven by:** [research/04-hermes-tool-interception.md](../research/04-hermes-tool-interception.md)

## Context

Phase 3 research (deepwiki query against NousResearch/hermes-agent) revealed two surprises that invalidate the original plugin-hook routing plan:

1. **`pre_tool_call` hooks are observer-only** — they can `block` a call (returning an error to the model) but cannot supply a substitute result. They run before tool execution; their return values don't replace the tool's output. (Source: `model_tools.py:712-737`, `run_agent.py:~9841-9861`.)
2. **Hermes voice mode bypasses the tool registry entirely.** `gateway/run.py::_enrich_message_with_transcription` and `_send_voice_reply` import `transcribe_audio` and `text_to_speech_tool` as Python symbols and call them as Python functions, not as registry-dispatched tools. So even though `ctx.register_tool(name="transcribe_audio", ...)` is last-write-wins for LLM-invoked function calls, **it has no effect on the actual Discord/Telegram/CLI voice I/O path**.

The implications: the "register tools with the same name and override" strategy from the original Wave 1 plan would have produced a quietly broken result — moonshine/kokoro would work when the LLM explicitly called `transcribe_audio` as a tool, but not when a user spoke into Discord VC.

## Decision

Wire hermes-s2s providers into Hermes voice mode via the **command-provider mechanism** that already exists as a first-class extension point:

- **TTS:** `tts.providers.<name>: { type: command, command: "..." }` — Hermes writes the input text to a temp file, runs the command, reads the audio file the command writes. Built-in feature; explicitly designed for this purpose.
- **STT:** `HERMES_LOCAL_STT_COMMAND` env var — same pattern for transcription. (Less ergonomic than the TTS mechanism but functional.)

To make this work, hermes-s2s ships **CLI shims** as part of the package:

```
hermes-s2s-tts    # reads text from {input_path}, writes audio to {output_path}, uses Kokoro
hermes-s2s-stt    # reads audio from {input_path}, writes transcript to {output_path}.txt, uses Moonshine
hermes-s2s-call   # opens a duplex session to streaming-speech-to-speech (s2s-server mode)
```

The shims are tiny argparse wrappers around the providers we already have in `hermes_s2s/providers/{stt,tts}/`. Adding them to `pyproject.toml` `[project.scripts]` registers them on `pip install` so users get them on PATH.

Then `hermes s2s setup` (CLI subcommand, planned 0.2.0) writes the matching `~/.hermes/config.yaml` block:

```yaml
tts:
  provider: hermes-s2s-kokoro
  providers:
    hermes-s2s-kokoro:
      type: command
      command: "hermes-s2s-tts --provider kokoro --voice af_heart --output {output_path} --text-file {input_path}"
      output_format: wav
```

```bash
# ~/.hermes/.env
HERMES_LOCAL_STT_COMMAND='hermes-s2s-stt --provider moonshine --model tiny --output {output_path} --input {input_path}'
```

## Consequences

**Positive:**
- **Zero modifications to Hermes core needed** for 0.2.0. No fork, no upstream PR dependency, no merge conflicts. The integration is config-only on the Hermes side.
- **Works for all voice paths** — Discord VC, Telegram voice notes, CLI mic mode, programmatic `transcribe_audio` calls. The command provider is invoked everywhere voice happens because Hermes routes through `tts_tool.py` regardless of caller.
- **First-class mechanism** — Hermes maintainers explicitly designed the command-provider system "to allow integration of any CLI without Python code changes" (per the docs we extracted). We're using the API as intended.
- **Per-stage configurability is preserved** — users can mix `tts.provider: hermes-s2s-kokoro` with `stt.provider: groq` (Hermes built-in cloud) etc.

**Negative:**
- **Subprocess overhead per call.** Each transcription/synthesis spawns a Python process, which loads the model. For Moonshine + Kokoro on a 5090 this adds ~200-400ms of cold-start per call. Mitigations: (a) ship a long-lived daemon mode that the shims connect to via a Unix socket, lazy-loaded on first use; (b) accept the overhead for 0.2.0 and add the daemon in 0.2.1.
- **Duplex/realtime cannot be expressed** — command providers are inherently single-shot (text-in → audio-out). For 0.3.0 realtime mode, this mechanism doesn't fit and we revert to needing either an upstream `voice_pipeline_factory` hook or the monkeypatch bridge from ADR-0005.
- **Two different integration paths** — 0.2.0 uses command-providers; 0.3.0 uses a different mechanism for realtime. Documentation needs to make this clear.

## Implementation shape

### CLI shims (new)

```
hermes_s2s/cli_shims/
  __init__.py
  tts_shim.py           # `hermes-s2s-tts` entry point
  stt_shim.py           # `hermes-s2s-stt` entry point
  call_shim.py          # `hermes-s2s-call` (s2s-server duplex; 0.2.x stretch)
```

Each shim:
1. Argparse: `--provider`, `--input`, `--output`, plus per-provider knobs (`--voice`, `--model`, `--lang-code`).
2. Loads the matching provider via `hermes_s2s.registry.resolve_*`.
3. Reads input file → calls `provider.synthesize(text, output_path)` or `provider.transcribe(input_path)` → writes output (audio file or `.txt`).
4. Returns exit 0 / non-zero with stderr message on error.

### `hermes s2s setup` interactive command

Walks the user through:
- Pick a profile: `local-all` / `hybrid-privacy-in` / `cloud-cheap` / `realtime-gemini` / `realtime-openai` / `s2s-server` / `custom`.
- Probes installed dependencies (`moonshine_onnx`, `kokoro`, `ffmpeg`, env vars).
- Writes `~/.hermes/config.yaml` `tts:` and `stt:` blocks, plus `~/.hermes/.env` updates for `HERMES_LOCAL_STT_COMMAND` if needed.
- Prints next-step instructions ("restart Hermes; try `/voice on` then `/voice tts`").

### Daemon mode (0.2.1, deferred)

`hermes s2s serve --daemon` starts a long-lived process that:
- Loads Moonshine + Kokoro models once on startup.
- Listens on `~/.hermes/run/s2s.sock` (unix domain socket).
- The CLI shims become tiny socket-clients: write a JSON request, read a path back. Cold-start latency drops from ~300ms to ~5ms. The shim transparently falls back to in-process model load if the daemon isn't running, so the daemon is purely an optimization.

## Alternatives considered

- **Override `transcribe_audio` / `text_to_speech` via `ctx.register_tool` last-write-wins.** Confirmed broken: voice mode bypasses the registry. Would only work for LLM function-calls.
- **Monkey-patch `tools.transcription_tools._transcribe_local` at plugin load time.** Fragile; breaks on any Hermes version that refactors the function signature. Could work as an emergency bridge but maintenance burden is high.
- **Upstream PR adding a public `STT_PROVIDER_REGISTRY` / `TTS_PROVIDER_REGISTRY`.** Best long-term but blocks 0.2.0 on upstream review. Defer to "phase II" (after we have running plugin code to demonstrate value).
- **Proxy server approach** (run hermes-s2s as a localhost HTTP server, configure Hermes to point at it via `STT_OPENAI_BASE_URL` etc). Works for STT (since Hermes supports custom OpenAI-compatible STT base URLs) but doesn't work for TTS providers without an OpenAI-compatible TTS base URL. Asymmetric. Rejected.

## Open questions

1. The model-cache global in `hermes_s2s/providers/stt/moonshine.py` (`_MOONSHINE_CACHE`) and `tts/kokoro.py` (`_KOKORO_CACHE`) doesn't persist across subprocess invocations. The 0.2.1 daemon resolves this. For 0.2.0, every voice turn pays the model-load cost. **Acceptable for shipping; tracking as known issue in the README.**
2. How does the `hermes-s2s-stt` shim signal "transcription failed" to Hermes when invoked via `HERMES_LOCAL_STT_COMMAND`? Hermes reads the `.txt` output file; an empty file probably means "no speech detected." We need to verify what Hermes does on shim non-zero exit code.
