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


# ---------------------------------------------------------------------------
# B3 — _attach_realtime_to_voice_client wiring
# ---------------------------------------------------------------------------
#
# These tests mock the B1/B2 modules via sys.modules so they run regardless of
# whether hermes_s2s/_internal/audio_bridge.py, tool_bridge.py, discord_audio.py
# have been committed yet (B1/B2/B3/B4 run in parallel; the orchestrator
# commits them together).


def _install_b1_b2_stubs(monkeypatch):
    """Install sys.modules stubs for audio_bridge / tool_bridge / discord_audio.

    Returns ``(RealtimeAudioBridge_mock, HermesToolBridge_mock, QueuedPCMSource_mock)``
    so tests can assert construction + argument shape.
    """
    RealtimeAudioBridge = mock.MagicMock(name="RealtimeAudioBridge")
    HermesToolBridge = mock.MagicMock(name="HermesToolBridge")
    QueuedPCMSource = mock.MagicMock(name="QueuedPCMSource")

    audio_bridge_mod = types.ModuleType("hermes_s2s._internal.audio_bridge")
    audio_bridge_mod.RealtimeAudioBridge = RealtimeAudioBridge
    tool_bridge_mod = types.ModuleType("hermes_s2s._internal.tool_bridge")
    tool_bridge_mod.HermesToolBridge = HermesToolBridge
    discord_audio_mod = types.ModuleType("hermes_s2s._internal.discord_audio")
    discord_audio_mod.QueuedPCMSource = QueuedPCMSource

    monkeypatch.setitem(
        sys.modules, "hermes_s2s._internal.audio_bridge", audio_bridge_mod
    )
    monkeypatch.setitem(
        sys.modules, "hermes_s2s._internal.tool_bridge", tool_bridge_mod
    )
    monkeypatch.setitem(
        sys.modules, "hermes_s2s._internal.discord_audio", discord_audio_mod
    )
    return RealtimeAudioBridge, HermesToolBridge, QueuedPCMSource


def _make_voice_client(guild_id=42, loop=None):
    """Return a Mock simulating the bits of discord.VoiceClient the bridge touches."""
    vc = mock.MagicMock(name="VoiceClient")
    vc.is_playing = mock.Mock(return_value=False)
    vc.stop = mock.Mock()
    vc.play = mock.Mock()
    vc.guild = types.SimpleNamespace(id=guild_id)
    # A sentinel loop object; we only assert run_coroutine_threadsafe is called
    # with it, so it doesn't need real asyncio semantics.
    vc.loop = loop if loop is not None else mock.MagicMock(name="event_loop")
    return vc


def test_attach_realtime_wires_bridge_when_mode_is_realtime(monkeypatch):
    """(a) With s2s.mode='realtime' and everything mocked:
    * resolve_realtime is called with the configured provider + options
    * RealtimeAudioBridge is instantiated with backend + tool_bridge
    * voice_client.play is called with a QueuedPCMSource instance
    * bridge.start() is scheduled on the voice_client's loop
    * adapter._s2s_bridges[guild_id] is populated
    """
    RealtimeAudioBridgeCls, HermesToolBridgeCls, QueuedPCMSourceCls = (
        _install_b1_b2_stubs(monkeypatch)
    )

    fake_backend = object()
    fake_cfg = types.SimpleNamespace(
        mode="realtime",
        realtime_provider="gemini-live",
        realtime_options={"api_key": "xxx"},
    )

    bridge_instance = RealtimeAudioBridgeCls.return_value
    # Keep the buffer attr distinguishable from other magic attrs so we can
    # assert it's what QueuedPCMSource got handed.
    bridge_instance.buffer = mock.sentinel.bridge_buffer
    # start() must return a coroutine-like value run_coroutine_threadsafe can accept.
    async def _fake_start():
        return None
    bridge_instance.start.side_effect = lambda: _fake_start()

    adapter = types.SimpleNamespace()
    vc = _make_voice_client(guild_id=1234)
    receiver = mock.MagicMock(name="VoiceReceiver")
    # Receiver offers no public hook — exercises the monkey-patch shim path.
    del receiver.set_frame_callback
    ctx = types.SimpleNamespace(dispatch_tool=mock.Mock())

    with mock.patch.object(
        discord_bridge, "_install_frame_callback"
    ) as install_cb, mock.patch(
        "hermes_s2s.config.load_config", return_value=fake_cfg
    ), mock.patch(
        "hermes_s2s.registry.resolve_realtime", return_value=fake_backend
    ) as resolve_realtime, mock.patch(
        "asyncio.run_coroutine_threadsafe"
    ) as run_coro:
        discord_bridge._attach_realtime_to_voice_client(adapter, vc, receiver, ctx)

    # resolve_realtime called with the right args
    resolve_realtime.assert_called_once_with("gemini-live", {"api_key": "xxx"})
    # Tool bridge built against ctx.dispatch_tool
    HermesToolBridgeCls.assert_called_once()
    _, kwargs = HermesToolBridgeCls.call_args
    assert kwargs.get("dispatch_tool") is ctx.dispatch_tool
    # Audio bridge built with backend + tool_bridge
    RealtimeAudioBridgeCls.assert_called_once()
    _, ab_kwargs = RealtimeAudioBridgeCls.call_args
    assert ab_kwargs.get("backend") is fake_backend
    assert ab_kwargs.get("tool_bridge") is HermesToolBridgeCls.return_value
    # Frame callback installed on the receiver
    install_cb.assert_called_once()
    cb_args, _ = install_cb.call_args
    assert cb_args[0] is receiver
    assert cb_args[1] is bridge_instance.on_user_frame
    # voice_client.play called with a QueuedPCMSource built from bridge.buffer
    QueuedPCMSourceCls.assert_called_once_with(mock.sentinel.bridge_buffer)
    vc.play.assert_called_once_with(QueuedPCMSourceCls.return_value)
    # bridge.start() scheduled on the VC's loop
    run_coro.assert_called_once()
    _, kw = run_coro.call_args
    coro_arg = run_coro.call_args.args[0]
    loop_arg = run_coro.call_args.args[1]
    assert loop_arg is vc.loop
    # Coroutine object came from bridge.start()
    bridge_instance.start.assert_called_once()
    # Tracking for cleanup
    assert getattr(adapter, "_s2s_bridges", {}).get(1234) is bridge_instance
    # Close the coroutine we never awaited to keep pytest warnings quiet.
    try:
        coro_arg.close()
    except Exception:
        pass


def test_attach_realtime_is_noop_when_mode_is_cascaded(monkeypatch):
    """(b) With s2s.mode='cascaded', the attach function returns early:
    no bridge built, no frame callback installed, no voice_client.play call.
    """
    RealtimeAudioBridgeCls, HermesToolBridgeCls, QueuedPCMSourceCls = (
        _install_b1_b2_stubs(monkeypatch)
    )

    fake_cfg = types.SimpleNamespace(
        mode="cascaded",
        realtime_provider="gemini-live",
        realtime_options={},
    )
    adapter = types.SimpleNamespace()
    vc = _make_voice_client()
    receiver = mock.MagicMock(name="VoiceReceiver")
    ctx = types.SimpleNamespace(dispatch_tool=mock.Mock())

    with mock.patch.object(
        discord_bridge, "_install_frame_callback"
    ) as install_cb, mock.patch(
        "hermes_s2s.config.load_config", return_value=fake_cfg
    ), mock.patch(
        "hermes_s2s.registry.resolve_realtime"
    ) as resolve_realtime, mock.patch(
        "asyncio.run_coroutine_threadsafe"
    ) as run_coro:
        discord_bridge._attach_realtime_to_voice_client(adapter, vc, receiver, ctx)

    resolve_realtime.assert_not_called()
    RealtimeAudioBridgeCls.assert_not_called()
    HermesToolBridgeCls.assert_not_called()
    QueuedPCMSourceCls.assert_not_called()
    install_cb.assert_not_called()
    vc.play.assert_not_called()
    run_coro.assert_not_called()
    # Nothing tracked for cleanup either.
    assert getattr(adapter, "_s2s_bridges", {}) == {} or not hasattr(
        adapter, "_s2s_bridges"
    )


def test_leave_voice_channel_closes_matching_bridge(monkeypatch):
    """(c) The wrapped leave_voice_channel pops + closes the bridge for the
    guild being left, and forwards the call to the original method.
    """

    leave_calls: list = []

    async def original_leave(self, guild):
        leave_calls.append(guild)
        return True

    class FakeAdapter:
        leave_voice_channel = original_leave

    discord_bridge._wrap_leave_voice_channel(FakeAdapter)

    # Bridges tracked per-guild on an instance.
    guild = types.SimpleNamespace(id=7777)
    other_guild = types.SimpleNamespace(id=8888)
    bridge_for_7777 = mock.MagicMock(name="bridge-7777")
    bridge_for_8888 = mock.MagicMock(name="bridge-8888")
    # close() must be synchronous here so inspect.isawaitable() is False and
    # we don't need to await anything from the test harness.
    bridge_for_7777.close = mock.Mock(return_value=None)
    bridge_for_8888.close = mock.Mock(return_value=None)

    adapter = FakeAdapter()
    adapter._s2s_bridges = {7777: bridge_for_7777, 8888: bridge_for_8888}

    import asyncio as _asyncio

    result = _asyncio.new_event_loop().run_until_complete(
        FakeAdapter.leave_voice_channel(adapter, guild)
    )

    assert result is True
    bridge_for_7777.close.assert_called_once()
    bridge_for_8888.close.assert_not_called()
    # Matching bridge removed from the tracking dict
    assert 7777 not in adapter._s2s_bridges
    assert 8888 in adapter._s2s_bridges
    # Original leave was invoked with the same guild
    assert leave_calls == [guild]
    # Idempotency marker is set so a second wrap doesn't double-stack.
    assert getattr(
        FakeAdapter.leave_voice_channel,
        discord_bridge._S2S_LEAVE_WRAPPED_MARKER,
        False,
    ) is True
    before = FakeAdapter.leave_voice_channel
    discord_bridge._wrap_leave_voice_channel(FakeAdapter)
    assert FakeAdapter.leave_voice_channel is before
