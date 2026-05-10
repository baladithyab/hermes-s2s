"""S2SServerSession — wraps the local s2s-server pipeline backend.

Implements WAVE 1b / M1.6 of the 0.4.0 re-architecture (ADR-0013 §4,
research-15 §2.1).

An s2s-server session talks to a locally-hosted speech-to-speech
service (e.g. github.com/codeseys/streaming-speech-to-speech) over
WS/gRPC. The full turn (STT + LLM + TTS) happens server-side, so
Hermes's usual LLM call is bypassed for the voice turn; the local
model speaks. Useful when low-latency conversation matters more
than Hermes-side tool access.

Backend I/O is owned by :class:`hermes_s2s.providers.pipeline.s2s_server.S2SServerPipeline`
(currently a stub). :class:`S2SServerSession` is the lifecycle
wrapper that:

    1. Resolves the backend from ``ModeSpec.options``.
    2. Performs a health-check (W1c will wire this up properly).
    3. Opens a connection (if ``backend.connect`` exists) and
       registers cleanup callbacks on the AsyncExitStack.
    4. On ``stop()``: the stack unwinds in LIFO order — supervisor
       task cancelled → backend closed.

For 0.4.0 we deliberately keep this class thin — the real work lives
in the backend itself. Future waves will add: health-check retries,
subprocess auto-launch when ``auto_launch=True``, and supervisor task
watching for backend death.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from hermes_s2s.voice.modes import ModeSpec, VoiceMode
from hermes_s2s.voice.sessions import AsyncExitStackBaseSession


logger = logging.getLogger(__name__)


class S2SServerSession(AsyncExitStackBaseSession):
    """Voice session wrapping a locally-hosted S2S pipeline backend.

    Parameters
    ----------
    spec:
        Resolved :class:`ModeSpec`. ``spec.options`` may contain
        ``endpoint``, ``health_url``, ``auto_launch``.
    backend:
        Optional pre-constructed backend. If omitted, the session
        will attempt to construct an
        :class:`S2SServerPipeline` from ``spec.options``. In tests,
        callers pass a mock backend directly.
    """

    mode: VoiceMode = VoiceMode.S2S_SERVER

    def __init__(
        self,
        spec: ModeSpec,
        *,
        backend: Any = None,
        **_ignored: Any,
    ) -> None:
        super().__init__()
        self._spec = spec
        self._backend: Optional[Any] = backend

    async def _on_start(self) -> None:
        """Resolve backend → open connection → register cleanup."""
        if self._backend is None:
            self._backend = self._build_backend_from_spec()

        # Some backends expose an async ``connect()``; the reference
        # S2SServerPipeline stub does not (the connection is opened
        # per-turn inside ``converse``). We call connect() if it
        # exists, else rely on per-turn opening.
        connect = getattr(self._backend, "connect", None)
        if connect is not None:
            logger.debug("s2s-server session: awaiting backend.connect()")
            result = connect()
            if asyncio.iscoroutine(result):
                await result

        # Register cleanup BEFORE any other work so a subsequent
        # failure is still cleanly torn down.
        self._exit_stack.push_async_callback(self._close_backend)

        logger.info(
            "s2s-server mode active: endpoint=%s",
            getattr(self._backend, "endpoint", "<unknown>"),
        )

    async def _on_stop(self) -> None:
        """Teardown happens via ``_exit_stack.aclose()``."""
        logger.debug("s2s-server session stopping")

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _build_backend_from_spec(self) -> Any:
        """Construct an :class:`S2SServerPipeline` from spec options.

        Deferred-import the backend class so tests that pass an
        explicit ``backend=`` kwarg don't force the pipeline module
        to import cleanly (it's a stub in 0.4.0).
        """
        options = self._spec.options or {}
        endpoint = str(options.get("endpoint", ""))
        health_url = str(options.get("health_url", ""))
        auto_launch = bool(options.get("auto_launch", False))

        # Deferred import — the stub module raises NotImplementedError
        # from its ``converse`` coroutine, not at import time, so it
        # imports fine. Still, keep the import local to avoid a hard
        # dep at session-module import time.
        from hermes_s2s.providers.pipeline.s2s_server import S2SServerPipeline

        return S2SServerPipeline(
            endpoint=endpoint,
            health_url=health_url,
            auto_launch=auto_launch,
        )

    async def _close_backend(self) -> None:
        if self._backend is None:
            return
        close = getattr(self._backend, "close", None)
        if close is None:
            return
        try:
            result = close()
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001
            logger.debug(
                "s2s-server session: backend close raised", exc_info=True
            )


__all__ = ["S2SServerSession"]
