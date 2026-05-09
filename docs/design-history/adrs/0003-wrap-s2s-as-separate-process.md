# ADR-0003: Wrap user's streaming-speech-to-speech as a separate process, not absorb its code

**Status:** proposed
**Date:** 2026-05-09
**Deciders:** Codeseys (user), ARIA-orchestrator

## Context

User maintains a production research project at `/mnt/e/CS/HF/streaming-speech-to-speech/` (v6: 212ms best, 239ms median TTFA on A10G). It already has:

- A complete FastAPI WebSocket server (`server/app/main.py`, `ws.py`, `pipeline.py`).
- Moonshine + vLLM + Kokoro composition with all v1→v6 optimizations.
- ADR-driven design (`docs/adr/0001-0004`).
- Phase-2 plan for NeMo FastConformer + StreamingInput.
- A live HF Space demo and a published HF README that's part of his research portfolio.
- Independent maintenance cadence (commits, branches, codex critiques, Playwright tests).

Hermes wants to offer a "use my local pipeline" backend option. Three integration shapes:

**A. In-process import.** Hermes imports `pipelines.v6_final.run_v6` directly. Hermes's process loads vLLM (~2GB VRAM idle), Moonshine, Kokoro on startup.

**B. Subprocess management.** Hermes auto-launches `streaming-speech-to-speech/server/app/main.py` as a child process and connects to its WS endpoint.

**C. External-server reference.** User runs the streaming-speech-to-speech server independently (already does); Hermes connects to it as a WS client. Hermes does not own the server's lifecycle.

## Decision

**Option C, with optional Option B as a convenience for fresh installs.**

Default: external-server. User keeps streaming-speech-to-speech as a standalone product. Hermes config points at its WS endpoint (`voice.local.endpoint: ws://localhost:8000/ws`). Hermes is a WS client.

Convenience: a `hermes voice serve` subcommand that finds an installed `streaming-speech-to-speech` checkout (env `STREAMING_S2S_PATH` or default `~/streaming-speech-to-speech/`) and runs its server. Pure DX sugar; no required dependency.

## Rationale

1. **Don't take ownership of someone else's product.** The S2S repo has its own README, its own CI, its own demo Space, its own audience. Hermes shouldn't fork its code. If it changes (e.g., Phase 2 NeMo migration), the user shouldn't have to update Hermes.

2. **vLLM in Hermes's process is a footgun.** Hermes is meant to start in <2s. vLLM startup is 30-60s and pins ~2GB VRAM idle. Forcing every Hermes user to load vLLM just because some users want local S2S is wrong. Subprocess + WS keeps the cost out of Hermes's normal startup.

3. **Concurrent-process is what the S2S code is designed for.** The FastAPI server already handles concurrent WS sessions, has its own backpressure model, its own benchmark harness. Wrapping it in-process means re-implementing that.

4. **Failure isolation.** If vLLM crashes (OOM, CUDA error, model load fail), Hermes stays up and degrades to cloud STT/TTS gracefully. Co-process means Hermes crashes too.

5. **Replaceability.** Treating S2S as a WS-protocol contract means any future server that speaks the same protocol works (LiveKit Agents, Pipecat, custom). The contract becomes the moat, not the import path.

## Implementation shape

### Hermes-side: a new `s2s-local` provider type

This is a different beast from STT/TTS. It owns the entire user-turn — STT + LLM + TTS happens inside the external server. So it doesn't slot into the STT-then-LLM-then-TTS dispatch chain.

Two integration modes:

**Mode 1 — full ownership (`voice.mode: "s2s-local"`):**
- Hermes voice handler opens WS to S2S server.
- Sends audio frames in.
- Receives audio frames + transcripts out.
- The local pipeline IS the agent for that turn. No Hermes LLM call. No Hermes tools.
- This is what the v6 demo does today.
- Useful when: low-latency conversational UX matters more than tool access.

**Mode 2 — STT-only or TTS-only via local server (cascaded mode with local stages):**
- `stt.provider: "s2s-local"` — Hermes sends audio to the local server, gets transcript back, then proceeds with normal Hermes LLM + TTS.
- `tts.provider: "s2s-local"` — Hermes sends text to the local server, gets audio back. Skips the LLM in the local server.
- Requires the S2S server to expose dedicated STT and TTS endpoints (not just full-pipeline). May need a small PR to streaming-speech-to-speech to add `/asr` and `/tts` endpoints alongside the existing `/ws`.

### Config schema

```yaml
voice:
  mode: "cascaded"  # | "s2s-local" | "realtime"
  s2s_local:
    endpoint: "ws://localhost:8000/ws"
    health_url: "http://localhost:8000/health"  # optional pre-flight check
    fallback_provider: "groq"  # if local server is down, fall back to Groq STT (cascaded mode)
    auto_launch: false  # if true, run `hermes voice serve` on first use

stt:
  provider: "s2s-local"  # use local server for just STT
  s2s_local:
    endpoint: "ws://localhost:8000/asr"  # if S2S server exposes a /asr-only path

tts:
  provider: "s2s-local"
  s2s_local:
    endpoint: "ws://localhost:8000/tts"
    voice: "af_heart"  # passed to the server
```

### WS protocol

Define a small JSON+binary protocol Hermes implements as a client. Match what the existing `streaming-speech-to-speech/server/app/ws.py` already speaks (binary PCM frames + JSON `turn_start`/`turn_end`/`asr_result`/`tts_chunk`). If the protocol is already documented in `docs/design/streaming-pipeline.md`, reference and conform; otherwise bring our own.

### `hermes voice serve` (convenience)

```python
# In hermes_cli/voice_serve.py:
def cmd_voice_serve(args):
    repo = Path(os.environ.get("STREAMING_S2S_PATH", "~/streaming-speech-to-speech")).expanduser()
    if not repo.exists():
        click.echo("streaming-speech-to-speech not found. Set STREAMING_S2S_PATH or clone it first:")
        click.echo("  git clone https://huggingface.co/spaces/Codeseys/streaming-speech-to-speech-demo")
        sys.exit(1)
    # Spawn `python -m server.app.main` in the repo, stream its logs to ~/.hermes/logs/voice-server.log
    ...
```

## Consequences

**Positive:**
- User's S2S repo stays fully independent.
- Hermes startup cost stays small.
- Failure isolation between Hermes and vLLM/CUDA.
- Protocol contract is the integration point — replaceable backends.
- Works today: user can already run their server and Hermes connects.

**Negative:**
- Two processes to manage in fully-local setup. Mitigation: `hermes voice serve` convenience.
- Two distinct integration modes (full-pipeline vs STT-only/TTS-only) with different protocol surfaces.
- Adding `/asr` and `/tts`-only endpoints to streaming-speech-to-speech is upstream work for that repo (good citizen: PR there, not vendored fork in Hermes).

## Open questions

1. **WS protocol versioning:** What header/handshake field tags compatibility? Probably `?version=1` query param.
2. **`s2s-local` mode 1 vs Hermes tools:** in pure "full pipeline" mode, the local LLM doesn't have access to Hermes's tools. Is that ever a deal-breaker, or do power users always want tools and so they should use cascaded mode? Default: tools-needed users use cascaded mode (`stt.provider: s2s-local` or `tts.provider: s2s-local`); chat-without-tools users get the latency of mode 1.
3. **Auto-launch lifecycle:** if `auto_launch: true` and the server crashes mid-call, what's the UX? Probably: log error, fallback to `fallback_provider`, surface an error to the user.

## Alternatives considered

- **Vendor v6 into Hermes** — rejected. Forks divergent maintenance and conflicts with user's HF research portfolio.
- **Build a Hermes-internal mini local pipeline (not v6)** — rejected. v6 is the user's product; reinventing it duplicates effort and produces a worse pipeline.
- **Make Hermes import vLLM directly** — rejected. Startup cost, VRAM cost, fragility.
