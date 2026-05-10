"""VoiceSession protocol and ``AsyncExitStackBaseSession`` base class.

Implements WAVE 1a / M1.2 of the 0.4.0 re-architecture.

The four concrete session classes (CascadedSession, CustomPipelineSession,
RealtimeSession, S2SServerSession) live in ``sessions_*.py`` modules owned
by WAVE 1b — this file intentionally only provides the shared protocol,
state machine, and AsyncExitStack lifecycle.

References:
- docs/adrs/0013-four-mode-voicesession.md §4
- docs/research/15-modes-and-meta-deep-dive.md §2 (lifecycle)
- docs/research/12-voice-mode-rearchitecture.md §3 (pseudocode)
"""

from __future__ import annotations

import contextlib
from enum import Enum, auto
from typing import Any, Optional, Protocol, runtime_checkable

from .modes import VoiceMode


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class SessionState(Enum):
    """Lifecycle states for a :class:`VoiceSession`.

    Legal transitions::

        CREATED  → STARTING   (start())
        STARTING → RUNNING    (start() success)
        STARTING → STOPPING   (start() failure → cleanup)
        RUNNING  → STOPPING   (stop())
        STOPPING → STOPPED    (aclose complete)

    ``stop()`` is idempotent: calling it on ``CREATED``/``STOPPED``/``STOPPING``
    is a no-op; state always ends at ``STOPPED``.
    """

    CREATED = auto()
    STARTING = auto()
    RUNNING = auto()
    STOPPING = auto()
    STOPPED = auto()


class InvalidTransition(RuntimeError):
    """Raised when ``start()`` is called from a non-``CREATED`` state.

    Session objects are single-use: re-starting a stopped session is a
    bug, not a valid workflow. If you want a fresh session, construct
    a new one.
    """


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


# TODO(W4a): replace ``Optional[Any]`` with ``MetaCommandSink | None`` once
# the MetaCommandSink type lands in WAVE 4a. Keeping the type loose for now
# avoids a circular dep + lets W1b subclasses forward-ref the attribute
# without importing a module that doesn't yet exist.
MetaCommandSink = Any


@runtime_checkable
class VoiceSession(Protocol):
    """Common shape every voice session subclass exposes.

    Attributes
    ----------
    mode
        The :class:`VoiceMode` this session implements. Used by the
        factory for registration keying and by observability for
        labeling.
    meta_command_sink
        Optional hook set by the factory once WAVE 4a lands. Sessions
        that support meta-commands (``/new``, ``/title``, …) route
        matched utterances through this sink. ``None`` in 0.4.0 while
        MetaCommandSink is still in development.
    """

    mode: VoiceMode
    meta_command_sink: Optional[MetaCommandSink]

    async def start(self) -> None:
        """Acquire resources and transition the session to ``RUNNING``."""
        ...

    async def stop(self) -> None:
        """Release all resources and transition to ``STOPPED``.

        Must be idempotent — callers frequently stop a session both on
        the happy path and from error handlers.
        """
        ...


# ---------------------------------------------------------------------------
# AsyncExitStackBaseSession
# ---------------------------------------------------------------------------


class AsyncExitStackBaseSession:
    """Concrete base class that wires up the shared lifecycle plumbing.

    Subclasses override :meth:`_on_start` (required) and optionally
    :meth:`_on_stop` to do mode-specific teardown *before* the exit
    stack unwinds. Anything acquired via ``self._exit_stack`` is
    released automatically on :meth:`stop`.

    The state machine is enforced in a single place so subclasses don't
    each re-invent transition checks.
    """

    #: Default mode — subclasses MUST override this in their ``__init__``
    #: (or as a class-level attribute) to the concrete mode they implement.
    mode: VoiceMode = VoiceMode.CASCADED

    def __init__(self) -> None:
        self._exit_stack: contextlib.AsyncExitStack = contextlib.AsyncExitStack()
        self._state: SessionState = SessionState.CREATED
        # Public alias per the VoiceSession Protocol — W4a will populate.
        self.meta_command_sink: Optional[MetaCommandSink] = None

    # --- introspection --------------------------------------------------

    @property
    def state(self) -> SessionState:
        """Current lifecycle state (read-only)."""
        return self._state

    # --- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        """Drive ``CREATED → STARTING → RUNNING``.

        On any exception during :meth:`_on_start`, the exit stack is
        closed to release partial resources, the state is set to
        ``STOPPED``, and the original exception propagates.
        """
        if self._state is not SessionState.CREATED:
            raise InvalidTransition(
                f"cannot start() session in state {self._state.name}; "
                "sessions are single-use — construct a new one"
            )
        self._state = SessionState.STARTING
        try:
            await self._on_start()
        except BaseException:
            # Half-started: unwind whatever we acquired so far, then
            # land in STOPPED so idempotent stop() is a no-op.
            self._state = SessionState.STOPPING
            with contextlib.suppress(Exception):
                await self._exit_stack.aclose()
            self._state = SessionState.STOPPED
            raise
        self._state = SessionState.RUNNING

    async def stop(self) -> None:
        """Release all resources and land in ``STOPPED``.

        Idempotent:

        - ``CREATED`` → no-op, state becomes ``STOPPED``.
        - ``STOPPING``/``STOPPED`` → no-op, state unchanged (or
          stays ``STOPPED``).
        - ``RUNNING`` → calls :meth:`_on_stop`, then ``aclose()``.
        """
        if self._state in (SessionState.STOPPED, SessionState.STOPPING):
            return
        if self._state is SessionState.CREATED:
            # Nothing was acquired, but still close the (empty) stack
            # defensively so subclass subclasses can't leak even if they
            # mis-registered something pre-start.
            self._state = SessionState.STOPPING
            with contextlib.suppress(Exception):
                await self._exit_stack.aclose()
            self._state = SessionState.STOPPED
            return

        # RUNNING (or STARTING, which shouldn't happen but handle it
        # defensively — a caller racing start()/stop() from a different
        # task is their bug, not ours to crash on).
        self._state = SessionState.STOPPING
        try:
            await self._on_stop()
        finally:
            with contextlib.suppress(Exception):
                await self._exit_stack.aclose()
            self._state = SessionState.STOPPED

    # --- subclass hooks -------------------------------------------------

    async def _on_start(self) -> None:
        """Subclass-specific startup work.

        Subclasses register cleanup with
        ``await self._exit_stack.enter_async_context(...)`` or
        ``self._exit_stack.push_async_callback(...)`` so
        :meth:`stop` unwinds them in LIFO order.

        The default implementation is a no-op — useful for
        CascadedSession (W1b) which has nothing to acquire.
        """

    async def _on_stop(self) -> None:
        """Subclass-specific pre-teardown work.

        Runs BEFORE ``_exit_stack.aclose()``. Use this for synchronous
        signaling (e.g. ``tool_bridge.cancel_all()``) that should
        precede resource unwinding. Default is a no-op.
        """


__all__ = [
    "SessionState",
    "InvalidTransition",
    "VoiceSession",
    "AsyncExitStackBaseSession",
    "MetaCommandSink",
]
