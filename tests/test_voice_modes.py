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
