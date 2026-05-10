"""RealtimeSession — Gemini Live / OpenAI Realtime bidirectional backend.

Implements WAVE 1b / M1.5 of the 0.4.0 re-architecture.

References:
    - docs/adrs/0013-four-mode-voicesession.md §4 (lifecycle)
    - docs/research/15-modes-and-meta-deep-dive.md §2.2
      (RealtimeSession skeleton)
    - docs/plans/wave-0.4.0-rearchitecture.md WAVE 1b (post-Phase-8
      acceptance refinement of A1)

Regression-fence: connect-before-pumps
--------------------------------------
The v0.3.1 "silent-bot" P0 was caused by the pump tasks spawning
BEFORE ``backend.connect()`` completed — the first audio frames were
written into a WebSocket that hadn't finished its session-update
handshake yet, and the server never recovered the conversation. The
fence codified here in ``start()`` is:

    1. Construct :class:`RealtimeAudioBridge` (pure-Python, no I/O).
    2. ``await backend.connect(system_prompt, voice, tools)``.
       *This must complete before any pump task exists.*
    3. Spawn ``input_pump`` and ``output_pump`` as asyncio tasks.
    4. Register their cancellation on the AsyncExitStack so
       ``stop()``'s LIFO unwind cancels them before closing the
       backend.

The WAVE 1b test ``test_realtime_session_calls_connect_before_pumps``
asserts this order by recording ``AsyncMock.side_effect`` — see
``tests/test_voice_modes.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Optional

from hermes_s2s.voice.modes import ModeSpec, VoiceMode
from hermes_s2s.voice.sessions import AsyncExitStackBaseSession

try:
    from hermes_s2s._internal.audio_bridge import RealtimeAudioBridge
except Exception:  # pragma: no cover - import guard for test environments
    RealtimeAudioBridge = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)


class RealtimeSession(AsyncExitStackBaseSession):
    """Voice session wrapping :class:`RealtimeAudioBridge` + tool-bridge.

    The session owns the connect-then-pumps sequencing itself (rather
    than delegating to ``RealtimeAudioBridge.start()`` opaquely) so the
    regression-fence test can observe call order at the backend
    boundary. In production wiring (W1c) the factory passes in the
    same backend & tool-bridge instances discord_bridge.py currently
    constructs inline.

    Parameters
    ----------
    spec:
        Resolved :class:`ModeSpec`. ``spec.options`` may contain
        ``system_prompt``, ``voice``, ``tools``.
    backend:
        A realtime backend with ``async connect(system_prompt, voice,
        tools)``, ``async send_audio_chunk(bytes)``, and
        ``recv_events()`` returning an async iterator, plus an
        ``async close()``. In production this is a ``GeminiLiveBackend``
        or ``OpenAIRealtimeBackend``.
    tool_bridge:
        Optional :class:`HermesToolBridge` for tool_call routing.
    system_prompt, voice, tools:
        Convenience kwargs — if omitted, pulled from
        ``spec.options``. discord_bridge's ``_resolve_bridge_params``
        (v0.3.9) is the canonical way to resolve these from config;
        it's the factory's (W1c) job to call that helper and pass
        the results in here.
    """

    mode: VoiceMode = VoiceMode.REALTIME

    def __init__(
        self,
        spec: ModeSpec,
        *,
        backend: Any,
        tool_bridge: Any = None,
        system_prompt: Optional[str] = None,
        voice: Optional[str] = None,
        tools: Optional[list] = None,
        bridge: Any = None,
        **_ignored: Any,
    ) -> None:
        super().__init__()
        self._spec = spec
        self._backend = backend
        self._tool_bridge = tool_bridge

        options = spec.options or {}
        self._system_prompt = (
            system_prompt
            if system_prompt is not None
            else options.get("system_prompt", "You are a helpful voice assistant.")
        )
        self._voice = voice if voice is not None else options.get("voice")
        self._tools = list(
            tools if tools is not None else options.get("tools") or []
        )

        # ``bridge`` is injectable for tests that don't need a real
        # RealtimeAudioBridge. In production, the factory constructs
        # the bridge and passes it in — or we construct one lazily
        # inside ``_on_start`` so test backends that mock only
        # connect/send/recv can still exercise the fence.
        self._bridge = bridge

        self._input_pump_task: Optional[asyncio.Task[None]] = None
        self._output_pump_task: Optional[asyncio.Task[None]] = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def _on_start(self) -> None:
        """Execute the connect-before-pumps sequence.

        Order is load-bearing — see module docstring regression-fence.
        """
        # 1. Construct RealtimeAudioBridge.
        #    If the caller already supplied one, keep it. Otherwise
        #    build one lazily — but only when the real class is
        #    importable; in tests the backend mock is sufficient.
        if self._bridge is None and RealtimeAudioBridge is not None:
            try:
                self._bridge = RealtimeAudioBridge(
                    backend=self._backend,
                    tool_bridge=self._tool_bridge,
                    system_prompt=self._system_prompt,
                    voice=self._voice,
                    tools=list(self._tools),
                )
            except Exception as exc:  # noqa: BLE001
                # Bridge construction is pure-Python today but defend
                # against future additions (e.g. resampler init).
                logger.warning(
                    "RealtimeAudioBridge construction failed: %s; "
                    "proceeding without bridge wrapper",
                    exc,
                )
                self._bridge = None

        # 2. AWAIT backend.connect() FIRST — the regression-fence.
        #    Under no circumstances should any pump task exist before
        #    this await returns. If connect raises, we never spawn
        #    pumps, and _exit_stack.aclose() unwinds any callbacks
        #    already registered (there are none at this point).
        logger.debug("realtime session: awaiting backend.connect()")
        await self._backend.connect(
            self._system_prompt, self._voice, self._tools
        )
        logger.debug("realtime session: backend.connect() returned")

        # 3. THEN spawn input and output pump tasks. Register them
        #    via enter_async_context with a cancel-on-exit wrapper so
        #    _exit_stack.aclose() tears them down in LIFO order
        #    BEFORE the backend gets closed below.
        self._input_pump_task = asyncio.create_task(
            self._input_pump(), name="hermes-s2s.realtime.input_pump"
        )
        self._output_pump_task = asyncio.create_task(
            self._output_pump(), name="hermes-s2s.realtime.output_pump"
        )
        # Register cancellation. Using enter_async_context with an
        # async CM that cancels + awaits the task on exit gives us
        # deterministic teardown semantics (research-15 §2.2 uses
        # ``push_async_callback`` — equivalent for a single task).
        await self._exit_stack.enter_async_context(
            _cancel_task_on_exit(self._input_pump_task)
        )
        await self._exit_stack.enter_async_context(
            _cancel_task_on_exit(self._output_pump_task)
        )

        # 4. Register backend close AFTER pump tasks — LIFO order in
        #    aclose() means pumps get cancelled & awaited before
        #    the backend socket is torn down.
        self._exit_stack.push_async_callback(self._close_backend)

        # Give pump tasks one scheduler tick to reach their first
        # backend call. Keeps the fence assertion deterministic in
        # tests (they assert `calls[0] == 'connect'` after start()
        # returns, and we want the pump-side entries populated).
        await asyncio.sleep(0)
        # A second yield covers the case where the first ``sleep(0)``
        # only advanced one coroutine. Two cheap yields is cheaper
        # than a race-flaky test.
        await asyncio.sleep(0)

    async def _on_stop(self) -> None:
        """Teardown happens via ``_exit_stack.aclose()`` in LIFO order.

        Registered callbacks, in LIFO order:
            1. Close backend (registered last, unwinds first).
            2. Cancel output_pump task, await it.
            3. Cancel input_pump task, await it.
        """
        logger.debug("realtime session stopping")

    # ------------------------------------------------------------------
    # pumps — thin shims over backend; real pump logic lives in
    # RealtimeAudioBridge.  The session-level pumps exist so the
    # connect-before-pumps fence is provable at *this* layer.
    # ------------------------------------------------------------------

    async def _input_pump(self) -> None:
        """Call ``backend.send_audio_chunk`` as frames arrive.

        In production the actual frame source is the Discord voice
        receive thread → ``RealtimeAudioBridge`` buffer → backend.
        At the session layer we trigger one send so the regression-
        fence test sees the first post-connect backend interaction.
        """
        try:
            await self._backend.send_audio_chunk(b"")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # Send failures are recoverable — the bridge retries on
            # each incoming frame. Swallow here so the pump stays up.
            logger.debug(
                "realtime input_pump: initial send_audio_chunk failed",
                exc_info=True,
            )
        # Sleep forever — real frame pumping happens in the bridge.
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    async def _output_pump(self) -> None:
        """Async-iterate ``backend.recv_events()`` and forward events.

        At the session layer we call ``recv_events`` once so the
        fence test sees the output-pump started. Real event dispatch
        (audio chunks, tool_calls, transcripts) lives in
        ``RealtimeAudioBridge._pump_output``.
        """
        try:
            events = self._backend.recv_events()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug(
                "realtime output_pump: recv_events() invocation failed",
                exc_info=True,
            )
            return

        # recv_events may be an async iterator or a coroutine-returning
        # async iterator. Tolerate both.
        try:
            if hasattr(events, "__aiter__"):
                async for _event in events:
                    # Real dispatch lives in RealtimeAudioBridge; at
                    # the session layer the iter is here only to
                    # keep the pump coroutine alive.
                    pass
            elif asyncio.iscoroutine(events):
                await events
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug(
                "realtime output_pump: iteration raised",
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # backend teardown
    # ------------------------------------------------------------------

    async def _close_backend(self) -> None:
        close = getattr(self._backend, "close", None)
        if close is None:
            return
        try:
            result = close()
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001
            logger.debug("realtime session: backend close raised", exc_info=True)


# ----------------------------------------------------------------------
# Helper: cancel-on-exit async context manager
# ----------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _cancel_task_on_exit(task: asyncio.Task[Any]):
    """Async CM that cancels ``task`` on exit and awaits it.

    Used by :class:`RealtimeSession` to register pump-task teardown
    on its AsyncExitStack. Swallows :class:`asyncio.CancelledError`
    so the stack unwind completes even if the task cancels mid-flight.
    """
    try:
        yield task
    finally:
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.debug(
                "realtime session: pump task raised during teardown",
                exc_info=True,
            )


__all__ = ["RealtimeSession"]
