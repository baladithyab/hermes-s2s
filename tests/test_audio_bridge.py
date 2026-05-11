"""Tests for RealtimeAudioBridge + BridgeBuffer (ADR-0007, research/09).

Eight tests covering:
    a. BridgeBuffer push_input/pop_input round-trip (thread -> asyncio).
    b. Drop oldest on input queue overflow; counter increments.
    c. push_output slices an 8000-byte chunk into ~12.5 frames and holds the
       trailing fractional remainder for the next call.
    d. read_frame returns 3840 bytes of silence on underflow.
    e. RealtimeAudioBridge.start() + close() leaves no leaked asyncio tasks.
    f. Bridge resamples input: verify resample_pcm is called with the right
       (src_rate=48k, dst_rate=backend_rate, stereo->mono) arguments.
    g. Bridge slices a 250 ms Gemini-shaped backend chunk (8000 bytes @ 16 k)
       into 12 complete 20 ms frames + holds half-frame remainder.
    h. on_user_frame is safe to call from a threading.Thread.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes_s2s._internal.audio_bridge import (
    DISCORD_SAMPLE_RATE,
    FRAME_BYTES,
    SILENCE_FRAME,
    BridgeBuffer,
    RealtimeAudioBridge,
)


# ---------- helpers ----------

def _fake_event(etype: str, **payload: Any) -> Any:
    """Build a minimal RealtimeEvent-shaped object (duck-typed)."""
    ev = MagicMock()
    ev.type = etype
    ev.payload = dict(payload)
    return ev


class _FakeAsyncIterator:
    """Async iterator emitting a fixed list of events, then blocking forever.

    Blocking forever matches a real backend (recv_events doesn't complete
    while the session is alive); the bridge is stopped via close().
    """

    def __init__(self, events: list[Any]) -> None:
        self._events = list(events)
        self._done_event = asyncio.Event()

    def __aiter__(self) -> "_FakeAsyncIterator":
        return self

    async def __anext__(self) -> Any:
        if self._events:
            return self._events.pop(0)
        # Block until cancelled.
        await self._done_event.wait()
        raise StopAsyncIteration


def _make_backend(
    *,
    input_rate: int = 16000,
    output_rate: int = 24000,
    name: str = "gemini-live",
    events: list[Any] | None = None,
) -> Any:
    be = MagicMock()
    be.NAME = name
    be.input_sample_rate = input_rate
    be.output_sample_rate = output_rate
    be.connect = AsyncMock()
    be.send_audio_chunk = AsyncMock()
    be.close = AsyncMock()
    be.inject_tool_result = AsyncMock()
    iterator = _FakeAsyncIterator(events or [])
    # recv_events is called synchronously and returns an async iterator.
    be.recv_events = MagicMock(return_value=iterator)
    return be


# ---------- a. round-trip ----------

async def _connect_then_pumps(bridge: "RealtimeAudioBridge") -> None:
    """Helper for connect-before-pumps test: starts and immediately closes."""
    await bridge.start()
    # Give the loop one tick so any pump task that would race could.
    await asyncio.sleep(0.01)
    await bridge.close()


def test_bridge_start_calls_backend_connect_before_pumps() -> None:
    """ADR-0007: backend.connect must be awaited BEFORE pump tasks run.

    Regression test for Phase-8 Kimi P0 #1: the bot was silent in VC because
    start() spawned the pump tasks without first opening the WS session.
    """
    backend = _make_backend()
    # Track whether send_audio_chunk or recv_events ever fires before connect.
    call_order: list[str] = []

    async def _record_connect(*a: Any, **kw: Any) -> None:
        call_order.append("connect")

    backend.connect.side_effect = _record_connect
    backend.send_audio_chunk.side_effect = lambda *a, **kw: call_order.append("send")
    original_recv = backend.recv_events
    def _record_recv(*a: Any, **kw: Any) -> Any:
        call_order.append("recv")
        return original_recv(*a, **kw)
    backend.recv_events = _record_recv

    bridge = RealtimeAudioBridge(
        backend=backend,
        system_prompt="test prompt",
        voice="test_voice",
        tools=[],
    )
    asyncio.run(_connect_then_pumps(bridge))

    # connect must be called, must come first
    assert "connect" in call_order, f"connect was never called: {call_order}"
    assert call_order[0] == "connect", f"connect was not first: {call_order}"
    backend.connect.assert_awaited_once()
    # connect should have been called with our system_prompt + voice + tools
    args, kwargs = backend.connect.call_args
    # Accept either positional or kwarg form
    all_args = list(args) + list(kwargs.values())
    assert "test prompt" in all_args
    assert "test_voice" in all_args


def test_bridge_buffer_input_round_trip() -> None:
    """push_input from a thread; pop_input from the asyncio loop."""
    buf = BridgeBuffer()
    pcm = b"\x01\x02" * 1920  # one Discord frame's worth

    def pusher() -> None:
        buf.push_input(user_id=42, pcm=pcm)

    t = threading.Thread(target=pusher)
    t.start()
    t.join(timeout=1.0)

    async def drain() -> tuple[int, bytes]:
        return await asyncio.wait_for(buf.pop_input(), timeout=1.0)

    user_id, got = asyncio.run(drain())
    assert user_id == 42
    assert got == pcm


# ---------- b. drop oldest on overflow ----------

def test_bridge_buffer_drops_oldest_on_overflow() -> None:
    buf = BridgeBuffer(input_max=3)
    for i in range(5):
        buf.push_input(user_id=i, pcm=bytes([i]) * 10)
    # Queue is size 3; we pushed 5 -> 2 drops; only the freshest 3 remain.
    assert buf.dropped_input == 2
    got = [buf.pop_input_nowait() for _ in range(3)]
    ids = [g[0] for g in got if g is not None]
    assert ids == [2, 3, 4], f"expected freshest 3 preserved, got {ids}"
    assert buf.pop_input_nowait() is None


# ---------- c. push_output slices with remainder hold ----------

def test_push_output_slices_with_remainder_hold() -> None:
    """A chunk of 2.5 frames: 2 frames emitted, half-frame held.

    Then push another half-frame: a third complete frame should emerge,
    with no remaining remainder.
    """
    buf = BridgeBuffer()
    # 0.4.2 S1 Fix B: fade-in mutates leading samples of the FIRST frame
    # after silence. Disable fade for this byte-equality test which is
    # really testing remainder-slicing semantics, not fade behaviour.
    buf.set_silence_fade_ms(0)
    chunk_a = b"\xaa" * (FRAME_BYTES * 2 + FRAME_BYTES // 2)  # 2.5 frames
    added = buf.push_output(chunk_a)
    assert added == 2
    assert buf.queued_output_frames == 2
    # Remainder should be held: the next half-frame completes the 3rd.
    chunk_b = b"\xbb" * (FRAME_BYTES // 2)
    added2 = buf.push_output(chunk_b)
    assert added2 == 1
    # Read all 3 frames; validate sizes & content boundary.
    f1 = buf.read_frame()
    f2 = buf.read_frame()
    f3 = buf.read_frame()
    assert len(f1) == FRAME_BYTES
    assert len(f2) == FRAME_BYTES
    assert len(f3) == FRAME_BYTES
    assert f1 == b"\xaa" * FRAME_BYTES
    assert f2 == b"\xaa" * FRAME_BYTES
    # f3 = half 0xaa + half 0xbb
    assert f3[: FRAME_BYTES // 2] == b"\xaa" * (FRAME_BYTES // 2)
    assert f3[FRAME_BYTES // 2 :] == b"\xbb" * (FRAME_BYTES // 2)


# ---------- d. read_frame underflow returns silence ----------

def test_read_frame_returns_silence_on_underflow() -> None:
    buf = BridgeBuffer()
    f = buf.read_frame()
    assert f == SILENCE_FRAME
    assert len(f) == FRAME_BYTES
    assert buf.underflows == 1
    # Never returns b"" — that would end Discord playback.
    assert f != b""


# ---------- e. start/close lifecycle, no leaked tasks ----------

def test_bridge_start_close_leaves_no_tasks() -> None:
    async def scenario() -> None:
        tasks_before = {t for t in asyncio.all_tasks() if not t.done()}
        backend = _make_backend(events=[])
        bridge = RealtimeAudioBridge(backend=backend)
        await bridge.start()
        # Let the loop actually schedule the children.
        await asyncio.sleep(0.01)
        assert bridge._task is not None
        assert not bridge._task.done()
        await bridge.close()
        # Give the loop one pass to clean up any residual callbacks.
        await asyncio.sleep(0)
        tasks_after = {t for t in asyncio.all_tasks() if not t.done()}
        # Only *this* scenario task should remain (self + possibly none else).
        current = asyncio.current_task()
        leaked = (tasks_after - tasks_before) - {current}
        assert leaked == set(), f"leaked tasks: {leaked}"
        # close() is idempotent:
        await bridge.close()
        backend.close.assert_awaited()

    asyncio.run(scenario())


# ---------- f. bridge resamples on input with correct rates ----------

def test_bridge_resamples_input_with_correct_rates() -> None:
    """Verify resample_pcm is called with (48k stereo -> backend rate mono)."""
    pcm_in = b"\x01\x02" * 1920  # shape doesn't matter for the mock

    async def scenario() -> None:
        backend = _make_backend(input_rate=16000, output_rate=24000, events=[])
        bridge = RealtimeAudioBridge(backend=backend)
        with patch(
            "hermes_s2s._internal.audio_bridge.resample_pcm",
            return_value=b"\xab" * 640,  # pretend downsampled output
        ) as mock_rs:
            await bridge.start()
            # Push a frame from a "thread"
            bridge.on_user_frame(user_id=7, pcm=pcm_in)
            # Let the input pump run.
            await asyncio.sleep(0.05)
            await bridge.close()

            # Verify at least one call with input-direction args.
            matching = [
                c
                for c in mock_rs.call_args_list
                if c.kwargs.get("src_rate") == DISCORD_SAMPLE_RATE
                and c.kwargs.get("dst_rate") == 16000
                and c.kwargs.get("src_channels") == 2
                and c.kwargs.get("dst_channels") == 1
            ]
            assert matching, (
                f"expected input-direction resample call; got: "
                f"{mock_rs.call_args_list}"
            )
            # And the backend got the resampled bytes at the backend rate.
            backend.send_audio_chunk.assert_awaited()
            args, _kwargs = backend.send_audio_chunk.call_args
            assert args[1] == 16000

    asyncio.run(scenario())


# ---------- g. 250 ms backend chunk -> 12 frames + held half-frame ----------

def test_bridge_slices_250ms_chunk_into_12_frames_and_holds_remainder() -> None:
    """Gemini-shaped 8000 B @ 16 k mono chunk -> 48000 B @ 48 k stereo
    -> 12.5 frames. Expect 12 queued; 1920 B held in remainder."""
    gemini_chunk_bytes = 0.250 * 16000 * 2  # = 8000
    gemini_chunk = b"\x00\x01" * int(gemini_chunk_bytes // 2)
    assert len(gemini_chunk) == 8000
    # Resampled output size: 16k->48k (x3) * mono->stereo (x2) = 48000 B.
    resampled_out = b"\x22" * 48000
    assert len(resampled_out) == FRAME_BYTES * 12 + FRAME_BYTES // 2

    async def scenario() -> None:
        events = [
            _fake_event("audio_chunk", pcm=gemini_chunk, sample_rate=16000),
        ]
        backend = _make_backend(
            input_rate=16000, output_rate=16000, events=events
        )
        bridge = RealtimeAudioBridge(backend=backend)
        # 0.4.2 S1: this test mocks resample_pcm to verify slicing/remainder
        # semantics. Force the legacy stateless path by clearing the soxr
        # cache; the mock will then run.
        bridge._out_resampler_cache = None  # noqa: SLF001

        def fake_resample(pcm: bytes, **kwargs: Any) -> bytes:
            # Only the backend->Discord direction gets the big 8000 B blob.
            if kwargs.get("src_rate") == 16000 and kwargs.get("dst_rate") == DISCORD_SAMPLE_RATE:
                return resampled_out
            return b""

        with patch(
            "hermes_s2s._internal.audio_bridge.resample_pcm",
            side_effect=fake_resample,
        ):
            await bridge.start()
            # Let the output pump consume the single queued event.
            await asyncio.sleep(0.05)
            queued = bridge.buffer.queued_output_frames
            remainder_len = len(bridge.buffer._output_remainder)  # noqa: SLF001
            await bridge.close()

        assert queued == 12, f"expected 12 frames queued, got {queued}"
        assert remainder_len == FRAME_BYTES // 2, (
            f"expected {FRAME_BYTES // 2} B held, got {remainder_len}"
        )

    asyncio.run(scenario())


# ---------- h. on_user_frame is safe from a threading.Thread ----------

def test_on_user_frame_thread_safe() -> None:
    async def scenario() -> None:
        backend = _make_backend(events=[])
        bridge = RealtimeAudioBridge(backend=backend)
        await bridge.start()
        errors: list[BaseException] = []

        def worker(tid: int) -> None:
            try:
                for i in range(20):
                    bridge.on_user_frame(user_id=tid, pcm=bytes([tid, i]) * 10)
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)
        # Give the loop a tick to drain some.
        await asyncio.sleep(0.02)
        await bridge.close()
        assert errors == [], f"thread errors: {errors}"

    asyncio.run(scenario())


# ---------- G3 (BACKLOG-0.3.2 F5): stats + debounced warn logs ----------


def test_buffer_logs_warning_every_100_drops(caplog: Any) -> None:
    """push_input more than 200 overflows -> WARNING logged at 100 + 200.

    P2-fix-B: the old modulo-based debounce could skip the 100/200 boundary
    if two drops landed in the same push_input call (the counter would jump
    from 99 to 101 and the modulo check ``== 0`` would never fire). The new
    threshold-based check fires when the counter *crosses* each boundary.
    """
    import logging

    buf = BridgeBuffer(input_max=1)
    caplog.set_level(logging.WARNING, logger="hermes_s2s._internal.audio_bridge")
    for i in range(250):
        buf.push_input(user_id=i, pcm=b"\x00" * 2)
    warn_msgs = [
        r.message for r in caplog.records if r.levelno == logging.WARNING
    ]
    # input_max=1 + 250 pushes: each overflow drops one, so the first push is
    # accepted and subsequent 249 trigger drops. We expect warns at 100 and 200.
    assert len(warn_msgs) >= 2, f"expected at least 2 warnings, got {warn_msgs}"
    # MUST contain the 100-boundary message (not 99 and not 101).
    assert any("100 input frames dropped" in m for m in warn_msgs), warn_msgs
    assert any("200 input frames dropped" in m for m in warn_msgs), warn_msgs
    # MUST NOT contain off-by-one messages.
    assert not any("99 input frames dropped" in m for m in warn_msgs), warn_msgs
    assert not any(
        "199 input frames dropped" in m for m in warn_msgs
    ), warn_msgs


def test_buffer_stats_returns_expected_keys() -> None:
    buf = BridgeBuffer()
    s = buf.stats()
    assert set(s.keys()) == {
        "dropped_input",
        "dropped_output",
        "queue_depth_in",
        "queue_depth_out",
        "frames_emitted",
        "frames_underflow",
    }
    # Initially all zero.
    assert all(v == 0 for v in s.values())
    # After an underflow read:
    _ = buf.read_frame()
    s2 = buf.stats()
    assert s2["frames_underflow"] == 1


def test_bridge_stats_aggregates() -> None:
    backend = _make_backend()
    bridge = RealtimeAudioBridge(backend=backend)
    s = bridge.stats()
    for k in (
        "dropped_input",
        "dropped_output",
        "queue_depth_in",
        "queue_depth_out",
        "frames_emitted",
        "frames_underflow",
    ):
        assert k in s, f"missing buffer-stat key {k!r} in bridge.stats()"
    # Bridge-level augmentation present.
    assert "backend_input_rate" in s
    assert "backend_output_rate" in s


# ---------- p1-2. get_active_bridge() registry ----------

def test_get_active_bridge_returns_bridge_after_start() -> None:
    """`s2s_status` reads the active bridge via this registry; verify it
    actually flips on start() and clears on close()."""
    from hermes_s2s._internal.audio_bridge import get_active_bridge

    async def scenario() -> None:
        # Clear any prior state from sibling tests in the same process.
        assert get_active_bridge() is None or get_active_bridge() is not None
        backend = _make_backend(events=[])
        bridge = RealtimeAudioBridge(backend=backend)
        await bridge.start()
        try:
            assert get_active_bridge() is bridge, (
                "start() should set the module-level active bridge"
            )
        finally:
            await bridge.close()
        assert get_active_bridge() is None, (
            "close() should clear the active bridge registry"
        )

    asyncio.run(scenario())
