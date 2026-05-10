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


# ---------------------------------------------------------------------------
# 3-bucket tool-export policy (WAVE 4b M4.4; ADR-0014 §3).
#
# Per Phase-8 security review P0-F1: collapsing to 2 buckets silently promotes
# ``ask``-bucket tools (file reads, session_search, memory, browser_navigate,
# HA reads) into ``default_exposed``, creating a voice → filesystem-read path.
# Fail-closed: unknown tools are hard-dropped from the manifest. The CI fence
# ``tests/test_tool_export.py::test_every_core_tool_classified`` asserts that
# every known core tool appears in exactly one of the three buckets.
# ---------------------------------------------------------------------------

#: Auto-allow, no prompt. Tools with no user-data, no system mutation,
#: no cost amplification.
DEFAULT_EXPOSED: set[str] = {
    "web_search",
    "vision_analyze",
    "text_to_speech",
    "clarify",
}

#: Synchronous voice yes/no prompt; 5s window; default-deny on timeout.
#: User-data reads or external-call cost. The meta_dispatcher (W4a) owns the
#: pending-confirm flow.
ASK: set[str] = {
    "read_file",
    "search_files",
    "session_search",
    "memory",
    "browser_navigate",
    "ha_state_read",
    "kanban_show",
    "kanban_list",
    "spotify_search",
    "feishu_doc_read",
    "xurl_read",
    "obsidian_read",
    "youtube_transcript",
    "arxiv_search",
    "gif_search",
}

#: Hard-blocked; tool is removed from the manifest entirely. The LLM cannot
#: invoke these via voice, and the user cannot override per the persona
#: overlay (section 4).
DENY: set[str] = {
    "terminal",
    "process",
    "execute_code",
    "patch",
    "write_file",
    "computer_use",
    "delegate_task",
    "cronjob",
    "ha_call_service",
    "kanban_create",
    "kanban_complete",
    "kanban_block",
    "kanban_link",
    "kanban_comment",
    "kanban_heartbeat",
    "browser_click",
    "browser_type",
    "browser_press",
    "browser_scroll",
    "browser_console",
    "browser_get_images",
    "send_message",
    "skill_manage",
    "image_generate",
    "comfyui",
    "spotify_play",
    "spotify_pause",
    "xurl_post",
    "xurl_dm",
}


def build_tool_manifest(
    enabled_tools: list[dict], mode: str = "realtime"
) -> list[dict]:
    """Filter a Hermes tool list by the 3-bucket voice-export policy.

    Returns a new list containing only tools in ``DEFAULT_EXPOSED`` or
    ``ASK``, plus the always-exposed ``hermes_meta_*`` tools appended at
    the end. Tools in ``DENY`` are hard-removed. Unknown/unclassified
    tools are conservatively dropped (fail-closed).

    Args:
        enabled_tools: The Hermes-side list of tool schemas. Each entry
            must have a ``name`` key.
        mode: Reserved for future per-mode policy variants
            (``realtime``/``cascaded``). Currently unused; the same
            3-bucket policy applies to all voice modes.

    Returns:
        A new list of tool schemas, safe to serialize into a realtime
        backend's tool manifest.
    """
    # Local import to avoid a circular dependency: voice.meta_tools is a
    # leaf module but lives in a sibling package; importing it eagerly at
    # module load would tighten the import graph unnecessarily.
    from hermes_s2s.voice.meta_tools import get_meta_tools

    out: list[dict] = []
    for tool in enabled_tools:
        name = tool.get("name", "")
        if not name:
            continue
        if name in DENY:
            # Hard-drop. The LLM should not even see the tool exists.
            continue
        if name in DEFAULT_EXPOSED or name in ASK:
            out.append(tool)
            continue
        # Unknown tool → conservative deny (fail-closed). The CI fence
        # in tests/test_tool_export.py ensures every known core tool
        # is classified so this path only fires for genuinely new tools
        # that Hermes core added without updating the bucket tables.
        logger.warning(
            "build_tool_manifest: dropping unclassified tool %r "
            "(add to DEFAULT_EXPOSED, ASK, or DENY in tool_bridge.py)",
            name,
        )

    # Meta tools are always exposed, regardless of the enabled_tools input.
    out.extend(get_meta_tools())
    return out


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
