# ADR-0002: Realtime duplex bridge as a NEW abstraction (not part of STT/TTS dispatch)

**Status:** proposed
**Date:** 2026-05-09
**Deciders:** Codeseys (user), ARIA-orchestrator

## Context

ADR-0001 keeps STT and TTS in the existing `if/elif` dispatch shape. Realtime backends (Gemini Live, gpt-4o-realtime) are not STT or TTS — they consume audio and produce audio bidirectionally over a persistent WebSocket. There's no transcription step, no separate text-generation step, no separate synthesis step from Hermes's perspective. Hermes provides tool-calling and identity; the model handles everything else in its own session.

Trying to fit realtime into the STT/TTS dispatch pattern would be wrong:
- The STT path expects "give it a file, get back a transcript string" — realtime is a continuous duplex stream.
- The TTS path expects "give it text, get back an audio file" — realtime emits audio in response to audio input, not text.
- Tool-calling in realtime mode happens INSIDE the model's session via WebSocket events — Hermes's normal tool dispatch needs a bridge.

## Decision

Introduce a new top-level abstraction: `voice.mode: "cascaded" | "realtime"` config switch, with a `RealtimeBackend` interface for the realtime mode.

```python
class RealtimeBackend(Protocol):
    async def connect(self, system_prompt: str, voice: str, tools: list[dict]) -> None: ...
    async def send_audio_chunk(self, pcm_chunk: bytes, sample_rate: int) -> None: ...
    async def recv_events(self) -> AsyncIterator[RealtimeEvent]: ...
    async def inject_tool_result(self, call_id: str, result: str) -> None: ...
    async def interrupt(self) -> None: ...
    async def close(self) -> None: ...
```

`RealtimeEvent` is a tagged union: `{type: "audio_chunk", pcm: bytes, sample_rate: int}` | `{type: "transcript_partial", text: str}` | `{type: "transcript_final", text: str}` | `{type: "tool_call", call_id: str, name: str, args: dict}` | `{type: "session_resumed"}` | `{type: "error", message: str}`.

Backend implementations: `RealtimeGeminiBackend`, `RealtimeOpenAIBackend`. Each adapts its native WebSocket protocol to this interface.

When `voice.mode: "realtime"`:
- Discord voice path: `gateway/platforms/discord.py` voice handler bypasses STT, opens the realtime backend, pipes Discord-decoded PCM in, pipes backend audio out to Discord, routes tool_call events to Hermes's tool dispatcher and injects results back.
- CLI voice mode: same shape but with PortAudio mic/speaker instead of Discord audio.

When `voice.mode: "cascaded"` (the default, current behavior):
- Use existing STT → AIAgent → TTS pipeline. No change.

## Why a new abstraction (not a plugin type) for now

Same logic as ADR-0001: don't generalize until you have two working implementations. We'll have Gemini + OpenAI realtime first. If a third party (e.g., Moonshine v3 native realtime, Mistral Voice, AWS Nova Sonic) joins the family, we can promote `RealtimeBackend` to a formal plugin type then.

## Audio resampling layer

Discord delivers 48kHz s16le stereo. Realtime backends require:
- Gemini Live: 16kHz PCM mono (input), 24kHz PCM mono (output)
- OpenAI Realtime: 24kHz PCM mono (both directions)

We need a small `audio_resample` utility module in `gateway/voice/` (new path) handling:
- Stereo → mono mixdown
- Sample-rate conversion (use `scipy.signal.resample_poly` — already in user's v6 deps; otherwise `samplerate` package)
- s16le ↔ float32 conversion

This is shared by both realtime backends and likely useful in cascaded mode too.

## Tool-calling bridge

Both Gemini Live and gpt-4o-realtime support function calling natively. We translate Hermes's `tool_schemas` (already JSON-schema-shaped) into each backend's expected format at `connect()` time. When the backend emits a `tool_call` event:

1. Pause audio output briefly (model expects the result quickly).
2. Run the tool via Hermes's existing dispatcher (`model_tools.handle_function_call` or equivalent in voice context).
3. Send the result back to the backend via `inject_tool_result(call_id, result)`.
4. Backend resumes generating its response, now with the tool result in context.

For long-running tools (>5s), emit a "let me check on that" filler audio chunk and inject the result asynchronously. Configurable timeout; if exceeded, inject an error and let the model recover gracefully.

## Session lifecycle

- Gemini Live: ~15min audio-only sessions, extensible via session-resumption tokens. Backend transparently re-resumes; emits `session_resumed` event so the gateway can log it.
- OpenAI Realtime: hard 30-min cap. Backend tears down and reconnects on cap; system prompt + tool list re-sent; the model resumes from the conversation history we replay (this is lossy — flag in docs).

## Consequences

**Positive:**
- Realtime mode lives parallel to cascaded mode; cascaded users see no change.
- Tool-calling integrates cleanly with existing Hermes tool dispatch.
- Audio resampling utility is reusable (cascaded mode likely benefits too — Discord 48kHz vs Whisper 16kHz already needs this).
- Two backends behind one interface gives us A/B testing on quality and cost.

**Negative:**
- New abstraction surface (~300 LOC for the interface + 2 backends ~500 LOC each).
- Tool-calling latency budget is tight (long tools need filler-audio UX).
- Session resumption / reconnect complexity is real and needs end-to-end tests with mock WS servers.

## Open questions (resolve in Wave 3 plan)

- Where exactly does the realtime branch live in the Discord voice handler? Need to grep `gateway/platforms/discord.py` for the current STT call site.
- Can we get the realtime backend to NOT do its own VAD and instead rely on Discord's SPEAKING events for turn detection? (Avoids double-VAD.)
- How does `/voice backend gemini` slash command interact with `voice.mode: realtime`? Probably: realtime-* providers force mode to realtime; cascaded providers force to cascaded; explicit mode overrides only when ambiguous.

## Alternatives considered

- **Pretend realtime is a "STT provider that returns chunks of TTS-ready text"** — fundamentally wrong shape; tool-calling and audio-out can't be expressed.
- **Add realtime as a third top-level mode parallel to STT/TTS** — proposed here.
- **Build realtime as a separate Hermes plugin (out-of-tree)** — rejected for v1; in-tree gets it tested with the rest of voice mode and lets us share the resampling layer.
