"""Audio resampling + format-conversion utilities.

Per ADR-0005: uses ``scipy.signal.resample_poly`` with rational-ratio
(via ``math.gcd``) for sample-rate conversion. scipy is an opt-in dep
(``pip install hermes-s2s[audio]``) and is imported lazily inside
``resample_pcm`` so this module imports cleanly without it; same-rate
operations (format conversion, stereo↔mono mixdown) never require scipy.
"""

from __future__ import annotations

from math import gcd
from typing import Literal

import numpy as np

Dtype = Literal["s16le", "f32"]

_SCIPY_HINT = (
    "scipy is required for audio resampling when src_rate != dst_rate. "
    "Install with: pip install 'hermes-s2s[audio]'"
)


def pcm_to_numpy(audio_bytes: bytes, dtype: Dtype, channels: int) -> np.ndarray:
    """Decode raw PCM bytes to a float32 numpy array in [-1.0, 1.0].

    Returns a 1-D array for mono, 2-D (frames, channels) for multi-channel.
    """
    if dtype == "s16le":
        arr = np.frombuffer(audio_bytes, dtype="<i2").astype(np.float32) / 32768.0
    elif dtype == "f32":
        arr = np.frombuffer(audio_bytes, dtype="<f4").astype(np.float32, copy=True)
    else:
        raise ValueError(f"Unknown src_dtype: {dtype!r} (expected 's16le' or 'f32')")

    if channels > 1:
        arr = arr.reshape(-1, channels)
    return arr


def numpy_to_pcm(arr: np.ndarray, dtype: Dtype) -> bytes:
    """Encode a float32 numpy array (any shape) back to interleaved PCM bytes."""
    flat = arr.reshape(-1) if arr.ndim > 1 else arr
    if dtype == "s16le":
        out = np.clip(flat * 32768.0, -32768, 32767).astype("<i2")
    elif dtype == "f32":
        out = flat.astype("<f4")
    else:
        raise ValueError(f"Unknown dst_dtype: {dtype!r} (expected 's16le' or 'f32')")
    return out.tobytes()


def resample_pcm(
    audio_bytes: bytes,
    src_rate: int,
    dst_rate: int,
    src_channels: int = 1,
    dst_channels: int = 1,
    src_dtype: Dtype = "s16le",
    dst_dtype: Dtype = "s16le",
) -> bytes:
    """Convert PCM audio between sample rates, channel counts, and bit depths.

    Optimized for hermes-s2s's boundary cases: Discord 48k stereo s16le,
    realtime backend 16/24k mono PCM16, and ML model float32 mono.

    Raises ImportError if scipy is not installed AND src_rate != dst_rate.
    Same-rate operations (format + channel conversion only) work without scipy.
    """
    # Decode
    arr = pcm_to_numpy(audio_bytes, src_dtype, src_channels)

    # Channel conversion
    if src_channels > 1 and dst_channels == 1:
        arr = arr.mean(axis=1)
    elif src_channels == 1 and dst_channels > 1:
        # upmix mono -> N by duplication
        arr = np.repeat(arr[:, None], dst_channels, axis=1)
    elif src_channels > 1 and dst_channels > 1 and src_channels != dst_channels:
        raise ValueError(
            f"Unsupported channel conversion {src_channels}->{dst_channels}; "
            "only mono<->multi supported."
        )

    # Resample (lazy-import scipy)
    if src_rate != dst_rate:
        try:
            from scipy.signal import resample_poly
        except ImportError as e:  # pragma: no cover - exercised when scipy absent
            raise ImportError(_SCIPY_HINT) from e

        g = gcd(src_rate, dst_rate)
        up = dst_rate // g
        down = src_rate // g
        # resample_poly operates along axis=0 by default, which is what we want
        arr = resample_poly(arr, up, down, axis=0).astype(np.float32, copy=False)

    return numpy_to_pcm(arr, dst_dtype)
