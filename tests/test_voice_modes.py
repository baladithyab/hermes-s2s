"""WAVE 1a tests: VoiceMode + ModeRouter precedence + base session lifecycle.

Per plan docs/plans/wave-0.4.0-rearchitecture.md:
- This file is SHARED between W1a (this commit) and W1b (concrete
  session tests land later).
- W1a M1.1 owns: mode-router precedence, typo rejection, alias
  normalization.
- W1a M1.2 owns: VoiceSession protocol + AsyncExitStackBaseSession
  lifecycle (stop idempotency + state machine + half-start cleanup).
- W1b owns: the four per-session tests (do not add them here).
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from hermes_s2s.voice import (
    AsyncExitStackBaseSession,
    InvalidTransition,
    ModeRouter,
    ModeSpec,
    SessionState,
    VoiceMode,
    VoiceSession,
)


# ---------------------------------------------------------------------------
# VoiceMode enum basics
# ---------------------------------------------------------------------------


def test_voice_mode_values_are_canonical_strings():
    """Serialized values match the strings used in slash args + YAML."""
    assert VoiceMode.CASCADED.value == "cascaded"
    assert VoiceMode.PIPELINE.value == "pipeline"
    assert VoiceMode.REALTIME.value == "realtime"
    # S2S_SERVER name uses an underscore (Python identifier) but the
    # value is the canonical hyphenated form.
    assert VoiceMode.S2S_SERVER.name == "S2S_SERVER"
    assert VoiceMode.S2S_SERVER.value == "s2s-server"
    # StrEnum: str(member) returns the VALUE, not the NAME.
    assert str(VoiceMode.S2S_SERVER) == "s2s-server"
    assert str(VoiceMode.REALTIME) == "realtime"


# ---------------------------------------------------------------------------
# ModeRouter — precedence (the 6-level chain)
# ---------------------------------------------------------------------------


def _cfg(
    *,
    default_mode=None,
    guild_overrides=None,
    channel_overrides=None,
):
    """Helper: build a minimal config dict shaped like the real one."""
    voice: dict = {}
    if default_mode is not None:
        voice["default_mode"] = default_mode
    if guild_overrides is not None:
        voice["guild_overrides"] = guild_overrides
    if channel_overrides is not None:
        voice["channel_overrides"] = channel_overrides
    return {"s2s": {"voice": voice}}


def test_mode_router_precedence():
    """All 6 precedence levels resolve correctly; later levels override earlier."""
    guild_id = 1111
    chan_id = 2222

    # --- Level 6: hard default "cascaded" --------------------------------
    r_empty = ModeRouter({}, env={})
    assert r_empty.resolve().mode is VoiceMode.CASCADED

    # --- Level 5: config default_mode ------------------------------------
    r_default = ModeRouter(_cfg(default_mode="realtime"), env={})
    assert r_default.resolve().mode is VoiceMode.REALTIME
    # default_mode still yields cascaded when NOT set:
    r_no_default = ModeRouter(_cfg(), env={})
    assert r_no_default.resolve().mode is VoiceMode.CASCADED

    # --- Level 4: guild override beats default ---------------------------
    r_guild = ModeRouter(
        _cfg(
            default_mode="realtime",
            guild_overrides={guild_id: "pipeline"},
        ),
        env={},
    )
    assert r_guild.resolve(guild_id=guild_id).mode is VoiceMode.PIPELINE
    # Guild override does NOT apply when its guild doesn't match:
    assert r_guild.resolve(guild_id=9999).mode is VoiceMode.REALTIME
    # Absent guild_id falls through to default:
    assert r_guild.resolve().mode is VoiceMode.REALTIME

    # --- Level 3: channel override beats guild ---------------------------
    r_chan = ModeRouter(
        _cfg(
            default_mode="realtime",
            guild_overrides={guild_id: "pipeline"},
            channel_overrides={chan_id: "s2s-server"},
        ),
        env={},
    )
    assert (
        r_chan.resolve(guild_id=guild_id, channel_id=chan_id).mode
        is VoiceMode.S2S_SERVER
    )
    # Channel-absent case: guild override still wins over default:
    assert (
        r_chan.resolve(guild_id=guild_id, channel_id=7777).mode
        is VoiceMode.PIPELINE
    )

    # --- Level 2: env var beats everything below -------------------------
    r_env = ModeRouter(
        _cfg(
            default_mode="realtime",
            guild_overrides={guild_id: "pipeline"},
            channel_overrides={chan_id: "s2s-server"},
        ),
        env={"HERMES_S2S_VOICE_MODE": "cascaded"},
    )
    assert (
        r_env.resolve(guild_id=guild_id, channel_id=chan_id).mode
        is VoiceMode.CASCADED
    )

    # --- Level 1: explicit slash hint beats env --------------------------
    r_hint = ModeRouter(
        _cfg(default_mode="realtime"),
        env={"HERMES_S2S_VOICE_MODE": "cascaded"},
    )
    assert r_hint.resolve(mode_hint="pipeline").mode is VoiceMode.PIPELINE

    # --- String-keyed overrides also work (YAML/JSON friendliness) -------
    r_string_key = ModeRouter(
        _cfg(channel_overrides={str(chan_id): "realtime"}),
        env={},
    )
    assert r_string_key.resolve(channel_id=chan_id).mode is VoiceMode.REALTIME


def test_mode_router_rejects_typo():
    """Unknown modes raise ValueError with the valid-modes list in the message."""
    router = ModeRouter({}, env={})
    with pytest.raises(ValueError) as excinfo:
        router.resolve(mode_hint="realitme")
    msg = str(excinfo.value)
    assert "realitme" in msg
    # Every valid mode value must appear in the error message so users
    # can fix their typo without digging into docs.
    for m in VoiceMode:
        assert m.value in msg


def test_mode_router_normalizes_aliases():
    """`s2s_server` and `S2S Server` resolve to VoiceMode.S2S_SERVER."""
    router = ModeRouter({}, env={})
    assert router.resolve(mode_hint="s2s_server").mode is VoiceMode.S2S_SERVER
    assert router.resolve(mode_hint="S2S Server").mode is VoiceMode.S2S_SERVER
    assert router.resolve(mode_hint="  S2S-SERVER  ").mode is VoiceMode.S2S_SERVER
    # Aliases also work for the other modes (case + whitespace):
    assert router.resolve(mode_hint="CASCADED").mode is VoiceMode.CASCADED
    assert router.resolve(mode_hint=" Realtime ").mode is VoiceMode.REALTIME

    # "auto"/"default"/None/"" are treated as "no hint" and fall through.
    r_with_default = ModeRouter(_cfg(default_mode="realtime"), env={})
    assert r_with_default.resolve(mode_hint="auto").mode is VoiceMode.REALTIME
    assert r_with_default.resolve(mode_hint="default").mode is VoiceMode.REALTIME
    assert r_with_default.resolve(mode_hint="").mode is VoiceMode.REALTIME
    assert r_with_default.resolve(mode_hint=None).mode is VoiceMode.REALTIME


def test_mode_router_returns_mode_spec():
    """resolve() returns a frozen ModeSpec carrying the VoiceMode."""
    router = ModeRouter({}, env={})
    spec = router.resolve(mode_hint="realtime")
    assert isinstance(spec, ModeSpec)
    assert spec.mode is VoiceMode.REALTIME
    # ModeSpec is frozen — attempting mutation raises.
    with pytest.raises(Exception):
        spec.mode = VoiceMode.CASCADED  # type: ignore[misc]


# ---------------------------------------------------------------------------
# WAVE 1b — concrete session class tests
# ---------------------------------------------------------------------------
# These tests are owned by W1b and exercise M1.3 (CascadedSession),
# M1.4 (CustomPipelineSession), M1.5 (RealtimeSession), and M1.6
# (S2SServerSession). They depend on symbols introduced by W1a
# (VoiceMode, ModeSpec, AsyncExitStackBaseSession) and therefore
# sit below the W1a section.
#
# The marquee test here is
# ``test_realtime_session_calls_connect_before_pumps`` which, per the
# post-Phase-8 acceptance refinement in
# docs/plans/wave-0.4.0-rearchitecture.md WAVE 1b, uses
# ``AsyncMock.side_effect`` to record call order. Plain
# ``assert_called()`` is REJECTED — it passes even with the v0.3.1
# silent-bot bug shape where pumps spawned before ``connect()``
# returned.

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from hermes_s2s.voice.sessions_cascaded import CascadedSession
from hermes_s2s.voice.sessions_pipeline import (
    CustomPipelineSession,
    _STT_ENV,
    _TTS_ENV,
)
from hermes_s2s.voice.sessions_realtime import RealtimeSession
from hermes_s2s.voice.sessions_s2s_server import S2SServerSession


def _spec(mode: VoiceMode, **options: Any) -> ModeSpec:
    return ModeSpec(mode=mode, provider=None, options=dict(options))


# --- M1.3: CascadedSession ------------------------------------------------


@pytest.mark.asyncio
async def test_cascaded_session_is_noop():
    """CascadedSession start/stop succeed without touching env or spawning tasks."""
    spec = _spec(VoiceMode.CASCADED)
    before_env = dict(os.environ)

    session = CascadedSession(spec)
    assert session.mode is VoiceMode.CASCADED

    await session.start()
    # Env untouched.
    assert os.environ.get(_STT_ENV) == before_env.get(_STT_ENV)
    assert os.environ.get(_TTS_ENV) == before_env.get(_TTS_ENV)

    await session.stop()
    # Stop is idempotent.
    await session.stop()


# --- M1.4: CustomPipelineSession -----------------------------------------


@pytest.mark.asyncio
async def test_pipeline_session_restores_env(monkeypatch):
    """start() installs env vars; stop() restores prior state (unset stays unset)."""
    # Prior state: STT had a value, TTS did not.
    monkeypatch.setenv(_STT_ENV, "PRIOR_STT")
    monkeypatch.delenv(_TTS_ENV, raising=False)

    spec = _spec(
        VoiceMode.PIPELINE,
        stt_command="new-stt --model tiny",
        tts_command="new-tts --voice af",
    )
    session = CustomPipelineSession(spec)

    await session.start()
    assert os.environ[_STT_ENV] == "new-stt --model tiny"
    assert os.environ[_TTS_ENV] == "new-tts --voice af"

    await session.stop()
    # Prior value restored; absent var stays absent.
    assert os.environ.get(_STT_ENV) == "PRIOR_STT"
    assert _TTS_ENV not in os.environ


@pytest.mark.asyncio
async def test_pipeline_session_restores_env_when_no_prior_value(monkeypatch):
    """stop() should *unset* env vars that were unset going in."""
    monkeypatch.delenv(_STT_ENV, raising=False)
    monkeypatch.delenv(_TTS_ENV, raising=False)

    spec = _spec(
        VoiceMode.PIPELINE, stt_command="X", tts_command="Y"
    )
    session = CustomPipelineSession(spec)
    await session.start()
    assert os.environ[_STT_ENV] == "X"
    await session.stop()
    assert _STT_ENV not in os.environ
    assert _TTS_ENV not in os.environ


# --- M1.5: RealtimeSession -----------------------------------------------


def _make_realtime_backend(record: list):
    """Build a mock backend whose async methods append to ``record``."""
    backend = MagicMock()

    async def _connect(*_a, **_kw):
        record.append("connect")

    async def _send(*_a, **_kw):
        record.append("pump_input_started")

    def _recv_events(*_a, **_kw):
        # recv_events() returns an async iterator; creating it marks
        # the output pump as started so the fence test can observe it.
        record.append("pump_output_started")

        async def _gen():
            # Sleep forever so the pump stays alive until cancelled.
            await asyncio.sleep(3600)
            if False:  # pragma: no cover - async generator shape only
                yield None

        return _gen()

    async def _close(*_a, **_kw):
        record.append("close")

    backend.connect = AsyncMock(side_effect=_connect)
    backend.send_audio_chunk = AsyncMock(side_effect=_send)
    backend.recv_events = MagicMock(side_effect=_recv_events)
    backend.close = AsyncMock(side_effect=_close)
    return backend


@pytest.mark.asyncio
async def test_realtime_session_calls_connect_before_pumps():
    """THE regression-fence test (post-Phase-8 A1 acceptance).

    Uses ``AsyncMock.side_effect`` to record the exact order in which
    the session touches the backend. The v0.3.1 silent-bot P0 happened
    because the input pump task spawned and called
    ``send_audio_chunk`` into an as-yet-unopened socket BEFORE
    ``connect()`` returned. The fence proved here is: ``connect``
    MUST be the first backend method observed, and MUST be called
    exactly once.
    """
    calls: list = []
    backend = _make_realtime_backend(calls)
    spec = _spec(VoiceMode.REALTIME, system_prompt="hi", voice="alloy")

    # bridge=object() suppresses the optional RealtimeAudioBridge
    # construction path — the backend mock is sufficient to prove
    # the fence at the session layer.
    session = RealtimeSession(spec, backend=backend, bridge=object())
    try:
        await session.start()

        # THE FENCE ASSERT: first backend method called must be connect.
        assert calls, "no backend method was called during start()"
        assert calls[0] == "connect", (
            f"connect must be the FIRST backend method called, "
            f"got order: {calls!r}"
        )
        # connect must not be re-called by the pumps.
        assert calls.count("connect") == 1, (
            f"connect must be called exactly once, got: {calls!r}"
        )
        # Both pump-started markers must have appeared AFTER connect.
        assert "pump_input_started" in calls[1:], (
            f"input pump never called send_audio_chunk after connect; "
            f"order: {calls!r}"
        )
        assert "pump_output_started" in calls[1:], (
            f"output pump never called recv_events after connect; "
            f"order: {calls!r}"
        )
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_realtime_session_failed_start_cleans_up():
    """If RealtimeSession.start() raises partway, stop() must still safely unwind.

    The AsyncExitStack lifecycle (research-15 §2) requires that any
    cleanup callbacks registered *before* the failing step get
    invoked, and that the session ends up in a stopped state.

    Distinct from ``test_session_failed_start_cleans_up`` (W1a, M1.2),
    which exercises the same discipline at the base-class layer with
    a :class:`_DummySession`. This test exercises it on the real
    :class:`RealtimeSession` using a backend whose ``connect()``
    raises — the same shape the v0.3.1 P0 hit in production.
    """
    backend = MagicMock()
    # connect raises — simulates a gateway-down / bad-API-key boot.
    backend.connect = AsyncMock(
        side_effect=RuntimeError("simulated connect failure")
    )
    backend.send_audio_chunk = AsyncMock()
    backend.recv_events = MagicMock()
    backend.close = AsyncMock()

    spec = _spec(VoiceMode.REALTIME)
    session = RealtimeSession(spec, backend=backend, bridge=object())

    with pytest.raises(RuntimeError, match="simulated connect failure"):
        await session.start()

    # stop() must be safe to call even after a failed start.
    await session.stop()
    # And idempotent.
    await session.stop()

    # No pump tasks should have spawned — the fence held.
    backend.send_audio_chunk.assert_not_called()
    backend.recv_events.assert_not_called()


# --- M1.6: S2SServerSession ----------------------------------------------


@pytest.mark.asyncio
async def test_s2s_server_session_smoke():
    """S2SServerSession start/stop smoke with a mock backend."""
    backend = MagicMock()
    backend.endpoint = "ws://127.0.0.1:9999"
    backend.connect = AsyncMock()
    backend.close = AsyncMock()

    spec = _spec(VoiceMode.S2S_SERVER, endpoint="ws://127.0.0.1:9999")
    session = S2SServerSession(spec, backend=backend)
    assert session.mode is VoiceMode.S2S_SERVER

    await session.start()
    backend.connect.assert_awaited_once()

    await session.stop()
    backend.close.assert_awaited_once()

    # Idempotent stop.
    await session.stop()


@pytest.mark.asyncio
async def test_s2s_server_session_smoke_without_connect_method():
    """Backend without a connect() method still starts & stops cleanly.

    The reference S2SServerPipeline stub doesn't expose connect() —
    the session must tolerate that (connect is optional, per module
    docstring).
    """
    backend = MagicMock(spec=["endpoint", "close"])
    backend.endpoint = "ws://local"
    backend.close = AsyncMock()

    spec = _spec(VoiceMode.S2S_SERVER, endpoint="ws://local")
    session = S2SServerSession(spec, backend=backend)

    await session.start()
    await session.stop()
    backend.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# VoiceSession base class — lifecycle + state machine (M1.2)
# ---------------------------------------------------------------------------


class _FakeResource:
    """Async context manager recording enter/exit for base-session tests."""

    def __init__(self, log: list[str], name: str) -> None:
        self._log = log
        self._name = name

    async def __aenter__(self):
        self._log.append(f"enter:{self._name}")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._log.append(f"exit:{self._name}")
        return None


class _DummySession(AsyncExitStackBaseSession):
    """Minimal subclass for testing the base-class lifecycle."""

    mode = VoiceMode.CASCADED

    def __init__(self, *, log: list[str] | None = None, fail_on_start: bool = False):
        super().__init__()
        self._log = log if log is not None else []
        self._fail_on_start = fail_on_start
        self.on_start_called = 0
        self.on_stop_called = 0

    async def _on_start(self) -> None:
        self.on_start_called += 1
        # Register a resource so we can observe LIFO cleanup.
        await self._exit_stack.enter_async_context(_FakeResource(self._log, "r1"))
        if self._fail_on_start:
            raise RuntimeError("boom")
        await self._exit_stack.enter_async_context(_FakeResource(self._log, "r2"))

    async def _on_stop(self) -> None:
        self.on_stop_called += 1
        self._log.append("on_stop")


def test_session_satisfies_protocol():
    """The concrete base + a trivial subclass implement the VoiceSession Protocol."""
    s = _DummySession()
    assert isinstance(s, VoiceSession)
    assert s.mode is VoiceMode.CASCADED
    assert s.meta_command_sink is None  # None in 0.4.0 per ADR-0013


def test_session_stop_idempotent():
    """Calling stop() twice is fine; state ends at STOPPED."""

    async def _run():
        s = _DummySession()
        await s.start()
        assert s.state is SessionState.RUNNING
        await s.stop()
        assert s.state is SessionState.STOPPED
        # Second stop() must not raise and must not re-trigger teardown.
        await s.stop()
        assert s.state is SessionState.STOPPED
        # Third for good measure.
        await s.stop()
        assert s.state is SessionState.STOPPED
        # _on_stop only fires on the transition FROM RUNNING.
        assert s.on_stop_called == 1

    asyncio.run(_run())


def test_session_state_machine():
    """
    - start() drives CREATED -> STARTING -> RUNNING.
    - stop() on CREATED is a no-op (lands at STOPPED).
    - start() from STOPPED raises InvalidTransition.
    - Exit-stack resources unwind in LIFO order.
    """

    async def _run():
        # Fresh session is CREATED.
        s = _DummySession()
        assert s.state is SessionState.CREATED
        await s.start()
        assert s.state is SessionState.RUNNING
        # Resources were entered in order r1, r2.
        assert s._log[:2] == ["enter:r1", "enter:r2"]

        await s.stop()
        assert s.state is SessionState.STOPPED
        # _on_stop ran BEFORE exit-stack cleanup (pre-teardown hook).
        # And exit-stack unwinds LIFO -> r2 exits before r1.
        stop_idx = s._log.index("on_stop")
        r2_exit_idx = s._log.index("exit:r2")
        r1_exit_idx = s._log.index("exit:r1")
        assert stop_idx < r2_exit_idx < r1_exit_idx

        # Re-starting a stopped session is forbidden.
        with pytest.raises(InvalidTransition):
            await s.start()

        # stop() on a brand-new (CREATED) session is a no-op that
        # simply lands at STOPPED without calling _on_stop.
        s2 = _DummySession()
        assert s2.state is SessionState.CREATED
        await s2.stop()
        assert s2.state is SessionState.STOPPED
        assert s2.on_stop_called == 0

        # And a stopped-from-CREATED session also can't be started.
        with pytest.raises(InvalidTransition):
            await s2.start()

    asyncio.run(_run())


def test_session_failed_start_cleans_up():
    """If _on_start raises, partial resources are released and state is STOPPED."""

    async def _run():
        log: list[str] = []
        s = _DummySession(log=log, fail_on_start=True)
        with pytest.raises(RuntimeError, match="boom"):
            await s.start()
        assert s.state is SessionState.STOPPED
        # r1 was entered before the failure; it must have been released.
        assert "enter:r1" in log
        assert "exit:r1" in log
        # r2 was never entered (the failure came between r1 and r2).
        assert "enter:r2" not in log
        # stop() after a failed start is still a safe no-op.
        await s.stop()
        assert s.state is SessionState.STOPPED

    asyncio.run(_run())


def test_session_exit_stack_is_shared_resource():
    """Subclasses access the same AsyncExitStack used by stop()."""
    s = _DummySession()
    assert isinstance(s._exit_stack, contextlib.AsyncExitStack)


# ---------------------------------------------------------------------------
# WAVE 1c — factory + capability tests
# ---------------------------------------------------------------------------


import types
from unittest.mock import AsyncMock, MagicMock

from hermes_s2s.voice import (
    CapabilityError,
    ModeRequirements,
    VoiceSessionFactory,
    check_requirements,
    requirements_for,
)
from hermes_s2s.voice.sessions_cascaded import CascadedSession
from hermes_s2s.voice.sessions_realtime import RealtimeSession


def _mk_vc(guild_id: int = 111, channel_id: int = 222):
    """Minimal VoiceClient-shaped mock with .guild.id and .channel.id."""
    vc = MagicMock()
    vc.guild = MagicMock()
    vc.guild.id = guild_id
    vc.channel = MagicMock()
    vc.channel.id = channel_id
    return vc


def _mk_adapter():
    """Minimal adapter that starts without a _s2s_sessions dict."""
    adapter = types.SimpleNamespace()
    return adapter


# --- capability gate --------------------------------------------------------


def test_requirements_for_cascaded_is_empty():
    reqs = requirements_for(VoiceMode.CASCADED, ModeSpec(VoiceMode.CASCADED))
    assert isinstance(reqs, ModeRequirements)
    assert reqs.env_vars == []
    assert reqs.python_packages == []


def test_requirements_for_realtime_gemini_requires_gemini_key():
    spec = ModeSpec(VoiceMode.REALTIME, provider="gemini-live")
    reqs = requirements_for(VoiceMode.REALTIME, spec)
    assert "GEMINI_API_KEY" in reqs.env_vars
    assert "OPENAI_API_KEY" not in reqs.env_vars


def test_requirements_for_realtime_openai_requires_openai_key():
    spec = ModeSpec(VoiceMode.REALTIME, provider="gpt-realtime")
    reqs = requirements_for(VoiceMode.REALTIME, spec)
    assert "OPENAI_API_KEY" in reqs.env_vars
    assert "GEMINI_API_KEY" not in reqs.env_vars


def test_check_requirements_cascaded_always_ok():
    spec = ModeSpec(VoiceMode.CASCADED)
    result = check_requirements(VoiceMode.CASCADED, spec, env={})
    assert result["ok"] is True
    assert result["missing"] == []


def test_check_requirements_realtime_misses_env_var():
    spec = ModeSpec(VoiceMode.REALTIME, provider="gemini-live")
    result = check_requirements(VoiceMode.REALTIME, spec, env={})
    assert result["ok"] is False
    assert "env:GEMINI_API_KEY" in result["missing"]


def test_check_requirements_realtime_satisfied_when_env_set():
    spec = ModeSpec(VoiceMode.REALTIME, provider="gemini-live")
    # Include websockets as found since it's installed in this env.
    result = check_requirements(
        VoiceMode.REALTIME, spec, env={"GEMINI_API_KEY": "xyz"}
    )
    # websockets IS installed in the dev env; if not, test environment
    # itself is broken — accept either answer here.
    if result["ok"]:
        assert result["missing"] == []
    else:
        # The only acceptable miss is pip:websockets (env was satisfied).
        assert all(m.startswith("pip:") for m in result["missing"])


def test_capability_error_user_message_includes_mode_and_missing():
    err = CapabilityError(["env:GEMINI_API_KEY"], VoiceMode.REALTIME)
    msg = err.user_message()
    assert "realtime" in msg
    assert "GEMINI_API_KEY" in msg


# --- factory construction ---------------------------------------------------


def test_factory_builds_cascaded_session_with_no_capability_requirements():
    """Cascaded is the zero-dep baseline — factory returns CascadedSession."""
    factory = VoiceSessionFactory(registry=None)
    spec = ModeSpec(VoiceMode.CASCADED, options={"_explicit": True})
    vc = _mk_vc()
    adapter = _mk_adapter()
    session = factory.build(spec, vc, adapter)
    assert isinstance(session, CascadedSession)
    # Registration keyed per-channel.
    key = (vc.guild.id, vc.channel.id)
    assert adapter._s2s_sessions[key] is session


def test_factory_idempotent(monkeypatch):
    """Calling factory.build() twice on the same adapter registers under
    the same key; the second call replaces but doesn't double-wrap."""
    factory = VoiceSessionFactory(registry=None)
    spec = ModeSpec(VoiceMode.CASCADED, options={"_explicit": True})
    vc = _mk_vc()
    adapter = _mk_adapter()

    s1 = factory.build(spec, vc, adapter)
    s2 = factory.build(spec, vc, adapter)

    key = (vc.guild.id, vc.channel.id)
    # Only one entry per (guild, channel) — the dict slot is replaced.
    assert adapter._s2s_sessions[key] is s2
    assert s1 is not s2

    # Also: the discord_bridge _BRIDGE_WRAPPED_MARKER idempotency guard
    # must still prevent double-wrap if the install runs twice. Verify
    # by invoking _install_via_monkey_patch with a stub gateway module.
    import sys

    from hermes_s2s._internal import discord_bridge

    class _StubAdapterCls:
        async def join_voice_channel(self, channel):  # pragma: no cover
            return None

        async def leave_voice_channel(self, *a, **k):  # pragma: no cover
            return None

    stub_mod = types.ModuleType("gateway.platforms.discord")
    stub_mod.DiscordAdapter = _StubAdapterCls
    monkeypatch.setitem(sys.modules, "gateway", types.ModuleType("gateway"))
    monkeypatch.setitem(
        sys.modules, "gateway.platforms", types.ModuleType("gateway.platforms")
    )
    monkeypatch.setitem(sys.modules, "gateway.platforms.discord", stub_mod)

    discord_bridge._install_via_monkey_patch(ctx=None)
    first_wrap = _StubAdapterCls.join_voice_channel
    assert getattr(first_wrap, discord_bridge._BRIDGE_WRAPPED_MARKER, False) is True

    # Second call should be a no-op — the marker short-circuits.
    discord_bridge._install_via_monkey_patch(ctx=None)
    assert _StubAdapterCls.join_voice_channel is first_wrap


def test_factory_falls_back_to_cascaded_when_config_default_unavailable():
    """Config default is realtime + no GEMINI_API_KEY → CascadedSession, no raise."""
    factory = VoiceSessionFactory(registry=None)
    # _explicit=False (i.e. came from config default) → warn+fallback.
    spec = ModeSpec(
        VoiceMode.REALTIME,
        provider="gemini-live",
        options={"_explicit": False},
    )
    vc = _mk_vc()
    adapter = _mk_adapter()

    import os

    prior = os.environ.pop("GEMINI_API_KEY", None)
    try:
        session = factory.build(spec, vc, adapter)
    finally:
        if prior is not None:
            os.environ["GEMINI_API_KEY"] = prior
    # Must land on cascaded (not raise).
    assert isinstance(session, CascadedSession)


def test_factory_raises_when_slash_explicit_unavailable():
    """_explicit=True + missing capability → CapabilityError (fail-closed)."""
    factory = VoiceSessionFactory(registry=None)
    spec = ModeSpec(
        VoiceMode.REALTIME,
        provider="gemini-live",
        options={"_explicit": True},
    )
    vc = _mk_vc()
    adapter = _mk_adapter()

    import os

    prior = os.environ.pop("GEMINI_API_KEY", None)
    try:
        with pytest.raises(CapabilityError) as excinfo:
            factory.build(spec, vc, adapter)
    finally:
        if prior is not None:
            os.environ["GEMINI_API_KEY"] = prior
    assert excinfo.value.mode is VoiceMode.REALTIME
    assert any("GEMINI_API_KEY" in m for m in excinfo.value.missing)


def test_factory_realtime_path_uses_resolve_bridge_params(monkeypatch):
    """RealtimeSession is built with system_prompt/voice from
    _resolve_bridge_params (the v0.3.9 regression fence) when backend is
    resolved via the registry."""
    from hermes_s2s._internal import discord_bridge

    # Fake cfg so _resolve_bridge_params returns a known (prompt, voice).
    class _FakeCfg:
        realtime_provider = "gemini-live"
        realtime_options = {
            "gemini_live": {
                "system_prompt": "MARKER-FROM-CFG",
                "voice": "Puck",
            }
        }

    fake_cfg = _FakeCfg()

    # Patch load_config via the factory's lazy import point.
    import hermes_s2s.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "load_config", lambda: fake_cfg, raising=False)

    # Fake registry that returns a fake backend.
    fake_backend = object()
    registry = MagicMock()
    registry.resolve_realtime = MagicMock(return_value=fake_backend)

    factory = VoiceSessionFactory(registry=registry)
    # Set GEMINI_API_KEY so capability gate passes.
    import os

    prior = os.environ.get("GEMINI_API_KEY")
    os.environ["GEMINI_API_KEY"] = "xyz"
    try:
        spec = ModeSpec(
            VoiceMode.REALTIME,
            provider="gemini-live",
            options={"_explicit": True},
        )
        vc = _mk_vc()
        adapter = _mk_adapter()
        session = factory.build(spec, vc, adapter)
    finally:
        if prior is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = prior

    assert isinstance(session, RealtimeSession)
    assert session._system_prompt == "MARKER-FROM-CFG"
    assert session._voice == "Puck"
    assert session._backend is fake_backend


# --- discord_bridge join_voice_channel wrapper + CapabilityError rollback ---


def test_capability_error_rolls_back_vc_join(monkeypatch):
    """When factory.build raises CapabilityError inside the wrapped
    join_voice_channel, the wrapper must:
      1. Call voice_client.disconnect() (if connected).
      2. Not propagate the exception into discord.py.

    We simulate by calling the installed wrapper directly on a stub
    adapter, with a monkey-patched _install_bridge_on_adapter that
    raises CapabilityError.
    """
    import asyncio as _asyncio
    import sys

    from hermes_s2s._internal import discord_bridge

    class _StubAdapterCls:
        async def join_voice_channel(self, channel):
            # Simulate "Hermes's original join" — record a connected VC.
            self._connected_channel = channel
            return True

        async def leave_voice_channel(self, *a, **k):
            return None

    stub_mod = types.ModuleType("gateway.platforms.discord")
    stub_mod.DiscordAdapter = _StubAdapterCls
    monkeypatch.setitem(sys.modules, "gateway", types.ModuleType("gateway"))
    monkeypatch.setitem(
        sys.modules, "gateway.platforms", types.ModuleType("gateway.platforms")
    )
    monkeypatch.setitem(sys.modules, "gateway.platforms.discord", stub_mod)

    # Patch _install_bridge_on_adapter to raise CapabilityError — this
    # simulates the factory inside the wrapper refusing to build.
    def _raise_capability_error(adapter, channel, ctx):
        raise CapabilityError(
            ["env:GEMINI_API_KEY"], VoiceMode.REALTIME
        )

    monkeypatch.setattr(
        discord_bridge, "_install_bridge_on_adapter", _raise_capability_error
    )

    # Install wrapper + capture mock VC.
    disconnect_mock = AsyncMock()
    vc = MagicMock()
    vc.is_connected = MagicMock(return_value=True)
    vc.disconnect = disconnect_mock

    # Stub the adapter's state so the wrapper can find the VC.
    adapter = _StubAdapterCls()
    guild = MagicMock()
    guild.id = 42
    channel = MagicMock()
    channel.guild = guild
    channel.id = 100
    adapter._voice_clients = {42: vc}
    adapter._voice_text_channels = {}

    # Install the wrap.
    discord_bridge._install_via_monkey_patch(ctx=None)
    wrapped = _StubAdapterCls.join_voice_channel
    assert getattr(wrapped, discord_bridge._BRIDGE_WRAPPED_MARKER, False)

    async def _invoke():
        return await wrapped(adapter, channel)

    # The wrapper must NOT propagate CapabilityError.
    result = _asyncio.run(_invoke())
    # Return value should still be the original True (VC join succeeded
    # before we refused to attach the bridge), OR the wrapper can return
    # None after rollback — accept either but require disconnect() was
    # called.
    assert disconnect_mock.await_count == 1, (
        "wrapper must call vc.disconnect() on CapabilityError"
    )
    # Sanity: the join result isn't propagated as an exception.
    assert result is True or result is None


# ---------------------------------------------------------------------------
# P0-6 — leave_voice_channel stops S2S sessions registered on the adapter
# ---------------------------------------------------------------------------


def test_leave_voice_calls_session_stop():
    """``/voice leave`` must call ``session.stop()`` on matching _s2s_sessions.

    Regression test for P0-6 (session leak on /voice leave): before this
    fix, pipeline / s2s-server sessions registered on
    ``adapter._s2s_sessions`` were orphaned when the user left the VC,
    keeping their subprocess / WS resources open. The
    ``_wrap_leave_voice_channel`` monkey-patch must iterate the dict and
    stop every session whose key matches the leaving guild.
    """
    import asyncio as _asyncio
    from unittest.mock import AsyncMock, MagicMock

    from hermes_s2s._internal import discord_bridge

    # Minimal stub adapter class that the wrapper can be applied to.
    class _StubAdapterCls:
        async def leave_voice_channel(self, *args, **kwargs):
            return "left"

    # Apply the wrap directly (don't go through _install_via_monkey_patch
    # — we just want this one function exercised in isolation).
    discord_bridge._wrap_leave_voice_channel(_StubAdapterCls)
    wrapped = _StubAdapterCls.leave_voice_channel
    assert getattr(wrapped, discord_bridge._S2S_LEAVE_WRAPPED_MARKER, False)

    # Build an adapter with two sessions: one matching the leaving
    # guild (should stop) and one belonging to a different guild
    # (must be left alone).
    adapter = _StubAdapterCls()
    guild_id = 42
    other_guild_id = 99

    matching_session = MagicMock()
    matching_session.stop = AsyncMock()
    non_matching_session = MagicMock()
    non_matching_session.stop = AsyncMock()

    adapter._s2s_sessions = {
        (guild_id, 100): matching_session,
        (other_guild_id, 200): non_matching_session,
    }

    # Invoke leave with a raw guild id (simplest shape the wrapper
    # handles — see its guild-id extraction block).
    result = _asyncio.run(wrapped(adapter, guild_id))

    # Original leave_voice_channel return value flows through.
    assert result == "left"
    # Matching session was stopped + removed from the dict.
    matching_session.stop.assert_awaited_once()
    assert (guild_id, 100) not in adapter._s2s_sessions
    # Non-matching session is untouched.
    non_matching_session.stop.assert_not_called()
    assert (other_guild_id, 200) in adapter._s2s_sessions
