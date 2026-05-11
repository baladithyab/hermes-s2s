"""Telegram inline-keyboard presenter for ``/s2s``.

Mirrors the Discord rich UI on Telegram. Telegram ``callback_data`` has a
64-byte limit, so we use short tokens: ``s2s:<verb>:<arg>`` — e.g.
``s2s:mode:realtime``, ``s2s:rt:gpt-realtime-2``, ``s2s:test``,
``s2s:reset``, ``s2s:refresh``.

Registration entry point: :func:`install_s2s_telegram_handlers`, called
from the plugin's ``register(ctx)`` hook once the Hermes Telegram adapter
is reachable via ``ctx.runner.adapters``. The installer is idempotent via
an ``__hermes_s2s_telegram_installed__`` sentinel on the Application.

Telegram has no concept of guilds; we key the per-chat override store on
``(chat_id, chat_id)``. Per-topic (forum) keying lands in 0.5.1 — see
ADR-0015.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Sequence

logger = logging.getLogger(__name__)

# Short callback-data verbs keep us under Telegram's 64-byte limit even
# with long provider names. Keep this list in sync with the router in
# :func:`handle_s2s_callback`.
_PROVIDER_KIND_TO_FIELD = {
    "rt": "realtime_provider",
    "stt": "stt_provider",
    "tts": "tts_provider",
}

# Cap provider buttons per row so mobile keyboards stay readable.
_MAX_PROVIDER_BUTTONS_PER_ROW = 3


def build_configure_keyboard(
    *,
    realtime_providers: Sequence[str],
    stt_providers: Sequence[str],
    tts_providers: Sequence[str],
) -> Any:
    """Build an ``InlineKeyboardMarkup`` with mode + provider + action rows.

    Returns ``None`` if ``python-telegram-bot`` isn't importable (the
    calling code path is already gated by :func:`install_s2s_telegram_handlers`,
    but belt-and-braces here keeps this helper safe to import).
    """
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    except ImportError:  # pragma: no cover — tests gate on importorskip
        return None

    # Row 1: mode picker (always shown).
    mode_row = [
        InlineKeyboardButton("Cascaded", callback_data="s2s:mode:cascaded"),
        InlineKeyboardButton("Realtime", callback_data="s2s:mode:realtime"),
        InlineKeyboardButton("Pipeline", callback_data="s2s:mode:pipeline"),
        InlineKeyboardButton("Server", callback_data="s2s:mode:s2s-server"),
    ]

    def _provider_row(kind_short: str, names: Sequence[str]) -> list:
        return [
            InlineKeyboardButton(n, callback_data=f"s2s:{kind_short}:{n}")
            for n in list(names)[:_MAX_PROVIDER_BUTTONS_PER_ROW]
        ]

    rows: list = [mode_row]
    if realtime_providers:
        rows.append(_provider_row("rt", realtime_providers))
    if stt_providers:
        rows.append(_provider_row("stt", stt_providers))
    if tts_providers:
        rows.append(_provider_row("tts", tts_providers))

    # Actions row: always present.
    rows.append([
        InlineKeyboardButton("🧪 Test", callback_data="s2s:test"),
        InlineKeyboardButton("♻️ Reset", callback_data="s2s:reset"),
        InlineKeyboardButton("🔄 Refresh", callback_data="s2s:refresh"),
    ])
    return InlineKeyboardMarkup(rows)


# Indirection so tests can monkeypatch the store without mutating the
# singleton in ``voice.slash``.
def get_default_store():
    """Return the shared :class:`S2SModeOverrideStore` singleton."""
    from .slash import get_default_store as _slash_default_store

    return _slash_default_store()


async def handle_s2s_command_telegram(update, context):
    """Implement ``/s2s`` on Telegram — reply with status + inline keyboard."""
    from ..registry import list_registered
    from ..tools import s2s_status
    from .slash_format import format_status

    chat = update.effective_chat
    chat_id = int(chat.id)
    # Telegram has no guild concept; repeat chat_id so store key stays stable.
    g_id, c_id = chat_id, chat_id

    payload = json.loads(s2s_status({}))
    rec = get_default_store().get_record(g_id, c_id)
    text = format_status(
        active_mode=payload["active_mode"],
        config_mode=payload["config_mode"],
        realtime_provider=payload["realtime"]["provider"],
        stt_provider=payload["cascaded"]["stt_provider"],
        tts_provider=payload["cascaded"]["tts_provider"],
        guild_id=g_id,
        channel_id=c_id,
        per_channel_record=rec,
    )
    reg = list_registered()
    kb = build_configure_keyboard(
        realtime_providers=reg.get("realtime", []),
        stt_providers=reg.get("stt", []),
        tts_providers=reg.get("tts", []),
    )
    await update.effective_message.reply_text(
        text, reply_markup=kb, parse_mode="Markdown"
    )


async def handle_s2s_callback(update, context):
    """Route inline-keyboard taps to the override store / test / refresh."""
    query = update.callback_query
    await query.answer()  # ack within 3s
    parts = (query.data or "").split(":", 2)
    if len(parts) < 2 or parts[0] != "s2s":
        return

    chat_id = int(query.message.chat.id)
    g_id, c_id = chat_id, chat_id
    store = get_default_store()
    verb = parts[1]
    arg = parts[2] if len(parts) >= 3 else None

    if verb == "mode" and arg:
        store.patch_record(g_id, c_id, mode=arg)
        await query.edit_message_text(
            f"Mode → `{arg}`", parse_mode="Markdown"
        )
        return

    if verb in _PROVIDER_KIND_TO_FIELD and arg:
        field = _PROVIDER_KIND_TO_FIELD[verb]
        store.patch_record(g_id, c_id, **{field: arg})
        await query.edit_message_text(
            f"{verb.upper()} provider → `{arg}`", parse_mode="Markdown"
        )
        return

    if verb == "test":
        from ..tools import s2s_test_pipeline

        r = json.loads(s2s_test_pipeline({}))
        if r.get("ok"):
            msg = (
                f"✅ TTS OK — {r.get('bytes')} bytes via "
                f"`{r.get('tts_provider')}`"
            )
        else:
            msg = (
                f"❌ failed at `{r.get('stage')}`: {r.get('error')}"
            )
        await query.message.reply_text(msg, parse_mode="Markdown")
        return

    if verb == "reset":
        store.set_record(g_id, c_id, {})
        await query.edit_message_text(
            "✅ Cleared overrides for this chat."
        )
        return

    if verb == "refresh":
        await handle_s2s_command_telegram(update, context)
        return

    # Unknown verb under the s2s: namespace — silently drop (we already
    # acked so Telegram's spinner stops).


_INSTALLED_SENTINEL = "__hermes_s2s_telegram_installed__"


def install_s2s_telegram_handlers(application: Any) -> bool:
    """Register ``/s2s`` + callback handler on a python-telegram-bot Application.

    Idempotent — repeated calls are no-ops on the same Application
    instance. Returns ``True`` if handlers were newly added, ``False``
    otherwise (already installed, or python-telegram-bot not importable).
    """
    try:
        from telegram.ext import CallbackQueryHandler, CommandHandler
    except ImportError:  # pragma: no cover — tests gate on importorskip
        return False

    if getattr(application, _INSTALLED_SENTINEL, False):
        return False

    application.add_handler(
        CommandHandler("s2s", handle_s2s_command_telegram)
    )
    application.add_handler(
        CallbackQueryHandler(handle_s2s_callback, pattern=r"^s2s:")
    )
    setattr(application, _INSTALLED_SENTINEL, True)
    return True


__all__ = [
    "build_configure_keyboard",
    "handle_s2s_command_telegram",
    "handle_s2s_callback",
    "install_s2s_telegram_handlers",
    "get_default_store",
]
