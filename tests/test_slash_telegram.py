"""Tests for Telegram inline-keyboard presenter — Wave 3 Task 3.1.

Gates on ``pytest.importorskip('telegram')`` so environments without
python-telegram-bot silently skip (ADR-0015).
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("telegram")  # python-telegram-bot


# ---------------------------------------------------------------------------
# Keyboard builder
# ---------------------------------------------------------------------------


def test_build_status_keyboard_has_mode_and_provider_rows():
    from hermes_s2s.voice.slash_telegram import build_configure_keyboard

    kb = build_configure_keyboard(
        realtime_providers=["gpt-realtime-2", "gemini-live"],
        stt_providers=["moonshine"],
        tts_providers=["kokoro"],
    )
    assert kb is not None
    rows = kb.inline_keyboard
    # at least 1 mode row + 3 provider rows + 1 actions row
    assert len(rows) >= 4
    for row in rows:
        for btn in row:
            assert btn.callback_data.startswith("s2s:")


def test_build_configure_keyboard_omits_missing_provider_rows():
    from hermes_s2s.voice.slash_telegram import build_configure_keyboard

    kb = build_configure_keyboard(
        realtime_providers=[],
        stt_providers=[],
        tts_providers=[],
    )
    rows = kb.inline_keyboard
    # Only mode row + actions row when no providers are registered
    assert len(rows) == 2
    # Actions row contains test/reset/refresh
    action_datas = {btn.callback_data for btn in rows[-1]}
    assert action_datas == {"s2s:test", "s2s:reset", "s2s:refresh"}


def test_build_configure_keyboard_mode_row_has_all_four_modes():
    from hermes_s2s.voice.slash_telegram import build_configure_keyboard

    kb = build_configure_keyboard(
        realtime_providers=["x"],
        stt_providers=["y"],
        tts_providers=["z"],
    )
    mode_row = kb.inline_keyboard[0]
    datas = {btn.callback_data for btn in mode_row}
    assert datas == {
        "s2s:mode:cascaded",
        "s2s:mode:realtime",
        "s2s:mode:pipeline",
        "s2s:mode:s2s-server",
    }


def test_build_configure_keyboard_caps_providers_per_row_at_three():
    from hermes_s2s.voice.slash_telegram import build_configure_keyboard

    kb = build_configure_keyboard(
        realtime_providers=["a", "b", "c", "d", "e"],
        stt_providers=["s1"],
        tts_providers=["t1"],
    )
    # Realtime row is rows[1]; only 3 buttons even though 5 provided
    rt_row = kb.inline_keyboard[1]
    assert len(rt_row) == 3
    assert [btn.callback_data for btn in rt_row] == [
        "s2s:rt:a",
        "s2s:rt:b",
        "s2s:rt:c",
    ]


def test_all_callback_data_is_within_telegram_64_byte_limit():
    from hermes_s2s.voice.slash_telegram import build_configure_keyboard

    kb = build_configure_keyboard(
        realtime_providers=["gpt-realtime-2-very-long-name"],
        stt_providers=["moonshine-v1-turbo"],
        tts_providers=["kokoro-82m"],
    )
    for row in kb.inline_keyboard:
        for btn in row:
            assert len(btn.callback_data.encode("utf-8")) <= 64


# ---------------------------------------------------------------------------
# Callback handler routing
# ---------------------------------------------------------------------------


def _make_update(callback_data: str, chat_id: int = 12345):
    """Build a mock Update with a callback_query carrying ``callback_data``."""
    update = MagicMock()
    query = MagicMock()
    query.data = callback_data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message = MagicMock()
    query.message.chat = MagicMock()
    query.message.chat.id = chat_id
    query.message.reply_text = AsyncMock()
    update.callback_query = query
    # For handle_s2s_command_telegram path (refresh)
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_message = MagicMock()
    update.effective_message.reply_text = AsyncMock()
    return update


@pytest.mark.asyncio
async def test_handle_callback_sets_mode_via_store(tmp_path, monkeypatch):
    from hermes_s2s.voice import slash as slash_mod
    from hermes_s2s.voice import slash_telegram

    store_path = tmp_path / "overrides.json"
    monkeypatch.setattr(slash_mod, "_DEFAULT_STORE", None, raising=False)
    monkeypatch.setenv("HERMES_S2S_OVERRIDE_STORE_PATH", str(store_path))
    # Fresh store keyed on this env var
    store = slash_mod.S2SModeOverrideStore(path=store_path)
    monkeypatch.setattr(slash_telegram, "get_default_store", lambda: store)

    update = _make_update("s2s:mode:realtime", chat_id=777)
    await slash_telegram.handle_s2s_callback(update, context=MagicMock())

    rec = store.get_record(777, 777)
    assert rec.get("mode") == "realtime"
    update.callback_query.answer.assert_awaited()
    update.callback_query.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_callback_sets_realtime_provider(tmp_path, monkeypatch):
    from hermes_s2s.voice import slash as slash_mod
    from hermes_s2s.voice import slash_telegram

    store_path = tmp_path / "overrides.json"
    store = slash_mod.S2SModeOverrideStore(path=store_path)
    monkeypatch.setattr(slash_telegram, "get_default_store", lambda: store)

    update = _make_update("s2s:rt:gpt-realtime-2", chat_id=42)
    await slash_telegram.handle_s2s_callback(update, context=MagicMock())

    rec = store.get_record(42, 42)
    assert rec.get("realtime_provider") == "gpt-realtime-2"


@pytest.mark.asyncio
async def test_handle_callback_sets_stt_and_tts_providers(tmp_path, monkeypatch):
    from hermes_s2s.voice import slash as slash_mod
    from hermes_s2s.voice import slash_telegram

    store_path = tmp_path / "overrides.json"
    store = slash_mod.S2SModeOverrideStore(path=store_path)
    monkeypatch.setattr(slash_telegram, "get_default_store", lambda: store)

    update = _make_update("s2s:stt:moonshine", chat_id=9)
    await slash_telegram.handle_s2s_callback(update, context=MagicMock())
    update = _make_update("s2s:tts:kokoro", chat_id=9)
    await slash_telegram.handle_s2s_callback(update, context=MagicMock())

    rec = store.get_record(9, 9)
    assert rec.get("stt_provider") == "moonshine"
    assert rec.get("tts_provider") == "kokoro"


@pytest.mark.asyncio
async def test_handle_callback_reset_clears_record(tmp_path, monkeypatch):
    from hermes_s2s.voice import slash as slash_mod
    from hermes_s2s.voice import slash_telegram

    store_path = tmp_path / "overrides.json"
    store = slash_mod.S2SModeOverrideStore(path=store_path)
    store.set_record(55, 55, {"mode": "realtime", "stt_provider": "moonshine"})
    monkeypatch.setattr(slash_telegram, "get_default_store", lambda: store)

    update = _make_update("s2s:reset", chat_id=55)
    await slash_telegram.handle_s2s_callback(update, context=MagicMock())

    assert store.get_record(55, 55) == {}


@pytest.mark.asyncio
async def test_handle_callback_test_invokes_test_pipeline(monkeypatch):
    from hermes_s2s import tools as s2s_tools
    from hermes_s2s.voice import slash_telegram

    monkeypatch.setattr(
        s2s_tools,
        "s2s_test_pipeline",
        lambda args: json.dumps(
            {"ok": True, "bytes": 1234, "tts_provider": "kokoro"}
        ),
    )
    # Ensure handler imports the patched symbol
    monkeypatch.setattr(slash_telegram, "get_default_store", lambda: MagicMock())

    update = _make_update("s2s:test", chat_id=1)
    await slash_telegram.handle_s2s_callback(update, context=MagicMock())
    update.callback_query.message.reply_text.assert_awaited_once()
    args, kwargs = update.callback_query.message.reply_text.call_args
    msg = args[0] if args else kwargs.get("text", "")
    assert "1234" in msg and "kokoro" in msg


@pytest.mark.asyncio
async def test_handle_callback_refresh_redraws_status(tmp_path, monkeypatch):
    from hermes_s2s.voice import slash as slash_mod
    from hermes_s2s.voice import slash_telegram

    store_path = tmp_path / "overrides.json"
    store = slash_mod.S2SModeOverrideStore(path=store_path)
    monkeypatch.setattr(slash_telegram, "get_default_store", lambda: store)

    called = {"n": 0}

    async def _fake_cmd(update, context):
        called["n"] += 1

    monkeypatch.setattr(
        slash_telegram, "handle_s2s_command_telegram", _fake_cmd
    )

    update = _make_update("s2s:refresh", chat_id=1)
    await slash_telegram.handle_s2s_callback(update, context=MagicMock())
    assert called["n"] == 1


@pytest.mark.asyncio
async def test_handle_callback_ignores_unknown_prefix(monkeypatch):
    from hermes_s2s.voice import slash_telegram

    monkeypatch.setattr(slash_telegram, "get_default_store", lambda: MagicMock())

    update = _make_update("other:thing", chat_id=1)
    # Should not raise; simply returns after ack
    await slash_telegram.handle_s2s_callback(update, context=MagicMock())
    update.callback_query.answer.assert_awaited()
    update.callback_query.edit_message_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------


def test_install_s2s_telegram_handlers_is_idempotent():
    from telegram.ext import ApplicationBuilder
    from hermes_s2s.voice.slash_telegram import install_s2s_telegram_handlers

    app = ApplicationBuilder().token("111:dummy").build()
    assert install_s2s_telegram_handlers(app) is True
    # Second call should be a no-op
    assert install_s2s_telegram_handlers(app) is False
    assert getattr(app, "__hermes_s2s_telegram_installed__", False) is True


def test_install_s2s_telegram_handlers_adds_command_and_callback_handlers():
    from telegram.ext import (
        ApplicationBuilder,
        CallbackQueryHandler,
        CommandHandler,
    )
    from hermes_s2s.voice.slash_telegram import install_s2s_telegram_handlers

    app = ApplicationBuilder().token("111:dummy").build()
    assert install_s2s_telegram_handlers(app) is True
    # Flatten handlers across all groups
    flat = []
    for group_handlers in app.handlers.values():
        flat.extend(group_handlers)
    cmd_handlers = [h for h in flat if isinstance(h, CommandHandler)]
    cb_handlers = [h for h in flat if isinstance(h, CallbackQueryHandler)]
    assert any("s2s" in (getattr(h, "commands", ()) or ()) for h in cmd_handlers)
    assert len(cb_handlers) >= 1
