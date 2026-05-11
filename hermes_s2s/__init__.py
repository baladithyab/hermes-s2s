"""hermes-s2s — Configurable speech-to-speech backends for Hermes Agent.

Three top-level modes — `cascaded`, `realtime`, `s2s-server` — each backed by
a registry of swappable providers. Mix and match per-stage. See README.md.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import S2SConfig, load_config
from .registry import (
    register_pipeline,
    register_realtime,
    register_stt,
    register_tts,
    resolve_pipeline,
    resolve_realtime,
    resolve_stt,
    resolve_tts,
)

__version__ = "0.5.2"
__all__ = [
    "S2SConfig",
    "load_config",
    "register_stt",
    "register_tts",
    "register_realtime",
    "register_pipeline",
    "resolve_stt",
    "resolve_tts",
    "resolve_realtime",
    "resolve_pipeline",
]

logger = logging.getLogger(__name__)


def register(ctx: Any) -> None:
    """Hermes plugin entry point.

    Wires built-in providers into the registry, registers tool schemas + handlers,
    optional slash command, and a CLI subcommand tree.
    """
    # Late imports so we don't blow up at import time on a half-installed system
    from .providers import (
        register_builtin_pipeline_providers,
        register_builtin_realtime_providers,
        register_builtin_stt_providers,
        register_builtin_tts_providers,
    )
    from . import tools as s2s_tools
    from . import schemas as s2s_schemas

    register_builtin_stt_providers()
    register_builtin_tts_providers()
    register_builtin_realtime_providers()
    register_builtin_pipeline_providers()

    # Tools the LLM can call to inspect / control S2S
    ctx.register_tool(
        name="s2s_status",
        toolset="s2s",
        schema=s2s_schemas.S2S_STATUS,
        handler=s2s_tools.s2s_status,
    )
    ctx.register_tool(
        name="s2s_set_mode",
        toolset="s2s",
        schema=s2s_schemas.S2S_SET_MODE,
        handler=s2s_tools.s2s_set_mode,
    )
    ctx.register_tool(
        name="s2s_test_pipeline",
        toolset="s2s",
        schema=s2s_schemas.S2S_TEST_PIPELINE,
        handler=s2s_tools.s2s_test_pipeline,
    )
    ctx.register_tool(
        name="s2s_doctor",
        toolset="s2s",
        schema=s2s_schemas.S2S_DOCTOR,
        handler=s2s_tools.s2s_doctor,
    )

    # Slash command — works in CLI and gateway
    ctx.register_command(
        "s2s",
        handler=s2s_tools.handle_s2s_command,
        description="Speech-to-speech: /s2s status | /s2s mode <cascaded|realtime|s2s-server>",
    )

    # CLI command tree — `hermes s2s ...`
    try:
        from . import cli as s2s_cli
        ctx.register_cli_command(
            name="s2s",
            help="Configure and test speech-to-speech backends",
            setup_fn=s2s_cli.setup_argparse,
            handler_fn=s2s_cli.dispatch,
        )
    except Exception as exc:
        logger.debug("CLI registration skipped: %s", exc)

    # Skill — voice mode usage / troubleshooting playbook
    try:
        from pathlib import Path
        skill_md = Path(__file__).parent / "skills" / "hermes-s2s" / "SKILL.md"
        if skill_md.exists():
            ctx.register_skill("hermes-s2s", skill_md)
    except Exception as exc:
        logger.debug("Skill registration skipped: %s", exc)

    # Discord voice bridge (no-op unless HERMES_S2S_MONKEYPATCH_DISCORD=1 or
    # ctx exposes a native voice-pipeline hook). See ADR-0006.
    from ._internal.discord_bridge import install_discord_voice_bridge
    install_discord_voice_bridge(ctx)

    # Telegram /s2s inline-keyboard UI — mirror the Discord rich UI on any
    # python-telegram-bot Application we can reach through ctx.runner.
    # Failure is non-fatal: the plugin's tool handlers still work via the
    # regular text /s2s command pipeline.
    try:
        from .voice.slash_telegram import install_s2s_telegram_handlers

        runner = getattr(ctx, "runner", None)
        adapters = getattr(runner, "adapters", {}) if runner is not None else {}
        adapter_iter = adapters.values() if isinstance(adapters, dict) else []
        for ad in adapter_iter:
            # The live Hermes Telegram adapter stores its Application at
            # ``self._app`` (see gateway/platforms/telegram.py). We check
            # ``_application`` and ``application`` as defensive fallbacks
            # in case a downstream fork renames the attribute.
            app = (
                getattr(ad, "_app", None)
                or getattr(ad, "_application", None)
                or getattr(ad, "application", None)
            )
            if app is None:
                continue
            # Duck-typing: a python-telegram-bot Application exposes
            # add_handler + handlers.
            if not callable(getattr(app, "add_handler", None)):
                continue
            if install_s2s_telegram_handlers(app):
                logger.info("hermes-s2s: /s2s installed on Telegram")
                break
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Telegram /s2s install skipped: %s", exc)

    # Deferred /s2s slash install — the register-time install path in
    # install_discord_voice_bridge runs BEFORE the Discord bot has logged
    # in, so ``ctx.runner.adapters["discord"]._client.tree`` returns None
    # and the slash never lands. Wiring a one-shot pre_gateway_dispatch
    # hook gives us a guaranteed point where:
    #   - the gateway is fully up (we're processing an inbound message)
    #   - all adapters are connected (Discord client.tree is live)
    #   - the gateway runner is passed as a kwarg (gateway=...)
    # Idempotent: install_s2s_command_on_adapter sets a sentinel on
    # the tree, so subsequent dispatches are cheap no-ops.
    try:
        from .voice.slash import install_s2s_command_on_adapter

        _slash_install_done = {"discord": False}

        def _on_pre_gateway_dispatch(**kwargs):
            if _slash_install_done["discord"]:
                return None
            gateway = kwargs.get("gateway")
            if gateway is None:
                return None
            adapters = getattr(gateway, "adapters", {}) or {}
            ad = adapters.get("discord") if isinstance(adapters, dict) else None
            if ad is None:
                return None
            try:
                installed = install_s2s_command_on_adapter(ad)
            except Exception as exc:
                logger.warning(
                    "hermes-s2s: deferred /s2s install failed: %s", exc
                )
                return None
            # Mark done ONLY when:
            #   - the install actually fired this dispatch (installed=True), OR
            #   - the tree already carries our sentinel (a previous dispatch
            #     or the bridge-side path landed it).
            # Returning False from install_s2s_command_on_adapter when the
            # adapter has no live client/tree must NOT short-circuit future
            # dispatches; otherwise users whose first inbound message races
            # bot login never get /s2s. (Codex review PR #2.)
            if installed:
                _slash_install_done["discord"] = True
                logger.info(
                    "hermes-s2s: /s2s slash installed on first "
                    "gateway-dispatch (deferred path)"
                )
                return None
            # Probe whether some other path already installed it.
            try:
                from .voice.slash import _S2S_COMMAND_INSTALLED

                client = getattr(ad, "_client", None) or getattr(ad, "client", None)
                tree = getattr(client, "tree", None) if client is not None else None
                if tree is not None and getattr(tree, _S2S_COMMAND_INSTALLED, False):
                    _slash_install_done["discord"] = True
            except Exception:
                # Sentinel probe failures are non-fatal — keep retrying.
                pass
            return None

        ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("hermes-s2s: deferred slash hook registration skipped: %s", exc)

    logger.info("hermes-s2s plugin v%s registered", __version__)
