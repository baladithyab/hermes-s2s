"""Tests for hermes_s2s.audio.resample.

Covers the five cases called out in the wave-0.3.0 plan (R1) / ADR-0005:
  a. 48k stereo s16le  -> 16k mono float32   (Discord -> Gemini Live in)
  b. 24k mono float32  -> 48k stereo s16le   (Gemini Live out -> Discord)
  c. same-rate same-channel passthrough      (no scipy needed)
  d. stereo -> mono mixdown only             (no scipy needed)
  e. round-trip 1s 440 Hz tone preserves peak frequency within 1 FFT bin
"""

from __future__ import annotations

import numpy as np
import pytest

from hermes_s2s.audio.resample import numpy_to_pcm, pcm_to_numpy, resample_pcm


def _sine_s16le(freq: float, rate: int, duration_s: float, channels: int = 1) -> bytes:
    t = np.arange(int(rate * duration_s), dtype=np.float32) / rate
    mono = 0.5 * np.sin(2 * np.pi * freq * t).astype(np.float32)
    if channels == 1:
        arr = mono
    else:
        arr = np.repeat(mono[:, None], channels, axis=1).reshape(-1)
    return np.clip(arr * 32768.0, -32768, 32767).astype("<i2").tobytes()


# --- (c) same-rate passthrough — no scipy ------------------------------------


def test_same_rate_same_channels_passthrough_no_scipy():
    src = _sine_s16le(440.0, 16000, 0.1, channels=1)
    out = resample_pcm(
        src, 16000, 16000, src_channels=1, dst_channels=1,
        src_dtype="s16le", dst_dtype="s16le",
    )
    # Exactly equal byte-for-byte: no resampling, no dtype change, no channel change.
    assert out == src
    # And no dependency on scipy — if scipy is absent, this must still work.
    # (The test collection itself proves the module imported cleanly.)


# --- (d) stereo -> mono mixdown only — no scipy ------------------------------


def test_stereo_to_mono_mixdown_no_rate_change_no_scipy():
    # Two channels with known values: L=+0.5, R=-0.5 at s16le -> mean should be 0
    n = 1024
    L = np.full(n, 0.5, dtype=np.float32)
    R = np.full(n, -0.5, dtype=np.float32)
    stereo = np.stack([L, R], axis=1).reshape(-1)
    src_bytes = np.clip(stereo * 32768.0, -32768, 32767).astype("<i2").tobytes()

    out_bytes = resample_pcm(
        src_bytes, 48000, 48000,
        src_channels=2, dst_channels=1,
        src_dtype="s16le", dst_dtype="s16le",
    )
    out = np.frombuffer(out_bytes, dtype="<i2")
    assert out.shape == (n,)
    # Mean of +0.5 and -0.5 is 0 -> samples should be at/near zero.
    assert np.max(np.abs(out)) <= 1  # allow 1 LSB of rounding


# --- (a) 48k stereo s16le -> 16k mono float32 --------------------------------


def test_discord_to_gemini_live_in_48k_stereo_s16le_to_16k_mono_f32():
    pytest.importorskip("scipy")
    src = _sine_s16le(440.0, 48000, 0.25, channels=2)  # 0.25s stereo
    out = resample_pcm(
        src, 48000, 16000,
        src_channels=2, dst_channels=1,
        src_dtype="s16le", dst_dtype="f32",
    )
    out_arr = np.frombuffer(out, dtype="<f4")
    # 0.25s at 16k mono -> 4000 samples (resample_poly may be off by ~1 due to polyphase)
    assert abs(len(out_arr) - 4000) <= 2
    # Amplitude should still be bounded (float32 in [-1, 1])
    assert np.max(np.abs(out_arr)) < 1.0
    # And clearly non-zero (signal preserved)
    assert np.max(np.abs(out_arr)) > 0.1


# --- (b) 24k mono float32 -> 48k stereo s16le --------------------------------


def test_gemini_live_out_24k_mono_f32_to_48k_stereo_s16le():
    pytest.importorskip("scipy")
    t = np.arange(24000, dtype=np.float32) / 24000.0  # 1s
    mono_f32 = (0.5 * np.sin(2 * np.pi * 440.0 * t)).astype("<f4").tobytes()
    out = resample_pcm(
        mono_f32, 24000, 48000,
        src_channels=1, dst_channels=2,
        src_dtype="f32", dst_dtype="s16le",
    )
    out_arr = np.frombuffer(out, dtype="<i2")
    # 1s at 48k stereo -> 96000 interleaved samples
    assert abs(len(out_arr) - 96000) <= 4
    # Channel duplication: L == R (mono upmix)
    stereo = out_arr.reshape(-1, 2)
    assert np.array_equal(stereo[:, 0], stereo[:, 1])


# --- (e) round-trip preserves peak frequency ---------------------------------


def test_round_trip_440hz_preserves_peak_frequency_within_one_fft_bin():
    pytest.importorskip("scipy")
    src_rate = 48000
    mid_rate = 16000
    duration = 1.0
    freq = 440.0

    src = _sine_s16le(freq, src_rate, duration, channels=1)
    # 48k -> 16k -> 48k, stay mono s16le
    down = resample_pcm(src, src_rate, mid_rate, 1, 1, "s16le", "s16le")
    back = resample_pcm(down, mid_rate, src_rate, 1, 1, "s16le", "s16le")

    arr = pcm_to_numpy(back, "s16le", 1)  # float32 in [-1, 1]
    # FFT on the recovered signal
    n = len(arr)
    spectrum = np.abs(np.fft.rfft(arr))
    freqs = np.fft.rfftfreq(n, d=1.0 / src_rate)
    peak_hz = freqs[int(np.argmax(spectrum))]
    bin_width = src_rate / n  # ≈ 1 Hz for 1s @ 48k
    assert abs(peak_hz - freq) <= bin_width, (
        f"peak {peak_hz} Hz drifted > {bin_width} Hz from {freq} Hz"
    )
