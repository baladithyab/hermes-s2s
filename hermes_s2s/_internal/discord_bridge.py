"""Discord voice bridge — routes Discord VC audio through a realtime S2S backend.

Strategy (see docs/adrs/0006-discord-voice-bridge.md and 0007-audio-bridge-frame-callback.md):

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

---------------------------------------------------------------------------
SPIKE — Hermes VoiceReceiver frame-callback availability (ADR-0007)
---------------------------------------------------------------------------
Reference: /home/codeseys/.hermes/hermes-agent/gateway/platforms/discord.py

Findings from the VoiceReceiver class (discord.py:128-477):

* **No `set_frame_callback` or any public per-frame hook exists.** The
  VoiceReceiver pushes decoded PCM into private per-SSRC bytearrays
  (``self._buffers[ssrc]``, discord.py:157/378) and only surfaces *completed
  utterances* via ``check_silence()`` (discord.py:414), which is polled on a
  ~1.5s silence timeout — far too coarse for realtime duplex audio.

* **Decoded PCM appears at discord.py:376** (``pcm = self._decoders[ssrc].decode(decrypted)``)
  inside the private ``_on_packet`` method (discord.py:250). That is the
  earliest point 48 kHz/stereo/s16 PCM is available, exactly one Opus frame
  (~20 ms) at a time, tagged with an ``ssrc`` that maps to a user via
  ``self._ssrc_to_user`` (discord.py:153).

* **Injection point chosen:** monkey-patch the instance's ``_on_packet``
  method. We wrap it so the original runs first (NaCl+DAVE decrypt + Opus
  decode + buffer append), then we diff the ``_buffers[ssrc]`` bytearray to
  extract just the fresh PCM bytes from this frame and forward
  ``(user_id, pcm_bytes)`` to our bridge callback. This keeps the existing
  silence-detection path working (cascaded mode keeps functioning if someone
  toggles back) and doesn't require re-implementing RTP/DAVE/Opus decode.

* This is a best-effort shim until Hermes exposes a proper
  ``set_frame_callback`` seam; tracked in the upstream PR linked below.
---------------------------------------------------------------------------
"""

from __future__ import annotations

import logging
import os
from functools import partial
from typing import Any, Optional, Tuple

from hermes_s2s.voice import (
    CapabilityError,
    ModeRouter,
    VoiceMode,
    VoiceSessionFactory,
)
from hermes_s2s.voice.threads import ThreadResolver
from hermes_s2s.voice.transcript import TranscriptMirror

logger = logging.getLogger(__name__)

# Hermes versions we've smoke-tested the monkey-patch against. The lower bound
# will tighten as we test; the upper bound exists so that a future breaking
# refactor in gateway/platforms/discord.py surfaces as a clear error instead
# of a silent broken patch. Inclusive on both ends.
SUPPORTED_HERMES_RANGE: Tuple[str, str] = ("0.0.0", "999.0.0")  # TODO: tighten upper bound after testing against Hermes 1.x release

_ENV_FLAG = "HERMES_S2S_MONKEYPATCH_DISCORD"
_UPSTREAM_PR_URL = (
    "https://github.com/NousResearch/hermes-agent — "
    "track the VoicePipelineFactory upstream PR for the supported path"
)

# Sentinel so tests (and repeated register() calls) can detect the wrapping.
_BRIDGE_WRAPPED_MARKER = "__hermes_s2s_voice_bridge__"
# Marker for the leave_voice_channel wrap so it's idempotent per adapter class.
_S2S_LEAVE_WRAPPED_MARKER = "__hermes_s2s_leave_wrapped__"
# Marker on a VoiceReceiver instance so we only install our frame shim once.
_FRAME_CB_INSTALLED_MARKER = "__hermes_s2s_frame_cb_installed__"
# Marker on the AIAgentRunner class so the runner-level monkey-patch that
# wires thread resolution into ``_handle_voice_channel_join`` is idempotent.
# See B6 fixup / research-14 §2 — runner site has the ``event`` object and
# runs BEFORE the source snapshot at ``adapter._voice_sources[guild_id] =
# event.source.to_dict()``, which is the correct place to mutate
# ``event.source.thread_id`` / ``chat_type`` so Hermes core routes the
# synthetic voice MessageEvents into the target thread.
_S2S_RUNNER_WRAPPED = "__hermes_s2s_runner_wrapped__"

# English-anchored default voice-assistant prompt. Mirrors cli.py's wizard
# default. Gemini Live native-audio models auto-detect language from input
# audio, so an explicit English anchor is the most reliable way to keep the
# assistant from slipping into another language when the first turn is quiet
# or accented (see docs/research/10-arabic-language-rootcause.md).
_DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful voice assistant.\n"
    "Always respond in English unless the user explicitly asks you to "
    "switch languages.\n"
    "Respond briefly and naturally — one to three sentences for casual "
    "conversation."
)


def _resolve_bridge_params(cfg: Any) -> Tuple[str, Any]:
    """Resolve ``(system_prompt, voice)`` for RealtimeAudioBridge from an S2SConfig.

    Mirrors ``doctor.py:_active_provider_block`` — looks up the
    provider-specific sub-block under ``realtime_options`` (e.g.
    ``realtime_options['gemini_live']``) and pulls ``system_prompt`` / ``voice``
    from there, with fallback to the outer ``realtime_options`` for
    back-compat with flat configs, and finally to the module default.
    """
    realtime_opts = getattr(cfg, "realtime_options", {}) or {}
    provider = (getattr(cfg, "realtime_provider", "") or "").lower()
    if "gemini" in provider:
        key = "gemini_live"
    elif "openai" in provider or "gpt-realtime" in provider:
        key = "openai"
    else:
        key = provider.replace("-", "_")
    provider_block = realtime_opts.get(key, {}) if isinstance(realtime_opts, dict) else {}
    if not isinstance(provider_block, dict):
        provider_block = {}
    system_prompt = provider_block.get(
        "system_prompt",
        realtime_opts.get("system_prompt", _DEFAULT_SYSTEM_PROMPT),
    )
    voice = provider_block.get("voice", realtime_opts.get("voice"))
    return system_prompt, voice


# ---------------------------------------------------------------------------
# CapabilityError rollback (Phase-8 P1-F7)
# ---------------------------------------------------------------------------


async def _handle_capability_error_rollback(
    adapter: Any, channel: Any, cap_exc: CapabilityError
) -> None:
    """Roll back a VC join when the factory rejected the mode.

    Called from the outermost wrapper of ``join_voice_channel`` when
    :func:`_install_bridge_on_adapter` raises :class:`CapabilityError`.
    Responsibilities:

    1. If the VoiceClient for this guild is connected, call
       ``voice_client.disconnect()`` to roll back.
    2. Post a user-friendly message to the adapter's bound text channel
       for this guild (``adapter._voice_text_channels[guild_id]``) when
       available; fall back to logging otherwise.
    3. NEVER re-raise — returning normally prevents the exception from
       propagating into discord.py's callback machinery.
    """
    guild = getattr(channel, "guild", None)
    guild_id = getattr(guild, "id", None)

    # Roll back the VC connection.
    voice_clients = getattr(adapter, "_voice_clients", {}) or {}
    vc = voice_clients.get(guild_id) if guild_id is not None else None
    if vc is not None:
        try:
            is_connected = getattr(vc, "is_connected", None)
            connected = bool(is_connected()) if callable(is_connected) else True
            if connected:
                disconnect = getattr(vc, "disconnect", None)
                if callable(disconnect):
                    try:
                        result = disconnect()
                        if hasattr(result, "__await__"):
                            await result
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.warning(
                            "hermes-s2s: voice_client.disconnect() raised during "
                            "CapabilityError rollback: %s",
                            exc,
                        )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(
                "hermes-s2s: unexpected error while rolling back VC: %s", exc
            )

    # Post user-friendly message to the bound text channel, if any.
    try:
        text_chans = getattr(adapter, "_voice_text_channels", {}) or {}
        chat_id = text_chans.get(guild_id) if guild_id is not None else None
        if chat_id is not None:
            sender = getattr(adapter, "_send_text", None) or getattr(
                adapter, "send_text", None
            )
            if callable(sender):
                try:
                    msg_result = sender(chat_id, cap_exc.user_message())
                    if hasattr(msg_result, "__await__"):
                        await msg_result
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug(
                        "hermes-s2s: failed to post CapabilityError message: %s", exc
                    )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("hermes-s2s: text-channel notify best-effort failed: %s", exc)

    logger.warning(
        "hermes-s2s: refused to attach bridge due to CapabilityError "
        "(mode=%s, missing=%s); VC rolled back. %s",
        cap_exc.mode.value,
        cap_exc.missing,
        cap_exc,
    )


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
            _install_via_monkey_patch(ctx)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("hermes-s2s: Discord voice bridge install failed: %s", exc)

        # W2a M2.1: install the plugin-owned /s2s slash command. Failure
        # here is non-fatal — the voice bridge itself works without it,
        # users just lose the mode-picker UX.
        try:
            from hermes_s2s.voice.slash import install_s2s_command

            install_s2s_command(ctx)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "hermes-s2s: /s2s slash command install failed (voice bridge "
                "still active, just no mode picker): %s",
                exc,
            )
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


def _install_via_monkey_patch(ctx: Any = None) -> None:
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
        # W3b M3.4 — resolve target thread + construct a TranscriptMirror
        # BEFORE the original join runs so Hermes core's runner sees the
        # thread context when it snapshots event.source. Best-effort:
        # failures are logged inside the helper and don't block the VC
        # join (voice still works; only the text-mirror is lost).
        try:
            await _resolve_and_mirror_thread(self, channel)
        except Exception as exc:  # noqa: BLE001 — never break voice join
            logger.warning(
                "hermes-s2s: pre-join thread resolution failed: %s", exc
            )

        # Let Hermes do its normal connect + VoiceReceiver setup.
        result = await original(self, channel)
        if result:
            try:
                _install_bridge_on_adapter(self, channel, ctx)
            except CapabilityError as cap_exc:
                # Phase-8 P1-F7: the factory refused to build because the
                # explicitly-requested mode is unavailable. Roll back the
                # VC join and post a user-friendly message; NEVER let
                # CapabilityError propagate into discord.py callbacks.
                await _handle_capability_error_rollback(self, channel, cap_exc)
            except Exception as exc:
                logger.warning("hermes-s2s: voice bridge attach failed: %s", exc)
        return result

    setattr(join_voice_channel_wrapped, _BRIDGE_WRAPPED_MARKER, True)
    DiscordAdapter.join_voice_channel = join_voice_channel_wrapped

    # Wrap leave_voice_channel once per adapter class for bridge cleanup.
    _wrap_leave_voice_channel(DiscordAdapter)

    # B6 fixup — wrap the runner's _handle_voice_channel_join too. This is
    # the authoritative site for thread resolution because it has the
    # ``event`` object and runs BEFORE the source snapshot at
    # ``adapter._voice_sources[guild_id] = event.source.to_dict()``. The
    # adapter-level ``_resolve_and_mirror_thread`` above is kept as a
    # defense-in-depth fallback (e.g. for programmatic joins that bypass
    # the runner, or future Hermes versions that move the site).
    _wrap_runner_handle_voice_channel_join()

    logger.info(
        "hermes-s2s: patched DiscordAdapter.join_voice_channel for realtime bridge"
    )


def _wrap_runner_handle_voice_channel_join() -> None:
    """Monkey-patch ``AIAgentRunner._handle_voice_channel_join`` for threads.

    The adapter-level wrap of ``join_voice_channel`` does not have access
    to the originating ``MessageEvent`` — Hermes core doesn't stash it on
    the adapter before invoking the adapter method. The runner method
    (``gateway/run.py:_handle_voice_channel_join``) DOES have the event,
    and — crucially — runs BEFORE the line that snapshots the source
    into ``adapter._voice_sources[guild_id]``. That snapshot is what
    Hermes uses to construct synthetic MessageEvents for subsequent
    voice utterances, so mutating ``event.source.thread_id`` /
    ``chat_type`` here is what actually routes transcripts into the
    correct thread.

    Runner method signature (verified against Hermes
    gateway/run.py:9125):

        async def _handle_voice_channel_join(self, event: MessageEvent) -> str: ...

    Success/failure is conveyed via the return string — success returns
    a message starting with "Joined voice channel **...**."; failures
    return strings starting with "Failed", "You need", "This command",
    "Not in", or "Voice channels are not supported". We treat any return
    that starts with "Joined" as success.

    Idempotent via :data:`_S2S_RUNNER_WRAPPED` marker on the class.
    """
    try:
        import gateway.run as hermes_run  # type: ignore[import-not-found]
    except ImportError as exc:
        logger.warning(
            "hermes-s2s: could not import gateway.run for runner-level thread "
            "patch (%s); adapter-level fallback will handle thread resolution "
            "but may miss cases where adapter._current_event is unset.",
            exc,
        )
        return

    Runner = getattr(hermes_run, "AIAgentRunner", None)
    if Runner is None:
        logger.warning(
            "hermes-s2s: AIAgentRunner not found in gateway.run; skipping "
            "runner-level thread patch. %s",
            _UPSTREAM_PR_URL,
        )
        return

    if getattr(Runner, _S2S_RUNNER_WRAPPED, False):
        logger.debug("hermes-s2s: runner-level thread patch already installed")
        return

    original_handler = getattr(Runner, "_handle_voice_channel_join", None)
    if original_handler is None:
        logger.warning(
            "hermes-s2s: AIAgentRunner._handle_voice_channel_join not found; "
            "Hermes internals may have moved. %s",
            _UPSTREAM_PR_URL,
        )
        return

    async def _handle_voice_channel_join_wrapped(self, event):  # type: ignore[no-untyped-def]
        # Let the original runner method do its job (permission checks,
        # adapter.join_voice_channel call, callback wiring, source
        # snapshot). If it succeeds, we mutate the snapshot in place so
        # future voice events inherit the thread context.
        result = await original_handler(self, event)

        # The runner returns a user-facing string. Success is the
        # "Joined voice channel" branch; everything else is a refusal
        # or failure and we skip thread resolution.
        if not (isinstance(result, str) and result.startswith("Joined voice channel")):
            return result

        try:
            adapter = self.adapters.get(event.source.platform)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "hermes-s2s: runner wrap could not resolve adapter: %s", exc
            )
            return result

        if adapter is None:
            return result

        # Re-resolve the voice channel to pass to ThreadResolver. The
        # runner already did this to call adapter.join_voice_channel;
        # we repeat it so ThreadResolver can inspect the parent channel.
        try:
            guild_id = self._get_guild_id(event)
        except Exception:  # noqa: BLE001
            guild_id = None

        voice_channel = None
        if guild_id is not None and hasattr(adapter, "get_user_voice_channel"):
            try:
                voice_channel = await adapter.get_user_voice_channel(
                    guild_id, event.source.user_id
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "hermes-s2s: runner wrap get_user_voice_channel failed: %s",
                    exc,
                )

        try:
            from ..config import load_config

            cfg = load_config()
            cfg_dict = cfg.dict() if hasattr(cfg, "dict") else {}
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug(
                "hermes-s2s: runner wrap config load failed (%s); "
                "using default thread-resolver config",
                exc,
            )
            cfg_dict = {}

        resolver = ThreadResolver(cfg_dict)
        try:
            thread_id = await resolver.resolve(adapter, event, voice_channel)
        except Exception as exc:  # noqa: BLE001 — never break voice join
            logger.warning(
                "hermes-s2s: runner wrap ThreadResolver.resolve failed: %s", exc
            )
            return result

        if thread_id is None:
            return result

        # Mutate event.source — this is the critical step. The snapshot
        # the runner just took at ``adapter._voice_sources[guild_id] =
        # event.source.to_dict()`` was BEFORE we ran. We have two jobs:
        #   (a) mutate the live event (for any logic reading it later);
        #   (b) re-snapshot into adapter._voice_sources so subsequent
        #       voice MessageEvents inherit the thread.
        source = getattr(event, "source", None)
        if source is not None:
            try:
                setattr(source, "thread_id", str(thread_id))
                setattr(source, "chat_type", "thread")
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "hermes-s2s: runner wrap could not mutate event.source "
                    "(possibly frozen): %s",
                    exc,
                )

        # Re-snapshot so voice utterances see the thread.
        voice_sources = getattr(adapter, "_voice_sources", None)
        if voice_sources is not None and guild_id is not None and source is not None:
            try:
                voice_sources[guild_id] = source.to_dict()
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "hermes-s2s: runner wrap could not re-snapshot source: %s",
                    exc,
                )

        # Stash a mirror too so the audio bridge / transcript wiring can
        # find it if it runs after this point.
        try:
            from hermes_s2s.voice.transcript import TranscriptMirror

            mirrors = getattr(adapter, "_s2s_transcript_mirrors", None)
            if mirrors is None:
                mirrors = {}
                try:
                    adapter._s2s_transcript_mirrors = mirrors
                except Exception:  # pragma: no cover
                    pass
            if guild_id is not None:
                mirrors[guild_id] = (TranscriptMirror(adapter), thread_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("hermes-s2s: runner wrap mirror stash failed: %s", exc)

        return result

    setattr(_handle_voice_channel_join_wrapped, _S2S_RUNNER_WRAPPED, True)
    Runner._handle_voice_channel_join = _handle_voice_channel_join_wrapped
    setattr(Runner, _S2S_RUNNER_WRAPPED, True)
    logger.info(
        "hermes-s2s: patched AIAgentRunner._handle_voice_channel_join for "
        "thread resolution (B6 fixup)"
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
# Bridge attachment — resolves adapter/vc/receiver then delegates to _attach_*
# ---------------------------------------------------------------------------


async def _resolve_and_mirror_thread(
    adapter: Any, channel: Any
) -> Optional[int]:
    """Resolve a target thread + stash a TranscriptMirror on the adapter.

    W3b M3.4 — called from the wrapped ``join_voice_channel`` BEFORE the
    original join runs so Hermes core's runner snapshots the thread
    context when it captures ``event.source``. Returns the resolved
    thread_id (int) or ``None`` if no thread could be resolved (forum
    parent, missing event, resolver failure).

    The helper is defensive in the face of missing adapter state —
    voice joins invoked outside of a normal message-event flow (e.g. a
    programmatic call) won't have a ``_current_event`` on the adapter,
    in which case we skip thread resolution entirely rather than crash.

    Side effects:
        * On success, mutates ``event.source.thread_id`` and
          ``event.source.chat_type``. ``SessionSource`` may be frozen
          in some Hermes versions; we use ``setattr`` defensively and
          catch the AttributeError-on-frozen-dataclass path.
        * Stashes a ``TranscriptMirror`` on
          ``adapter._s2s_transcript_mirrors`` keyed on ``guild_id`` so
          ``_attach_realtime_to_voice_client`` can wire the sink after
          the factory builds the session.
    """
    event = (
        getattr(adapter, "_current_event", None)
        or getattr(adapter, "_pending_voice_event", None)
    )
    if event is None:
        logger.debug(
            "hermes-s2s.discord_bridge: no current event on adapter; "
            "skipping thread resolution (voice still works, no mirror)"
        )
        return None

    try:
        from ..config import load_config

        cfg = load_config()
        cfg_dict = cfg.dict() if hasattr(cfg, "dict") else {}
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug(
            "hermes-s2s.discord_bridge: config load failed (%s); "
            "using default thread-resolver config",
            exc,
        )
        cfg_dict = {}

    resolver = ThreadResolver(cfg_dict)
    try:
        thread_id = await resolver.resolve(adapter, event, channel)
    except Exception as exc:  # noqa: BLE001 — never break voice join
        logger.warning(
            "hermes-s2s.discord_bridge: ThreadResolver.resolve failed: %s", exc
        )
        return None

    if thread_id is None:
        return None

    # Mutate event.source — defensive because SessionSource may be
    # frozen in some Hermes versions.
    source = getattr(event, "source", None)
    if source is not None:
        try:
            setattr(source, "thread_id", str(thread_id))
            setattr(source, "chat_type", "thread")
        except Exception as exc:  # noqa: BLE001 — frozen-dataclass tolerant
            logger.debug(
                "hermes-s2s.discord_bridge: could not mutate event.source "
                "(possibly frozen): %s",
                exc,
            )

    # Stash a mirror on the adapter keyed by guild for the attach step.
    mirror = TranscriptMirror(adapter)
    mirrors = getattr(adapter, "_s2s_transcript_mirrors", None)
    if mirrors is None:
        mirrors = {}
        try:
            adapter._s2s_transcript_mirrors = mirrors
        except Exception:  # pragma: no cover — defensive
            pass
    guild = getattr(channel, "guild", None)
    guild_id = getattr(guild, "id", None)
    if guild_id is not None:
        mirrors[guild_id] = (mirror, thread_id)

    return thread_id


def _install_bridge_on_adapter(adapter: Any, channel: Any, ctx: Any = None) -> None:
    """Locate the VoiceClient + VoiceReceiver for ``channel`` and attach a bridge.

    Thin wrapper around :func:`_attach_realtime_to_voice_client` that does the
    adapter-side lookup (``adapter._voice_clients[guild_id]`` + optional
    ``adapter._voice_receivers[guild_id]``). Kept separate so the real work
    in ``_attach_realtime_to_voice_client`` is trivially testable in isolation.
    """
    guild = getattr(channel, "guild", None)
    guild_id = getattr(guild, "id", None)
    voice_clients = getattr(adapter, "_voice_clients", {}) or {}
    voice_receivers = getattr(adapter, "_voice_receivers", {}) or {}
    vc = voice_clients.get(guild_id) if guild_id is not None else None
    if vc is None:
        logger.debug(
            "hermes-s2s: no VoiceClient on adapter for guild=%s; bridge skipped", guild_id
        )
        return
    receiver = voice_receivers.get(guild_id) if guild_id is not None else None
    _attach_realtime_to_voice_client(adapter, vc, receiver, ctx)


def _attach_realtime_to_voice_client(
    adapter: Any,
    voice_client: Any,
    voice_receiver: Any,
    ctx: Any,
) -> None:
    """Wire a :class:`RealtimeAudioBridge` between the Discord VC and the s2s backend.

    Only runs when the resolved :class:`VoiceMode` is ``REALTIME``;
    cascaded / pipeline / s2s-server modes are either no-ops here
    (cascaded) or handled by their own session lifecycle (pipeline /
    s2s-server) — for all non-realtime modes we still register the
    session on ``adapter._s2s_sessions`` via the factory so the leave-
    voice-channel wrapper can stop them cleanly.

    Flow (see ADR-0013 §3 + ADR-0007 + wave-0.3.1 plan B3):
      1. Load s2s config and resolve the target :class:`ModeSpec` via
         :class:`ModeRouter`. This supports both the back-compat
         ``s2s.mode`` key (legacy) and the new ``s2s.voice.default_mode``
         key the wizard writes.
      2. Build a HermesToolBridge (if ctx exposes ``dispatch_tool``).
      3. Delegate construction to :class:`VoiceSessionFactory`.
         CapabilityError propagates out so the outer wrapper rolls back
         the VC join (Phase-8 P1-F7).
      4. If the resulting session is a RealtimeSession, install the
         frame callback + QueuedPCMSource + silence cascaded STT. Other
         session types are left to their own ``start()`` to do mode-
         specific setup.
      5. Schedule ``session.start()`` on the voice_client's event loop.
      6. Track the bridge on ``adapter._s2s_bridges[guild_id]`` for
         legacy leave-voice-channel cleanup (preserved for back-compat).

    v0.3.9 REGRESSION FENCE: the factory's RealtimeSession path calls
    :func:`_resolve_bridge_params` internally to unwrap the provider
    sub-block in ``realtime_options``. See ``tests/test_config_unwrap.py``.
    """
    # Lazy imports — these modules live under _internal/ and depend on the
    # optional realtime extras; keeping the import here means this module loads
    # cleanly even if audio_bridge/tool_bridge/discord_audio aren't installable.
    try:
        from ..config import load_config
        from .. import registry as registry_mod
        from .tool_bridge import HermesToolBridge
        from .discord_audio import QueuedPCMSource
        import asyncio
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "hermes-s2s: realtime bridge imports failed (%s); leaving built-in voice in place",
            exc,
        )
        return

    try:
        cfg = load_config()
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("hermes-s2s: s2s config load failed: %s", exc)
        return

    # Build a ModeRouter config dict that supports both the back-compat
    # ``s2s.mode`` key AND the new ``s2s.voice.default_mode`` key. The
    # legacy string carries through as the default_mode fallback so
    # existing 0.3.x deployments keep behaving identically.
    legacy_mode = str(getattr(cfg, "mode", "") or "").strip().lower()
    router_cfg: dict = {"s2s": {"voice": {}}}
    # Merge any new-style s2s.voice.* config if the operator wrote it.
    voice_cfg_obj = getattr(cfg, "voice", None)
    if isinstance(voice_cfg_obj, dict):
        router_cfg["s2s"]["voice"].update(voice_cfg_obj)
    # Legacy s2s.mode wins only if voice.default_mode wasn't set.
    if legacy_mode and "default_mode" not in router_cfg["s2s"]["voice"]:
        router_cfg["s2s"]["voice"]["default_mode"] = legacy_mode
    # Preserve provider info for realtime capability gating.
    realtime_provider = getattr(cfg, "realtime_provider", None)
    if realtime_provider:
        router_cfg["s2s"]["voice"].setdefault("provider", realtime_provider)

    guild = getattr(voice_client, "guild", None)
    guild_id = getattr(guild, "id", None)
    channel_obj = getattr(voice_client, "channel", None)
    channel_id = getattr(channel_obj, "id", None)

    # W2a M2.1 part 3: fold the /s2s override store into the router's
    # ``channel_overrides`` (precedence level 3). The store is the
    # canonical persistence for per-channel picks made via the
    # plugin-owned /s2s slash command. Reload on every join so another
    # process (e.g. a fresh gateway start) writing an override while we
    # were idle still takes effect. Failure is non-fatal — we just lose
    # the override for this join.
    try:
        from ..voice.slash import get_default_store

        store = get_default_store()
        store.reload()
        if guild_id is not None and channel_id is not None:
            override = store.get(int(guild_id), int(channel_id))
            if override:
                router_cfg["s2s"]["voice"].setdefault(
                    "channel_overrides", {}
                )[channel_id] = override
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "hermes-s2s: override-store lookup failed (%s); continuing "
            "without /s2s override",
            exc,
        )

    try:
        router = ModeRouter(router_cfg)
        spec = router.resolve(guild_id=guild_id, channel_id=channel_id)
    except Exception as exc:
        logger.warning("hermes-s2s: ModeRouter.resolve() failed: %s", exc)
        return

    # For cascaded mode, we intentionally don't install a realtime bridge —
    # Hermes core's native STT/TTS path owns the conversation. Still, we
    # want the factory to register a CascadedSession so observability +
    # leave-voice-channel cleanup are symmetric across modes.
    if spec.mode is VoiceMode.CASCADED:
        logger.debug(
            "hermes-s2s: voice mode=%s — leaving Hermes's default STT/TTS path "
            "untouched (will still register CascadedSession for observability)",
            spec.mode.value,
        )
        try:
            factory = VoiceSessionFactory(registry=registry_mod)
            factory.build(spec, voice_client, adapter, ctx)
        except CapabilityError:
            # Cascaded has no capability requirements; this should be
            # unreachable but propagate so the outer wrapper can log.
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "hermes-s2s: CascadedSession registration failed: %s", exc
            )
        return

    # (b) Tool bridge — only if Hermes ctx exposes dispatch_tool
    tool_bridge = None
    if ctx is not None and hasattr(ctx, "dispatch_tool"):
        try:
            tool_bridge = HermesToolBridge(dispatch_tool=ctx.dispatch_tool)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("hermes-s2s: tool bridge construction failed: %s", exc)
            tool_bridge = None

    # (c) Delegate construction to the factory. The factory:
    #     - runs the capability gate (raises CapabilityError for
    #       explicit-but-unavailable modes — caught by the outer wrapper);
    #     - resolves the realtime backend via the registry;
    #     - calls _resolve_bridge_params() to preserve the v0.3.9 wizard-
    #       nested-config unwrap behavior;
    #     - eagerly constructs a RealtimeAudioBridge so we can reach
    #       session._bridge below for frame-callback / PCM-source wiring.

    # 0.4.2 S2: fetch prior thread history so the realtime session has
    # context. Best-effort — voice still boots if history fetch fails.
    # This MUST run before factory.build so spec.options["history"] is
    # available when RealtimeSession._on_start passes ConnectOptions to
    # the backend. See research/18 + plan-v2 §S2.
    try:
        from ..config.realtime_config import RealtimeConfig
        from .history import (
            build_history_payload,
            find_most_recent_thread_session_id,
            get_or_create_adapter_session_db,
            resolve_session_id_for_thread,
        )

        # Find the thread we're attached to (set by ThreadResolver
        # earlier in the join flow; mirror table maintained on adapter).
        # NOTE: ThreadResolver writes mirrors[guild_id] = (TranscriptMirror,
        # thread_id) — a TUPLE. Earlier draft assumed dict shape; v0.4.2.1
        # fix unpacks the tuple correctly.
        mirrors = getattr(adapter, "_s2s_transcript_mirrors", None) or {}
        thread_id = None
        if isinstance(mirrors, dict) and guild_id is not None:
            entry = mirrors.get(guild_id)
            if isinstance(entry, tuple) and len(entry) >= 2:
                thread_id = entry[1]  # (TranscriptMirror, thread_id)
            elif isinstance(entry, dict):
                thread_id = entry.get("thread_id") or entry.get("channel_id")
            elif isinstance(entry, int):
                thread_id = entry

        # Read the user's history config (typed envelope).
        try:
            rt_cfg = RealtimeConfig.from_full_config(
                {"s2s": {"voice": {"realtime": getattr(cfg, "voice_realtime", {}) or {}}}}
            )
        except Exception:  # noqa: BLE001
            rt_cfg = RealtimeConfig()

        history_payload: list = []
        session_id: Optional[str] = None  # for logging

        if rt_cfg.history.enabled:
            session_db = get_or_create_adapter_session_db(adapter)
            if session_db is not None:
                # First try: synthesized lookup from explicit thread_id.
                # NOTE on user_id: thread keys ignore user_id under
                # thread_sessions_per_user=False (default), so what we
                # pass doesn't matter for the lookup. We pass thread_id
                # for symmetry with the existing key shape.
                if thread_id is not None:
                    session_id = resolve_session_id_for_thread(
                        adapter,
                        thread_id=int(thread_id),
                        user_id=int(thread_id),
                    )

                # 0.4.4 fallback: if the explicit lookup found nothing
                # (joined VC from a non-thread channel, or no
                # ThreadResolver match), scan _entries for the most
                # recently-updated thread session in this platform/guild.
                # This is the "I was just chatting in another thread,
                # remember that" UX path — common for users who bounce
                # between text-thread and voice-channel UI.
                if session_id is None:
                    found = find_most_recent_thread_session_id(
                        adapter,
                        user_id=None,
                        guild_id=guild_id,
                        platform="discord",
                        max_age_hours=24.0,
                    )
                    if found is not None:
                        thread_id_resolved, session_id = found
                        if thread_id is None:
                            thread_id = thread_id_resolved
                        logger.info(
                            "hermes-s2s: history fallback — using most "
                            "recently-updated thread session: "
                            "thread_id=%s session_id=%s",
                            thread_id_resolved,
                            session_id,
                        )

                if session_id:
                    history_payload = build_history_payload(
                        session_db,
                        session_id,
                        max_turns=rt_cfg.history.max_turns,
                        max_tokens=rt_cfg.history.max_tokens,
                    )
                    if history_payload:
                        logger.info(
                            "hermes-s2s: realtime history loaded — "
                            "thread_id=%s session_id=%s turns=%d",
                            thread_id,
                            session_id,
                            len(history_payload),
                        )
                    else:
                        logger.info(
                            "hermes-s2s: history empty for session_id=%s "
                            "(thread is fresh or all turns filtered)",
                            session_id,
                        )
                else:
                    logger.info(
                        "hermes-s2s: no session_id resolved (thread_id=%s) — "
                        "voice will start fresh",
                        thread_id,
                    )

        # Stash on spec.options. The factory + RealtimeSession forward
        # this to backend.connect via ConnectOptions.history.
        new_options = dict(getattr(spec, "options", {}) or {})
        new_options["history"] = history_payload
        # ModeSpec is a dataclass; rebuild with merged options.
        from ..voice.modes import ModeSpec  # local import — circular safe

        spec = ModeSpec(
            mode=spec.mode,
            provider=spec.provider,
            options=new_options,
        )
    except Exception as exc:  # noqa: BLE001 - history is best-effort
        # Promoted from debug to warning: silent debug-level swallow
        # was the reason 0.4.2 first install showed no history. If
        # this fires we want to see it in default-level logs.
        logger.warning(
            "hermes-s2s: history fetch failed (%s); voice will start "
            "without text context",
            exc,
            exc_info=True,
        )

    factory = VoiceSessionFactory(registry=registry_mod, tool_bridge=tool_bridge)
    # CapabilityError intentionally propagates to the outer wrapper.
    session = factory.build(spec, voice_client, adapter, ctx)

    # Non-realtime sessions (pipeline / s2s-server) — their _on_start
    # does the mode-specific setup. Schedule start() and return.
    if spec.mode is not VoiceMode.REALTIME:
        try:
            loop = getattr(voice_client, "loop", None)
            if loop is not None:
                asyncio.run_coroutine_threadsafe(session.start(), loop)
            else:  # pragma: no cover - defensive
                logger.warning(
                    "hermes-s2s: voice_client.loop is None — cannot start %s session",
                    spec.mode.value,
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "hermes-s2s: scheduling %s session.start() failed: %s",
                spec.mode.value,
                exc,
            )
        return

    # --- realtime-mode-specific glue below (frame cb + PCM source + TTS silencing) ---

    bridge = getattr(session, "_bridge", None)
    if bridge is None:
        logger.warning(
            "hermes-s2s: RealtimeSession has no _bridge — backend may be "
            "unresolved; skipping frame/PCM wiring"
        )
        return

    # (d) Frame callback — see SPIKE notes at top of module re: monkey-patch
    if voice_receiver is not None:
        try:
            _install_frame_callback(voice_receiver, bridge.on_user_frame)
        except Exception as exc:
            logger.warning(
                "hermes-s2s: failed to install frame callback on VoiceReceiver: %s", exc
            )

    # (d.5) W3b M3.4 — if a TranscriptMirror was stashed for this guild
    # during the pre-join resolver step, install it as the bridge's
    # transcript sink. The mirror's schedule_send is a SYNC callable
    # that handles loop-scheduling internally, matching the bridge's
    # `sink(*, role, text, final)` contract.
    try:
        mirrors = getattr(adapter, "_s2s_transcript_mirrors", None) or {}
        slot = mirrors.get(guild_id) if guild_id is not None else None
        if slot is not None:
            mirror, thread_id = slot
            bridge._transcript_sink = partial(
                mirror.schedule_send, channel_id=int(thread_id)
            )
            logger.info(
                "hermes-s2s: transcript mirror wired (guild=%s, thread=%s)",
                guild_id,
                thread_id,
            )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "hermes-s2s: failed to wire transcript mirror: %s", exc
        )

    # (e) Outbound audio — route bridge buffer to the VoiceClient
    try:
        if voice_client.is_playing():
            voice_client.stop()
        voice_client.play(QueuedPCMSource(bridge.buffer))
    except Exception as exc:
        logger.warning("hermes-s2s: voice_client.play(QueuedPCMSource) failed: %s", exc)

    # (f) Start the session AND the bridge on the VC's asyncio loop.
    #
    # CRITICAL (v0.4.1 hotfix): RealtimeSession._on_start() runs the
    # connect-before-pumps regression fence at the session boundary, but
    # the SESSION's own pump tasks are no-op shims (sleep-3600). The
    # actual production audio pump lives in RealtimeAudioBridge.start()
    # which spawns _pump_input / _pump_output (audio_bridge.py:458-459).
    # In v0.4.0 we forgot to invoke bridge.start() after session.start(),
    # so frames piled up in BridgeBuffer's input queue (capacity 50) and
    # dropped — exactly the "100 input frames dropped" symptom seen in
    # production. This was a real regression vs v0.3.x where the inline
    # construction called bridge.start() directly.
    #
    # Order matters: session.start() awaits backend.connect() first
    # (regression fence). bridge.start() ALSO awaits backend.connect() —
    # but RealtimeAudioBridge.start() is idempotent (early-return when
    # self._task is not None and not done), AND backend connect() is also
    # safe to call twice on a live WS. The redundant connect is the cost
    # of keeping both layers' fence-tests independent. A future tidy-up
    # is to teach the session to invoke bridge.start() directly and have
    # the bridge skip its connect when it sees an already-connected
    # backend; tracked as 0.4.2 cleanup.
    async def _start_session_and_bridge() -> None:
        await session.start()
        try:
            await bridge.start()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes-s2s: bridge.start() failed (frames will not flow "
                "to backend): %s",
                exc,
            )

    try:
        loop = getattr(voice_client, "loop", None)
        if loop is not None:
            asyncio.run_coroutine_threadsafe(_start_session_and_bridge(), loop)
        else:  # pragma: no cover — defensive
            logger.warning(
                "hermes-s2s: voice_client.loop is None — cannot start session"
            )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "hermes-s2s: scheduling session+bridge start failed: %s", exc
        )

    # (g) Track bridge on adapter for legacy leave-voice-channel cleanup.
    # This dict is still consumed by _wrap_leave_voice_channel below.
    bridges = getattr(adapter, "_s2s_bridges", None)
    if bridges is None:
        bridges = {}
        try:
            adapter._s2s_bridges = bridges
        except Exception:  # pragma: no cover — defensive
            pass
    if guild_id is not None:
        bridges[guild_id] = bridge

    # (h) Silence Hermes core's cascaded voice loop while realtime is active.
    # Hermes core wires:
    #   adapter._voice_input_callback = self._handle_voice_channel_input
    # which fires faster-whisper STT every time the VoiceReceiver flushes a
    # speech buffer, then injects the transcript as a text turn — duplicating
    # what our realtime backend is already doing, and on this user's GPU
    # spamming "cuBLAS failed" errors. Stash the original and replace with a
    # no-op for the lifetime of this bridge; restored in leave_voice_channel.
    try:
        prev_input_cb = getattr(adapter, "_voice_input_callback", None)
        if prev_input_cb is not None and not getattr(prev_input_cb, "_s2s_silenced", False):
            async def _s2s_silenced_input_callback(*_args, **_kwargs):
                """No-op: realtime backend handles this user's audio directly."""
                return None
            _s2s_silenced_input_callback._s2s_silenced = True  # type: ignore[attr-defined]
            _s2s_silenced_input_callback._s2s_prev = prev_input_cb  # type: ignore[attr-defined]
            adapter._voice_input_callback = _s2s_silenced_input_callback
            logger.info(
                "hermes-s2s: silenced cascaded voice STT (was %s); realtime backend owns audio now",
                getattr(prev_input_cb, "__qualname__", repr(prev_input_cb)),
            )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("hermes-s2s: could not silence _voice_input_callback: %s", exc)

    # Also disable Hermes core's auto-TTS path for this chat so it doesn't
    # try to TTS-play the agent's text reply (which would conflict with the
    # realtime backend's already-streaming audio). This is best-effort —
    # different Hermes versions expose the toggle differently.
    try:
        text_chans = getattr(adapter, "_voice_text_channels", {}) or {}
        chat_id = text_chans.get(guild_id)
        if chat_id is not None:
            disable = getattr(adapter, "_set_auto_tts_enabled", None)
            if callable(disable):
                disable(chat_id, False)
                logger.info(
                    "hermes-s2s: disabled cascaded auto-TTS for chat=%s (realtime backend owns audio out)",
                    chat_id,
                )
            else:
                # Fallback: flip the dict directly if it exists.
                auto_tts = getattr(adapter, "_auto_tts_enabled", None)
                if isinstance(auto_tts, dict):
                    auto_tts[str(chat_id)] = False
                    logger.info(
                        "hermes-s2s: disabled cascaded auto-TTS via dict for chat=%s",
                        chat_id,
                    )
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("hermes-s2s: cascaded auto-TTS disable best-effort failed: %s", exc)

    logger.info(
        "hermes-s2s: realtime audio bridge attached (guild=%s, backend=%s)",
        guild_id,
        realtime_provider,
    )


def _install_frame_callback(voice_receiver: Any, callback: Any) -> None:
    """Route per-Opus-frame decoded PCM from Hermes's VoiceReceiver to ``callback``.

    Preferred path: if the receiver exposes a public ``set_frame_callback``
    method, use it. (Target state once Hermes ships the upstream seam.)

    Fallback (current state — see spike notes at top of module): monkey-patch
    the instance's ``_on_packet`` method. We wrap it so the original runs
    first (RTP/NaCl/DAVE decrypt + Opus decode + buffer append at
    discord.py:376-378), then compare ``_buffers[ssrc]`` length before/after
    to extract the PCM bytes this frame just appended, resolve the user_id
    via ``_ssrc_to_user``, and hand ``(user_id, pcm)`` to our callback.

    Idempotent: marks the receiver with ``_FRAME_CB_INSTALLED_MARKER`` so
    repeated installs just swap the callback without stacking wrappers.
    """
    # Preferred — public hook (once Hermes exposes it).
    setter = getattr(voice_receiver, "set_frame_callback", None)
    if callable(setter):
        setter(callback)
        logger.info("hermes-s2s: installed frame callback via set_frame_callback()")
        return

    # Already installed once — just swap the callback slot.
    if getattr(voice_receiver, _FRAME_CB_INSTALLED_MARKER, False):
        try:
            voice_receiver._hermes_s2s_frame_cb = callback
            logger.debug("hermes-s2s: frame callback swapped in existing shim")
        except Exception:  # pragma: no cover — defensive
            pass
        return

    # Fallback — monkey-patch _on_packet on this instance.
    original_on_packet = getattr(voice_receiver, "_on_packet", None)
    if not callable(original_on_packet):
        logger.warning(
            "hermes-s2s: VoiceReceiver has neither set_frame_callback nor _on_packet; "
            "cannot capture decoded PCM frames. %s",
            _UPSTREAM_PR_URL,
        )
        return

    try:
        voice_receiver._hermes_s2s_frame_cb = callback
    except Exception:  # pragma: no cover — defensive
        logger.warning("hermes-s2s: could not attach callback slot on receiver")
        return

    # Snapshot buffer lengths so the first frame delta is correct even if the
    # buffers already contain pre-bridge audio.
    _prev_lens: dict = {}

    def wrapped_on_packet(data: bytes, _orig=original_on_packet, _rcv=voice_receiver) -> None:
        # Snapshot length per SSRC before the decode, run the original, then
        # diff to extract just the PCM appended by this packet.
        buffers = getattr(_rcv, "_buffers", None)
        before: dict = {}
        if buffers is not None:
            try:
                for ssrc, buf in list(buffers.items()):
                    before[ssrc] = len(buf)
            except Exception:  # pragma: no cover — defensive
                before = {}

        _orig(data)

        cb = getattr(_rcv, "_hermes_s2s_frame_cb", None)
        if not callable(cb) or buffers is None:
            return
        ssrc_map = getattr(_rcv, "_ssrc_to_user", {}) or {}
        try:
            for ssrc, buf in list(buffers.items()):
                # Fall back to 0 (NOT len(buf)) so the first frame for a
                # brand-new SSRC produces the full buffer as the diff —
                # otherwise a new user joining mid-call would silently
                # drop their first decoded frame.
                prev = before.get(ssrc, _prev_lens.get(ssrc, 0))
                _prev_lens[ssrc] = len(buf)
                if len(buf) <= prev:
                    continue
                pcm_frame = bytes(buf[prev:])
                user_id = ssrc_map.get(ssrc, 0) or 0
                try:
                    cb(user_id, pcm_frame)
                except Exception as exc:  # pragma: no cover — defensive
                    logger.debug(
                        "hermes-s2s: frame callback raised for ssrc=%s: %s", ssrc, exc
                    )
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("hermes-s2s: frame-callback shim error: %s", exc)

    try:
        voice_receiver._on_packet = wrapped_on_packet  # type: ignore[attr-defined]
        setattr(voice_receiver, _FRAME_CB_INSTALLED_MARKER, True)
        # CRITICAL: Hermes core's VoiceReceiver registers `self._on_packet`
        # as a socket listener at start() time (gateway/platforms/discord.py:181).
        # That registration captures the BOUND METHOD by reference — setting
        # `voice_receiver._on_packet = wrapped_on_packet` only shadows the
        # attribute; it does NOT replace the registered listener. So unless we
        # swap the listener too, the wrapper is never called and our frame
        # callback never fires (frames_emitted stays at 0 forever — the v0.3.6
        # bridge-stats heartbeat made this glaringly obvious).
        try:
            vc = getattr(voice_receiver, "_vc", None)
            conn = getattr(vc, "_connection", None) if vc is not None else None
            if conn is not None:
                # Remove the originally-registered bound method, then add the
                # wrapper. add_socket_listener accepts any callable.
                try:
                    conn.remove_socket_listener(original_on_packet)
                except Exception as exc:  # pragma: no cover — defensive
                    logger.debug(
                        "hermes-s2s: remove_socket_listener(original) failed: %s",
                        exc,
                    )
                try:
                    conn.add_socket_listener(wrapped_on_packet)
                    setattr(
                        voice_receiver,
                        "_hermes_s2s_socket_listener",
                        wrapped_on_packet,
                    )
                    setattr(
                        voice_receiver,
                        "_hermes_s2s_original_on_packet",
                        original_on_packet,
                    )
                    logger.info(
                        "hermes-s2s: swapped socket listener to wrapped _on_packet "
                        "(frames will now reach the bridge)"
                    )
                except Exception as exc:  # pragma: no cover — defensive
                    logger.warning(
                        "hermes-s2s: add_socket_listener(wrapper) failed; "
                        "frame callback will not fire: %s",
                        exc,
                    )
            else:
                logger.warning(
                    "hermes-s2s: could not access voice connection to swap "
                    "socket listener — frames may not reach the bridge"
                )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "hermes-s2s: failed to swap socket listener: %s", exc
            )
        logger.warning(
            "hermes-s2s: VoiceReceiver has no set_frame_callback; installed "
            "per-frame shim via monkey-patched _on_packet (see ADR-0007)."
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("hermes-s2s: failed to monkey-patch _on_packet: %s", exc)


def _wrap_leave_voice_channel(DiscordAdapter: Any) -> None:
    """Wrap ``DiscordAdapter.leave_voice_channel`` once to close bridges on leave.

    Idempotent: marks the wrapped method with ``_S2S_LEAVE_WRAPPED_MARKER`` so
    subsequent calls to :func:`_install_via_monkey_patch` don't double-wrap.
    """
    original = getattr(DiscordAdapter, "leave_voice_channel", None)
    if original is None:
        logger.debug(
            "hermes-s2s: DiscordAdapter.leave_voice_channel not found; skipping cleanup wrap"
        )
        return
    if getattr(original, _S2S_LEAVE_WRAPPED_MARKER, False):
        return

    async def leave_voice_channel_wrapped(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        # Figure out which guild we're leaving so we can close the matching bridge.
        guild_id = None
        if args:
            first = args[0]
            # Common shapes: a guild object, a channel, or a raw id.
            guild_id = getattr(getattr(first, "guild", None), "id", None)
            if guild_id is None:
                guild_id = getattr(first, "id", None)
            if guild_id is None and isinstance(first, int):
                guild_id = first
        bridges = getattr(self, "_s2s_bridges", None) or {}
        bridge = bridges.pop(guild_id, None) if guild_id is not None else None
        # Fallback: if we couldn't pinpoint the guild but there's only one
        # bridge, close it (the common single-guild dev case).
        if bridge is None and len(bridges) == 1:
            _, bridge = bridges.popitem()
        if bridge is not None:
            try:
                close = getattr(bridge, "close", None)
                if callable(close):
                    res = close()
                    # close() may be sync or async; await if it's a coroutine.
                    import inspect
                    if inspect.isawaitable(res):
                        await res
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("hermes-s2s: bridge.close() failed: %s", exc)

        # P0-6: stop any S2S sessions registered for this guild on
        # ``adapter._s2s_sessions``. Non-realtime modes (pipeline,
        # s2s-server) track their session here so that /voice leave
        # can shut them down cleanly — before P0-6 the sessions were
        # orphaned when the user left the VC and kept holding their
        # WS / subprocess resources open (see P0-6 in the 0.4.0 plan).
        sessions_dict = getattr(self, "_s2s_sessions", None)
        if sessions_dict:
            for key in list(sessions_dict.keys()):
                # Keys are (guild_id, channel_id) tuples; tolerate
                # scalar keys too, for forwards-compat.
                try:
                    gid = key[0] if isinstance(key, tuple) else key
                except Exception:  # pragma: no cover — defensive
                    gid = None
                if guild_id is not None and gid != guild_id:
                    continue
                session = sessions_dict.pop(key, None)
                if session is None:
                    continue
                stop = getattr(session, "stop", None)
                if not callable(stop):
                    continue
                try:
                    res = stop()
                    import inspect as _inspect
                    if _inspect.isawaitable(res):
                        await res
                except Exception as exc:  # pragma: no cover — defensive
                    logger.warning(
                        "hermes-s2s: session.stop() failed: %s", exc
                    )

        # Restore Hermes core's silenced cascaded callback (mirror of step (i)
        # in _attach_realtime_to_voice_client). If the user re-joins in
        # cascaded mode after this, the original STT path takes over again.
        try:
            cb = getattr(self, "_voice_input_callback", None)
            prev = getattr(cb, "_s2s_prev", None)
            if cb is not None and getattr(cb, "_s2s_silenced", False):
                self._voice_input_callback = prev
                logger.info(
                    "hermes-s2s: restored cascaded voice STT callback (was silenced)"
                )
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("hermes-s2s: restore _voice_input_callback failed: %s", exc)

        # Restore the original socket listener on the VoiceReceiver. We swapped
        # it during _install_frame_callback; if we leave the wrapper in place
        # after the bridge is gone, packets keep flowing through a wrapper
        # whose frame-callback is dangling. Best-effort — receiver may already
        # be torn down.
        try:
            receivers = getattr(self, "_voice_receivers", None) or {}
            receiver = None
            if guild_id is not None:
                receiver = receivers.get(guild_id)
            if receiver is None and len(receivers) == 1:
                # Same single-guild fallback as the bridge lookup above.
                receiver = next(iter(receivers.values()))
            if receiver is not None:
                wrapper = getattr(receiver, "_hermes_s2s_socket_listener", None)
                orig_on_packet = getattr(
                    receiver, "_hermes_s2s_original_on_packet", None
                )
                vc = getattr(receiver, "_vc", None)
                conn = getattr(vc, "_connection", None) if vc is not None else None
                if conn is not None and wrapper is not None:
                    try:
                        conn.remove_socket_listener(wrapper)
                    except Exception:  # pragma: no cover — defensive
                        pass
                    if callable(orig_on_packet):
                        try:
                            conn.add_socket_listener(orig_on_packet)
                        except Exception:  # pragma: no cover — defensive
                            pass
                # Drop our markers/refs so a future attach starts clean.
                for attr in (
                    "_hermes_s2s_socket_listener",
                    "_hermes_s2s_original_on_packet",
                    "_hermes_s2s_frame_cb",
                    _FRAME_CB_INSTALLED_MARKER,
                ):
                    try:
                        if hasattr(receiver, attr):
                            delattr(receiver, attr)
                    except Exception:  # pragma: no cover — defensive
                        pass
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug(
                "hermes-s2s: socket-listener restoration failed: %s", exc
            )

        return await original(self, *args, **kwargs)

    setattr(leave_voice_channel_wrapped, _S2S_LEAVE_WRAPPED_MARKER, True)
    DiscordAdapter.leave_voice_channel = leave_voice_channel_wrapped
    logger.debug(
        "hermes-s2s: patched DiscordAdapter.leave_voice_channel for bridge cleanup"
    )


# ---------------------------------------------------------------------------
# Native-hook factory (used only when Hermes exposes register_voice_pipeline_factory)
# ---------------------------------------------------------------------------


def _voice_pipeline_factory(adapter: Any, voice_client: Any, config: Any) -> Any:
    """Placeholder pipeline factory for the native hook path.

    Returns a stub object that satisfies the expected VoicePipeline protocol.
    Real implementation lands alongside the native-hook path (post-0.3.1 /
    whenever Hermes ships ``register_voice_pipeline_factory``).
    """
    return _StubVoicePipeline(adapter, voice_client, config)


class _StubVoicePipeline:
    """No-op VoicePipeline — logs lifecycle calls until the native-hook real one lands."""

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
