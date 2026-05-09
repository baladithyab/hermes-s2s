"""Tests for hermes_s2s._internal.discord_bridge.

Strategy: we never import discord.py or hermes-agent. Everything is mocked via
``sys.modules`` and ``unittest.mock``. The point of these tests is to verify
strategy selection (native hook vs monkey-patch vs no-op) and the monkey-patch
attachment site — not the audio loop.
"""

from __future__ import annotations

import logging
import sys
import types
from unittest import mock

import pytest

from hermes_s2s._internal import discord_bridge


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Each test starts with a clean HERMES_S2S_MONKEYPATCH_DISCORD env."""
    monkeypatch.delenv("HERMES_S2S_MONKEYPATCH_DISCORD", raising=False)
    yield


# ---------------------------------------------------------------------------
# (a) default: no env var, no native hook -> no-op
# ---------------------------------------------------------------------------


def test_install_is_no_op_when_no_env_and_no_native_hook(caplog):
    ctx = types.SimpleNamespace()  # no register_voice_pipeline_factory attr

    # Patch the monkey-patch helper so we can assert it's never reached.
    with mock.patch.object(
        discord_bridge, "_install_via_monkey_patch"
    ) as patched, caplog.at_level(logging.INFO, logger=discord_bridge.logger.name):
        discord_bridge.install_discord_voice_bridge(ctx)

    patched.assert_not_called()
    joined_logs = " ".join(rec.getMessage() for rec in caplog.records)
    assert "realtime in Discord disabled" in joined_logs
    assert "HERMES_S2S_MONKEYPATCH_DISCORD" in joined_logs


# ---------------------------------------------------------------------------
# (b) env var set + Hermes module mocked -> monkey-patch wraps join_voice_channel
# ---------------------------------------------------------------------------


def test_install_monkey_patches_discord_adapter(monkeypatch):
    """With HERMES_S2S_MONKEYPATCH_DISCORD=1 and a mocked gateway.platforms.discord
    module, install_discord_voice_bridge must wrap DiscordAdapter.join_voice_channel.
    """

    # Sentinel original method; capture its identity so we can assert it was replaced.
    async def original_join(self, channel):  # noqa: ARG001
        return True

    class FakeDiscordAdapter:
        join_voice_channel = original_join

    class FakeVoiceReceiver:
        pass

    fake_hermes_discord = types.ModuleType("gateway.platforms.discord")
    fake_hermes_discord.DiscordAdapter = FakeDiscordAdapter
    fake_hermes_discord.VoiceReceiver = FakeVoiceReceiver

    fake_gateway = types.ModuleType("gateway")
    fake_platforms = types.ModuleType("gateway.platforms")
    fake_gateway.platforms = fake_platforms
    fake_platforms.discord = fake_hermes_discord

    # Also mock hermes_agent with a known-good version so the version gate passes.
    fake_hermes_agent = types.ModuleType("hermes_agent")
    fake_hermes_agent.__version__ = "0.1.0"

    monkeypatch.setitem(sys.modules, "gateway", fake_gateway)
    monkeypatch.setitem(sys.modules, "gateway.platforms", fake_platforms)
    monkeypatch.setitem(sys.modules, "gateway.platforms.discord", fake_hermes_discord)
    monkeypatch.setitem(sys.modules, "hermes_agent", fake_hermes_agent)
    monkeypatch.setenv("HERMES_S2S_MONKEYPATCH_DISCORD", "1")

    ctx = types.SimpleNamespace()  # no native hook

    assert FakeDiscordAdapter.join_voice_channel is original_join
    discord_bridge.install_discord_voice_bridge(ctx)

    # The attribute should now point at a *different* callable than the original.
    assert FakeDiscordAdapter.join_voice_channel is not original_join
    # And it should carry our sentinel marker.
    assert getattr(
        FakeDiscordAdapter.join_voice_channel,
        discord_bridge._BRIDGE_WRAPPED_MARKER,
        False,
    ) is True

    # Idempotency: a second install must NOT double-wrap.
    wrapped_once = FakeDiscordAdapter.join_voice_channel
    discord_bridge.install_discord_voice_bridge(ctx)
    assert FakeDiscordAdapter.join_voice_channel is wrapped_once


def test_install_bails_cleanly_when_gateway_module_missing(monkeypatch, caplog):
    """Env var set but gateway.platforms.discord not importable -> warn, return."""
    # Ensure our fake from the previous test (if cached) is gone.
    for mod in list(sys.modules):
        if mod == "gateway" or mod.startswith("gateway."):
            monkeypatch.delitem(sys.modules, mod, raising=False)

    # Block the import by inserting a finder that denies the name.
    class _Blocker:
        def find_spec(self, name, path=None, target=None):  # noqa: ARG002
            if name == "gateway" or name.startswith("gateway."):
                raise ImportError(f"blocked: {name}")
            return None

    blocker = _Blocker()
    sys.meta_path.insert(0, blocker)
    try:
        monkeypatch.setenv("HERMES_S2S_MONKEYPATCH_DISCORD", "1")
        with caplog.at_level(logging.WARNING, logger=discord_bridge.logger.name):
            discord_bridge.install_discord_voice_bridge(types.SimpleNamespace())
    finally:
        sys.meta_path.remove(blocker)

    joined = " ".join(rec.getMessage() for rec in caplog.records)
    assert "gateway.platforms.discord" in joined


# ---------------------------------------------------------------------------
# (c) ctx exposes native hook -> that wins, no monkey-patch
# ---------------------------------------------------------------------------


def test_native_hook_takes_precedence_over_monkey_patch(monkeypatch):
    """If ctx has register_voice_pipeline_factory, install uses it and skips
    the monkey-patch path — even if the env var is set."""
    monkeypatch.setenv("HERMES_S2S_MONKEYPATCH_DISCORD", "1")

    hook = mock.Mock()
    ctx = types.SimpleNamespace(register_voice_pipeline_factory=hook)

    with mock.patch.object(
        discord_bridge, "_install_via_monkey_patch"
    ) as monkeypatch_impl:
        discord_bridge.install_discord_voice_bridge(ctx)

    hook.assert_called_once()
    args, _ = hook.call_args
    assert args[0] == "discord"
    assert callable(args[1])
    monkeypatch_impl.assert_not_called()
