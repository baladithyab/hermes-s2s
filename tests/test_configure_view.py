"""Tests for the /s2s configure rich Discord View (Wave 2 / Task 2.3).

The View is a discord.ui.View subclass so we need discord.py installed;
uses ``pytest.importorskip`` to bail out cleanly when it's not.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

pytest.importorskip("discord")

import discord  # noqa: E402
from discord import ui  # noqa: E402

from hermes_s2s.voice.slash import S2SConfigureView  # noqa: E402


class MockStore:
    """Minimal S2SModeOverrideStore stand-in for View tests."""

    def __init__(self) -> None:
        self.patches: list[Dict[str, Any]] = []
        self.set_calls: list[Dict[str, Any]] = []

    def get_record(self, guild_id: int, channel_id: int) -> Dict[str, str]:
        return {}

    def set_record(
        self, guild_id: int, channel_id: int, record: Dict[str, str]
    ) -> None:
        self.set_calls.append({"g": guild_id, "c": channel_id, "record": record})

    def patch_record(
        self, guild_id: int, channel_id: int, **fields: str
    ) -> Dict[str, str]:
        self.patches.append({"g": guild_id, "c": channel_id, **fields})
        return fields


@pytest.fixture
def mock_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub registry.list_registered so we get deterministic provider sets."""
    from hermes_s2s import registry

    monkeypatch.setattr(
        registry,
        "list_registered",
        lambda: {
            "stt": ["moonshine", "whisper"],
            "tts": ["kokoro", "elevenlabs"],
            "realtime": ["gpt-realtime-2", "gemini-live"],
            "pipeline": [],
        },
    )


def test_configure_view_has_expected_components(mock_registry: None) -> None:
    """View must contain 4 selects (mode + 3 providers) + 3 buttons."""
    store = MockStore()
    view = S2SConfigureView(guild_id=1, channel_id=2, store=store)

    selects = [c for c in view.children if isinstance(c, ui.Select)]
    buttons = [c for c in view.children if isinstance(c, ui.Button)]
    assert len(selects) == 4, (
        f"expected 4 selects (mode + 3 providers), got {len(selects)}"
    )
    assert len(buttons) >= 3, (
        f"expected >=3 buttons (test/reset/refresh), got {len(buttons)}"
    )


def test_configure_view_mode_select_has_all_modes(mock_registry: None) -> None:
    """The mode select must expose all four canonical voice modes."""
    store = MockStore()
    view = S2SConfigureView(guild_id=1, channel_id=2, store=store)

    selects = [c for c in view.children if isinstance(c, ui.Select)]
    mode_select = next(
        s for s in selects if s.placeholder and "mode" in s.placeholder.lower()
    )
    values = {opt.value for opt in mode_select.options}
    assert {"cascaded", "realtime", "pipeline", "s2s-server"} <= values


def test_configure_view_provider_selects_respect_discord_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discord caps a Select at 25 options — the View must slice [:25]."""
    from hermes_s2s import registry

    many = [f"tts{i}" for i in range(40)]
    monkeypatch.setattr(
        registry,
        "list_registered",
        lambda: {"stt": [], "tts": many, "realtime": [], "pipeline": []},
    )
    store = MockStore()
    view = S2SConfigureView(guild_id=1, channel_id=2, store=store)
    selects = [c for c in view.children if isinstance(c, ui.Select)]
    # There may be no stt or realtime select (empty registries) but the
    # tts one must exist and be capped at 25.
    tts_sel = next(
        s for s in selects if s.placeholder and "tts" in s.placeholder.lower()
    )
    assert len(tts_sel.options) == 25


def test_configure_view_skips_select_for_empty_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a provider kind has no registered entries the select is omitted."""
    from hermes_s2s import registry

    monkeypatch.setattr(
        registry,
        "list_registered",
        lambda: {"stt": [], "tts": ["kokoro"], "realtime": [], "pipeline": []},
    )
    store = MockStore()
    view = S2SConfigureView(guild_id=1, channel_id=2, store=store)
    selects = [c for c in view.children if isinstance(c, ui.Select)]
    # Mode select + tts select only — stt/realtime have empty registries
    assert len(selects) == 2
