# 07 — discord.py Audio I/O Contract

Scope: Rapptz `discord.py[voice]` (NOT py-cord). Drives ADR-0007 for
hermes-s2s 0.3.1 audio bridge (Discord VC ↔ Gemini Live / OpenAI Realtime).
Cross-referenced against Hermes's existing custom receiver at
`gateway/platforms/discord.py`.

---

## 1. Receive side — there is no `AudioSink`

**Rapptz discord.py ships NO public audio-sink / receive abstraction.**
`VoiceClient` and `AudioSource` are send-only. Confirmed via DeepWiki:
> "discord.py does not appear to have a public AudioSink abstraction for
> receiving voice audio... The AudioSource abstraction is designed for
> sending."

py-cord has `discord.sinks.Sink.write(data, user)` — Rapptz does not.
An upstream PR has been open for years; treat it as not landing.

### What Rapptz exposes internally

- `VoiceClient._connection` → `VoiceConnectionState` (`discord/voice_state.py`)
- `VoiceConnectionState.socket` — raw UDP socket
- `VoiceConnectionState._socket_reader` — `SocketReader` thread, loops
  `recvfrom` and fans out to registered callbacks
- `_socket_reader.register(cb)` / `unregister(cb)` — **the only hook point**
- `VoiceConnectionState.secret_key` — NaCl XChaCha20-Poly1305 key (rtpsize mode)
- `VoiceConnectionState.ssrc` — bot's own SSRC (skip to avoid echo)
- `VoiceConnectionState.dave_session` — DAVE E2EE session (optional)
- `VoiceConnectionState.hook` — voice-WS message hook (needed for op 5 SPEAKING
  → SSRC-to-user mapping)
- `discord.opus.Decoder` — per-SSRC Opus→PCM decoder (bundled, usable)

Wire format Discord actually delivers on the UDP socket:
- RTP header (12 B + 4·CSRC + 4 if extension), payload-type `0x78` (120)
- AEAD XChaCha20-Poly1305 with 4-byte big-endian nonce suffix (rtpsize)
- After NaCl decrypt: optional encrypted RTP-extension blob, then Opus frame
- **Per-SSRC (≈ per-user)**, 48 kHz stereo, 20 ms frames — no server-side mix
- Padding bit (RFC 3550 §5.1) must be stripped before Opus decode
- DAVE-E2EE layer wraps the Opus payload when the session is active

### How Hermes already does it

`gateway/platforms/discord.py:128` — `class VoiceReceiver` implements the
full stack on top of the Rapptz internals:

| Concern                     | File:line                                       |
|-----------------------------|-------------------------------------------------|
| Socket-listener registration | `discord.py:181` `conn.add_socket_listener(self._on_packet)` |
| SPEAKING-hook wrapping      | `discord.py:213-244` wraps `conn.hook` + live `ws._hook` |
| RTP parse + PT=0x78 filter  | `discord.py:262-285`                            |
| NaCl XChaCha decrypt        | `discord.py:307-321` (`nacl.secret.Aead`)       |
| RTP padding strip           | `discord.py:327-350`                            |
| DAVE E2EE decrypt           | `discord.py:352-370` (`davey` lib, optional)    |
| Opus decode (per-SSRC)      | `discord.py:372-382` (`discord.opus.Decoder`)   |
| Per-user PCM buffer + silence VAD | `discord.py:414-445`                      |
| Lifecycle wiring from join  | `discord.py:1878-1889` in `join_voice_channel`  |
| Pause-on-TTS (echo avoid)   | `discord.py:1920-1923` in `play_in_voice_channel` |

Note: `add_socket_listener` is Hermes's own name for `_socket_reader.register`;
check the helper shim in `discord.py`. The DeepWiki-documented API is
`conn._socket_reader.register(cb)`.

---

## 2. Send side — `AudioSource` IS stable

Source: `discord/player.py`, class `AudioSource`.

### Contract

```python
class AudioSource:
    def read(self) -> bytes: ...        # 20 ms frame, REQUIRED
    def is_opus(self) -> bool: return False
    def cleanup(self) -> None: pass
```

- `is_opus() is False` (default) → `read()` returns **3840 bytes** of
  `s16le` 48 kHz **stereo** PCM (= 48000 · 2 ch · 2 B · 0.020 s)
- `is_opus() is True` → `read()` returns one pre-encoded Opus frame
- `b""` (empty bytes) → player stops, invokes `after(None)` cleanly
- `None` → `TypeError` downstream, playback dies
- Raises → caught by `AudioPlayer`, stops, passes exc to `after(exc)`

### Built-in sources

- `PCMAudio(stream)` — reads `OpusEncoder.FRAME_SIZE` (3840) per call from a
  file-like
- `FFmpegPCMAudio(src, pipe=False, options=...)` — `subprocess.Popen` of
  `ffmpeg`, reads raw `s16le/48k/2ch` from its stdout; `pipe=True` feeds
  `src` to ffmpeg stdin
- `FFmpegOpusAudio` — ffmpeg outputs Opus directly, `is_opus() == True`
- `PCMVolumeTransformer(original, volume)` — wraps a non-opus source,
  applies `audioop.mul`; asserts `not original.is_opus()`

### Subclassing for a realtime queue (OUR CASE)

**Yes, subclass `AudioSource` directly.** Do *not* pipe through ffmpeg.

```python
class RealtimeQueueSource(discord.AudioSource):
    FRAME = 3840  # 20 ms @ 48k/s16le/2ch
    def __init__(self, q: queue.Queue[bytes]):  # threading.Queue, NOT asyncio
        self._q = q
        self._buf = bytearray()
    def read(self) -> bytes:
        while len(self._buf) < self.FRAME:
            try:
                chunk = self._q.get(timeout=0.015)   # < 20 ms
            except queue.Empty:
                return b"\x00" * self.FRAME          # silence (keeps stream alive)
            self._buf.extend(chunk)
        frame, self._buf = bytes(self._buf[:self.FRAME]), self._buf[self.FRAME:]
        return frame
    def is_opus(self): return False
```

**Critical threading rule** (DeepWiki, Key Concepts): `AudioPlayer._do_run`
runs in a **dedicated `threading.Thread`**, not on the asyncio loop.
- DO use `queue.Queue` (thread-safe) or a deque + `threading.Event`
- DO NOT `await` an `asyncio.Queue` from `read()` — that blocks the thread
- Feed the queue from the asyncio side with
  `loop.call_soon_threadsafe(q.put_nowait, chunk)` or have the realtime
  provider's callback deliver directly to a `queue.Queue`
- Backend rate mismatch: Gemini/OpenAI send 24 kHz mono PCM → resample to
  48 kHz stereo before queueing (2× upsample + L=R duplicate). Cheapest:
  `audioop.ratecv` then `audioop.tostereo`.

---

## 3. Frame timing & jitter

`AudioPlayer._do_run` (`discord/player.py`) wall-clock loop:

```
DELAY = OpusEncoder.FRAME_LENGTH / 1000.0  = 0.020 s
_start = time.perf_counter()
loops = 0
while self._connected.is_set():
    data = self.source.read()
    if not data: stop; break
    self.client.send_audio_packet(data, encode=not self.source.is_opus())
    loops += 1
    next_time = self._start + self.DELAY * loops
    delay = max(0, self.DELAY + (next_time - time.perf_counter()))
    time.sleep(delay)
```

### Jitter behaviour — measured contract

| Condition                               | Outcome                                          |
|-----------------------------------------|--------------------------------------------------|
| `read()` < 20 ms                        | Clean cadence, `time.sleep` absorbs slack        |
| `read()` > 20 ms sporadically           | Catches up (delay clamps to 0); audible skip     |
| `read()` > 20 ms sustained              | Drift grows; playback degrades; bot **not kicked** |
| `read()` returns `b""`                  | Player stops, `after` fires                      |
| Player `.pause()` / disconnect reconnect | `send_silence()` emits `OPUS_SILENCE` packets   |
| No audio at all                         | Discord voice gateway does NOT kick for quiet    |

**There is no server-side jitter buffer.** The 20 ms cadence is enforced
by the sender. Clients experience whatever jitter the bot emits.

### Best practice for realtime sources

1. **Never block `read()`.** Return silence (`b"\x00" * 3840`) on underrun
   rather than stalling the thread.
2. **Own queue buffers the bursts**, not `read()`'s clock loop. Target
   40–80 ms of pre-buffer before starting playback.
3. Use `time.perf_counter()` for any additional pacing — discord.py already
   does; don't double-pace.
4. Never call `asyncio.sleep` inside `read()` — wrong loop.
5. Stop conditions must be explicit: return `b""` on EOS, or call
   `vc.stop()` from the asyncio side.

---

## 4. Recommended path for hermes-s2s

### Outgoing (bot → user): subclass `AudioSource`
`voice_client.play(RealtimeQueueSource(q), after=on_done)` — clean, documented,
Hermes's existing `play_in_voice_channel` (`discord.py:1914`) already uses
this pattern with file paths. Swap `FFmpegPCMAudio(path)` → our queue source
and reuse the `receiver.pause()` echo-avoidance at `discord.py:1920-1923`.

### Incoming (user → bot): three options

#### (a) Reuse Hermes's `VoiceReceiver`, add a raw-PCM callback — RECOMMENDED
Add a `on_pcm_frame(user_id, pcm_20ms)` callback invoked from
`_on_packet` right after the Opus decode at `discord.py:376`, *in parallel*
with the existing silence-VAD buffer. S2S streams per-frame PCM to the
realtime backend; the legacy STT path keeps its utterance buffer for
fallback / cost mode.

- **Pros:** Zero new UDP/NaCl/DAVE code. Unified SSRC→user mapping.
  Single socket listener (Discord routes each packet to one registered
  callback — stacking is fine). Backward-compatible with Whisper flow.
- **Cons:** Hermes `VoiceReceiver` becomes a public API surface for s2s;
  need a stable hook contract. DAVE/NaCl bugs affect both paths.

#### (b) Register a second `_socket_reader` callback from s2s
Parallel `VoiceReceiver`-style class owned by hermes-s2s.

- **Pros:** Total isolation; s2s can ship without touching Hermes.
- **Cons:** Duplicates the whole stack — NaCl + rtpsize nonce + RTP parse
  + padding strip + DAVE + Opus decoder state machine + SPEAKING-hook
  wrapping (`discord.py:213-244`). ~300 LOC of security-sensitive code
  duplicated. Two Opus decoders per SSRC wastes CPU. **Rejected.**

#### (c) Wait for upstream AudioSink PR
Open for years on Rapptz/discord.py. **Do not wait.**

### Decision
**Take (a).** Add to `gateway/platforms/discord.py` `VoiceReceiver`:

```python
def set_frame_callback(self, cb: Callable[[int, bytes], None]) -> None:
    """cb(user_id, pcm_bytes) invoked per 20 ms decoded frame."""
    self._frame_cb = cb
```

Invoked at the end of `_on_packet` post-decode (`discord.py:376`), guarded
by `if self._frame_cb and user_id`. hermes-s2s constructs the bridge,
obtains the `VoiceReceiver` via a public accessor on `DiscordAdapter`, and
registers its callback. Frame delivery happens on Hermes's `SocketReader`
thread — the bridge must hand off to its own asyncio loop via
`loop.call_soon_threadsafe`.

---

## 5. Gotchas checklist

- [ ] `read()` runs on a non-asyncio thread — no `await`, no `asyncio.Queue.get`
- [ ] Backend PCM (24 kHz mono) must be resampled to 48 kHz stereo before
      feeding `RealtimeQueueSource` — mismatched rates cause chipmunk audio
- [ ] Emit silence on underrun, not empty bytes (empty = EOS)
- [ ] Pause the `VoiceReceiver` while bot is playing TTS
      (`discord.py:1920-1923`) to avoid the bot hearing itself
- [ ] Skip bot's own SSRC in receive path (`discord.py:277`) — redundant but
      catches echo from misconfigured RTP loopback
- [ ] SPEAKING-event hook must be reinstalled on voice WS reconnect —
      Hermes sets it on both `conn.hook` and live `ws._hook`
      (`discord.py:236-244`)
- [ ] DAVE decrypt can raise "Unencrypted" during rollout — treat as
      passthrough, don't drop the frame (`discord.py:362-367`)
- [ ] `vc.is_playing()` + `vc.stop()` are the only clean way to interrupt
      a source from asyncio land (barge-in)

---

## Sources

- DeepWiki Rapptz/discord.py:
  - AudioSink question: https://deepwiki.com/search/does-discordpy-rapptz-have-a-s_3837ec59-1c1c-4eda-9001-8a0ba8d90548
  - AudioSource question: https://deepwiki.com/search/explain-the-audiosource-abstra_5dd93ea5-72d2-4a32-a8d2-ee68881b0134
  - Send-loop timing: https://deepwiki.com/search/how-does-discordpys-voice-send_1cfb7d44-abb7-4e19-b2a3-299260047206
- Hermes: `gateway/platforms/discord.py` (commit on local checkout)
  - `VoiceReceiver` class: lines 128-477
  - `join_voice_channel` / receiver wiring: lines 1857-1889
  - `play_in_voice_channel` / pause-receiver: lines 1914-1923
