"""Smoke tests for hermes-s2s — verify imports, registry, config defaults, and tool surface."""

from __future__ import annotations

import json

import pytest


def test_package_imports():
    import hermes_s2s
    assert hermes_s2s.__version__


def test_version_is_0_5_1():
    """Sanity: bump version each release; smoke test catches forgotten bumps."""
    import hermes_s2s
    assert hermes_s2s.__version__ == "0.5.1", (
        f"__version__ is {hermes_s2s.__version__!r}; expected '0.5.1'. "
        "Update hermes_s2s/__init__.py and pyproject.toml together."
    )


def test_registry_starts_empty_then_populates():
    from hermes_s2s.providers import (
        register_builtin_pipeline_providers,
        register_builtin_realtime_providers,
        register_builtin_stt_providers,
        register_builtin_tts_providers,
    )
    from hermes_s2s.registry import list_registered

    register_builtin_stt_providers()
    register_builtin_tts_providers()
    register_builtin_realtime_providers()
    register_builtin_pipeline_providers()

    snapshot = list_registered()
    assert "moonshine" in snapshot["stt"]
    assert "kokoro" in snapshot["tts"]
    # Realtime + pipeline backends are stubs but should still be registered
    assert "gemini-live" in snapshot["realtime"]
    assert "gpt-realtime-mini" in snapshot["realtime"]
    assert "s2s-server" in snapshot["pipeline"]


def test_config_defaults_sensible(tmp_path, monkeypatch):
    from hermes_s2s.config import S2SConfig, load_config

    cfg = S2SConfig.from_dict({})
    assert cfg.mode == "cascaded"
    assert cfg.stt.provider == "moonshine"
    assert cfg.tts.provider == "kokoro"
    assert cfg.realtime_provider == "gemini-live"


def test_config_round_trips_provider_options():
    from hermes_s2s.config import S2SConfig

    cfg = S2SConfig.from_dict({
        "mode": "cascaded",
        "pipeline": {
            "stt": {"provider": "moonshine", "moonshine": {"model": "base"}},
            "tts": {"provider": "kokoro", "kokoro": {"voice": "af_bella"}},
        },
    })
    assert cfg.stage_options("stt") == {"model": "base"}
    assert cfg.stage_options("tts") == {"voice": "af_bella"}


def test_status_tool_returns_valid_json():
    # Make sure built-in providers are registered first
    from hermes_s2s.providers import (
        register_builtin_pipeline_providers,
        register_builtin_realtime_providers,
        register_builtin_stt_providers,
        register_builtin_tts_providers,
    )
    register_builtin_stt_providers()
    register_builtin_tts_providers()
    register_builtin_realtime_providers()
    register_builtin_pipeline_providers()

    from hermes_s2s.tools import s2s_status
    result = s2s_status({})
    payload = json.loads(result)
    assert "active_mode" in payload
    assert payload["registered_backends"]["stt"]
    assert "moonshine" in payload["registered_backends"]["stt"]


def test_set_mode_rejects_invalid():
    from hermes_s2s.tools import s2s_set_mode
    out = json.loads(s2s_set_mode({"mode": "telepathy"}))
    assert "error" in out


def test_set_mode_accepts_valid():
    from hermes_s2s.tools import s2s_set_mode
    out = json.loads(s2s_set_mode({"mode": "realtime"}, task_id="t1"))
    assert out["ok"] is True
    assert out["session_mode"] == "realtime"


def test_realtime_backend_requires_api_key(monkeypatch):
    """Gemini Live backend should fail loudly when no API key is configured."""
    import asyncio
    from hermes_s2s.providers.realtime.gemini_live import make_gemini_live

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    backend = make_gemini_live({})
    with pytest.raises((RuntimeError, NotImplementedError)):
        asyncio.new_event_loop().run_until_complete(
            backend.connect("system prompt", "Aoede", [])
        )
