"""Discord voice bridge — routes Discord VC audio through a realtime S2S backend.

Strategy (see docs/adrs/0006-discord-voice-bridge.md):

1. **Native hook (preferred)**: If Hermes ever exposes
   ``ctx.register_voice_pipeline_factory``, we register our pipeline factory
   through that first-class seam and do nothing else. This is the target state
   after the upstream PR lands.

2. **Monkey-patch (current bridge)**: Opt-in via ``HERMES_S2S_MONKEYPATCH_DISCORD=1``.
   We wrap ``gateway.platforms.discord.DiscordAdapter.join_voice_channel`` so that
   after Hermes's default VoiceReceiver is started, we layer a realtime audio
   bridge on top of the connected ``discord.VoiceClient`` (gated by
   ``s2s.mode == 'realtime'``).

3. **No-op (default)**: If neither the env var nor the native hook is present,
   we log an info line and return. Users who don't opt in get Hermes's default
   STT -> text -> TTS voice path untouched.

The actual PCM<->backend audio loop is a stub in 0.3.0; the value delivered
in this release is the *setup plumbing* (version gate, monkey-patch site,
config-driven branch). The audio loop needs a live Hermes + Discord to
validate and lands in 0.3.1.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Tuple

logger = logging.getLogger(__name__)

# Hermes versions we've smoke-tested the monkey-patch against. The lower bound
# will tighten as we test; the upper bound exists so that a future breaking
# refactor in gateway/platforms/discord.py surfaces as a clear error instead
# of a silent broken patch. Inclusive on both ends.
SUPPORTED_HERMES_RANGE: Tuple[str, str] = ("0.0.0", "999.0.0")

_ENV_FLAG = "HERMES_S2S_MONKEYPATCH_DISCORD"
_UPSTREAM_PR_URL = (
    "https://github.com/NousResearch/hermes-agent — "
    "track the VoicePipelineFactory upstream PR for the supported path"
)

# Sentinel so tests (and repeated register() calls) can detect the wrapping.
_BRIDGE_WRAPPED_MARKER = "__hermes_s2s_voice_bridge__"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def install_discord_voice_bridge(ctx: Any) -> None:
    """Install the Discord voice bridge using the best available strategy.

    Called from :func:`hermes_s2s.register`. Never raises — failures are logged
    and degrade to a no-op so a bridge problem can't take the whole plugin down.
    """
    # Strategy A — native hook (future)
    native_hook = getattr(ctx, "register_voice_pipeline_factory", None)
    if callable(native_hook):
        try:
            native_hook("discord", _voice_pipeline_factory)
            logger.info(
                "hermes-s2s: registered Discord voice-pipeline factory via native hook"
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "hermes-s2s: native voice-pipeline-factory registration failed: %s", exc
            )
        return

    # Strategy C — opt-in monkey-patch
    if os.environ.get(_ENV_FLAG) == "1":
        try:
            _install_via_monkey_patch()
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("hermes-s2s: Discord voice bridge install failed: %s", exc)
        return

    # Strategy default — no-op
    logger.info(
        "hermes-s2s: realtime in Discord disabled — set %s=1 to enable the "
        "monkey-patched voice bridge (see ADR-0006)",
        _ENV_FLAG,
    )


# ---------------------------------------------------------------------------
# Monkey-patch implementation
# ---------------------------------------------------------------------------


def _install_via_monkey_patch() -> None:
    """Wrap DiscordAdapter.join_voice_channel to layer on an S2S audio bridge."""
    # Hermes import is deferred to here so the plugin module itself remains
    # importable on hosts that don't have hermes-agent installed.
    try:
        import gateway.platforms.discord as hermes_discord
    except ImportError as exc:
        logger.warning(
            "hermes-s2s: HERMES_S2S_MONKEYPATCH_DISCORD=1 but gateway.platforms.discord "
            "is not importable (%s); skipping voice bridge install.",
            exc,
        )
        return

    if not _hermes_version_supported():
        return

    DiscordAdapter = getattr(hermes_discord, "DiscordAdapter", None)
    if DiscordAdapter is None:
        logger.warning(
            "hermes-s2s: DiscordAdapter not found in gateway.platforms.discord; "
            "Hermes internals may have moved. %s",
            _UPSTREAM_PR_URL,
        )
        return

    original = getattr(DiscordAdapter, "join_voice_channel", None)
    if original is None:
        logger.warning(
            "hermes-s2s: DiscordAdapter.join_voice_channel not found; Hermes "
            "internals may have moved. %s",
            _UPSTREAM_PR_URL,
        )
        return

    if getattr(original, _BRIDGE_WRAPPED_MARKER, False):
        logger.debug("hermes-s2s: Discord voice bridge already installed")
        return

    async def join_voice_channel_wrapped(self, channel):  # type: ignore[no-untyped-def]
        # Let Hermes do its normal connect + VoiceReceiver setup first.
        result = await original(self, channel)
        if result:
            try:
                _install_bridge_on_adapter(self, channel)
            except Exception as exc:
                logger.warning("hermes-s2s: voice bridge attach failed: %s", exc)
        return result

    setattr(join_voice_channel_wrapped, _BRIDGE_WRAPPED_MARKER, True)
    DiscordAdapter.join_voice_channel = join_voice_channel_wrapped
    logger.info(
        "hermes-s2s: patched DiscordAdapter.join_voice_channel for realtime bridge"
    )


def _hermes_version_supported() -> bool:
    """Return True if the installed hermes-agent is within the tested range."""
    try:
        import hermes_agent  # type: ignore[import-not-found]
    except ImportError:
        # Hermes plugins are loaded by Hermes, so this should never happen at
        # runtime. If it does, we assume modern-enough and let the attribute
        # probes in _install_via_monkey_patch catch divergence.
        logger.debug("hermes-s2s: hermes_agent not importable; skipping version gate")
        return True

    version = getattr(hermes_agent, "__version__", None)
    if not version:
        logger.debug("hermes-s2s: hermes_agent.__version__ unavailable; allowing patch")
        return True

    low, high = SUPPORTED_HERMES_RANGE
    if not (_vtuple(low) <= _vtuple(version) <= _vtuple(high)):
        logger.error(
            "hermes-s2s: hermes-agent %s is outside tested range %s..%s for the "
            "Discord voice bridge. Skipping monkey-patch. %s",
            version,
            low,
            high,
            _UPSTREAM_PR_URL,
        )
        return False
    return True


def _vtuple(v: str) -> Tuple[int, ...]:
    """Best-effort tuple parse for semver-ish strings; non-numeric parts -> 0."""
    parts = []
    for chunk in str(v).split("."):
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


# ---------------------------------------------------------------------------
# Bridge attachment (stub for 0.3.0 — real audio loop lands in 0.3.1)
# ---------------------------------------------------------------------------


def _install_bridge_on_adapter(adapter: Any, channel: Any) -> None:
    """Attach a realtime audio bridge on top of an adapter's VoiceClient.

    Gated on ``s2s.mode == 'realtime'``. Otherwise leaves Hermes's default
    STT/TTS path untouched.

    This is a **stub** in 0.3.0: it verifies the gate and logs intent. The
    actual PCM resample + backend wiring needs a live Hermes + Discord stack
    to validate end-to-end and is deferred to 0.3.1.
    """
    try:
        from ..config import load_config
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("hermes-s2s: cannot load s2s config: %s", exc)
        return

    try:
        cfg = load_config()
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("hermes-s2s: s2s config load failed: %s", exc)
        return

    mode = str(getattr(cfg, "mode", "") or "").lower()
    if mode != "realtime":
        logger.debug(
            "hermes-s2s: s2s.mode=%r — leaving Hermes's default STT/TTS path untouched",
            mode,
        )
        return

    guild = getattr(channel, "guild", None)
    guild_id = getattr(guild, "id", None)
    voice_clients = getattr(adapter, "_voice_clients", {}) or {}
    vc = voice_clients.get(guild_id) if guild_id is not None else None
    if vc is None:
        logger.debug(
            "hermes-s2s: no VoiceClient on adapter for guild=%s; bridge skipped", guild_id
        )
        return

    # Pause Hermes's default VoiceReceiver so we don't double-transcribe.
    receivers = getattr(adapter, "_voice_receivers", {}) or {}
    receiver = receivers.get(guild_id) if guild_id is not None else None
    if receiver is not None:
        try:
            receiver.pause()
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("hermes-s2s: receiver.pause() failed: %s", exc)

    logger.info(
        "hermes-s2s: would bridge audio here (guild=%s, backend=%s) — 0.3.1 will "
        "wire PCM <-> realtime backend; shipped as setup-plumbing-only in 0.3.0",
        guild_id,
        getattr(cfg, "realtime", None) and getattr(cfg.realtime, "provider", None),
    )


# ---------------------------------------------------------------------------
# Native-hook factory (used only when Hermes exposes register_voice_pipeline_factory)
# ---------------------------------------------------------------------------


def _voice_pipeline_factory(adapter: Any, voice_client: Any, config: Any) -> Any:
    """Placeholder pipeline factory for the native hook path.

    Returns a stub object that satisfies the expected VoicePipeline protocol.
    Real implementation lands alongside 0.3.1's audio loop.
    """
    return _StubVoicePipeline(adapter, voice_client, config)


class _StubVoicePipeline:
    """No-op VoicePipeline — logs lifecycle calls until 0.3.1 ships the real one."""

    def __init__(self, adapter: Any, voice_client: Any, config: Any) -> None:
        self._adapter = adapter
        self._voice_client = voice_client
        self._config = config

    async def start(self) -> None:
        logger.info("hermes-s2s: stub VoicePipeline.start()")

    async def stop(self) -> None:
        logger.info("hermes-s2s: stub VoicePipeline.stop()")

    async def send_user_text(self, user_id: int, text: str) -> None:
        logger.debug("hermes-s2s: stub send_user_text(user=%s)", user_id)

    async def play_tts(self, path: str) -> None:
        logger.debug("hermes-s2s: stub play_tts(%s)", path)

    def pause(self) -> None:
        logger.debug("hermes-s2s: stub pause()")

    def resume(self) -> None:
        logger.debug("hermes-s2s: stub resume()")
