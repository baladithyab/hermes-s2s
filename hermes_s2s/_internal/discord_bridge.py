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
from typing import Any, Tuple

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
        # Let Hermes do its normal connect + VoiceReceiver setup first.
        result = await original(self, channel)
        if result:
            try:
                _install_bridge_on_adapter(self, channel, ctx)
            except Exception as exc:
                logger.warning("hermes-s2s: voice bridge attach failed: %s", exc)
        return result

    setattr(join_voice_channel_wrapped, _BRIDGE_WRAPPED_MARKER, True)
    DiscordAdapter.join_voice_channel = join_voice_channel_wrapped

    # Wrap leave_voice_channel once per adapter class for bridge cleanup.
    _wrap_leave_voice_channel(DiscordAdapter)

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
# Bridge attachment — resolves adapter/vc/receiver then delegates to _attach_*
# ---------------------------------------------------------------------------


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

    Only runs when ``s2s.mode == 'realtime'``; cascaded mode is a no-op so
    Hermes's built-in STT->text->TTS path stays untouched.

    Flow (see ADR-0007 + wave-0.3.1 plan B3):
      1. Load s2s config; bail early if mode != 'realtime'.
      2. Resolve the realtime backend from the provider registry.
      3. Build a HermesToolBridge (if ctx exposes ``dispatch_tool``).
      4. Build a RealtimeAudioBridge wrapping both.
      5. Install a per-frame PCM callback on the VoiceReceiver (see spike notes
         at top of this module re: no public hook — we monkey-patch _on_packet).
      6. Replace whatever the VoiceClient is playing with a QueuedPCMSource fed
         from the bridge's outbound buffer.
      7. Schedule ``bridge.start()`` on the voice_client's event loop.
      8. Track the bridge on ``adapter._s2s_bridges[guild_id]`` for later cleanup.
    """
    # Lazy imports — these modules live under _internal/ and depend on the
    # optional realtime extras; keeping the import here means this module loads
    # cleanly even if audio_bridge/tool_bridge/discord_audio aren't installable.
    try:
        from ..config import load_config
        from ..registry import resolve_realtime
        from .audio_bridge import RealtimeAudioBridge
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

    mode = str(getattr(cfg, "mode", "") or "").lower()
    if mode != "realtime":
        logger.debug(
            "hermes-s2s: s2s.mode=%r — leaving Hermes's default STT/TTS path untouched",
            mode,
        )
        return

    # (b) Resolve backend
    try:
        backend = resolve_realtime(cfg.realtime_provider, cfg.realtime_options)
    except Exception as exc:
        logger.warning(
            "hermes-s2s: failed to resolve realtime backend %r: %s",
            getattr(cfg, "realtime_provider", "?"),
            exc,
        )
        return

    # (c) Tool bridge — only if Hermes ctx exposes dispatch_tool
    tool_bridge = None
    if ctx is not None and hasattr(ctx, "dispatch_tool"):
        try:
            tool_bridge = HermesToolBridge(dispatch_tool=ctx.dispatch_tool)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("hermes-s2s: tool bridge construction failed: %s", exc)
            tool_bridge = None

    # (d) Audio bridge
    # Pull system_prompt + voice via _resolve_bridge_params, which unwraps the
    # provider sub-block (realtime_options['gemini_live'] / 'openai') where
    # the wizard writes the anchored defaults. Outer-level read kept as a
    # back-compat fallback for pre-0.3.8 flat configs. See
    # docs/research/10-arabic-language-rootcause.md (Fix A).
    system_prompt, voice = _resolve_bridge_params(cfg)
    bridge = RealtimeAudioBridge(
        backend=backend,
        tool_bridge=tool_bridge,
        system_prompt=system_prompt,
        voice=voice,
        tools=[],
    )

    # (e) Frame callback — see SPIKE notes at top of module re: monkey-patch
    if voice_receiver is not None:
        try:
            _install_frame_callback(voice_receiver, bridge.on_user_frame)
        except Exception as exc:
            logger.warning(
                "hermes-s2s: failed to install frame callback on VoiceReceiver: %s", exc
            )

    # (f) Outbound audio — route bridge buffer to the VoiceClient
    try:
        if voice_client.is_playing():
            voice_client.stop()
        voice_client.play(QueuedPCMSource(bridge.buffer))
    except Exception as exc:
        logger.warning("hermes-s2s: voice_client.play(QueuedPCMSource) failed: %s", exc)

    # (g) Start the bridge loop on the VC's asyncio loop (thread-safe)
    try:
        loop = getattr(voice_client, "loop", None)
        if loop is not None:
            asyncio.run_coroutine_threadsafe(bridge.start(), loop)
        else:  # pragma: no cover — defensive (discord.VoiceClient always has .loop)
            logger.warning(
                "hermes-s2s: voice_client.loop is None — cannot start bridge"
            )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("hermes-s2s: scheduling bridge.start() failed: %s", exc)

    # (h) Track on adapter for cleanup
    bridges = getattr(adapter, "_s2s_bridges", None)
    if bridges is None:
        bridges = {}
        try:
            adapter._s2s_bridges = bridges
        except Exception:  # pragma: no cover — defensive
            pass
    guild = getattr(voice_client, "guild", None)
    guild_id = getattr(guild, "id", None)
    if guild_id is not None:
        bridges[guild_id] = bridge

    # (i) Silence Hermes core's cascaded voice loop while realtime is active.
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
        getattr(cfg, "realtime_provider", None),
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
