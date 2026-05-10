"""VoiceMode enum, ModeSpec dataclass, and ModeRouter.

Implements WAVE 1a / M1.1 of the 0.4.0 re-architecture.

References:
- docs/adrs/0013-four-mode-voicesession.md §1–§2
- docs/research/15-modes-and-meta-deep-dive.md §1 (precedence + normalization)
- docs/research/12-voice-mode-rearchitecture.md §3 (pseudocode)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


# ---------------------------------------------------------------------------
# VoiceMode
# ---------------------------------------------------------------------------


class VoiceMode(StrEnum):
    """The four voice topologies supported in 0.4.0.

    NAMES are Python identifiers (so ``S2S_SERVER`` with an underscore), but
    the serialized *value* uses the canonical hyphenated form ``s2s-server``
    that appears in slash-command args, config, and log lines.

    ``StrEnum`` makes ``str(VoiceMode.REALTIME) == "realtime"`` so it flows
    through YAML/JSON without special casing.
    """

    CASCADED = "cascaded"
    PIPELINE = "pipeline"
    REALTIME = "realtime"
    S2S_SERVER = "s2s-server"

    # --- normalization --------------------------------------------------

    @classmethod
    def normalize(cls, value: str | "VoiceMode" | None) -> "VoiceMode":
        """Coerce a user-supplied string into a :class:`VoiceMode`.

        Accepts (case-insensitive, trimmed):

        - ``cascaded``, ``pipeline``, ``realtime``, ``s2s-server``
        - ``s2s_server`` and ``s2s server`` (whitespace/underscore → hyphen)

        Raises :class:`ValueError` on unknown input, with a message that
        lists every valid mode — programmatic callers get a loud, obvious
        failure instead of a quiet fall-through (research-15 §1.3).
        """
        if isinstance(value, cls):
            return value
        if value is None:
            raise ValueError(
                f"unknown mode None, valid: {cls._valid_values_str()}"
            )

        s = str(value).strip().lower()
        if not s:
            raise ValueError(
                f"unknown mode '', valid: {cls._valid_values_str()}"
            )

        # Collapse whitespace runs to a single space first, then swap
        # spaces/underscores for hyphens. That handles "s2s_server" and
        # "S2S Server" (and even "s2s  server") → "s2s-server".
        s = "-".join(s.split())
        s = s.replace("_", "-")

        try:
            return cls(s)
        except ValueError:
            raise ValueError(
                f"unknown mode {value!r}, valid: {cls._valid_values_str()}"
            ) from None

    @classmethod
    def _valid_values_str(cls) -> str:
        return ", ".join(m.value for m in cls)

    # --- sanity: StrEnum gives value-based __str__, but guarantee it ----

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


# ---------------------------------------------------------------------------
# ModeSpec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModeSpec:
    """Immutable description of *how* a VoiceSession should be built.

    Created by :class:`ModeRouter` and consumed by the factory (W1c).
    The factory is the only component that should turn a ``ModeSpec``
    into a live :class:`~hermes_s2s.voice.sessions.VoiceSession`.
    """

    mode: VoiceMode
    provider: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# ModeRouter
# ---------------------------------------------------------------------------


# Env-var name per WAVE 1a task contract. Research-15 §1.1 uses
# HERMES_S2S_FORCE_MODE historically; the task brief overrides to
# HERMES_S2S_VOICE_MODE for 0.4.0. Keep the research name as a secondary
# alias to avoid breaking dev loops — first-set wins.
_ENV_VAR_PRIMARY = "HERMES_S2S_VOICE_MODE"
_ENV_VAR_LEGACY = "HERMES_S2S_FORCE_MODE"


class ModeRouter:
    """Resolves a :class:`ModeSpec` from the 6-level precedence chain.

    Precedence (highest wins):

    1. Explicit slash hint passed as ``mode_hint``.
    2. ``HERMES_S2S_VOICE_MODE`` env var (falls back to
       ``HERMES_S2S_FORCE_MODE`` for research-15 continuity).
    3. ``s2s.voice.channel_overrides[channel_id]``.
    4. ``s2s.voice.guild_overrides[guild_id]``.
    5. ``s2s.voice.default_mode``.
    6. Hard default: ``"cascaded"``.

    The router **does not** perform capability gating — that belongs to
    the factory (W1c). Its sole job is to deterministically pick *which*
    mode the user wants, with typos surfaced as a loud :class:`ValueError`.
    """

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config or {}
        # ``env`` is pluggable primarily to keep tests hermetic.
        self._env = env if env is not None else os.environ

    # --- public API -----------------------------------------------------

    def resolve(
        self,
        *,
        mode_hint: str | VoiceMode | None = None,
        guild_id: int | str | None = None,
        channel_id: int | str | None = None,
    ) -> ModeSpec:
        """Pick a mode and wrap it in a :class:`ModeSpec`.

        The returned ``spec.options`` dict carries a boolean ``_explicit``
        flag that is True iff the resolved mode came from a source the
        operator picked *directly* (slash hint or env var), as opposed
        to a config default. The factory (W1c M1.8) consults this flag
        to decide between fail-closed (explicit) and warn-and-fallback
        (non-explicit) behavior when capability gating fails.
        """
        raw, explicit = self._resolve_with_source(
            mode_hint=mode_hint, guild_id=guild_id, channel_id=channel_id
        )
        mode = VoiceMode.normalize(raw)
        voice_cfg = self._voice_cfg()
        options = dict(voice_cfg.get("options") or {})
        options["_explicit"] = explicit
        return ModeSpec(
            mode=mode,
            provider=voice_cfg.get("provider"),
            options=options,
        )

    # --- precedence chain ----------------------------------------------

    def _resolve_raw(
        self,
        *,
        mode_hint: str | VoiceMode | None,
        guild_id: int | str | None,
        channel_id: int | str | None,
    ) -> str:
        raw, _explicit = self._resolve_with_source(
            mode_hint=mode_hint, guild_id=guild_id, channel_id=channel_id
        )
        return raw

    def _resolve_with_source(
        self,
        *,
        mode_hint: str | VoiceMode | None,
        guild_id: int | str | None,
        channel_id: int | str | None,
    ) -> tuple[str, bool]:
        """Walk the precedence chain and return ``(raw_mode, explicit)``.

        ``explicit`` is True iff the winning level was the slash hint
        (level 1) or the env var (level 2) — those are the two places
        where the operator typed the mode directly. Channel/guild
        overrides and the config default are all configuration-sourced
        (not "explicit request") and therefore take the warn+fallback
        path in the factory.
        """
        # Each level returns None to signal "skip me"; first non-None wins.
        levels: list[tuple[bool, Any]] = [
            (True, lambda: self._from_hint(mode_hint)),
            (True, self._from_env),
            (False, lambda: self._from_channel(channel_id)),
            (False, lambda: self._from_guild(guild_id)),
            (False, self._from_default),
        ]
        for explicit, level in levels:
            result = level()
            if result is not None:
                return result, explicit
        return VoiceMode.CASCADED.value, False

    def _from_hint(self, mode_hint: str | VoiceMode | None) -> str | None:
        if mode_hint is None:
            return None
        if isinstance(mode_hint, VoiceMode):
            return mode_hint.value
        s = str(mode_hint).strip()
        if not s:
            return None
        # "auto"/"default" mean "no hint" per research-15 §1.3.
        if s.lower() in {"auto", "default"}:
            return None
        return s

    def _from_env(self) -> str | None:
        for name in (_ENV_VAR_PRIMARY, _ENV_VAR_LEGACY):
            raw = self._env.get(name)
            if raw is None:
                continue
            raw = raw.strip()
            if not raw:
                continue
            return raw
        return None

    def _from_channel(self, channel_id: int | str | None) -> str | None:
        if channel_id is None:
            return None
        overrides = self._voice_cfg().get("channel_overrides") or {}
        return self._lookup_override(overrides, channel_id)

    def _from_guild(self, guild_id: int | str | None) -> str | None:
        if guild_id is None:
            return None
        overrides = self._voice_cfg().get("guild_overrides") or {}
        return self._lookup_override(overrides, guild_id)

    def _from_default(self) -> str | None:
        raw = self._voice_cfg().get("default_mode")
        if raw is None:
            return None
        s = str(raw).strip()
        return s or None

    # --- helpers --------------------------------------------------------

    def _voice_cfg(self) -> Mapping[str, Any]:
        s2s = self._config.get("s2s") or {}
        return s2s.get("voice") or {}

    @staticmethod
    def _lookup_override(
        overrides: Mapping[Any, Any], key: int | str
    ) -> str | None:
        """Look up ``key`` in ``overrides`` trying both int and str forms.

        Discord IDs are 64-bit ints; YAML/JSON users commonly key them as
        strings. Accept either shape so configs are forgiving.
        """
        if key in overrides:
            raw = overrides[key]
        else:
            alt: Any
            if isinstance(key, int):
                alt = str(key)
            else:
                try:
                    alt = int(key)
                except (TypeError, ValueError):
                    alt = None
            if alt is not None and alt in overrides:
                raw = overrides[alt]
            else:
                return None

        if raw is None:
            return None
        # Entries may be a bare mode string or a nested dict {"mode": "..."}.
        if isinstance(raw, Mapping):
            inner = raw.get("mode")
            if inner is None:
                return None
            s = str(inner).strip()
            return s or None
        s = str(raw).strip()
        return s or None


__all__ = [
    "VoiceMode",
    "ModeSpec",
    "ModeRouter",
]
