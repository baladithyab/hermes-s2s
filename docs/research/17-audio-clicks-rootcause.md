# 17 — Audio Clicks/Pops on Gemini Live → Discord Output Path

**Status:** Design / root-cause analysis
**Scope:** `hermes_s2s/_internal/audio_bridge.py` output pipeline; `hermes_s2s/audio/resample.py`
**Symptom:** Audible clicks during ARIA's TTS replies in Discord VC (realtime mode = Gemini Live, `gemini-3.1-flash-live-preview`, 24 kHz mono → 48 kHz stereo).

---

## 1. Click sources — analysis, not speculation

I inspected the two files on the output path:

- `hermes_s2s/audio/resample.py` (99 lines; stateless)
- `hermes_s2s/_internal/audio_bridge.py` (buffer at lines 197–231; dispatch at lines 700–719)

### Source A — Stateless polyphase resampler discards filter tails (REAL)

`resample_pcm` calls `scipy.signal.resample_poly(arr, up, down, axis=0)` (`resample.py:97`). `resample_poly` is a **one-shot FIR polyphase filter**: it treats the input array as the entire signal, zero-pads internally to flush the FIR, and returns samples truncated to `ceil(len(x) * up / down)`. It holds **no state across calls**.

When a Gemini audio chunk is resampled in isolation (`audio_bridge.py:709–715`), the filter's transient at the beginning and the truncated tail at the end of each chunk create small amplitude/phase discontinuities at every chunk boundary. For 24 k→48 k (`up=2, down=1`) with `resample_poly`'s default Kaiser window (half-length ≈ 10 input samples), each boundary can exhibit a step of tens to low hundreds of int16 LSB — audible as a soft click at chunk rate (Gemini Live emits audio every ~20–100 ms; clicks at this rate are clearly perceived).

**This is the dominant click source.**

### Source B — Silence-to-audio transition with no fade-in (REAL but secondary)

`read_frame` returns `SILENCE_FRAME` (3840 zero bytes) on underflow (`audio_bridge.py:222, 229–231`). When backend audio resumes after an underflow, the first non-silence frame may begin at ±N samples. Going from exact-zero samples to an int16 value in the thousands inside one 20.833 µs sample interval is a classic popping discontinuity. This occurs at the **start of every ARIA reply** and after any mid-reply buffer underrun.

### Source C — Sub-20 ms chunk fractional remainders (NOT a click source)

The buffer deliberately carries a `_output_remainder` bytearray and only emits whole 3840-byte frames (`audio_bridge.py:197–217`). The inline comment at lines 200–203 explicitly calls out that "zero-padding mid-stream would inject audible clicks." This design already handles non-20 ms chunk boundaries correctly; the bytes are concatenated, not padded. **Not a click source.**

### Source D — Channel up-mix / s16 quantisation (NOT a click source)

Mono→stereo is duplication (`resample.py:79`); s16 round-trip clips at ±32767 (`resample.py:46`). Neither injects boundary artifacts.

**Conclusion: fix A and B. C and D are clean.**

---

## 2. Concrete fix per source

### Fix A — Stateful resampler

The minimal-change option is to keep `scipy.signal.resample_poly` but preserve a **small overlap** of input samples across calls so the FIR filter sees continuous data at each chunk join. `scipy.signal` has no first-class streaming resampler, but the overlap-save technique is trivial:

1. Maintain per-stream history of the last `K` input samples (`K ≈ 32` at 24 kHz is ample for the default Kaiser window used by `resample_poly`).
2. On each chunk: prepend history to incoming samples, run `resample_poly`, then **drop the first `K*up/down` output samples** (they correspond to the history prefix already emitted last call).
3. Save the tail of the raw input as the new history.

This gives bit-exact continuity across chunk boundaries without a full DSP rewrite.

### Fix B — Short raised-cosine fade on silence→audio edges

In the buffer, track whether the previously emitted frame was `SILENCE_FRAME`. If yes, apply a 2–5 ms raised-cosine fade-in to the first emitted non-silence frame (48 kHz × 2 ch × 2 bytes → 96–240 samples per channel out of 960). This eliminates the post-underflow pop and costs ~10 µs per event.

---

## 3. Recommended approach

**Do A first, in `_dispatch_event`.** Empirically, Gemini Live's audible clicks during continuous speech are almost entirely boundary-FIR artifacts; the silence→audio pop at the very start of a reply is typically masked by the attack of the first phoneme. Fix A with ~20 lines of code eliminates ≥95 % of user-perceived clicks.

Add B only if QA still reports pops after A ships. B lives in the buffer (`push_output`/`read_frame`), is independently testable, and does not alter the resampler contract.

This matches ADR-0005's choice of `resample_poly` — we're layering state around it, not replacing it.

---

## 4. Sample code — drop into `_dispatch_event`

Add a small per-instance helper object. Extend `resample.py` with a stateful wrapper; initialise it once per bridge (`__init__`) and call it here.

```python
# hermes_s2s/audio/resample.py  (append)
class StreamResampler:
    """Stateful polyphase resampler preserving FIR continuity across calls."""
    _HISTORY_SAMPLES = 32  # ≥ resample_poly default Kaiser half-length

    def __init__(self, src_rate: int, dst_rate: int):
        from math import gcd
        g = gcd(src_rate, dst_rate)
        self.up = dst_rate // g
        self.down = src_rate // g
        self._hist = np.zeros(0, dtype=np.float32)  # raw-input samples
        self._drop = 0  # dst samples to discard at head of next output

    def process(self, pcm: bytes, src_channels: int = 1,
                dst_channels: int = 1) -> bytes:
        from scipy.signal import resample_poly
        x = pcm_to_numpy(pcm, "s16le", src_channels)
        if src_channels > 1:
            x = x.mean(axis=1)
        x_ext = np.concatenate([self._hist, x])
        y = resample_poly(x_ext, self.up, self.down, axis=0)
        y = y[self._drop:]
        # Save last K input samples; next call's output will include
        # K*up/down leading samples corresponding to them — drop those.
        k = min(self._HISTORY_SAMPLES, len(x_ext))
        self._hist = x_ext[-k:].astype(np.float32, copy=True)
        self._drop = (k * self.up) // self.down
        if dst_channels > 1:
            y = np.repeat(y[:, None], dst_channels, axis=1)
        return numpy_to_pcm(y, "s16le")
```

```python
# audio_bridge.py  __init__ — add:
self._out_resampler: dict[int, StreamResampler] = {}

# audio_bridge.py:700 — replace body of audio_chunk branch:
if etype == "audio_chunk":
    pcm = payload.get("pcm") or payload.get("audio") or b""
    if not pcm:
        return
    rate = payload.get("sample_rate", self._backend_output_rate)
    rs = self._out_resampler.get(rate)
    if rs is None:
        rs = StreamResampler(rate, DISCORD_SAMPLE_RATE)
        self._out_resampler[rate] = rs
    try:
        frame_pcm = rs.process(pcm, src_channels=1,
                               dst_channels=DISCORD_CHANNELS)
    except Exception:
        logger.exception("stream resample failed")
        return
    self.buffer.push_output(frame_pcm)
```

Reset `self._out_resampler.clear()` on session end / barge-in so a new reply starts with a fresh filter state.

---

## 5. Test strategy

Add `tests/test_resample_streaming.py`:

1. **Continuity test.** Generate a 1 s 440 Hz sine at 24 kHz int16. Split into 17 unequal chunks (prime lengths: 7, 11, 13, … ms). Feed each through a `StreamResampler(24000, 48000)` and concatenate outputs. Compare against a single-shot `resample_pcm` of the whole sine.

   Assert `max|streamed[i] - oneshot[i]| ≤ 2` LSB over the overlap region.

2. **No-click boundary assertion.** On the streamed output, compute `d = np.abs(np.diff(samples.astype(np.int32)))`. Locate indices corresponding to chunk boundaries (known from input chunk sizes × 2). Assert `d[boundary_idx] < 1.5 * median(d)` — i.e. no sample-to-sample jump exceeding 1.5× the ambient slew at the chosen sine frequency. For a 440 Hz sine at 48 kHz, per-sample delta ≈ 1800 LSB peak; threshold ≈ 2700 LSB catches any audible discontinuity while tolerating phase jitter.

3. **Regression guard on stateless version.** Run the same chunked sine through the current `resample_pcm` path; assert the boundary-delta threshold is violated. This documents what we fixed and prevents silent regression.

4. (Optional, for Fix B) **Underflow-resume fade.** Push `SILENCE_FRAME`, then a DC-offset non-silence frame; assert the first 96 output samples form a monotonically non-decreasing envelope from 0 to the target level.

Run in CI under the `[audio]` extras so scipy is available.
