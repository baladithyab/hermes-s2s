"""VoiceSessionFactory — one-stop shop for building a live :class:`VoiceSession`.

Implements WAVE 1c / M1.8 of the 0.4.0 re-architecture.

Responsibilities
----------------
1. Run the :mod:`capabilities` gate against the resolved :class:`ModeSpec`.
2. Decide between fail-closed (explicit request → :class:`CapabilityError`)
   and fall-through-to-cascaded (config default) based on
   ``spec.options["_explicit"]``.
3. Construct the correct concrete :class:`VoiceSession` subclass.
4. Register the session under the per-channel key
   ``adapter._s2s_sessions[(guild_id, channel_id)]`` so the leave-
   voice-channel wrapper (M1.9) can find and stop it.

References
----------
- docs/adrs/0013-four-mode-voicesession.md §3 (factory)
- docs/research/15-modes-and-meta-deep-dive.md §1–§2
- docs/plans/wave-0.4.0-rearchitecture.md WAVE 1c M1.8 + post-Phase-8
  acceptance refinements.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

from .capabilities import CapabilityError, check_requirements
from .modes import ModeSpec, VoiceMode
from .sessions import VoiceSession
from .sessions_cascaded import CascadedSession
from .sessions_pipeline import CustomPipelineSession
from .sessions_realtime import RealtimeSession
from .sessions_s2s_server import S2SServerSession


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session_key(vc: Any) -> Tuple[Optional[int], Optional[int]]:
    """Derive ``(guild_id, channel_id)`` from a VoiceClient-like object.

    Both fields are optional — tests frequently pass mock objects
    lacking a ``.channel``. We return ``None`` for missing pieces and
    skip registration rather than throw.
    """
    guild = getattr(vc, "guild", None)
    guild_id = getattr(guild, "id", None)
    channel = getattr(vc, "channel", None)
    channel_id = getattr(channel, "id", None)
    return guild_id, channel_id


def _register(adapter: Any, key: Tuple[Optional[int], Optional[int]], session: VoiceSession) -> None:
    """Put ``session`` into ``adapter._s2s_sessions[key]`` (create dict if absent)."""
    if adapter is None:
        return
    if None in key:
        # Without a full key we can't route leave-voice-channel cleanup
        # back to this session; skip registration rather than poison
        # the map with None keys.
        return
    sessions = getattr(adapter, "_s2s_sessions", None)
    if sessions is None:
        sessions = {}
        try:
            adapter._s2s_sessions = sessions
        except Exception:  # pragma: no cover - defensive
            return
    sessions[key] = session


# ---------------------------------------------------------------------------
# VoiceSessionFactory
# ---------------------------------------------------------------------------


class VoiceSessionFactory:
    """Builds and registers :class:`VoiceSession` instances per :class:`ModeSpec`.

    The factory is stateless apart from the dependencies it's handed at
    construction time (a provider registry, optional tool-bridge and
    meta-dispatcher). Every call to :meth:`build` is independent; the
    factory produces a fresh session and registers it on the supplied
    adapter.

    Parameters
    ----------
    registry
        Provider registry object exposing ``resolve_realtime(provider,
        options)``. Only consulted when building a
        :class:`RealtimeSession`. May be None — the factory will then
        defer backend construction to the session itself (useful in
        tests where the mock backend is injected via ``spec.options``).
    tool_bridge
        Optional :class:`HermesToolBridge` instance passed to realtime
        sessions. None disables tool-call plumbing.
    meta_dispatcher
        Optional meta-command sink (hooked up in W4a). Stashed on the
        session so voice-meta-commands can route through it when that
        wave lands.
    """

    def __init__(
        self,
        registry: Any = None,
        *,
        tool_bridge: Any = None,
        meta_dispatcher: Any = None,
    ) -> None:
        self._registry = registry
        self._tool_bridge = tool_bridge
        self._meta_dispatcher = meta_dispatcher

    # ------------------------------------------------------------------
    # build()
    # ------------------------------------------------------------------

    def build(
        self,
        spec: ModeSpec,
        vc: Any,
        adapter: Any,
        hermes_ctx: Any = None,
    ) -> VoiceSession:
        """Run capability gate → construct session → register on adapter.

        Parameters
        ----------
        spec
            Resolved :class:`ModeSpec` from :class:`ModeRouter`. The
            ``_explicit`` flag in ``spec.options`` is consulted to
            decide fail-closed vs fall-through on capability miss.
        vc
            The live Discord voice client. Used for session registration
            keying and handed to sessions that need it (currently none
            — the audio bridge owns the VC reference).
        adapter
            The Discord adapter. The built session is stored on
            ``adapter._s2s_sessions[(guild_id, channel_id)]``.
        hermes_ctx
            Optional Hermes context exposing ``dispatch_tool`` etc.
            Forwarded into session construction where relevant.

        Returns
        -------
        VoiceSession
            A :class:`VoiceSession` instance in the ``CREATED`` state.
            The caller is responsible for calling ``await session.start()``.

        Raises
        ------
        CapabilityError
            If the spec was explicitly requested and its capability
            gate fails. The M1.9 :func:`join_voice_channel` wrapper
            catches this, posts a user-friendly message, and rolls back
            the VC connection.
        """
        explicit = bool((spec.options or {}).get("_explicit", False))
        effective_spec = spec
        effective_mode = spec.mode

        check = check_requirements(spec.mode, spec)
        if not check["ok"]:
            if explicit:
                logger.warning(
                    "hermes-s2s factory: mode=%s explicitly requested but "
                    "capability gate failed: missing=%s",
                    spec.mode.value,
                    check["missing"],
                )
                raise CapabilityError(check["missing"], spec.mode)
            # Non-explicit: warn and fall back to CASCADED.
            logger.warning(
                "hermes-s2s factory: mode=%s unavailable (missing=%s); "
                "falling back to cascaded because the request came from the "
                "config default, not an explicit slash/env selection",
                spec.mode.value,
                check["missing"],
            )
            effective_mode = VoiceMode.CASCADED
            # Preserve provider/options but swap the mode so downstream
            # observability reports the *effective* mode.
            effective_spec = ModeSpec(
                mode=VoiceMode.CASCADED,
                provider=spec.provider,
                options=dict(spec.options or {}),
            )

        session = self._construct(effective_mode, effective_spec, hermes_ctx)
        # Wire meta dispatcher per ADR-0013 §4 / VoiceSession protocol.
        if self._meta_dispatcher is not None:
            try:
                session.meta_command_sink = self._meta_dispatcher  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - defensive
                logger.debug(
                    "hermes-s2s factory: could not attach meta_dispatcher to session",
                    exc_info=True,
                )

        key = _session_key(vc)
        _register(adapter, key, session)
        logger.info(
            "hermes-s2s factory: built %s session (key=%s, provider=%s)",
            effective_mode.value,
            key,
            effective_spec.provider,
        )
        return session

    # ------------------------------------------------------------------
    # Session construction per mode
    # ------------------------------------------------------------------

    def _construct(
        self, mode: VoiceMode, spec: ModeSpec, hermes_ctx: Any
    ) -> VoiceSession:
        """Dispatch to the correct session constructor.

        Each session class is imported eagerly at the top of this
        module; the dispatch here is a simple mapping so a typo in the
        mode enum surfaces as a loud ``KeyError`` in tests.
        """
        if mode is VoiceMode.CASCADED:
            return CascadedSession(spec)
        if mode is VoiceMode.PIPELINE:
            return CustomPipelineSession(spec)
        if mode is VoiceMode.REALTIME:
            return self._build_realtime(spec, hermes_ctx)
        if mode is VoiceMode.S2S_SERVER:
            backend = (spec.options or {}).get("_backend")
            return S2SServerSession(spec, backend=backend)
        # Unreachable for VoiceMode members, but keep the error obvious
        # if a new enum value is added without a factory update.
        raise ValueError(f"VoiceSessionFactory: unknown mode {mode!r}")

    def _build_realtime(self, spec: ModeSpec, hermes_ctx: Any) -> VoiceSession:
        """Construct a :class:`RealtimeSession` with backend + tool-bridge wiring.

        v0.3.9 REGRESSION FENCE
        -----------------------
        The bridge's ``system_prompt`` / ``voice`` come from
        ``_resolve_bridge_params(cfg)`` in
        :mod:`hermes_s2s._internal.discord_bridge` so nested
        ``realtime_options[gemini_live]`` configs (the wizard shape
        since 0.3.8) keep working. We call that helper here — routing
        through the factory does NOT regress the v0.3.9 fix.
        Tests: ``tests/test_config_unwrap.py``.

        Also eagerly constructs :class:`RealtimeAudioBridge` so the
        M1.9 ``_attach_realtime_to_voice_client`` glue can pull
        ``session._bridge`` out for frame-callback / PCM-source wiring
        BEFORE ``session.start()`` is scheduled on the VC loop.
        """
        options = dict(spec.options or {})

        # Test-path: the caller may pre-inject a backend through
        # spec.options["_backend"] to avoid the registry + real WS call.
        backend = options.pop("_backend", None)
        pre_resolved_system_prompt: Optional[str] = options.pop(
            "_system_prompt_override", None
        )
        pre_resolved_voice: Optional[str] = options.pop(
            "_voice_override", None
        )
        pre_built_bridge = options.pop("_bridge", None)

        system_prompt = pre_resolved_system_prompt
        voice = pre_resolved_voice

        # Production path: resolve backend from the registry and pull
        # system_prompt/voice from the shared helper. The helper lives
        # in discord_bridge — import lazily so this module stays
        # importable in test envs that mock discord.py out.
        if backend is None and self._registry is not None:
            cfg = options.pop("_cfg", None)
            if cfg is None:
                # Load the S2S config the same way the 0.3.x bridge did.
                try:
                    from ..config import load_config  # type: ignore

                    cfg = load_config()
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning(
                        "hermes-s2s factory: config load failed: %s", exc
                    )
                    cfg = None

            if cfg is not None:
                provider = getattr(cfg, "realtime_provider", None) or spec.provider
                provider_options = getattr(cfg, "realtime_options", {}) or {}
                try:
                    resolve = getattr(self._registry, "resolve_realtime", None)
                    if callable(resolve):
                        backend = resolve(provider, provider_options)
                    else:
                        logger.warning(
                            "hermes-s2s factory: registry has no "
                            "resolve_realtime(); backend remains unresolved"
                        )
                except Exception as exc:
                    logger.warning(
                        "hermes-s2s factory: failed to resolve realtime "
                        "backend %r: %s",
                        provider,
                        exc,
                    )
                    backend = None

                if system_prompt is None or voice is None:
                    # Delegate to the canonical v0.3.9 helper. Keep
                    # import lazy so tests that don't monkey-patch
                    # Discord can still exercise the factory.
                    try:
                        from hermes_s2s._internal.discord_bridge import (
                            _resolve_bridge_params,
                        )

                        resolved_prompt, resolved_voice = _resolve_bridge_params(cfg)
                        if system_prompt is None:
                            system_prompt = resolved_prompt
                        if voice is None:
                            voice = resolved_voice
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.warning(
                            "hermes-s2s factory: _resolve_bridge_params failed: %s",
                            exc,
                        )

        # Last-resort fallbacks so RealtimeSession.__init__ has something
        # to show — tests injecting their own backend hit this path.
        if system_prompt is None:
            system_prompt = options.get(
                "system_prompt", "You are a helpful voice assistant."
            )
        if voice is None:
            voice = options.get("voice")

        tools = list(options.get("tools") or [])
        # 0.4.2 S2: thread history through to the session so it can pass
        # to backend.connect() via ConnectOptions. Empty list = no history.
        history = list(options.get("history") or [])

        # Eagerly construct RealtimeAudioBridge if the backend is
        # available. The M1.9 attach glue reads ``session._bridge`` to
        # wire the frame callback + QueuedPCMSource BEFORE start() is
        # scheduled on the VC loop. If the bridge module isn't
        # importable (test env), fall through — the session will build
        # one lazily in _on_start().
        bridge = pre_built_bridge
        if bridge is None and backend is not None:
            try:
                from hermes_s2s._internal.audio_bridge import RealtimeAudioBridge

                bridge = RealtimeAudioBridge(
                    backend=backend,
                    tool_bridge=self._tool_bridge,
                    system_prompt=system_prompt,
                    voice=voice,
                    tools=list(tools),
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(
                    "hermes-s2s factory: eager RealtimeAudioBridge construction "
                    "failed (%s); session will build lazily",
                    exc,
                )
                bridge = None

        # Re-stamp options with the resolved values so the session has
        # a useful view for observability. Preserve the _explicit flag
        # so downstream code that reads the session's ``_spec`` sees
        # the original provenance.
        resolved_spec = ModeSpec(
            mode=spec.mode,
            provider=spec.provider,
            options={
                **options,
                "system_prompt": system_prompt,
                "voice": voice,
                "tools": tools,
                "history": history,  # 0.4.2 S2
            },
        )

        return RealtimeSession(
            resolved_spec,
            backend=backend,
            tool_bridge=self._tool_bridge,
            system_prompt=system_prompt,
            voice=voice,
            tools=tools,
            history=history,  # 0.4.2 S2
            bridge=bridge,
        )


__all__ = ["VoiceSessionFactory"]
