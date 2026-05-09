# ADR-0007: Audio bridge plugs into existing Hermes VoiceReceiver via frame-callback hook

**Status:** accepted
**Date:** 2026-05-09
**Driven by:** [research/07-discord-audio-contract.md](../design-history/research/07-discord-audio-contract.md)

## Context

Hermes uses Rapptz's `discord.py` (not py-cord). Discord.py has **no `AudioSink` abstraction** for receiving voice — it only ships outgoing audio (`AudioSource`). To receive voice, Hermes implements a custom 300-LOC stack at `gateway/platforms/discord.py:128-477`: hooks `VoiceConnectionState._socket_reader.register()`, decrypts XChaCha20-NaCl, parses RTP, strips DAVE padding, decodes Opus per-user, and runs a per-user 1.5s silence detector.

For 0.3.1 we need to route incoming user audio into our realtime backend instead of (or alongside) Hermes's STT+LLM+TTS path. Three options:

- **A. Hook into the existing VoiceReceiver** — add a `set_frame_callback(callback)` method that fires immediately after Opus decode (around `discord.py:376`). The callback receives `(user_id, pcm_48k_stereo_s16le)` for each 20ms frame. hermes-s2s installs the callback when monkey-patch + `s2s.mode: realtime` are both active.
- **B. Recreate the UDP stack** — install our own `_socket_reader.register()` hook. Means duplicating ~300 LOC of NaCl/RTP/DAVE/Opus code, including the security-critical decrypt path. Two consumers fighting for the same UDP packets is racy and wasteful.
- **C. Wait for upstream PR** — same long-term cleanup as ADR-0006 but blocks 0.3.1 indefinitely.

For the outgoing path (bot → user), discord.py's `AudioSource` is stable — subclass it, return 3840-byte 20ms s16le 48k stereo frames from `read()`, hand to `voice_client.play(source)`. AudioSource runs in a **separate threading.Thread**, so synchronization with our asyncio bridge needs `queue.Queue` (sync) at the boundary, not `asyncio.Queue`.

## Decision

**Adopt Option A for receive + standard `AudioSource` for send.**

The monkey-patch (per ADR-0006) wraps `DiscordAdapter.join_voice_channel`. After the original method connects the VoiceClient and constructs the VoiceReceiver, the patch:

1. Calls `voice_receiver.set_frame_callback(bridge.on_user_frame)` to redirect decoded PCM into our bridge's input queue (replacing or coexisting with Hermes's STT path — see "Coexistence" below).
2. Constructs a `QueuedPCMSource(bridge.output_queue)` subclass of `discord.AudioSource`.
3. Calls `voice_client.play(source)` to start the outgoing pipe.

The bridge owns:
- A `queue.Queue[tuple[int, bytes]]` input — `(user_id, pcm_48k_stereo_s16le_3840bytes)`.
- A `queue.Queue[bytes]` output — 20ms PCM frames ready for Discord.
- A background asyncio task that reads input, resamples (48k→16k or 24k mono via `hermes_s2s.audio.resample`), sends to backend, reads `backend.recv_events()`, resamples backend audio (16k/24k mono → 48k stereo), slices to 20ms frames, queues output.

### `set_frame_callback` injection

If Hermes's `VoiceReceiver` already exposes a hook, use it. If not, the monkey-patch wraps the `_decoder.decode` callsite (around `discord.py:376` per the research) to fire our callback in addition to Hermes's normal path. Monkey-patch is gated by `HERMES_S2S_MONKEYPATCH_DISCORD=1` (already established in ADR-0006).

### Coexistence with Hermes's STT path

When `s2s.mode: realtime` is active, the existing Hermes STT pipeline (Whisper transcription on silence-end) still fires for the same audio. Two reasonable behaviors:

- **Override:** the bridge's frame callback short-circuits Hermes's STT — Hermes never sees the audio.
- **Tee:** both paths receive the audio. Hermes STT still produces transcripts (useful for logging, analytics, fallback) but the bot's voice replies come from the realtime backend, not Hermes TTS.

We choose **override** for 0.3.1 — clean semantics, no double-handling. Tee is a 0.4.0 polish item.

To override: when monkey-patch installs our callback, it also pauses Hermes's `VoiceReceiver` audio consumer (the "would mute the bot" footgun from the 0.3.0 review now becomes "intentionally pause Hermes voice consumption while realtime mode owns the audio"). The outgoing `AudioSource` we install replaces Hermes's TTS-file player.

### Single-user limitation

Discord delivers per-user audio. Realtime backends expect a single audio stream. For 0.3.1 we route only the audio of users in `DISCORD_ALLOWED_USERS`, picking the first speaker (whoever's frames arrive first in a turn). Multi-party mixing is 0.4.0+.

## Consequences

**Positive:**
- Reuses Hermes's battle-tested 300 LOC of UDP/NaCl/Opus stack.
- Outgoing path is clean — no monkey-patching of the play side.
- The bridge is the only owner of the realtime backend session, so cleanup is centralized.

**Negative:**
- The frame-callback hook is a private extension point on Hermes's VoiceReceiver. If Hermes refactors that file, we break. Mitigation: the existing version-gate (ADR-0006 `SUPPORTED_HERMES_RANGE`) flags this; tightening the upper bound after testing each Hermes minor release.
- Override semantics mean Hermes loses voice-call-to-text logging. We could mitigate by writing transcripts of `transcript_partial` events ourselves to Hermes's session log.
- AudioSource runs in a thread, so the bridge needs a sync `queue.Queue` at the I/O boundary even though everything else is asyncio. Adds one synchronization point.

## Open questions

1. Does `VoiceReceiver` in Hermes today actually expose a `set_frame_callback` method, or do we need to install it via monkey-patch on the receiver class itself? Phase 5 spike confirms before implementation.
2. When the user stops speaking mid-realtime-turn, the realtime backend already handles silence; do we need our own VAD here? Probably not — Gemini Live and OpenAI Realtime both have server-side VAD.
3. Audio mixing across multiple speakers — 0.4.0 will need it. Defer until then.
