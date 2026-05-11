"""Tests for ConnectOptions and RealtimeConfig dataclasses (S5)."""

from __future__ import annotations

import pytest

from hermes_s2s.config.realtime_config import (
    AudioConfig,
    HistoryConfig,
    RealtimeConfig,
)
from hermes_s2s.voice.connect_options import ConnectOptions


class TestConnectOptions:
    def test_minimal_construction(self) -> None:
        opts = ConnectOptions(system_prompt="hi", voice="Aoede", tools=[])
        assert opts.system_prompt == "hi"
        assert opts.voice == "Aoede"
        assert opts.tools == []
        assert opts.history is None
        assert opts.tier is None  # v0.5.0 reserved
        assert opts.confirm_policy is None

    def test_history_defaults_to_none(self) -> None:
        opts = ConnectOptions(system_prompt="x", voice="x", tools=[])
        assert opts.history is None

    def test_frozen(self) -> None:
        opts = ConnectOptions(system_prompt="x", voice="x", tools=[])
        with pytest.raises(dataclasses.FrozenInstanceError if False else Exception):
            opts.system_prompt = "y"  # type: ignore[misc]

    def test_from_positional_legacy_call(self) -> None:
        # Pre-v0.4.2 callers passed positional triple
        opts = ConnectOptions.from_positional("hello", "Aoede", [{"name": "tool1"}])
        assert opts.system_prompt == "hello"
        assert opts.voice == "Aoede"
        assert opts.tools == [{"name": "tool1"}]
        assert opts.history is None

    def test_from_positional_with_kwargs(self) -> None:
        opts = ConnectOptions.from_positional(
            "hi", "Aoede", [], history=[{"role": "user", "content": "hi"}]
        )
        assert opts.history == [{"role": "user", "content": "hi"}]

    def test_from_positional_normalises_tools_none_to_empty(self) -> None:
        opts = ConnectOptions.from_positional("x", "x", None)  # type: ignore[arg-type]
        assert opts.tools == []

    def test_from_positional_v050_reserved_kwargs(self) -> None:
        opts = ConnectOptions.from_positional(
            "x", "x", [], tier="read", confirm_policy={"window": 5}, language_code="en-US"
        )
        assert opts.tier == "read"
        assert opts.confirm_policy == {"window": 5}
        assert opts.language_code == "en-US"

    def test_extras_default_empty_mapping(self) -> None:
        opts = ConnectOptions(system_prompt="x", voice="x", tools=[])
        assert dict(opts.extras) == {}


class TestHistoryConfig:
    def test_defaults(self) -> None:
        cfg = HistoryConfig()
        assert cfg.enabled is True
        assert cfg.max_turns == 20
        assert cfg.max_tokens == 8000

    def test_from_dict_empty(self) -> None:
        assert HistoryConfig.from_dict(None) == HistoryConfig()
        assert HistoryConfig.from_dict({}) == HistoryConfig()

    def test_from_dict_overrides(self) -> None:
        cfg = HistoryConfig.from_dict(
            {"enabled": False, "max_turns": 5, "max_tokens": 1000}
        )
        assert cfg.enabled is False
        assert cfg.max_turns == 5
        assert cfg.max_tokens == 1000

    def test_from_dict_partial(self) -> None:
        cfg = HistoryConfig.from_dict({"max_turns": 50})
        assert cfg.max_turns == 50
        assert cfg.enabled is True  # default
        assert cfg.max_tokens == 8000  # default


class TestAudioConfig:
    def test_defaults(self) -> None:
        cfg = AudioConfig()
        assert cfg.resampler == "soxr"
        assert cfg.silence_fade_ms == 5

    def test_unknown_resampler_falls_back(self) -> None:
        cfg = AudioConfig.from_dict({"resampler": "ffmpeg"})
        assert cfg.resampler == "soxr"

    def test_negative_fade_clamped_to_zero(self) -> None:
        cfg = AudioConfig.from_dict({"silence_fade_ms": -10})
        assert cfg.silence_fade_ms == 0

    def test_excessive_fade_clamped_to_50(self) -> None:
        cfg = AudioConfig.from_dict({"silence_fade_ms": 200})
        assert cfg.silence_fade_ms == 50

    def test_scipy_accepted(self) -> None:
        cfg = AudioConfig.from_dict({"resampler": "scipy"})
        assert cfg.resampler == "scipy"


class TestRealtimeConfig:
    def test_defaults(self) -> None:
        cfg = RealtimeConfig()
        assert cfg.history.enabled is True
        assert cfg.audio.resampler == "soxr"
        assert cfg.tools is None  # v0.5.0 reserved

    def test_from_dict_empty(self) -> None:
        assert RealtimeConfig.from_dict(None) == RealtimeConfig()
        assert RealtimeConfig.from_dict({}) == RealtimeConfig()

    def test_from_dict_nested(self) -> None:
        cfg = RealtimeConfig.from_dict(
            {
                "history": {"max_turns": 30},
                "audio": {"silence_fade_ms": 10},
            }
        )
        assert cfg.history.max_turns == 30
        assert cfg.audio.silence_fade_ms == 10

    def test_from_full_config_walks_nested_path(self) -> None:
        full = {
            "s2s": {
                "voice": {
                    "realtime": {
                        "history": {"enabled": False},
                        "audio": {"resampler": "scipy"},
                    }
                }
            }
        }
        cfg = RealtimeConfig.from_full_config(full)
        assert cfg.history.enabled is False
        assert cfg.audio.resampler == "scipy"

    def test_from_full_config_missing_path(self) -> None:
        cfg = RealtimeConfig.from_full_config({"unrelated": "stuff"})
        assert cfg == RealtimeConfig()

    def test_unknown_top_level_keys_ignored(self) -> None:
        cfg = RealtimeConfig.from_dict(
            {"history": {}, "audio": {}, "future_v060_thing": {"x": 1}}
        )
        # Doesn't raise; future keys silently dropped
        assert cfg.history == HistoryConfig()

    def test_tools_passthrough_for_v050(self) -> None:
        # v0.5.0 will populate tools; today we just stash it
        cfg = RealtimeConfig.from_dict({"tools": {"tier": "read"}})
        assert cfg.tools == {"tier": "read"}


# ---------- back-compat fence ----------------------------------------- #


def test_existing_positional_connect_signature_still_supported() -> None:
    """Critical fence: ``backend.connect(prompt, voice, tools)`` MUST keep working.

    Pre-v0.4.2 tests and any external code call connect positionally.
    The v0.4.2 protocol gains ``ConnectOptions`` overloads but the
    positional triple stays a no-op-equivalent path.
    """
    # We can't import the actual backends without env (websockets etc.),
    # but we can fence the dataclass adapter so any new field defaults
    # don't break the from_positional() round-trip.
    opts = ConnectOptions.from_positional("p", "v", [{"name": "t"}])
    assert opts.system_prompt == "p"
    assert opts.voice == "v"
    assert opts.tools == [{"name": "t"}]
    # Unknown future fields land as None / defaults — never raise:
    assert opts.tier is None
    assert opts.history is None


# Need to import dataclasses at module-level for the FrozenInstanceError test
import dataclasses  # noqa: E402
