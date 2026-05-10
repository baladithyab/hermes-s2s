"""CascadedSession — no-op shim for the classic STT→LLM→TTS voice path.

Implements WAVE 1b / M1.3 of the 0.4.0 re-architecture (ADR-0013 §4,
research-15 §2.1).

Why a no-op? The cascaded path is owned by Hermes core's native voice
worker (STT → LLM → TTS). The plugin's job in cascaded mode is simply
to *not interfere* — we don't open any sockets, don't install
providers, don't spawn any tasks. :class:`CascadedSession` exists so
the :class:`~hermes_s2s.voice.factory.VoiceSessionFactory` (W1c) has a
uniform return type across all four modes (ADR-0013 §3) and so
lifecycle observability (start/stop logging, state transitions) is
consistent regardless of which topology the operator picked.

Future `CascadedSession` work (0.4.1+): attach the
:class:`MetaCommandSink` to Hermes core's voice-adapter callback
chain so wakeword-anchored meta-commands work in cascaded mode. For
0.4.0 the sink hook-up happens through a separate seam (see W4a).
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from hermes_s2s.voice.modes import ModeSpec, VoiceMode
from hermes_s2s.voice.sessions import AsyncExitStackBaseSession


logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def _noop_async_cm():
    """An async context manager that does nothing on enter or exit.

    Registered on the session's AsyncExitStack so ``stop()`` has
    something to unwind — keeps the state-machine contract uniform
    with the other three sessions, which all push at least one cleanup
    entry during ``start()``.
    """
    yield None


class CascadedSession(AsyncExitStackBaseSession):
    """No-op voice session; lets Hermes core's native cascaded path run.

    Parameters
    ----------
    spec:
        The :class:`ModeSpec` that produced this session. Stored for
        observability; no fields are consulted at runtime.
    """

    mode: VoiceMode = VoiceMode.CASCADED

    def __init__(self, spec: ModeSpec, **_ignored: Any) -> None:
        super().__init__()
        self._spec = spec
        # No backend, no bridges, no pumps. The whole point.

    async def _on_start(self) -> None:
        """Record that cascaded mode is active; register a no-op teardown."""
        logger.info("cascaded mode active (no-op session)")
        # Push a no-op cleanup so the stack has at least one entry.
        # Keeps ``stop()`` symmetric with the other session classes and
        # exercises the AsyncExitStack happy path in tests.
        await self._exit_stack.enter_async_context(_noop_async_cm())

    async def _on_stop(self) -> None:
        """Nothing to stop explicitly — ``_exit_stack.aclose()`` handles it."""
        logger.debug("cascaded session stopping (no-op)")


__all__ = ["CascadedSession"]
