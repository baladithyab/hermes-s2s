"""Tests for hermes_s2s.audio.streaming_resample (S1: audio clicks fix)."""

from __future__ import annotations

import numpy as np
import pytest

soxr = pytest.importorskip("soxr", reason="soxr required for streaming resampler")

from hermes_s2s.audio.streaming_resample import ResamplerCache, StreamingResampler


# ---------- helpers --------------------------------------------------- #


def _sine_pcm(freq_hz: float, duration_s: float, rate: int, amplitude: int = 8000) -> bytes:
    """Generate a sine-wave int16 mono PCM byte string."""
    n = int(rate * duration_s)
    t = np.arange(n, dtype=np.float64) / rate
    x = (np.sin(2 * np.pi * freq_hz * t) * amplitude).astype(np.int16)
    return x.tobytes()


def _split_into_chunks(pcm: bytes, chunk_lens: list[int]) -> list[bytes]:
    """Split PCM bytes into chunks of given (in-bytes) lengths.

    Pads/truncates the last chunk to use up remaining data.
    """
    out = []
    pos = 0
    for L in chunk_lens:
        if pos + L > len(pcm):
            out.append(pcm[pos:])
            pos = len(pcm)
            break
        out.append(pcm[pos : pos + L])
        pos += L
    if pos < len(pcm):
        out.append(pcm[pos:])
    return out


def _abs_int16_diff(a: bytes, b: bytes) -> np.ndarray:
    """Element-wise abs delta between two int16 PCM buffers (truncated to min length)."""
    arr_a = np.frombuffer(a, dtype=np.int16)
    arr_b = np.frombuffer(b, dtype=np.int16)
    n = min(len(arr_a), len(arr_b))
    return np.abs(arr_a[:n].astype(np.int32) - arr_b[:n].astype(np.int32))


# ---------- StreamingResampler basics -------------------------------- #


class TestStreamingResamplerBasics:
    def test_construct_24k_to_48k_mono(self) -> None:
        rs = StreamingResampler(24000, 48000, channels=1)
        assert rs.in_rate == 24000
        assert rs.out_rate == 48000
        assert rs.channels == 1

    def test_invalid_rate_raises(self) -> None:
        with pytest.raises(ValueError):
            StreamingResampler(0, 48000)
        with pytest.raises(ValueError):
            StreamingResampler(24000, -1)

    def test_invalid_channels_raises(self) -> None:
        with pytest.raises(ValueError):
            StreamingResampler(24000, 48000, channels=3)

    def test_invalid_dtype_raises(self) -> None:
        with pytest.raises(ValueError):
            StreamingResampler(24000, 48000, dtype="int8")

    def test_empty_pcm_returns_empty(self) -> None:
        rs = StreamingResampler(24000, 48000)
        assert rs.process(b"") == b""

    def test_partial_frame_raises(self) -> None:
        rs = StreamingResampler(24000, 48000, channels=2)
        # 2 channels * 2 bytes = 4-byte frame; 5 bytes is invalid
        with pytest.raises(ValueError):
            rs.process(b"\x00" * 5)

    def test_process_increases_frames_counter(self) -> None:
        rs = StreamingResampler(24000, 48000)
        before = rs.frames_processed
        rs.process(_sine_pcm(440, 0.05, 24000))  # 50ms = 1200 frames
        assert rs.frames_processed > before
        # mono -> exactly 1200 frames
        assert rs.frames_processed == 1200


# ---------- continuity: chunked vs one-shot --------------------------- #


class TestChunkedContinuity:
    """Chunked processing should produce ~equivalent output to one-shot.

    soxr is HQ but not bit-exact — we allow a small tolerance.
    """

    def test_chunked_24k_to_48k_matches_oneshot(self) -> None:
        # 1 second of 1 kHz sine at 24kHz mono
        pcm_24k = _sine_pcm(1000.0, 1.0, 24000)

        # One-shot reference
        rs_ref = StreamingResampler(24000, 48000, channels=1)
        ref = rs_ref.process(pcm_24k, last=True)

        # Chunked: 13 prime-length chunks (in samples)
        # Convert sample-count to byte-count for mono int16
        sample_lens = [97, 113, 127, 199, 311, 401, 503, 619, 743, 853, 971, 1091, 1213]
        byte_lens = [n * 2 for n in sample_lens]
        chunks = _split_into_chunks(pcm_24k, byte_lens)

        rs_stream = StreamingResampler(24000, 48000, channels=1)
        out = b""
        for i, c in enumerate(chunks):
            is_last = i == len(chunks) - 1
            out += rs_stream.process(c, last=is_last)

        # Allow some tolerance — soxr HQ is high-fidelity but chunking
        # may shift the alignment slightly. Tolerance 64 LSB on a 1kHz
        # sine at amplitude 8000 = ~0.8% relative error.
        diff = _abs_int16_diff(out, ref)
        # Trim filter delay region (first ~60 samples) where startup
        # transient differs slightly between chunked and one-shot.
        diff_steady = diff[60:]
        assert diff_steady.max() < 64, (
            f"chunked vs oneshot max diff = {diff_steady.max()} LSB "
            f"(want < 64); mean = {diff_steady.mean():.1f}"
        )

    def test_no_click_at_chunk_boundaries(self) -> None:
        """Sample-to-sample delta at chunk boundaries should not exceed
        the natural slew of a clean signal."""
        # 0.5s of 1kHz sine; chunked into 5x100ms pieces
        pcm = _sine_pcm(1000.0, 0.5, 24000)
        chunk_size = 24000 // 10  # 100ms in samples
        byte_chunks = [
            pcm[i * chunk_size * 2 : (i + 1) * chunk_size * 2]
            for i in range(5)
        ]

        rs = StreamingResampler(24000, 48000, channels=1)
        out_pieces = [rs.process(c) for c in byte_chunks]
        out = b"".join(out_pieces)

        out_arr = np.frombuffer(out, dtype=np.int16).astype(np.int32)
        deltas = np.abs(np.diff(out_arr))
        # 1kHz sine at 48kHz: per-sample delta peak ≈ 1050 LSB at zero crossing.
        # No single sample-pair should be more than ~3x that — which would
        # only happen at a discontinuity.
        max_natural_slew = int(np.percentile(deltas, 99.5))
        assert deltas.max() < 3 * max_natural_slew, (
            f"max delta {deltas.max()} > 3x natural slew {max_natural_slew}"
        )

    def test_clear_resets_state_for_fresh_signal(self) -> None:
        """After clear(), the same resampler should treat new input as a
        fresh signal — no filter-state leak from before."""
        rs = StreamingResampler(24000, 48000)
        rs.process(_sine_pcm(1000.0, 0.1, 24000))  # warm filter
        rs.clear()
        # Now feed silence; output should converge to silence quickly
        # (would have residual ringing without clear)
        out = rs.process(b"\x00" * (24000 * 2 // 10))  # 100ms silence
        out_arr = np.frombuffer(out, dtype=np.int16).astype(np.int32)
        # Allow some startup transient; tail should be silent
        tail = out_arr[len(out_arr) // 2 :]
        assert np.abs(tail).max() < 100, (
            f"tail abs max = {np.abs(tail).max()} (want < 100); "
            "filter state may have leaked"
        )


# ---------- different ratios ----------------------------------------- #


class TestRatios:
    """Spot-check the ratios used in production."""

    def test_24k_to_48k(self) -> None:
        # Use last=True to flush filter delay so total output matches expected ratio.
        rs = StreamingResampler(24000, 48000)
        out = rs.process(_sine_pcm(1000.0, 0.1, 24000), last=True)
        in_samples = 24000 * 0.1
        out_samples = len(out) // 2
        assert 0.9 * (2 * in_samples) <= out_samples <= 1.1 * (2 * in_samples)

    def test_48k_to_16k(self) -> None:
        # Discord input → Gemini Live (the case red-team P0-2 flagged)
        rs = StreamingResampler(48000, 16000)
        out = rs.process(_sine_pcm(1000.0, 0.1, 48000), last=True)
        in_samples = 48000 * 0.1
        out_samples = len(out) // 2
        assert 0.9 * (in_samples / 3) <= out_samples <= 1.1 * (in_samples / 3)

    def test_16k_to_48k(self) -> None:
        rs = StreamingResampler(16000, 48000)
        out = rs.process(_sine_pcm(1000.0, 0.1, 16000), last=True)
        in_samples = 16000 * 0.1
        out_samples = len(out) // 2
        assert 0.9 * (3 * in_samples) <= out_samples <= 1.1 * (3 * in_samples)


# ---------- ResamplerCache ------------------------------------------- #


class TestResamplerCache:
    def test_get_caches_by_triple(self) -> None:
        cache = ResamplerCache()
        rs1 = cache.get(24000, 48000, 1)
        rs2 = cache.get(24000, 48000, 1)
        assert rs1 is rs2
        assert len(cache) == 1

    def test_different_triples_cached_separately(self) -> None:
        cache = ResamplerCache()
        rs_a = cache.get(24000, 48000, 1)
        rs_b = cache.get(48000, 16000, 1)
        rs_c = cache.get(24000, 48000, 2)
        assert rs_a is not rs_b
        assert rs_a is not rs_c
        assert len(cache) == 3

    def test_clear_resets_all_streams(self) -> None:
        cache = ResamplerCache()
        rs = cache.get(24000, 48000, 1)
        rs.process(_sine_pcm(1000.0, 0.1, 24000))
        # frames_processed is on resampler instance; clear() doesn't reset it
        # but the underlying soxr filter state should be flushed.
        cache.clear()
        # Confirm instance preserved
        assert cache.get(24000, 48000, 1) is rs
        assert len(cache) == 1

    def test_drop_releases_all_streams(self) -> None:
        cache = ResamplerCache()
        cache.get(24000, 48000, 1)
        cache.get(48000, 16000, 1)
        cache.drop()
        assert len(cache) == 0
        # New call constructs fresh
        rs_new = cache.get(24000, 48000, 1)
        assert len(cache) == 1
        assert rs_new is not None


# ---------- regression fence: stateless path WOULD click ------------- #


class TestRegressionAgainstStatelessPath:
    """This documents the bug we fixed: chunking through stateless
    resample_pcm produces visible boundary discontinuities. Future
    regressions (someone reverts to stateless) get caught."""

    def test_stateless_resample_pcm_produces_boundary_clicks(self) -> None:
        """Asserts the OLD path is bad: realistic Gemini Live chunk
        sizes (variable length, NOT period-aligned) produce visible
        discontinuities. If this test fails, either scipy got smarter
        or the chunking changed.

        Note: an earlier draft of this test used periodic 100ms chunks
        which happened to align with the 1kHz sine period — boundaries
        landed at near-zero crossings and the click was masked. Real
        Gemini chunks arrive at variable widths (~20-100ms) so we
        simulate that.
        """
        try:
            from hermes_s2s.audio.resample import resample_pcm
        except ImportError:
            pytest.skip("scipy not installed")

        import random

        rng = random.Random(42)
        # Variable chunk sizes 200-600 samples each (realistic Gemini Live)
        chunk_sample_lens = [rng.randint(200, 600) for _ in range(20)]
        total = sum(chunk_sample_lens)
        pcm = _sine_pcm(440.0, total / 24000, 24000)

        # Stateless: each chunk independently resampled
        out_pieces = []
        pos = 0
        for n in chunk_sample_lens:
            chunk = pcm[pos * 2 : (pos + n) * 2]
            pos += n
            try:
                res = resample_pcm(chunk, src_rate=24000, dst_rate=48000)
            except ImportError:
                pytest.skip("scipy not installed")
            out_pieces.append(res)

        out = b"".join(out_pieces)
        out_arr = np.frombuffer(out, dtype=np.int16).astype(np.int32)
        deltas = np.abs(np.diff(out_arr))

        # Find boundaries in OUTPUT-sample space
        out_chunk_lens = [len(p) // 2 for p in out_pieces]
        boundaries = []
        cumsum = 0
        for L in out_chunk_lens[:-1]:
            cumsum += L
            boundaries.append(cumsum - 1)

        boundary_deltas = [int(deltas[b]) for b in boundaries if b < len(deltas)]
        natural_95th = int(np.percentile(deltas, 95))
        big_jumps = [d for d in boundary_deltas if d > 2 * natural_95th]

        # We expect MOST boundaries to show clicks (not just ≥1).
        # On the experimental run, boundary deltas were 6x natural slew
        # for the majority of boundaries.
        assert len(big_jumps) >= len(boundary_deltas) // 2, (
            "Expected majority of stateless-path boundaries to show "
            f"audible discontinuities; saw {len(big_jumps)}/{len(boundary_deltas)} "
            f"jumps > 2x natural_95th={natural_95th}. "
            f"Boundary deltas: {boundary_deltas[:10]}... "
            "If scipy got smarter, remove this regression fence."
        )
