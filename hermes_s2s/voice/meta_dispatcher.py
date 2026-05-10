"""Meta-command dispatcher (WAVE 4a M4.2).

Routes a :class:`~hermes_s2s.voice.meta.MetaMatch` to the appropriate
side-effecting target:

- Slash-command verbs (``/new``, ``/compress``, ``/title``, ``/branch``,
  ``/clear``) fan out to ``runner.process_command(...)`` on the gateway's
  Hermes runner.
- ``stop_speaking`` calls ``voice_session.stop_audio_output()`` if the
  active session exposes it.

Design notes:

- ``runner.process_command`` may be sync or async — we check and await
  accordingly.
- Each runner call is wrapped in defensive try/except so a single
  failure produces a spoken error message rather than bubbling up and
  killing the audio bridge. Failures are logged.
- Returned string is what the caller should SPEAK back to the user as
  confirmation. ``None`` means speak nothing (e.g. ``stop_speaking``).

See docs/plans/wave-0.4.0-rearchitecture.md WAVE 4a, ADR-0014 §2-3.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Optional

from .meta import MetaMatch


logger = logging.getLogger(__name__)


# Human-readable spoken confirmations keyed by canonical verb.
# Kept short (≤1 sentence) to minimize TTS latency per the voice-style
# guidance in the persona overlay.
_CONFIRMATIONS: dict[str, str] = {
    "new": "Starting a new session.",
    "compress": "Compressing context.",
    "branch": "Branching off here.",
    "clear": "Starting a new session.",  # /clear aliases to /new
}

# Verb → slash command mapping for verbs that don't need argument
# interpolation. /title and /new-with-extra are handled specially.
_SIMPLE_SLASH: dict[str, str] = {
    "new": "/new",
    "compress": "/compress",
    "branch": "/branch",
    "clear": "/new",  # alias
}

_ERROR_MESSAGES: dict[str, str] = {
    "new": "Sorry, I could not start a new session.",
    "compress": "Sorry, I could not compress the context.",
    "title": "Sorry, I could not rename the session.",
    "branch": "Sorry, I could not branch the session.",
    "clear": "Sorry, I could not clear the context.",
    "stop_speaking": "Sorry, I could not stop audio output.",
}


class MetaDispatcher:
    """Dispatch :class:`MetaMatch` results to runner or voice session.

    Parameters:
        runner: Gateway's Hermes runner with a ``process_command(cmd: str)``
            method. May be sync or async. Optional — ``stop_speaking``
            does not need it.
        voice_session: Active voice session (e.g. a
            :class:`~hermes_s2s.voice.sessions.VoiceSession`). Optional —
            only needed for ``stop_speaking``.
    """

    def __init__(
        self,
        runner: Any = None,
        voice_session: Any = None,
    ) -> None:
        self._runner = runner
        self._voice_session = voice_session

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _invoke_process_command(self, command: str) -> None:
        """Call ``runner.process_command`` handling sync/async + errors.

        Raises the underlying exception so callers can decide how to
        surface it (typically as a spoken error message).
        """
        if self._runner is None:
            raise RuntimeError("no runner configured")
        process_command = getattr(self._runner, "process_command", None)
        if process_command is None:
            raise RuntimeError("runner has no process_command method")

        result = process_command(command)
        if inspect.isawaitable(result):
            await result

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def dispatch(self, match: MetaMatch) -> Optional[str]:
        """Dispatch ``match`` to the appropriate target.

        Returns the text to SPEAK back as confirmation, or ``None`` to
        stay silent.
        """
        verb = match.verb

        if verb == "stop_speaking":
            return await self._dispatch_stop_speaking()

        if verb == "title":
            return await self._dispatch_title(match)

        if verb == "new" or verb == "clear":
            return await self._dispatch_new(match, verb=verb)

        if verb in _SIMPLE_SLASH:
            return await self._dispatch_simple(verb)

        logger.warning("MetaDispatcher: unknown verb %r; ignoring", verb)
        return None

    # ------------------------------------------------------------------
    # Per-verb dispatch
    # ------------------------------------------------------------------
    async def _dispatch_simple(self, verb: str) -> Optional[str]:
        """Dispatch a verb with no argument interpolation."""
        slash = _SIMPLE_SLASH[verb]
        try:
            await self._invoke_process_command(slash)
        except Exception:  # pragma: no cover - defensive
            logger.exception(
                "MetaDispatcher: %s dispatch failed", slash
            )
            return _ERROR_MESSAGES.get(verb, "Sorry, that didn't work.")
        return _CONFIRMATIONS.get(verb)

    async def _dispatch_new(
        self, match: MetaMatch, verb: str = "new"
    ) -> Optional[str]:
        """Dispatch ``/new`` and optionally follow up with ``/title <extra>``."""
        try:
            await self._invoke_process_command("/new")
        except Exception:
            logger.exception("MetaDispatcher: /new dispatch failed")
            return _ERROR_MESSAGES[verb]

        extra = match.args.get("extra", "").strip() if match.args else ""
        if extra:
            try:
                await self._invoke_process_command(f"/title {extra}")
            except Exception:
                # /new succeeded; title is best-effort. Log and continue —
                # user still got a fresh session.
                logger.exception(
                    "MetaDispatcher: /title follow-up failed"
                )
        return _CONFIRMATIONS[verb]

    async def _dispatch_title(self, match: MetaMatch) -> Optional[str]:
        """Dispatch ``/title <title>``."""
        title = (match.args or {}).get("title", "").strip()
        if not title:
            logger.warning(
                "MetaDispatcher: title verb without args['title']; ignoring"
            )
            return _ERROR_MESSAGES["title"]

        try:
            await self._invoke_process_command(f"/title {title}")
        except Exception:
            logger.exception("MetaDispatcher: /title dispatch failed")
            return _ERROR_MESSAGES["title"]

        return f"Titled this session {title}."

    async def _dispatch_stop_speaking(self) -> Optional[str]:
        """Flush current TTS / realtime audio output.

        Returns ``None`` — no spoken confirmation (the user wanted
        silence). The action is logged for observability.
        """
        session = self._voice_session
        if session is None:
            logger.info(
                "MetaDispatcher: stop_speaking received with no voice session"
            )
            return None

        stop_fn = getattr(session, "stop_audio_output", None)
        if stop_fn is None:
            logger.info(
                "MetaDispatcher: session %r has no stop_audio_output",
                type(session).__name__,
            )
            return None

        try:
            result = stop_fn()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception(
                "MetaDispatcher: stop_audio_output failed"
            )
            # Intentionally still return None — don't speak over whatever
            # audio may still be playing.
            return None
        return None


__all__ = ["MetaDispatcher"]
