"""Tests for S3 (UX slip-ins) and S4 (P0 audit quick-wins).

S3:
- BridgeBuffer.clear_output (audit #21 barge-in semantics)
- bridge.stats() exposes history_injected, voice, model (audit #42)

S4:
- TranscriptMirror uses get_running_loop, not deprecated get_event_loop (audit #5)
- GeminiLive _pending_first_msg initialised in __init__ (audit #6, already done in S2)
"""

from __future__ import annotations

import asyncio
import logging
import warnings
from unittest.mock import MagicMock

import pytest

from hermes_s2s._internal.audio_bridge import (
    FRAME_BYTES,
    SILENCE_FRAME,
    BridgeBuffer,
    RealtimeAudioBridge,
)


# ---------- Audit #21: BridgeBuffer.clear_output --------------------- #


class TestClearOutputBargeIn:
    def test_clear_output_drops_queued_frames(self) -> None:
        buf = BridgeBuffer()
        buf.set_silence_fade_ms(0)  # disable fade for byte-equality
        # Push 3 frames worth
        buf.push_output(b"\xaa" * (FRAME_BYTES * 3))
        assert buf.queued_output_frames == 3
        dropped = buf.clear_output()
        assert dropped == 3
        assert buf.queued_output_frames == 0

    def test_clear_output_drops_remainder(self) -> None:
        """The half-frame held in remainder is also dropped."""
        buf = BridgeBuffer()
        # 1.5 frames -> 1 queued + half remainder
        buf.push_output(b"\xaa" * (FRAME_BYTES + FRAME_BYTES // 2))
        assert buf.queued_output_frames == 1
        # Verify remainder has data
        assert len(buf._output_remainder) == FRAME_BYTES // 2
        buf.clear_output()
        assert len(buf._output_remainder) == 0

    def test_clear_output_resets_silence_state(self) -> None:
        """After barge-in clear, next non-silence frame should fade in."""
        buf = BridgeBuffer()
        loud = b"\xff\x7f" * (FRAME_BYTES // 2)
        buf.push_output(loud)
        # Read once to trigger fade & flip last_was_silence to False
        buf.read_frame()
        assert not buf._last_was_silence  # type: ignore[attr-defined]
        buf.clear_output()
        # After clear, next non-silence MUST be treated as post-silence
        assert buf._last_was_silence  # type: ignore[attr-defined]

    def test_clear_output_returns_count(self) -> None:
        buf = BridgeBuffer()
        buf.push_output(b"\xaa" * (FRAME_BYTES * 5))
        assert buf.clear_output() == 5
        # Empty clear → 0
        assert buf.clear_output() == 0

    def test_clear_increments_output_drops_counter(self) -> None:
        buf = BridgeBuffer()
        buf.push_output(b"\xaa" * (FRAME_BYTES * 2))
        before = buf._output_drops
        buf.clear_output()
        assert buf._output_drops == before + 2


# ---------- Audit #42: bridge.stats() surfaces history state --------- #


class TestStatsObservability:
    def test_stats_includes_history_injected(self) -> None:
        backend = MagicMock()
        backend.NAME = "gemini-live"
        backend._history_injected = True
        backend.voice = "Aoede"
        backend.model = "gemini-3.1-flash-live-preview"
        bridge = RealtimeAudioBridge(backend=backend)
        s = bridge.stats()
        assert s["history_injected"] is True
        assert s["realtime_voice"] == "Aoede"
        assert s["realtime_model"] == "gemini-3.1-flash-live-preview"

    def test_stats_history_injected_false_by_default(self) -> None:
        backend = MagicMock(spec=[])  # no _history_injected attr
        backend.NAME = "test"
        bridge = RealtimeAudioBridge(backend=backend)
        s = bridge.stats()
        assert s["history_injected"] is False

    def test_stats_includes_resampler_cache_size(self) -> None:
        backend = MagicMock()
        backend.NAME = "gemini-live"
        bridge = RealtimeAudioBridge(backend=backend)
        s = bridge.stats()
        # soxr available in test env → cache count is non-negative
        assert s["out_resamplers_cached"] >= 0


# ---------- Audit #5: TranscriptMirror modern asyncio API ------------ #


class TestTranscriptMirrorModernAPI:
    def test_no_deprecation_warning_from_get_event_loop(self) -> None:
        """When called from a thread WITHOUT a running loop, the old
        get_event_loop() emits DeprecationWarning in py3.12+. After
        switching to get_running_loop(), no such warning should fire.
        """
        from hermes_s2s.voice.transcript import TranscriptMirror

        adapter = MagicMock()
        adapter._loop = None  # no fallback loop either
        mirror = TranscriptMirror(adapter)

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            # Must not raise DeprecationWarning. May log a warning that
            # no loop was found — that's fine, just not a deprecation.
            mirror.schedule_send(
                channel_id=12345, role="user", text="hi", final=False
            )

    def test_falls_back_gracefully_with_no_loops(self) -> None:
        """No running loop AND no adapter loop → log + drop, don't raise."""
        from hermes_s2s.voice.transcript import TranscriptMirror

        adapter = MagicMock()
        adapter._loop = None
        mirror = TranscriptMirror(adapter)
        # Must not raise
        mirror.schedule_send(
            channel_id=999, role="user", text="hi", final=False
        )


# ---------- Audit #6: _pending_first_msg initialised in __init__ ----- #


class TestGeminiPendingFirstMsgInit:
    def test_attribute_exists_after_construct_no_connect(self) -> None:
        """Pre-fix: only set in connect(). Touching recv_events first
        would AttributeError. After fix: always None on init.
        """
        from hermes_s2s.providers.realtime.gemini_live import GeminiLiveBackend

        backend = GeminiLiveBackend(
            api_key_env="X", url="ws://x", model="m"
        )
        # Must not raise AttributeError
        assert backend._pending_first_msg is None
        assert backend._history_injected is False
