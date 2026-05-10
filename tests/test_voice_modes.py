"""WAVE 1a tests: VoiceMode + ModeRouter precedence (M1.1).

Per plan docs/plans/wave-0.4.0-rearchitecture.md:
- This file is SHARED between W1a (this commit) and W1b (concrete
  session tests land later).
- W1a M1.1 owns: mode-router precedence, typo rejection, alias
  normalization.
- W1a M1.2 (next commit) adds base-session lifecycle tests.
- W1b owns: the four per-session tests (do not add them here).
"""

from __future__ import annotations

import pytest

from hermes_s2s.voice import ModeRouter, ModeSpec, VoiceMode


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
