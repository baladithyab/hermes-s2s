"""Tests for `hermes s2s setup` wizard."""

from __future__ import annotations

import argparse
import sys
import types
from pathlib import Path

import pytest
import yaml

from hermes_s2s import cli


def _make_args(**kwargs) -> argparse.Namespace:
    defaults = {"profile": None, "dry_run": False, "config_path": None, "s2s_command": "setup"}
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_local_all_dry_run_produces_expected_dict(capsys, tmp_path, monkeypatch):
    # dry-run shouldn't touch files, but we isolate HOME anyway
    monkeypatch.setenv("HOME", str(tmp_path))
    args = _make_args(profile="local-all", dry_run=True, config_path=str(tmp_path / "config.yaml"))
    rc = cli.cmd_setup(args)
    assert rc == 0
    captured = capsys.readouterr()
    # The printed YAML should include tts.provider: hermes-s2s-kokoro
    parsed = yaml.safe_load(captured.out.split("# dry-run")[1].split("\n", 1)[1].split("# .env")[0])
    assert parsed["tts"]["provider"] == "hermes-s2s-kokoro"
    assert parsed["tts"]["providers"]["hermes-s2s-kokoro"]["type"] == "command"
    assert parsed["stt"]["provider"] == "local"
    # no config file written
    assert not (tmp_path / "config.yaml").exists()


def test_hybrid_privacy_writes_correct_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_path = tmp_path / "config.yaml"
    args = _make_args(profile="hybrid-privacy", config_path=str(cfg_path))
    rc = cli.cmd_setup(args)
    assert rc == 0
    assert cfg_path.exists()
    data = yaml.safe_load(cfg_path.read_text())
    assert data["tts"]["provider"] == "elevenlabs"
    assert data["tts"]["providers"]["elevenlabs"]["type"] == "elevenlabs"
    assert data["stt"]["provider"] == "local"
    # .env should have HERMES_LOCAL_STT_COMMAND with marker
    env_path = tmp_path / ".env"
    assert env_path.exists()
    env_txt = env_path.read_text()
    assert "HERMES_LOCAL_STT_COMMAND=" in env_txt
    assert "hermes-s2s setup" in env_txt  # marker
    # Second run is idempotent — marker prevents duplication
    cli.cmd_setup(_make_args(profile="hybrid-privacy", config_path=str(cfg_path)))
    assert env_path.read_text().count("HERMES_LOCAL_STT_COMMAND=") == 1


def test_missing_dep_warning_surfaces(tmp_path, monkeypatch, capsys):
    # Force moonshine_onnx to look unimportable
    import importlib.util as _ilu

    real_find_spec = _ilu.find_spec

    def fake_find_spec(name, *a, **kw):
        if name == "moonshine_onnx":
            return None
        return real_find_spec(name, *a, **kw)

    monkeypatch.setattr(cli.importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setenv("HOME", str(tmp_path))
    args = _make_args(profile="local-all", dry_run=True, config_path=str(tmp_path / "c.yaml"))
    cli.cmd_setup(args)
    err = capsys.readouterr().err
    assert "Missing dependencies" in err
    assert "moonshine_onnx" in err


def test_existing_config_keys_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_path = tmp_path / "config.yaml"
    existing = {
        "model": "claude-sonnet-4.5",
        "providers": {
            "openrouter": {"api_key": "sk-or-existing"},
        },
        "tts": {
            "provider": "old-provider",
            "providers": {"old-provider": {"type": "old"}},
            "unrelated_setting": "keep-me",
        },
    }
    cfg_path.write_text(yaml.safe_dump(existing))
    args = _make_args(profile="local-all", config_path=str(cfg_path))
    cli.cmd_setup(args)
    data = yaml.safe_load(cfg_path.read_text())
    # Unrelated top-level keys preserved
    assert data["model"] == "claude-sonnet-4.5"
    assert data["providers"]["openrouter"]["api_key"] == "sk-or-existing"
    # tts.provider was overwritten with new profile value
    assert data["tts"]["provider"] == "hermes-s2s-kokoro"
    # but the unrelated nested tts setting is preserved (deep-merge)
    assert data["tts"].get("unrelated_setting") == "keep-me"
    # old provider entry remains alongside new one (deep merge, not replace)
    assert "old-provider" in data["tts"]["providers"]
    assert "hermes-s2s-kokoro" in data["tts"]["providers"]
