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

__version__ = "0.5.0"
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

    logger.info("hermes-s2s plugin v%s registered", __version__)
