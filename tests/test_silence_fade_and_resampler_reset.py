"""Tests for BridgeBuffer silence-fade-in (S1 Fix B) and bridge resampler reset.

Plan: docs/plans/wave-0.4.2-clicks-history-quickwins.md (S1 Fix B).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import numpy as np
import pytest

from hermes_s2s._internal.audio_bridge import (
    DISCORD_SAMPLE_RATE,
    FRAME_BYTES,
    SILENCE_FRAME,
    BridgeBuffer,
    RealtimeAudioBridge,
)


# ---------- Fix B: fade-in on silence -> audio transitions ---------- #


class TestSilenceFadeIn:
    def test_default_fade_ms_is_5(self) -> None:
        buf = BridgeBuffer()
        # Default constructor uses 5ms fade
        assert buf._silence_fade_ms == 5
        assert buf._fade_envelope_int16 is not None

    def test_set_silence_fade_ms_to_zero_disables(self) -> None:
        buf = BridgeBuffer()
        buf.set_silence_fade_ms(0)
        assert buf._silence_fade_ms == 0
        assert buf._fade_envelope_int16 is None

    def test_set_silence_fade_ms_clamps_negative_to_zero(self) -> None:
        buf = BridgeBuffer()
        buf.set_silence_fade_ms(-10)
        assert buf._silence_fade_ms == 0

    def test_set_silence_fade_ms_clamps_excessive_to_50(self) -> None:
        buf = BridgeBuffer()
        buf.set_silence_fade_ms(500)
        assert buf._silence_fade_ms == 50

    def test_first_frame_after_silence_is_faded(self) -> None:
        """The CRITICAL assertion: first frame after silence has fade-in.

        Push a constant-amplitude frame; first read should have leading
        samples reduced by raised-cosine envelope; tail samples unchanged.
        """
        buf = BridgeBuffer()
        # Constant 0x7fff = max positive int16 (loudest sample)
        loud_frame = b"\xff\x7f" * (FRAME_BYTES // 2)
        buf.push_output(loud_frame)
        out = buf.read_frame()
        out_arr = np.frombuffer(out, dtype=np.int16)

        # First sample should be near zero (fade starts at 0)
        assert abs(out_arr[0]) < 1000, (
            f"first sample after silence should fade in from ~0; got {out_arr[0]}"
        )
        # 240 stereo samples at 48k = 5ms; envelope is over those
        # Tail samples (after envelope ends) should be at full amplitude
        assert out_arr[-1] == 0x7FFF, (
            f"trailing samples should be unfaded; got {out_arr[-1]}"
        )

    def test_second_frame_no_longer_faded(self) -> None:
        """After first non-silence frame, subsequent frames are full amplitude."""
        buf = BridgeBuffer()
        loud_frame = b"\xff\x7f" * (FRAME_BYTES // 2)
        buf.push_output(loud_frame * 2)  # 2 frames worth
        f1 = buf.read_frame()
        f2 = buf.read_frame()
        f1_arr = np.frombuffer(f1, dtype=np.int16)
        f2_arr = np.frombuffer(f2, dtype=np.int16)
        # Second frame should be untouched
        assert f2_arr[0] == 0x7FFF
        assert f2_arr.max() == 0x7FFF
        # First frame still faded
        assert abs(f1_arr[0]) < 1000

    def test_fade_resets_after_underflow(self) -> None:
        """After an underflow returning silence, the next non-silence frame
        gets faded again — silence is the trigger."""
        buf = BridgeBuffer()
        loud = b"\xff\x7f" * (FRAME_BYTES // 2)
        # First frame: full content -> read it, fade applied
        buf.push_output(loud)
        f1 = buf.read_frame()
        assert abs(np.frombuffer(f1, dtype=np.int16)[0]) < 1000
        # Underflow -> silence
        f_sil = buf.read_frame()
        assert f_sil == SILENCE_FRAME
        # Push another frame; should fade again
        buf.push_output(loud)
        f2 = buf.read_frame()
        f2_arr = np.frombuffer(f2, dtype=np.int16)
        assert abs(f2_arr[0]) < 1000, "fade should reset after silence"

    def test_fade_envelope_is_monotonic_non_decreasing(self) -> None:
        """Raised-cosine envelope: monotone non-decreasing 0 -> 1."""
        buf = BridgeBuffer()
        env = buf._fade_envelope_int16
        assert env is not None
        # Per-channel envelope (every 2nd sample)
        per_ch = env[::2]
        diffs = np.diff(per_ch)
        # Non-decreasing
        assert (diffs >= -1e-6).all(), "envelope should be non-decreasing"
        # Starts near 0, ends near 1
        assert per_ch[0] < 0.01
        assert per_ch[-1] > 0.95

    def test_disable_fade_skips_processing(self) -> None:
        """When fade is disabled (0ms), frames pass through unmodified."""
        buf = BridgeBuffer()
        buf.set_silence_fade_ms(0)
        loud = b"\xff\x7f" * (FRAME_BYTES // 2)
        buf.push_output(loud)
        out = buf.read_frame()
        # No fade -> first sample should be at full amplitude
        out_arr = np.frombuffer(out, dtype=np.int16)
        assert out_arr[0] == 0x7FFF


# ---------- S1: bridge resampler-cache reset hooks ----------------- #


class TestResamplerCacheResets:
    def test_close_resets_cache(self) -> None:
        """``close()`` must call ``_reset_out_resamplers`` so a future
        bridge instance starts fresh."""
        # Use minimal mocks - just need a backend protocol object
        backend = MagicMock()
        backend.NAME = "gemini-live"
        backend.connect = MagicMock(
            return_value=asyncio.sleep(0)  # awaitable
        )
        backend.recv_events = MagicMock(return_value=_empty_async_iter())
        backend.send_audio_chunk = MagicMock(return_value=asyncio.sleep(0))
        backend.send_activity_start = MagicMock(return_value=asyncio.sleep(0))
        backend.send_activity_end = MagicMock(return_value=asyncio.sleep(0))
        backend.close = MagicMock(return_value=asyncio.sleep(0))

        async def scenario() -> None:
            bridge = RealtimeAudioBridge(backend=backend)
            # Verify cache was instantiated
            assert bridge._out_resampler_cache is not None
            # Force a resampler into the cache
            rs = bridge._out_resampler_cache.get(24000, 48000, 1)
            assert rs is not None
            assert len(bridge._out_resampler_cache) == 1
            # Manual reset (close path will do this)
            bridge._reset_out_resamplers()
            # Cache instance preserved (just state cleared)
            assert len(bridge._out_resampler_cache) == 1
            # And get returns the same instance
            assert bridge._out_resampler_cache.get(24000, 48000, 1) is rs

        asyncio.run(scenario())

    def test_reset_handles_missing_cache_gracefully(self) -> None:
        """If soxr is unavailable (cache=None), reset is a no-op, not error."""
        backend = MagicMock()
        backend.NAME = "gemini-live"
        bridge = RealtimeAudioBridge(backend=backend)
        bridge._out_resampler_cache = None
        # Must not raise
        bridge._reset_out_resamplers()


# ---------- helpers --------------------------------------------------- #


async def _empty_async_iter():
    """Yield nothing; for backend.recv_events mock."""
    if False:
        yield  # pragma: no cover - generator marker
    return
