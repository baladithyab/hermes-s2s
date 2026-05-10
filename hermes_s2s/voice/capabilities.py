"""ModeRequirements + capability-gate for the VoiceSessionFactory.

Implements WAVE 1c / M1.7 of the 0.4.0 re-architecture.

Each :class:`VoiceMode` declares what it needs at runtime — environment
variables, optional pip extras, system libraries, external services.
The factory (M1.8) consults :func:`check_requirements` before allocating
any resources; on miss it either raises :class:`CapabilityError` (when
the mode was explicitly requested) or falls back to cascaded with a
warning (when the mode came from the config default).

References:
    - docs/adrs/0013-four-mode-voicesession.md §5 (capability gating,
      fail-closed-on-explicit-request)
    - docs/research/15-modes-and-meta-deep-dive.md §1 (precedence +
      capability gate interaction)
    - docs/plans/wave-0.4.0-rearchitecture.md WAVE 1c M1.7
"""

from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional, TypedDict

from .modes import ModeSpec, VoiceMode


# ---------------------------------------------------------------------------
# ModeRequirements dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModeRequirements:
    """Declarative description of what a mode needs to run.

    All four lists may be empty (cascaded is the zero-dep baseline). The
    capability checker walks each list and records misses in the returned
    :class:`CapabilityCheck`.

    Attributes
    ----------
    env_vars
        Required environment-variable names. A var is satisfied when it
        is present AND non-empty (``os.environ.get(name, "").strip() != ""``).
    python_packages
        Required importable module names (pip extras). A package is
        satisfied when :func:`importlib.util.find_spec` returns non-None.
    system_libs
        Required shared libraries — informational in 0.4.0 (we don't
        actually probe ``ldconfig``). Included for completeness so a
        ``s2s doctor`` command can surface them in a future wave.
    services
        Required external services, each as ``"<name>@<url>"``. Also
        informational — real health-check lives in W1b S2SServerSession
        (M1.6) or the doctor command.
    """

    env_vars: List[str] = field(default_factory=list)
    python_packages: List[str] = field(default_factory=list)
    system_libs: List[str] = field(default_factory=list)
    services: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# CapabilityCheck return shape
# ---------------------------------------------------------------------------


class CapabilityCheck(TypedDict):
    """Result of :func:`check_requirements`.

    Keys
    ----
    ok
        True iff ``missing`` is empty.
    missing
        Human-readable strings naming each missing requirement, e.g.
        ``"env:GEMINI_API_KEY"`` or ``"pip:websockets"``.
    warnings
        Non-fatal notices (e.g. an env var that is set but empty). The
        factory logs these but does not treat them as capability misses.
    """

    ok: bool
    missing: List[str]
    warnings: List[str]


# ---------------------------------------------------------------------------
# Per-mode requirement lookup
# ---------------------------------------------------------------------------


def _realtime_requirements(spec: ModeSpec) -> ModeRequirements:
    """Return the provider-specific requirements for realtime mode.

    Realtime is the only mode where requirements depend on which
    provider the user picked — Gemini Live wants ``GEMINI_API_KEY`` and
    the ``google`` generative package is optional; OpenAI Realtime
    wants ``OPENAI_API_KEY`` and ``websockets``.

    We consult (in order):
        1. ``spec.provider`` — set when the config has
           ``s2s.voice.provider``.
        2. ``spec.options["provider"]`` — sometimes set by the router
           when a slash hint carries provider info.
        3. Default: treat as "generic realtime" (``websockets`` only).
    """
    provider = (spec.provider or "").lower()
    if not provider:
        provider = str((spec.options or {}).get("provider") or "").lower()

    env_vars: List[str] = []
    packages: List[str] = ["websockets"]

    if "gemini" in provider:
        env_vars.append("GEMINI_API_KEY")
    elif "openai" in provider or "gpt-realtime" in provider:
        env_vars.append("OPENAI_API_KEY")
    else:
        # No provider resolved yet — pessimistically require the Gemini
        # key since that's the dominant deployment. This keeps the
        # explicit-slash fail-closed behavior loud when the operator
        # asked for realtime with no provider configured.
        env_vars.append("GEMINI_API_KEY")

    return ModeRequirements(env_vars=env_vars, python_packages=packages)


def requirements_for(mode: VoiceMode, spec: ModeSpec) -> ModeRequirements:
    """Return the :class:`ModeRequirements` for ``mode`` given ``spec``.

    ``spec`` is consulted only for realtime mode (provider-specific
    requirements). The other three modes have fixed requirement sets.
    """
    if mode is VoiceMode.CASCADED:
        # Zero dependencies — cascaded works with STT/TTS that ship with
        # Hermes core. Packages/env vars here would be redundant with
        # core's own boot checks.
        return ModeRequirements()

    if mode is VoiceMode.PIPELINE:
        # Pipeline mode installs env-driven shell commands (see
        # sessions_pipeline.py). The STT/TTS binaries themselves are
        # the operator's responsibility; we don't probe for them at
        # capability-check time.
        return ModeRequirements()

    if mode is VoiceMode.REALTIME:
        return _realtime_requirements(spec)

    if mode is VoiceMode.S2S_SERVER:
        # S2S server needs a reachable local endpoint. We encode it as
        # a "service" entry for observability but don't probe in 0.4.0.
        endpoint = str((spec.options or {}).get("endpoint", "") or "")
        service_entry = f"s2s-server@{endpoint}" if endpoint else "s2s-server"
        return ModeRequirements(
            python_packages=["websockets"],
            services=[service_entry],
        )

    # Unknown mode — fail closed with an empty requirement set so the
    # factory surfaces the real error downstream when it tries to
    # construct the session.
    return ModeRequirements()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _env_satisfied(name: str, env: Mapping[str, str]) -> bool:
    """An env var is satisfied iff present AND non-empty (after strip)."""
    raw = env.get(name)
    if raw is None:
        return False
    return str(raw).strip() != ""


def _package_satisfied(name: str) -> bool:
    """A package is satisfied iff :func:`importlib.util.find_spec` finds it."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # pragma: no cover - defensive
        return False


def check_requirements(
    mode: VoiceMode,
    spec: ModeSpec,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> CapabilityCheck:
    """Walk the declared requirements for ``mode`` and return a :class:`CapabilityCheck`.

    Parameters
    ----------
    mode
        The :class:`VoiceMode` to check.
    spec
        The resolved :class:`ModeSpec` — consulted for provider-specific
        requirements in realtime mode.
    env
        Optional environment mapping (defaults to :data:`os.environ`).
        Pluggable primarily for hermetic tests.

    Returns
    -------
    CapabilityCheck
        ``{"ok": bool, "missing": [...], "warnings": [...]}``.
    """
    env_map = env if env is not None else os.environ
    reqs = requirements_for(mode, spec)

    missing: List[str] = []
    warnings: List[str] = []

    for var in reqs.env_vars:
        if not _env_satisfied(var, env_map):
            missing.append(f"env:{var}")

    for pkg in reqs.python_packages:
        if not _package_satisfied(pkg):
            missing.append(f"pip:{pkg}")

    # system_libs / services are informational only in 0.4.0; we surface
    # them as warnings if the operator put them in the config, so a
    # future s2s doctor can pick them up.
    for lib in reqs.system_libs:
        warnings.append(f"syslib:{lib} (not probed in 0.4.0)")

    for svc in reqs.services:
        warnings.append(f"service:{svc} (not probed in 0.4.0)")

    return CapabilityCheck(
        ok=not missing,
        missing=missing,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# CapabilityError
# ---------------------------------------------------------------------------


class CapabilityError(RuntimeError):
    """Raised by the factory when an explicitly-requested mode is unavailable.

    "Explicitly requested" means the mode came from a slash-command
    argument or the ``HERMES_S2S_VOICE_MODE`` env var — the user (or
    operator) typed it in directly. In that case we *fail closed*: no
    silent downgrade, because the user asked for that mode specifically
    (ADR-0013 §5).

    Configuration-default mode requests take the opposite path: the
    factory logs a warning and falls back to :class:`CascadedSession`.

    Attributes
    ----------
    missing
        The ``missing`` list from the :class:`CapabilityCheck` that
        triggered the error. Format: ``["env:GEMINI_API_KEY", "pip:websockets"]``.
    mode
        The :class:`VoiceMode` that was requested but can't run.
    """

    def __init__(
        self,
        missing: List[str],
        mode: VoiceMode,
        *,
        message: Optional[str] = None,
    ) -> None:
        self.missing = list(missing)
        self.mode = mode
        if message is None:
            message = self._default_message()
        super().__init__(message)

    def _default_message(self) -> str:
        if not self.missing:
            return (
                f"voice mode {self.mode.value!r} is unavailable (unknown reason)"
            )
        parts: List[str] = []
        for entry in self.missing:
            if entry.startswith("env:"):
                parts.append(f"environment variable {entry[4:]}")
            elif entry.startswith("pip:"):
                parts.append(f"Python package {entry[4:]}")
            elif entry.startswith("syslib:"):
                parts.append(f"system library {entry[7:]}")
            elif entry.startswith("service:"):
                parts.append(f"service {entry[8:]}")
            else:
                parts.append(entry)
        missing_phrase = ", ".join(parts)
        return (
            f"voice mode {self.mode.value!r} cannot start: missing {missing_phrase}. "
            "You explicitly selected this mode, so falling back to cascaded is "
            "not allowed — install the missing requirement(s) or pick a "
            "different mode."
        )

    def user_message(self) -> str:
        """Return a Discord-friendly one-liner describing the failure.

        Used by the ``join_voice_channel`` wrapper to post a message in
        the text channel before rolling back the VC connection
        (Phase-8 P1-F7).
        """
        if not self.missing:
            return (
                f"❌ Voice mode **{self.mode.value}** is unavailable. "
                "Falling back is not allowed because you explicitly "
                "requested this mode."
            )
        # Use a concise inline list for Discord.
        return (
            f"❌ Voice mode **{self.mode.value}** needs: "
            f"`{', '.join(self.missing)}`. "
            "Set it up or pick a different mode."
        )


__all__ = [
    "ModeRequirements",
    "CapabilityCheck",
    "CapabilityError",
    "requirements_for",
    "check_requirements",
]
