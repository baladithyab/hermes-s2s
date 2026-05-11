"""
RealtimeConfig — typed envelope for ``s2s.voice.realtime.*`` config.

Why this exists
---------------
v0.4.2 introduces ``s2s.voice.realtime.history.*`` keys. Architecture
review (critique §7) flagged config sprawl and the v0.5.0 collision
with ``s2s.voice.realtime.tools.*`` (tier system). Solution: typed
nested dataclasses with a ``from_dict`` parser that's forward-
compatible — adding ``tools`` / ``audio.silence_fade_ms`` later means
adding a dataclass field, not a parser.

Schema::

    s2s:
      voice:
        realtime:
          history:
            enabled: true
            max_turns: 20
            max_tokens: 8000
          audio:
            resampler: soxr        # "soxr" | "scipy"
            silence_fade_ms: 5
          # tools: <reserved for v0.5.0 ToolsConfig>

Unknown keys are logged at DEBUG level and ignored — preserves
forward-compat when newer plugin versions read older config.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class HistoryConfig:
    """``s2s.voice.realtime.history.*``"""

    enabled: bool = True
    max_turns: int = 20
    max_tokens: int = 8000

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "HistoryConfig":
        if not raw:
            return cls()
        return cls(
            enabled=bool(raw.get("enabled", True)),
            max_turns=int(raw.get("max_turns", 20)),
            max_tokens=int(raw.get("max_tokens", 8000)),
        )


@dataclasses.dataclass(frozen=True)
class AudioConfig:
    """``s2s.voice.realtime.audio.*``"""

    resampler: str = "soxr"  # "soxr" | "scipy"
    silence_fade_ms: int = 5

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "AudioConfig":
        if not raw:
            return cls()
        resampler = str(raw.get("resampler", "soxr")).lower()
        if resampler not in ("soxr", "scipy"):
            logger.warning(
                "RealtimeConfig.audio.resampler=%r unknown; using 'soxr'",
                resampler,
            )
            resampler = "soxr"
        fade = int(raw.get("silence_fade_ms", 5))
        if fade < 0:
            fade = 0
        if fade > 50:  # > 50ms fade is audible as muting
            logger.warning(
                "RealtimeConfig.audio.silence_fade_ms=%d > 50; clamping",
                fade,
            )
            fade = 50
        return cls(resampler=resampler, silence_fade_ms=fade)


@dataclasses.dataclass(frozen=True)
class RealtimeConfig:
    """Typed view of ``s2s.voice.realtime.*``.

    The ``tools`` field is reserved for v0.5.0 ToolsConfig and stays
    typed as ``Optional[Any]`` for now — adding the real dataclass
    later is purely additive.
    """

    history: HistoryConfig = dataclasses.field(default_factory=HistoryConfig)
    audio: AudioConfig = dataclasses.field(default_factory=AudioConfig)
    # Reserved for v0.5.0 — populated by future ToolsConfig.from_dict
    tools: Optional[Any] = None

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "RealtimeConfig":
        """Parse from ``cfg["s2s"]["voice"]["realtime"]`` shape.

        Unknown top-level keys logged at DEBUG and ignored.
        """
        if not raw:
            return cls()
        known = {"history", "audio", "tools"}
        unknown = set(raw.keys()) - known
        if unknown:
            logger.debug(
                "RealtimeConfig: ignoring unknown keys: %s",
                sorted(unknown),
            )
        return cls(
            history=HistoryConfig.from_dict(raw.get("history")),
            audio=AudioConfig.from_dict(raw.get("audio")),
            tools=raw.get("tools"),  # v0.5.0 will parse this
        )

    @classmethod
    def from_full_config(cls, cfg: Dict[str, Any]) -> "RealtimeConfig":
        """Walk full Hermes config dict to ``s2s.voice.realtime.*``."""
        try:
            raw = (
                cfg.get("s2s", {})
                .get("voice", {})
                .get("realtime", {})
            )
        except Exception:  # noqa: BLE001 - defensive against weird configs
            return cls()
        return cls.from_dict(raw)


__all__ = ["RealtimeConfig", "HistoryConfig", "AudioConfig"]
