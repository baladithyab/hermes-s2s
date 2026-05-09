"""Tests for HermesToolBridge (ADR-0008).

Covers:
    a. Happy path — dispatch returns fast, no filler audio fired.
    b. Soft timeout — filler audio fired exactly once, tool result still
       returned after the underlying task finishes.
    c. Hard timeout — tool task cancelled, error envelope with retryable=true.
    d. Tool raises — error envelope with retryable=false.
    e. Truncation — results larger than ``result_max_bytes`` get sliced + a
       suffix appended.
    f. cancel_all — outstanding in-flight tasks are cancelled and ``_inflight``
       is cleared.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from unittest.mock import AsyncMock

from hermes_s2s._internal.tool_bridge import HermesToolBridge


# ------------------------------------------------------------------ helpers

def _make_backend() -> AsyncMock:
    """A backend stub whose ``send_filler_audio`` is an AsyncMock."""
    backend = AsyncMock()
    backend.send_filler_audio = AsyncMock(return_value=None)
    return backend


def _dispatcher_for(coro_factory):
    """Wrap an async-callable so it plugs into ``HermesToolBridge(dispatch_tool=...)``.

    The bridge calls ``dispatch_tool(name, args)`` and awaits the result if it's
    awaitable, so we just return the coroutine from the factory.
    """

    def _dispatch(name, args):
        return coro_factory(name, args)

    return _dispatch


# ------------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_happy_path_no_filler():
    """Dispatch finishes in 0.01s: filler must NOT be called, result returned."""

    async def fast(name, args):
        await asyncio.sleep(0.01)
        return "quick-answer"

    backend = _make_backend()
    bridge = HermesToolBridge(
        _dispatcher_for(fast), soft_timeout=0.5, hard_timeout=1.0
    )

    result = await bridge.handle_tool_call(backend, "c1", "t", {})

    assert result == "quick-answer"
    backend.send_filler_audio.assert_not_awaited()
    assert bridge._inflight == {}


@pytest.mark.asyncio
async def test_soft_timeout_fires_filler_then_returns_result():
    """Dispatch 0.1s, soft=0.05, hard=0.5 → filler fires once, result returned."""

    async def slow(name, args):
        await asyncio.sleep(0.1)
        return {"ok": True}

    backend = _make_backend()
    bridge = HermesToolBridge(
        _dispatcher_for(slow), soft_timeout=0.05, hard_timeout=0.5
    )

    result = await bridge.handle_tool_call(backend, "c2", "t", {})

    backend.send_filler_audio.assert_awaited_once_with("let me check on that")
    assert result == json.dumps({"ok": True})
    assert bridge._inflight == {}


@pytest.mark.asyncio
async def test_hard_timeout_cancels_and_returns_error_envelope():
    """Dispatch 1s, soft=0.05, hard=0.1 → task cancelled, retryable error JSON."""

    cancelled = asyncio.Event()

    async def very_slow(name, args):
        try:
            await asyncio.sleep(1.0)
            return "never"
        except asyncio.CancelledError:
            cancelled.set()
            raise

    backend = _make_backend()
    bridge = HermesToolBridge(
        _dispatcher_for(very_slow), soft_timeout=0.05, hard_timeout=0.1
    )

    result = await bridge.handle_tool_call(backend, "c3", "t", {})

    payload = json.loads(result)
    assert payload["retryable"] is True
    assert "timed out" in payload["error"]
    # Give the cancellation a moment to propagate into the inner task.
    await asyncio.sleep(0)
    assert cancelled.is_set()
    assert bridge._inflight == {}


@pytest.mark.asyncio
async def test_tool_exception_non_retryable_envelope():
    """Tool raises ValueError → error envelope with retryable=false."""

    async def boom(name, args):
        raise ValueError("boom")

    backend = _make_backend()
    bridge = HermesToolBridge(
        _dispatcher_for(boom), soft_timeout=0.5, hard_timeout=1.0
    )

    result = await bridge.handle_tool_call(backend, "c4", "t", {})

    payload = json.loads(result)
    assert payload == {"error": "boom", "retryable": False}
    backend.send_filler_audio.assert_not_awaited()
    assert bridge._inflight == {}


@pytest.mark.asyncio
async def test_truncation_caps_bytes_and_appends_suffix():
    """5000-char string with max=4096 → truncated prefix + suffix note."""

    big = "a" * 5000

    async def fat(name, args):
        return big

    backend = _make_backend()
    bridge = HermesToolBridge(
        _dispatcher_for(fat),
        soft_timeout=0.5,
        hard_timeout=1.0,
        result_max_bytes=4096,
    )

    result = await bridge.handle_tool_call(backend, "c5", "t", {})

    assert result.startswith("a" * 4096)
    assert "[result truncated; original length: 5000 bytes]" in result
    # Prefix exactly 4096 bytes + suffix text; overall length > 4096.
    assert len(result) > 4096


@pytest.mark.asyncio
async def test_cancel_all_cancels_inflight_tasks():
    """Launch 2 long-running handle_tool_call tasks, then cancel_all clears them."""

    async def forever(name, args):
        await asyncio.sleep(10)
        return "unreachable"

    backend = _make_backend()
    bridge = HermesToolBridge(
        _dispatcher_for(forever), soft_timeout=5.0, hard_timeout=10.0
    )

    t1 = asyncio.create_task(bridge.handle_tool_call(backend, "a", "t", {}))
    t2 = asyncio.create_task(bridge.handle_tool_call(backend, "b", "t", {}))

    # Let both register themselves in _inflight.
    for _ in range(10):
        await asyncio.sleep(0)
        if len(bridge._inflight) == 2:
            break
    assert len(bridge._inflight) == 2

    await bridge.cancel_all()

    # Both outer tasks should observe CancelledError.
    with pytest.raises(asyncio.CancelledError):
        await t1
    with pytest.raises(asyncio.CancelledError):
        await t2

    assert bridge._inflight == {}

    # Idempotent.
    await bridge.cancel_all()
    assert bridge._inflight == {}
