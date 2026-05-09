"""Tests for the ``hermes s2s doctor`` pre-flight check (ADR-0009 §2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes_s2s import doctor
from hermes_s2s.doctor import format_human, format_json, run_doctor


# ---------- helpers ----------


def _write_config(tmp_path: Path, payload: dict) -> Path:
    import yaml

    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"s2s": payload}))
    return p


def _install_tmp_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict) -> None:
    """Point hermes_s2s.config.load_config at a tmp yaml."""
    cfg_path = _write_config(tmp_path, payload)
    import hermes_s2s.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", cfg_path)


# ---------- a. cascaded: skip backend connectivity probe ----------


def test_run_doctor_no_realtime_returns_cascaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_tmp_config(monkeypatch, tmp_path, {"mode": "cascaded"})
    report = run_doctor(probe=True)
    # Config check should mention cascaded.
    cfg_checks = [c for c in report["checks"] if c["category"] == "configuration"]
    assert any("cascaded" in c["message"] for c in cfg_checks), cfg_checks
    # Backend connectivity should be skipped for cascaded mode.
    bc = [c for c in report["checks"] if c["category"] == "backend_connectivity"]
    assert bc and all(c["status"] in ("skip", "pass") for c in bc), bc


# ---------- b. realtime + missing API key => fail ----------


def test_run_doctor_realtime_no_api_key_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_tmp_config(
        monkeypatch,
        tmp_path,
        {"mode": "realtime", "realtime": {"provider": "gemini-live"}},
    )
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    report = run_doctor(probe=False)  # no probe to stay hermetic
    assert report["overall_status"] == "fail"
    api_keys = [c for c in report["checks"] if c["category"] == "api_keys"]
    gemini_fail = [
        c for c in api_keys if "GEMINI" in c["name"].upper() and c["status"] == "fail"
    ]
    assert gemini_fail, api_keys


# ---------- c. format_human includes all 6 category labels ----------


def test_format_human_includes_all_categories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_tmp_config(monkeypatch, tmp_path, {"mode": "cascaded"})
    report = run_doctor(probe=False)
    out = format_human(report)
    for label in (
        "Configuration",
        "Python dependencies",
        "System dependencies",
        "API keys",
        "Hermes integration",
        "Backend connectivity",
    ):
        assert label in out, f"{label!r} not in formatted output"


# ---------- d. format_json parses round-trip ----------


def test_format_json_parseable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_tmp_config(monkeypatch, tmp_path, {"mode": "cascaded"})
    report = run_doctor(probe=False)
    js = format_json(report)
    parsed = json.loads(js)
    assert parsed["overall_status"] == report["overall_status"]
    assert parsed["checks"] == report["checks"]


# ---------- e. probe=False skips connectivity ----------


def test_run_doctor_no_probe_skips_connectivity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_tmp_config(
        monkeypatch,
        tmp_path,
        {"mode": "realtime", "realtime": {"provider": "gemini-live"}},
    )
    monkeypatch.setenv("GEMINI_API_KEY", "sk-fake-" + "x" * 30)
    report = run_doctor(probe=False)
    bc = [c for c in report["checks"] if c["category"] == "backend_connectivity"]
    # Either absent, or all skip-status.
    assert not bc or all(c["status"] == "skip" for c in bc), bc


# ---------- f. probe=True with mocked backend => connectivity passes ----------


def test_run_doctor_with_probe_mocked_websocket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_tmp_config(
        monkeypatch,
        tmp_path,
        {"mode": "realtime", "realtime": {"provider": "gemini-live"}},
    )
    monkeypatch.setenv("GEMINI_API_KEY", "sk-fake-" + "x" * 30)

    # Mock resolve_realtime to return a fake backend that connects successfully.
    fake_backend = MagicMock()
    fake_backend.connect = AsyncMock(return_value=None)
    fake_backend.close = AsyncMock(return_value=None)

    import hermes_s2s.registry as reg

    monkeypatch.setattr(reg, "resolve_realtime", lambda name, cfg: fake_backend)

    report = run_doctor(probe=True)
    bc = [c for c in report["checks"] if c["category"] == "backend_connectivity"]
    assert bc, "expected backend_connectivity check"
    assert any(c["status"] == "pass" for c in bc), bc
    fake_backend.connect.assert_awaited()
