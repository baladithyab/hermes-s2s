# 09 — Frame Timing & Cadence for the Discord Audio Bridge

**Context:** hermes-s2s 0.3.1. Discord expects exactly **20 ms frames of 48 kHz s16le
stereo PCM = 3 840 bytes/frame**. Backends deliver irregular chunk sizes (Gemini
~250 ms @ 16 kHz mono = 8 000 bytes; OpenAI Realtime ~50 ms). We need to
chunk + buffer + serve at a steady 50 fps cadence without drift.

## 1. What discord.py's `AudioSource.read()` actually expects

From the discord.py and Pycord API references, `AudioSource.read()` is called
**from a separate player thread** (not the event loop) on a steady 20 ms schedule
driven internally by `AudioPlayer._do_run`. The contract is:

- Return **exactly 3 840 bytes** of 48 kHz s16le stereo PCM per call (or 20 ms
  of Opus if `is_opus()` returns True).
- Returning an **empty bytes-like object (`b""`) terminates playback** — the
  player treats it as end-of-stream and calls `cleanup()`.
- The player already handles scheduling — it computes the next deadline with
  `time.perf_counter()` and compensates for drift in its own loop. Our job is
  to produce a frame synchronously and quickly (<20 ms), never return `b""`
  unless we really want playback to stop.

Sources:
- https://discordpy.readthedocs.io/en/latest/api.html (AudioSource)
- https://docs.pycord.dev/en/master/api/voice.html

**Implication:** Because the player already drifts-compensates its calls to
`read()`, we mostly just need to **never block** and **never return short/empty
frames** by accident. The hard drift problem only shows up if *we* drive the
cadence ourselves (e.g. pushing to an `asyncio.Queue` at 50 Hz from the bridge
task). Do that with absolute deadlines — see §3.

## 2. Jitter-buffer mental model (borrowed from RTP / PJSIP)

The backend is effectively a lossy-ish, jittery "network" delivering PCM. The
standard RTP approach (PJSIP, WebRTC NetEQ, Wildix write-ups) is:

1. **Prefetch / target latency.** Accumulate N frames (e.g. 3–5 × 20 ms = 60–100 ms)
   before starting playback so short bursts of jitter don't underflow.
2. **Fixed vs adaptive.** Fixed buffer = constant latency, easy. Adaptive grows
   under burst/jitter, shrinks by discarding when persistently over target.
3. **Underflow = play silence / PLC** (packet-loss concealment). *Never* stop
   the clock — downstream (Discord's encoder / the user's ear) keeps consuming.
4. **Overflow = discard.** PJSIP "progressive discard" drops frames when the
   actual latency exceeds the target for too long.

Sources:
- https://docs.pjsip.org/en/2.15.1/specific-guides/audio/jitter_buffer.html
- https://blog.wildix.com/rtp-rtcp-jitter-buffer/
- https://getstream.io/glossary/jitter-buffer/
- https://stackoverflow.com/questions/27079064/adaptive-jitter-buffer-without-packet-loss

For a one-way TTS/LLM-audio stream we don't need full adaptive logic — a small
fixed prefetch (3 frames ≈ 60 ms) plus silence-on-underflow plus drop-oldest
on sustained overflow is sufficient and is what pipecat/LiveKit effectively do
via their resampler + output transport pair.

## 3. Drift compensation for a self-driven 20 ms tick

A naive `while True: await asyncio.sleep(0.020)` accumulates drift because
`asyncio.sleep` sleeps *at least* `delay` seconds (Python bug tracker
issue 31539 — sleep is rounded up to `loop._clock_resolution`, ~500 ns on
Linux, ~15 ms on Windows with `GetTickCount64`). Over a 30-min call this
stacks into seconds.

The well-known fix (A. Jesse Jiryu Davis's pattern on SO question 37512182) is
an **absolute-deadline tick generator**: compute `next_deadline = start + N * period`
and sleep only to that absolute time, so any per-iteration overrun is absorbed
by the next (shorter) sleep instead of compounded.

```python
period_ns = 20_000_000           # 20 ms
start = time.monotonic_ns()
n = 0
while running:
    frame = await produce_frame()
    await emit(frame)
    n += 1
    deadline = start + n * period_ns
    delay = (deadline - time.monotonic_ns()) / 1e9
    if delay > 0:
        await asyncio.sleep(delay)
    # if delay < 0 we're behind — don't "catch up" by flooding; just no-sleep.
```

Use `time.monotonic_ns()` (not `time.time()`) so NTP steps don't break the
cadence. Prefer `loop.time()` if you only interact with the asyncio scheduler
— they both map to `CLOCK_MONOTONIC` on POSIX.

Sources:
- https://stackoverflow.com/questions/37512182/how-can-i-periodically-execute-a-function-with-asyncio
- https://bugs.python.org/issue31539
- https://discuss.python.org/t/how-does-time-sleep-influence-asyncio-sleep/104189

**Windows caveat:** On Windows the monotonic clock resolution can be ~15 ms.
That's the same order as our period, so cadence will be visibly jittery. The
mitigation for real production is `winmm.timeBeginPeriod(1)` (or running the
player in a dedicated thread, which is what discord.py already does —
another reason to let the discord.py player own the output cadence).

## 4. Chunk slicing math: 250 ms Gemini → 20 ms Discord frames

Given: Gemini outputs 16 kHz mono PCM16, ~250 ms chunks = `0.250 * 16000 * 2` =
**8 000 bytes/chunk**.

Target: 48 kHz stereo PCM16 = `48000 * 2 * 2` = **192 000 B/s** → 20 ms =
**3 840 B/frame**. Upsampling ratio is 48/16 = 3× samples, mono→stereo is 2×,
so the 8 000-byte chunk becomes `8000 * 3 * 2` = **48 000 bytes** of output
audio = **12.5 frames of 3 840 bytes**. Half-frame remainder every chunk.

### Standard answer: hold the remainder, don't zero-pad

Every production pipeline (pipecat's `AudioBufferProcessor`,
livekit-rtc's `AudioResampler.push()`) works this way:

- Maintain a persistent `bytearray` output buffer.
- After each resample, **append** the full resampled block.
- Slice off complete 3 840-byte frames; leave the fractional tail in the
  buffer for the next chunk to complete.
- Only zero-pad on **stream end** / underflow, never mid-stream.

LiveKit's `AudioResampler.push()` explicitly returns "*may be empty if no
output data is available yet*" — it holds fractional samples internally until
enough accumulate. Pipecat's `_resample_input_audio` is a thin wrapper around
the same pattern. This preserves sample continuity; zero-padding mid-stream
would inject an audible 400 µs click every 250 ms.

Sources:
- https://docs.livekit.io/reference/python/livekit/rtc/audio_resampler.html
- https://reference-server.pipecat.ai/en/latest/_modules/pipecat/processors/audio/audio_buffer_processor.html

### Resampler choice

Use `av.AudioResampler` (PyAV/libswresample) or `soxr` rather than a hand-rolled
`audioop.ratecv` — swresample/sox maintain proper filter state across calls so
the boundary between chunks doesn't alias. libav resamplers already return
fractional-sample-accurate output and internally hold the tail.

## 5. Decision table

| Scenario                     | What we return / do                                           | Why                                                                                 |
|------------------------------|---------------------------------------------------------------|-------------------------------------------------------------------------------------|
| Output buffer has ≥3840 B    | Return next 3840 B                                            | Normal path.                                                                        |
| Output buffer empty (underflow) | Return 3840 B of zeros (silence PCM)                        | Empty `b""` would terminate playback. Silence = standard PLC substitute.            |
| Backend closed & buffer empty | Return `b""` once, then `cleanup()`                          | Only intentional EOS should end the stream.                                         |
| Input queue full (overflow)  | Drop **oldest** chunk, log counter                            | For one-way user→backend: the freshest audio is what the model needs.               |
| Prefetch not yet reached     | Return silence until buffer has ≥3 frames                     | Avoids chattery start where first 2 frames play then underflow.                     |
| Backend sends partial frame  | Accumulate, serve next tick                                   | Sub-frame remainders are always held — never zero-padded mid-stream.                |

Rationale for **drop-oldest** on backpressure: in a live conversation, stale
audio from 2 seconds ago has no value to the assistant; current audio does.
This is the same decision WebRTC's NetEQ and PJSIP's progressive discard make.
The alternative — blocking the producer — stalls the Discord receive path and
can desync voice packets.

## 6. Concrete buffer class (reference shape, ~50 LOC)

```python
import asyncio, time
from collections import deque

FRAME_BYTES = 3840          # 20 ms @ 48 kHz stereo s16le
SILENCE     = b"\x00" * FRAME_BYTES
PREFETCH    = 3             # 60 ms
MAX_OUTPUT_FRAMES = 100     # 2 s hard cap; drop-oldest beyond this

class BridgeBuffer:
    """Bidirectional audio bridge with cadence-compensated read.

    - `push_output(pcm48k_stereo)`: backend -> Discord. Accepts any length;
      slices into 20 ms frames, holds remainder.
    - `read()`: called by discord.py's AudioSource on a 20 ms cadence (from
      the player thread). Never blocks, never returns short.
    - `push_input(pcm)` / `get_input()`: Discord -> backend, drop-oldest on
      overflow.
    """
    def __init__(self, input_max_frames: int = 250):  # 5 s @ 20ms
        self._out_bytes = bytearray()
        self._out_frames: deque[bytes] = deque()
        self._in_q: deque[bytes] = deque(maxlen=input_max_frames)
        self._started = False
        self._underflows = 0
        self._drops = 0

    def push_output(self, pcm: bytes) -> None:
        self._out_bytes.extend(pcm)
        while len(self._out_bytes) >= FRAME_BYTES:
            frame = bytes(self._out_bytes[:FRAME_BYTES])
            del self._out_bytes[:FRAME_BYTES]
            if len(self._out_frames) >= MAX_OUTPUT_FRAMES:
                self._out_frames.popleft()      # drop-oldest
                self._drops += 1
            self._out_frames.append(frame)

    def read(self) -> bytes:                    # called from player thread
        if not self._started:
            if len(self._out_frames) < PREFETCH:
                return SILENCE                  # still prefetching
            self._started = True
        if self._out_frames:
            return self._out_frames.popleft()
        self._underflows += 1
        return SILENCE                          # PLC substitute

    def push_input(self, pcm: bytes) -> None:   # deque auto-drops oldest
        self._in_q.append(pcm)

    async def get_input(self) -> bytes:
        while not self._in_q:
            await asyncio.sleep(0.005)
        return self._in_q.popleft()
```

Notes on the shape:
- `read()` is **sync** because discord.py calls it from its player thread —
  do not make it a coroutine. Use thread-safe primitives if you mutate
  `_out_frames` from the event loop (a `threading.Lock` around
  `popleft/append` is sufficient; the cost is ~200 ns).
- No absolute-deadline loop is needed here because discord.py drives the
  cadence. **Only** add §3's deadline loop if you are serving the other
  direction (pushing Discord input to the backend) on your own schedule.
- `MAX_OUTPUT_FRAMES=100` (2 s) is a safety net; the resampler + 250 ms
  arrival cadence means the steady-state occupancy is 12–13 frames.

## 7. Open questions / follow-ups for 0.3.1

- Do we need adaptive prefetch? Likely **no** for v0.3.1 — log underflow /
  drop counters and revisit if users report choppy audio.
- Should we expose a metrics hook (`on_underflow`, `on_drop`) for
  observability? Recommend yes, trivial to add to the class above.
- WSL / Windows monotonic clock resolution: benchmark before shipping; may
  need `timeBeginPeriod(1)` for self-driven ticks.
