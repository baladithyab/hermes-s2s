"""CustomPipelineSession — Moonshine STT + Kokoro TTS command-provider mode.

Implements WAVE 1b / M1.4 of the 0.4.0 re-architecture (ADR-0013 §4,
research-15 §2.1, ADR-0004 command-providers).

In pipeline mode the plugin installs local STT and TTS command-providers
(shell commands, per ADR-0004) by exporting ``HERMES_LOCAL_STT_COMMAND``
and ``HERMES_LOCAL_TTS_COMMAND`` for the duration of the session.
Hermes core's voice worker picks up these env vars on each turn.

``start()`` snapshots any existing values for those env vars and
installs the new ones from ``ModeSpec.options['stt_command']`` and
``ModeSpec.options['tts_command']``. ``stop()`` restores the snapshot
via an ``AsyncExitStack.callback()`` — so even if ``start()`` crashes
part-way, ``_exit_stack.aclose()`` walks the restore callbacks in LIFO
order and leaves the process environment untouched.

Why env-var driven? Because 0.3.x already ships Moonshine+Kokoro
via these same env vars (see docs/research/15 §2.1 and the 0.3.2
plug-and-play work). W1b only re-homes the lifecycle under a
proper VoiceSession shape; the 0.3.x semantics are preserved.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from hermes_s2s.voice.modes import ModeSpec, VoiceMode
from hermes_s2s.voice.sessions import AsyncExitStackBaseSession


logger = logging.getLogger(__name__)


_STT_ENV = "HERMES_LOCAL_STT_COMMAND"
_TTS_ENV = "HERMES_LOCAL_TTS_COMMAND"


class CustomPipelineSession(AsyncExitStackBaseSession):
    """Voice session that installs Moonshine+Kokoro via env-driven commands.

    :class:`ModeSpec.options` is consulted for:

    - ``stt_command``: shell command string invoked per STT turn.
    - ``tts_command``: shell command string invoked per TTS turn.

    Missing options are treated as "don't set this one" — useful if
    an operator wants Moonshine STT but the built-in Hermes TTS.
    """

    mode: VoiceMode = VoiceMode.PIPELINE

    def __init__(self, spec: ModeSpec, **_ignored: Any) -> None:
        super().__init__()
        self._spec = spec

    async def _on_start(self) -> None:
        options = self._spec.options or {}
        stt_cmd: Optional[str] = options.get("stt_command")
        tts_cmd: Optional[str] = options.get("tts_command")

        if stt_cmd:
            self._install_env(_STT_ENV, str(stt_cmd))
        if tts_cmd:
            self._install_env(_TTS_ENV, str(tts_cmd))

        logger.info(
            "pipeline mode active: stt_command=%s tts_command=%s",
            "set" if stt_cmd else "unset",
            "set" if tts_cmd else "unset",
        )

    def _install_env(self, name: str, value: str) -> None:
        """Set ``name=value`` and register a restore-on-stop callback.

        If the env var was unset going in, the restore callback
        *unsets* it (via ``os.environ.pop``) so we leave the process
        environment exactly as we found it. If it had a prior value,
        we put that value back.
        """
        had_prior = name in os.environ
        prior_value = os.environ.get(name)

        def _restore() -> None:
            if had_prior:
                os.environ[name] = prior_value  # type: ignore[assignment]
            else:
                os.environ.pop(name, None)
            logger.debug("pipeline session restored env var %s", name)

        # Register BEFORE mutating so that if the assignment raises
        # (unlikely for os.environ but still), we don't leak a callback
        # that restores garbage.
        self._exit_stack.callback(_restore)
        os.environ[name] = value
        logger.debug(
            "pipeline session installed env var %s (prior=%r)",
            name,
            prior_value,
        )

    async def _on_stop(self) -> None:
        """Env restoration happens via ``_exit_stack.aclose()`` callbacks."""
        logger.debug("pipeline session stopping")


__all__ = ["CustomPipelineSession"]
