"""Streaming PCM resampler — eliminates chunk-boundary clicks.

Why this module exists
----------------------
``resample_pcm`` (in ``resample.py``) is stateless: each call discards
the FIR filter's tail and starts the next call cold. When realtime
backends emit audio in 20-100ms chunks (Gemini Live, OpenAI Realtime),
the boundary discontinuity is audible as a click at the chunk rate.

Research/17 originally proposed a custom overlap-save wrapper around
``scipy.signal.resample_poly``. Adversarial review (red-team P0-1/2/3)
found the math was wrong in three ways: direction inverted (only fixed
the *next* chunk's leading edge, not the previous chunk's trailing
edge), K=32 history insufficient at 48k→16k, and integer-truncation
drift at 1:3 ratios.

We use ``soxr.ResampleStream`` instead. It's a streaming-first API
(maintained by SoX heritage), int16-native, and handles every reset
case correctly. Already a transitive dep of librosa/audiocraft so no
new install burden.

Public API
----------
``StreamingResampler(in_rate, out_rate, channels, dtype='int16')``
    Per-stream stateful resampler. Hold one per (in, out, channels)
    triple. Call ``process(pcm_bytes) -> bytes``. Call ``clear()`` on
    barge-in / reconnect / activity_start / session swap.

``ResamplerCache``
    Per-bridge cache keyed on ``(in_rate, out_rate, channels)``.
    ``get(in, out, ch)`` → cached or new ``StreamingResampler``.
    ``clear()`` → reset all (fresh start, no reallocation).
    ``drop()`` → release all (session end, free memory).

Both expose ``frames_processed`` for observability.
"""

from __future__ import annotations

import logging
from typing import Dict, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Lazy import for clean module-load when soxr is absent.
_soxr = None


def _get_soxr():
    """Lazy-import soxr; raise informative error if unavailable."""
    global _soxr
    if _soxr is None:
        try:
            import soxr as _module  # type: ignore
        except ImportError as e:
            raise ImportError(
                "StreamingResampler requires the `soxr` package. "
                "soxr is a transitive dep of librosa; install with: "
                "pip install soxr"
            ) from e
        _soxr = _module
    return _soxr


class StreamingResampler:
    """Per-stream stateful resampler over ``soxr.ResampleStream``.

    Lifecycle
    ---------
    Construct once per (in_rate, out_rate, channels) triple. Feed PCM
    chunks through ``process``. Call ``clear()`` whenever the stream
    is *logically discontinuous* — barge-in, reconnect, new
    utterance — so the filter state doesn't bleed from old audio
    into new.

    Parameters
    ----------
    in_rate, out_rate
        Sample rates in Hz.
    channels
        1 (mono) or 2 (stereo). Channels are preserved through the
        resampler (no mixdown). For mono→stereo upmix, do that
        BEFORE feeding the resampler — keep this class focused.
    dtype
        ``'int16'`` (default — Discord-native, no conversion cost) or
        ``'float32'`` (when downstream code prefers floats).
    """

    def __init__(
        self,
        in_rate: int,
        out_rate: int,
        channels: int = 1,
        dtype: str = "int16",
    ) -> None:
        if in_rate <= 0 or out_rate <= 0:
            raise ValueError(
                f"sample rates must be positive (got in={in_rate}, out={out_rate})"
            )
        if channels not in (1, 2):
            raise ValueError(f"channels must be 1 or 2, got {channels}")
        if dtype not in ("int16", "float32"):
            raise ValueError(f"dtype must be 'int16' or 'float32', got {dtype!r}")

        self.in_rate = in_rate
        self.out_rate = out_rate
        self.channels = channels
        self.dtype = dtype
        self._np_dtype = np.int16 if dtype == "int16" else np.float32
        self._sample_size = 2 if dtype == "int16" else 4
        self.frames_processed = 0

        soxr = _get_soxr()
        self._stream = soxr.ResampleStream(
            in_rate=in_rate,
            out_rate=out_rate,
            num_channels=channels,
            dtype=dtype,
            quality="HQ",
        )

    def process(self, pcm: bytes, *, last: bool = False) -> bytes:
        """Resample one chunk; preserves filter state across calls.

        Parameters
        ----------
        pcm
            Raw PCM bytes in the configured ``dtype`` and ``channels``
            interleaved layout. Length must be a multiple of
            ``channels * sample_size``; partial samples are an error.
        last
            If True, flush remaining filter state. Use only when
            tearing down (no more audio for this stream). Equivalent
            to ``soxr.ResampleStream.resample_chunk(..., last=True)``.

        Returns
        -------
        bytes
            Resampled PCM, same dtype/channels layout as input.
            May be empty for very small input chunks (filter delay).
        """
        if not pcm:
            return b""

        frame_bytes = self.channels * self._sample_size
        if len(pcm) % frame_bytes != 0:
            raise ValueError(
                f"pcm length {len(pcm)} is not a multiple of "
                f"channels*sample_size={frame_bytes}"
            )

        # Decode to ndarray. soxr expects (frames, channels) for >1ch
        # and (frames,) for mono.
        arr = np.frombuffer(pcm, dtype=self._np_dtype)
        if self.channels > 1:
            arr = arr.reshape(-1, self.channels)

        out = self._stream.resample_chunk(arr, last=last)
        self.frames_processed += len(arr)

        # soxr returns the same dtype/channel layout as input
        return out.tobytes()

    def clear(self) -> None:
        """Reset filter state. Use on barge-in / reconnect / new utterance.

        After ``clear()`` the resampler is ready for a fresh signal at
        the same configuration — does NOT need a new instance. Saves
        allocation cost compared to constructing a fresh instance.
        """
        self._stream.clear()

    def delay_samples(self) -> float:
        """Current algorithmic delay in *output* samples (introspection)."""
        return float(self._stream.delay())


class ResamplerCache:
    """Per-bridge cache of ``StreamingResampler`` instances.

    Why a cache: realtime backends emit audio at one (or two) source
    rates; constructing a new resampler per chunk is wasteful. The
    cache is keyed on ``(in_rate, out_rate, channels)`` so e.g. a
    24k mono backend that briefly switches to 48k mono mid-session
    (theoretical; current backends don't) doesn't trash filter state
    on the original rate.

    Reset semantics
    ---------------
    ``clear()`` calls ``StreamingResampler.clear()`` on every cached
    entry — keeps the instances around for the next chunk but flushes
    their filter state. Use this on:

    - ``RealtimeAudioBridge.stop()`` (session end)
    - Backend reconnect (``connect()`` reuse)
    - Barge-in / interrupt
    - User ``activity_start`` (new utterance, current output abandoned)

    ``drop()`` releases all instances. Use only when the bridge itself
    is being garbage-collected.
    """

    def __init__(self) -> None:
        self._cache: Dict[Tuple[int, int, int], StreamingResampler] = {}

    def get(
        self, in_rate: int, out_rate: int, channels: int = 1
    ) -> StreamingResampler:
        """Return cached resampler or construct + cache a new one."""
        key = (in_rate, out_rate, channels)
        rs = self._cache.get(key)
        if rs is None:
            rs = StreamingResampler(in_rate, out_rate, channels)
            self._cache[key] = rs
            logger.debug(
                "ResamplerCache: created %d->%dHz x%d channels",
                in_rate,
                out_rate,
                channels,
            )
        return rs

    def clear(self) -> None:
        """Flush filter state on all cached resamplers (keep instances)."""
        for rs in self._cache.values():
            try:
                rs.clear()
            except Exception:  # noqa: BLE001 - defensive
                logger.exception("ResamplerCache.clear: failed to reset stream")

    def drop(self) -> None:
        """Release all cached instances (free memory)."""
        self._cache.clear()

    def __len__(self) -> int:
        return len(self._cache)


__all__ = ["StreamingResampler", "ResamplerCache"]
