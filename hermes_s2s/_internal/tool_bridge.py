"""Tool-call bridge with soft/hard timeouts and filler audio.

See ADR-0008 (docs/adrs/0008-tool-call-bridging.md) for design rationale.

The bridge races a tool invocation against two deadlines:

* soft_timeout (default 5s): fire filler audio ("let me check on that") so the
  user isn't staring at silence. The underlying tool keeps running.
* hard_timeout (default 30s): cancel the tool and return a JSON error envelope
  so the model can recover gracefully.

`asyncio.shield` is used so that the soft-timeout wait can be cancelled
without cancelling the underlying tool task. The hard deadline is enforced
by a second `wait_for` that waits the remainder (hard - soft) before
cancelling for real.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


DispatchFn = Callable[[str, dict], Any]  # may be sync or async


class HermesToolBridge:
    """Bridges realtime-backend tool_call events to Hermes's dispatcher.

    Owns timeout policy, filler-audio triggering, result serialization/
    truncation, and cancellation. Backends own the actual wire-format work
    (see ``RealtimeBackend.inject_tool_result`` / ``send_filler_audio``).
    """

    def __init__(
        self,
        dispatch_tool: DispatchFn,
        soft_timeout: float = 5.0,
        hard_timeout: float = 30.0,
        result_max_bytes: int = 4096,
    ) -> None:
        self._dispatch = dispatch_tool
        self._soft = soft_timeout
        self._hard = hard_timeout
        self._max = result_max_bytes
        self._inflight: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------ public

    async def handle_tool_call(
        self, backend: Any, call_id: str, name: str, args: dict
    ) -> str:
        """Dispatch ``name(args)`` and return a result string for injection.

        On soft timeout: fires ``backend.send_filler_audio('let me check on that')``
        (swallowing ``NotImplementedError`` for stub backends) then keeps
        waiting. On hard timeout: cancels the tool and returns a JSON error
        envelope with ``retryable=true``. On tool exception: returns a JSON
        error envelope with ``retryable=false``.
        """
        coro = self._run_with_timeouts(backend, call_id, name, args)
        task = asyncio.create_task(coro)
        self._inflight[call_id] = task
        try:
            return await task
        except asyncio.CancelledError:
            # cancel_all() or external cancellation — propagate.
            raise
        finally:
            self._inflight.pop(call_id, None)

    async def cancel_all(self) -> None:
        """Cancel all in-flight tool tasks. Idempotent."""
        for task in list(self._inflight.values()):
            if not task.done():
                task.cancel()
        self._inflight.clear()

    # --------------------------------------------------------------- internals

    async def _invoke_tool(self, name: str, args: dict) -> Any:
        """Call the configured dispatcher. Supports sync and async dispatchers."""
        result = self._dispatch(name, args)
        if inspect.isawaitable(result):
            result = await result  # type: ignore[assignment]
        return result

    async def _run_with_timeouts(
        self, backend: Any, call_id: str, name: str, args: dict
    ) -> str:
        tool_task: asyncio.Task | None = None
        try:
            tool_task = asyncio.create_task(self._invoke_tool(name, args))
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(tool_task), self._soft
                )
            except asyncio.TimeoutError:
                # Soft timeout — fire filler, keep waiting for the real result.
                # Filler is best-effort: if the backend stub raises
                # NotImplementedError, or if the underlying WS is closed /
                # throws a network error, we log and keep the tool running.
                # Letting any other exception propagate here would orphan
                # the in-flight tool_task (we'd leave the timeouts path
                # without cancelling the task).
                try:
                    await backend.send_filler_audio("let me check on that")
                except Exception as filler_exc:  # noqa: BLE001
                    logger.warning(
                        "send_filler_audio failed: %s", filler_exc
                    )
                remaining = max(self._hard - self._soft, 0.0)
                try:
                    result = await asyncio.wait_for(tool_task, remaining)
                except asyncio.TimeoutError:
                    tool_task.cancel()
                    # Let cancellation settle without raising back at us.
                    try:
                        await tool_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    return json.dumps(
                        {
                            "error": f"tool timed out after {self._hard}s",
                            "retryable": True,
                        }
                    )
            return self._truncate(self._serialize(result))
        except asyncio.CancelledError:
            if tool_task is not None and not tool_task.done():
                tool_task.cancel()
            raise
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all
            return json.dumps({"error": str(exc), "retryable": False})

    # ------------------------------------------------------------------ format

    def _serialize(self, result: Any) -> str:
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            return json.dumps(result)
        return str(result)

    def _truncate(self, s: str) -> str:
        encoded = s.encode("utf-8")
        if len(encoded) <= self._max:
            return s
        truncated = encoded[: self._max].decode("utf-8", errors="ignore")
        return (
            truncated
            + f"\n\n[result truncated; original length: {len(encoded)} bytes]"
        )
