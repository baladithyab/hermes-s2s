"""In-process end-to-end integration test for the realtime audio bridge.

Unlike ``test_audio_bridge.py`` (which unit-tests ``BridgeBuffer`` and mocks
``resample_pcm`` + the backend with tiny call-arg assertions), this file wires
the REAL components together: a real ``RealtimeAudioBridge``, a real
``BridgeBuffer``, the real ``resample_pcm`` path, and a ``FakeBackend`` that
implements the ``RealtimeBackend`` Protocol and yields scripted audio_chunk
events. The goal is to catch integration regressions that unit tests miss.

Skips cleanly if scipy is not installed (resample_poly is required for
cross-rate integration).
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, AsyncIterator, List
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("scipy")  # real-path integration requires resample_poly

from hermes_s2s._internal.audio_bridge import (  # noqa: E402
    FRAME_BYTES,
    BridgeBuffer,
    RealtimeAudioBridge,
)
from hermes_s2s.providers.realtime import RealtimeEvent  # noqa: E402


# ---------- FakeBackend: a real RealtimeBackend Protocol implementation ----------


class FakeBackend:
    """Scriptable in-process backend. Implements the ``RealtimeBackend`` Protocol.

    - ``connect`` / ``close`` / ``inject_tool_result`` / ``send_filler_audio``
      / ``interrupt`` are tracked via ``AsyncMock`` so tests can assert on them.
    - ``send_audio_chunk`` records every call in ``sent_chunks`` so a test can
      verify the bridge resampled correctly before handing bytes off.
    - ``recv_events`` is an async generator that yields the list of scripted
      events then blocks (matches real-backend semantics: the iterator does not
      complete while the session is alive; shutdown happens via ``close()``).
    """

    NAME = "gemini-live"  # pick up the default 16k-in / 24k-out rate mapping

    def __init__(self, scripted_events: List[RealtimeEvent] | None = None) -> None:
        self.scripted_events = list(scripted_events or [])
        self.sent_chunks: list[tuple[bytes, int]] = []
        self.connect = AsyncMock()
        self.inject_tool_result = AsyncMock()
        self.send_filler_audio = AsyncMock()
        self.interrupt = AsyncMock()
        self.close = AsyncMock()
        self._shutdown = asyncio.Event()

    async def send_audio_chunk(self, pcm_chunk: bytes, sample_rate: int) -> None:
        self.sent_chunks.append((pcm_chunk, sample_rate))

    async def recv_events(self) -> AsyncIterator[RealtimeEvent]:
        for ev in self.scripted_events:
            yield ev
        # Block until the bridge cancels us — mirrors real WS iterator behavior.
        await self._shutdown.wait()


# ---------- helpers ----------


def _silence_48k_stereo_s16le(ms: int = 20) -> bytes:
    """20 ms of 48 kHz stereo s16le silence = 3840 bytes. Content: alternating
    small non-zero sample values so the resampler has real signal to chew on."""
    n_samples = int(48000 * ms / 1000) * 2  # stereo
    return (b"\x10\x00\xf0\xff") * (n_samples // 2)


# ---------- a. full-flow input + output round trip ----------


def test_full_flow_input_output() -> None:
    """Feed 5 frames @ 48k-stereo-s16le -> assert backend got 16k-mono chunks,
    then script 1 audio_chunk back and assert read_frame returns real PCM."""

    async def scenario() -> None:
        # 1 frame @ 16k mono s16le = 320 samples * 2 B = 640 B for 20 ms; script
        # 50 ms of non-silent audio (1600 B) so it slices into >2 output frames.
        backend_chunk = (b"\x22\x11") * 800  # 1600 B, 50 ms @ 16 k mono s16le
        events = [
            RealtimeEvent(
                type="audio_chunk",
                payload={"pcm": backend_chunk, "sample_rate": 16000},
            )
        ]
        backend = FakeBackend(scripted_events=events)
        bridge = RealtimeAudioBridge(backend=backend)
        await bridge.start()
        try:
            frame_in = _silence_48k_stereo_s16le(ms=20)
            for _ in range(5):
                bridge.on_user_frame(user_id=1, pcm=frame_in)
            # Give the pumps time to drain input and deliver the scripted event.
            for _ in range(60):
                await asyncio.sleep(0.01)
                if backend.sent_chunks and bridge.buffer.queued_output_frames:
                    break
        finally:
            backend._shutdown.set()
            await bridge.close()

        # Input direction: send_audio_chunk called with resampled mono 16 k PCM.
        assert backend.sent_chunks, "backend.send_audio_chunk was never called"
        for chunk, rate in backend.sent_chunks:
            assert rate == 16000, f"expected 16k mono, got {rate}"
            # 20 ms @ 16 k mono s16le = 640 B. Allow small resampler edge-variance.
            assert 600 <= len(chunk) <= 680, f"unexpected chunk size {len(chunk)}"

        # Output direction: read_frame returned a 3840 B frame that is NOT silence.
        frame_out = bridge.buffer.read_frame()
        assert len(frame_out) == FRAME_BYTES
        assert frame_out != b"\x00" * FRAME_BYTES, "read_frame returned silence"

    asyncio.run(scenario())


# ---------- b. close leaves no leaked asyncio tasks ----------


def test_close_leaves_no_leaked_tasks() -> None:
    async def scenario() -> None:
        tasks_before = {t for t in asyncio.all_tasks() if not t.done()}
        backend = FakeBackend(scripted_events=[])
        bridge = RealtimeAudioBridge(backend=backend)
        await bridge.start()
        await asyncio.sleep(0.1)  # run for 100 ms
        backend._shutdown.set()
        await bridge.close()
        await asyncio.sleep(0)  # let loop settle
        tasks_after = {t for t in asyncio.all_tasks() if not t.done()}
        current = asyncio.current_task()
        leaked = (tasks_after - tasks_before) - {current}
        assert leaked == set(), f"leaked tasks: {leaked}"
        backend.close.assert_awaited()

    asyncio.run(scenario())


# ---------- c. backpressure under heavy load ----------


def test_backpressure_under_load() -> None:
    """Push 200 frames into a bridge with input_max=10 -> expect ~190 drops and
    no exceptions; bridge must survive and still respond to close()."""

    async def scenario() -> None:
        backend = FakeBackend(scripted_events=[])
        bridge = RealtimeAudioBridge(backend=backend, input_queue_max=10)
        await bridge.start()
        errors: list[BaseException] = []

        def burst() -> None:
            # One Discord-sized frame per push; 200 × 3840 B.
            frame = _silence_48k_stereo_s16le(ms=20)
            try:
                for i in range(200):
                    bridge.on_user_frame(user_id=1, pcm=frame)
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        t = threading.Thread(target=burst)
        t.start()
        t.join(timeout=2.0)
        # Let the pump chew on whatever survived the drop-oldest pressure.
        await asyncio.sleep(0.05)
        dropped = bridge.buffer.dropped_input
        backend._shutdown.set()
        await bridge.close()

        assert errors == [], f"exceptions raised during burst: {errors}"
        assert (
            dropped >= 190
        ), f"expected >= 190 dropped frames on input_max=10; got {dropped}"
        # Bridge still callable after load.
        assert bridge._closed is True

    asyncio.run(scenario())


# ---------- d. backend chunks get resampled up to 48 k stereo s16le ----------


def test_backend_audio_chunks_get_resampled_to_48k_stereo() -> None:
    """Script TWO 24 k mono PCM chunks; assert read_frame yields 48 k stereo s16le
    frames of exactly 3840 B (= 20 ms). Plan wording says ``float32``, but the
    bridge dispatches PCM as s16le bytes per the protocol; we test the
    rate+channel-upconversion which is the substantive integration concern.

    0.4.2 S1: switched from stateless resample_pcm to streaming
    soxr.ResampleStream. The streaming resampler buffers ~1480 output
    samples of filter delay on each stream, so the FIRST chunk emits
    fewer frames than the stateless path did. Subsequent chunks emit
    those buffered samples + new content. Test with two chunks to
    exercise both transient and steady-state behaviour.
    """

    async def scenario() -> None:
        # Two 100 ms @ 24 k mono s16le chunks = 4800 B each.
        # Theoretical output per chunk: 24k->48k (x2) + mono->stereo (x2)
        # = 19_200 B = 5 frames stateless. Streaming buffers ~6 frames
        # of delay on first chunk, so ≥7 total across both chunks is
        # the steady-state lower bound.
        backend_chunk = (b"\x33\x44") * 2400  # 4800 B
        events = [
            RealtimeEvent(
                type="audio_chunk",
                payload={"pcm": backend_chunk, "sample_rate": 24000},
            ),
            RealtimeEvent(
                type="audio_chunk",
                payload={"pcm": backend_chunk, "sample_rate": 24000},
            ),
        ]
        backend = FakeBackend(scripted_events=events)
        bridge = RealtimeAudioBridge(backend=backend)
        await bridge.start()
        try:
            for _ in range(60):
                await asyncio.sleep(0.01)
                if bridge.buffer.queued_output_frames >= 7:
                    break
            queued = bridge.buffer.queued_output_frames
        finally:
            backend._shutdown.set()
            await bridge.close()

        # Two 100ms chunks => ~10 frames total stateless; ~7-9 streaming
        # depending on filter delay. Lower bound 7 catches regressions
        # without flapping on soxr quality-mode tweaks.
        assert queued >= 7, (
            f"expected ≥7 frames from two 100 ms 24 k chunks resampled to 48 k "
            f"stereo; got {queued}"
        )
        # Every frame returned from read_frame must be exactly 3840 B.
        buf: BridgeBuffer = bridge.buffer
        for _ in range(queued):
            f = buf.read_frame()
            assert len(f) == FRAME_BYTES, (
                f"expected 3840 B (20 ms @ 48k stereo s16le), got {len(f)}"
            )

    asyncio.run(scenario())
