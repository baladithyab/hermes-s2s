"""Tests for 0.4.2 manual-VAD integration in RealtimeAudioBridge.

Covers the silence-watchdog pattern that drives backend.send_activity_start /
send_activity_end based on input-frame timing. See
docs/plans/wave-0.4.2-manual-vad.md.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from hermes_s2s._internal.audio_bridge import RealtimeAudioBridge


# ---------------------------------------------------------------- helpers


def _make_backend() -> Any:
    """A fake backend exposing the send_audio_chunk / send_activity_* contract."""
    b = MagicMock()
    b.send_audio_chunk = AsyncMock(return_value=None)
    b.send_activity_start = AsyncMock(return_value=None)
    b.send_activity_end = AsyncMock(return_value=None)
    b.connect = AsyncMock(return_value=None)
    b.close = AsyncMock(return_value=None)
    # recv_events must return an async iterator that just hangs forever.
    async def _recv() -> Any:
        # awaitable that never completes; bridge's _pump_output will await it
        await asyncio.Event().wait()
        if False:
            yield None
    b.recv_events = _recv
    # rates expected by _resolve_backend_rates fallback path
    b.input_sample_rate = 16000
    b.output_sample_rate = 24000
    return b


def _bridge(backend: Any, silence_gap_s: float = 0.05) -> RealtimeAudioBridge:
    """Build a bridge with a short silence_gap so tests don't sleep 800ms.

    50ms is well above the 100ms watchdog poll? No — 100ms poll won't fire in
    50ms. We use 50ms gap + 150ms wait below; the watchdog polls every 100ms
    so by 150ms it has run at least once with a >= 50ms gap.
    """
    br = RealtimeAudioBridge(backend=backend)
    br._silence_gap_s = silence_gap_s
    return br


# ---------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_first_frame_emits_activity_start_then_audio() -> None:
    """First pop_input must emit send_activity_start before send_audio_chunk."""
    b = _make_backend()
    br = _bridge(b, silence_gap_s=10.0)  # never fires end during this test
    pump = asyncio.create_task(br._pump_input())
    try:
        br.buffer.push_input(123, b"\x00" * 3840)
        # Give pump_input a tick to run.
        await asyncio.sleep(0.05)
        assert b.send_activity_start.await_count == 1
        assert b.send_audio_chunk.await_count >= 1
        # send_activity_end must NOT have been called yet.
        assert b.send_activity_end.await_count == 0
        assert br._activity_open is True
        assert br._activity_starts_sent == 1
    finally:
        pump.cancel()
        try:
            await pump
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_consecutive_frames_emit_only_one_activity_start() -> None:
    """While activity is open, subsequent frames must not re-emit activity_start."""
    b = _make_backend()
    br = _bridge(b, silence_gap_s=10.0)
    pump = asyncio.create_task(br._pump_input())
    try:
        for _ in range(10):
            br.buffer.push_input(123, b"\x00" * 3840)
        await asyncio.sleep(0.1)
        assert b.send_activity_start.await_count == 1
        assert b.send_audio_chunk.await_count >= 10
        assert br._activity_starts_sent == 1
    finally:
        pump.cancel()
        try:
            await pump
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_silence_watchdog_emits_activity_end_after_gap() -> None:
    """After silence_gap_s of no frames, the watchdog must close the activity."""
    b = _make_backend()
    br = _bridge(b, silence_gap_s=0.05)
    pump = asyncio.create_task(br._pump_input())
    watchdog = asyncio.create_task(br._silence_watchdog())
    try:
        br.buffer.push_input(123, b"\x00" * 3840)
        await asyncio.sleep(0.03)  # let pump consume
        assert br._activity_open is True
        # Now wait past silence_gap + at least one watchdog poll (100ms).
        await asyncio.sleep(0.20)
        assert b.send_activity_end.await_count == 1
        assert br._activity_open is False
        assert br._activity_ends_sent == 1
    finally:
        for t in (pump, watchdog):
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_resume_after_silence_emits_new_start() -> None:
    """Frame -> gap -> frame should produce start/end/start sequence."""
    b = _make_backend()
    br = _bridge(b, silence_gap_s=0.05)
    pump = asyncio.create_task(br._pump_input())
    watchdog = asyncio.create_task(br._silence_watchdog())
    try:
        # First utterance.
        br.buffer.push_input(123, b"\x00" * 3840)
        await asyncio.sleep(0.20)  # past gap; end fires
        assert br._activity_open is False
        assert b.send_activity_start.await_count == 1
        assert b.send_activity_end.await_count == 1
        # Second utterance.
        br.buffer.push_input(123, b"\x00" * 3840)
        await asyncio.sleep(0.05)
        assert br._activity_open is True
        assert b.send_activity_start.await_count == 2
        # End not yet for second.
        assert b.send_activity_end.await_count == 1
    finally:
        for t in (pump, watchdog):
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_close_emits_final_activity_end_when_open() -> None:
    """close() with an open activity must flush activity_end before tearing down."""
    b = _make_backend()
    br = _bridge(b, silence_gap_s=10.0)
    # Manually set state — simulate that pump opened activity.
    br._activity_open = True
    br._activity_starts_sent = 1
    br._last_input_frame_monotonic = time.monotonic()
    # No supervisor task — close() handles None _task gracefully.
    await br.close()
    assert b.send_activity_end.await_count == 1
    assert br._activity_open is False


@pytest.mark.asyncio
async def test_stats_include_activity_diagnostics() -> None:
    """stats() must surface activity_open, starts_sent, ends_sent, time_since_last."""
    b = _make_backend()
    br = _bridge(b, silence_gap_s=10.0)
    # Initial state: nothing has happened.
    s = br.stats()
    assert s["activity_open"] is False
    assert s["activity_starts_sent"] == 0
    assert s["activity_ends_sent"] == 0
    assert s["time_since_last_frame_s"] is None

    # Simulate a frame having been processed.
    br._activity_open = True
    br._activity_starts_sent = 1
    br._last_input_frame_monotonic = time.monotonic() - 0.5
    s = br.stats()
    assert s["activity_open"] is True
    assert s["activity_starts_sent"] == 1
    # Approx 0.5s ago.
    assert s["time_since_last_frame_s"] is not None
    assert 0.4 <= s["time_since_last_frame_s"] <= 1.5


# ---------------------------------------------------------------- backend tests


@pytest.mark.asyncio
async def test_gemini_setup_disables_aad() -> None:
    """0.4.2: Gemini setup config must set automaticActivityDetection.disabled=True."""
    from hermes_s2s.providers.realtime.gemini_live import GeminiLiveBackend

    backend = GeminiLiveBackend(
        api_key_env="UNUSED_FOR_THIS_TEST",
        model="gemini-2.5-flash-native-audio-latest",
        voice="Aoede",
        language_code="en-US",
    )
    setup = backend._build_setup(system_prompt="hi", tools=[])
    aad = setup["realtimeInputConfig"]["automaticActivityDetection"]
    assert aad == {"disabled": True}


@pytest.mark.asyncio
async def test_gemini_send_activity_start_sends_correct_frame() -> None:
    """send_activity_start must emit the exact realtimeInput.activityStart JSON."""
    from hermes_s2s.providers.realtime.gemini_live import GeminiLiveBackend

    backend = GeminiLiveBackend(api_key_env="UNUSED")
    sent: List[str] = []

    class FakeWS:
        async def send(self, data: str) -> None:
            sent.append(data)

    backend._ws = FakeWS()
    await backend.send_activity_start()
    assert sent == ['{"realtimeInput": {"activityStart": {}}}']


@pytest.mark.asyncio
async def test_gemini_send_activity_end_sends_correct_frame() -> None:
    """send_activity_end must emit the exact realtimeInput.activityEnd JSON."""
    from hermes_s2s.providers.realtime.gemini_live import GeminiLiveBackend

    backend = GeminiLiveBackend(api_key_env="UNUSED")
    sent: List[str] = []

    class FakeWS:
        async def send(self, data: str) -> None:
            sent.append(data)

    backend._ws = FakeWS()
    await backend.send_activity_end()
    assert sent == ['{"realtimeInput": {"activityEnd": {}}}']


@pytest.mark.asyncio
async def test_openai_realtime_activity_methods_are_noops() -> None:
    """OpenAI Realtime backend's activity methods must be awaitable no-ops."""
    from hermes_s2s.providers.realtime.openai_realtime import OpenAIRealtimeBackend

    backend = OpenAIRealtimeBackend(api_key_env="UNUSED")
    # Both must complete without raising and without touching _ws.
    backend._ws = None
    assert await backend.send_activity_start() is None
    assert await backend.send_activity_end() is None
