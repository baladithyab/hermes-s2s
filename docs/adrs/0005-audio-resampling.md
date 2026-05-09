# ADR-0005: Audio resampling — `scipy.signal.resample_poly` with rational gcd

**Status:** accepted
**Date:** 2026-05-09
**Deciders:** Codeseys, ARIA-orchestrator

## Context

`hermes-s2s` needs to convert audio between sample rates and channel counts at multiple boundaries:

- **Discord-side:** 48 kHz s16le stereo (Discord-decoded Opus) ↔ realtime backend formats.
- **Gemini Live:** 16 kHz PCM16 mono in, 24 kHz PCM16 mono out.
- **OpenAI Realtime:** 24 kHz PCM16 mono both directions.
- **Moonshine STT:** 16 kHz mono float32.
- **Kokoro TTS:** outputs 24 kHz mono float32 (then we may need to resample to 48 kHz for Discord output).

Three serious resampling libraries available:

1. **`scipy.signal.resample_poly`** — already used in user's v6 pipeline. Polyphase rational resampler with anti-alias filter. Standard scipy install footprint (~30MB). Works everywhere scipy works.
2. **`samplerate`** (pip pkg, libsamplerate / Secret Rabbit Code bindings) — best quality, native code, smaller than scipy. Adds an extra dep.
3. **`librosa.resample`** — high-quality, used in audio-ML circles. ~50MB+ dep tree. Overkill.

## Decision

Use **`scipy.signal.resample_poly`** for all resampling. Compute up/down ratio via `math.gcd(src_rate, dst_rate)`.

Reasoning:
- scipy is already an optional `[audio]` extra; users who want hermes-s2s to handle weird sample rates already opt into it.
- v6 (which this plugin extends) already uses exactly this approach with documented good results.
- Rational-ratio polyphase is the right algorithm class for sample-rate conversions in our range (8 kHz to 48 kHz, all factorable into small primes).
- No need to add a third audio library when the user already has scipy for STT.

For pure stereo→mono mixdown (no rate change), use a plain numpy `mean(axis=1)` — no scipy needed, runs in microseconds.

For format conversion (s16le ↔ float32), implement inline in the resampling utility module:
- `s16le → float32`: `audio.astype(np.float32) / 32768.0`
- `float32 → s16le`: `np.clip(audio * 32768.0, -32768, 32767).astype(np.int16)`

## Implementation

`hermes_s2s/audio/resample.py`:

```python
import numpy as np
from math import gcd

def resample_pcm(
    audio_bytes: bytes,
    src_rate: int,
    dst_rate: int,
    src_channels: int = 1,
    dst_channels: int = 1,
    src_dtype: str = "s16le",        # s16le | f32
    dst_dtype: str = "s16le",
) -> bytes:
    """Convert PCM audio between sample rates, channel counts, and bit depths.

    Optimized for hermes-s2s's three boundary cases: Discord 48k stereo s16le,
    realtime backend 16/24k mono PCM16, and ML model float32 mono.
    """
    # Decode bytes to numpy
    if src_dtype == "s16le":
        arr = np.frombuffer(audio_bytes, dtype="<i2").astype(np.float32) / 32768.0
    else:
        arr = np.frombuffer(audio_bytes, dtype="<f4")

    # Reshape for channels
    if src_channels > 1:
        arr = arr.reshape(-1, src_channels)
        # Mixdown if requested
        if dst_channels == 1:
            arr = arr.mean(axis=1)

    # Resample if rates differ
    if src_rate != dst_rate:
        from scipy.signal import resample_poly
        g = gcd(src_rate, dst_rate)
        up = dst_rate // g
        down = src_rate // g
        arr = resample_poly(arr, up, down)

    # Encode back
    if dst_dtype == "s16le":
        arr = np.clip(arr * 32768.0, -32768, 32767).astype("<i2")
    else:
        arr = arr.astype("<f4")

    return arr.tobytes()
```

## Consequences

**Positive:**
- Single dep (`scipy[audio]` extra). Aligns with v6 pipeline.
- Polyphase quality is fine for speech (THD typically <0.1%, indistinguishable from raw at speech band).
- Clear API; one function, six args, every boundary case in our matrix expressible.

**Negative:**
- `resample_poly` allocates intermediate buffers — for very long streams we'd want chunked resampling. **Acceptable for 20-50ms frames** (our case).
- scipy is a heavy dep for users who don't otherwise need it. Mitigation: it's gated behind the `[audio]` extra; users who only do same-rate work (e.g., 24kHz Kokoro → 24kHz OpenAI Realtime, no Discord) never trigger it.

## Open questions

- Anti-aliasing filter quality at boundary cases (e.g., 48k → 16k 3:1 ratio at speech bandwidth) — confirmed sufficient in v6; flag if any backend reports aliasing artifacts.
- Streaming-friendly resampling (chunk-by-chunk with state preservation) — not needed for 0.2.0 / 0.3.0; if we ever do truly streaming resampling for very long calls, revisit.
