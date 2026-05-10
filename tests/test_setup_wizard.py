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


def test_env_stt_command_is_shell_quoted(tmp_path):
    """P1-D: HERMES_LOCAL_STT_COMMAND must be shlex.quote'd, not naive '{cmd}'.

    Pass a command containing an apostrophe and a space — naive single-quote
    wrapping would produce unparseable shell syntax. Verify the written line
    round-trips cleanly through shlex.split so a shell/dotenv parser can
    recover the original command.
    """
    import shlex as _shlex

    env_path = tmp_path / ".env"
    tricky = "hermes-s2s-stt --note \"it's fine\" --input {input_path} --output {output_path}"

    wrote = cli._write_env_command(env_path, tricky)
    assert wrote is True
    content = env_path.read_text()
    assert "HERMES_LOCAL_STT_COMMAND=" in content

    # Extract the VALUE portion of the HERMES_LOCAL_STT_COMMAND=... line.
    value_line = next(
        line for line in content.splitlines() if line.startswith("HERMES_LOCAL_STT_COMMAND=")
    )
    raw_value = value_line.split("=", 1)[1]

    # shlex.split must yield exactly one token which, when dequoted, equals
    # the original command — i.e. the shell-quoted form is faithful.
    tokens = _shlex.split(raw_value)
    assert tokens == [tricky]

    # And the raw line must NOT be the naive single-quote wrap (which would
    # break on the embedded apostrophe).
    assert raw_value != f"'{tricky}'"


# ---------------------------------------------------------------------------
# G2: realtime profile tests
# ---------------------------------------------------------------------------


def _parse_dry_run_yaml(stdout: str) -> dict:
    """Extract the YAML body between '# dry-run' header and the next '# .env' line."""
    after = stdout.split("# dry-run", 1)[1].split("\n", 1)[1]
    body = after.split("# .env")[0]
    return yaml.safe_load(body)


def test_setup_realtime_gemini_writes_full_config(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    args = _make_args(
        profile="realtime-gemini",
        dry_run=True,
        config_path=str(tmp_path / "config.yaml"),
    )
    rc = cli.cmd_setup(args)
    assert rc == 0
    out = capsys.readouterr().out
    parsed = _parse_dry_run_yaml(out)
    assert parsed["s2s"]["mode"] == "realtime"
    assert parsed["s2s"]["realtime"]["provider"] == "gemini-live"
    assert (
        parsed["s2s"]["realtime"]["gemini_live"]["model"] == "gemini-2.5-flash-native-audio-latest"
    )
    assert parsed["s2s"]["realtime"]["gemini_live"]["voice"] == "Aoede"


def test_setup_realtime_openai_writes_full_config(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    args = _make_args(
        profile="realtime-openai",
        dry_run=True,
        config_path=str(tmp_path / "config.yaml"),
    )
    rc = cli.cmd_setup(args)
    assert rc == 0
    out = capsys.readouterr().out
    parsed = _parse_dry_run_yaml(out)
    assert parsed["s2s"]["mode"] == "realtime"
    assert parsed["s2s"]["realtime"]["provider"] == "gpt-realtime"
    assert parsed["s2s"]["realtime"]["openai"]["model"] == "gpt-realtime"
    assert parsed["s2s"]["realtime"]["openai"]["voice"] == "alloy"


def test_setup_realtime_appends_monkeypatch_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_path = tmp_path / "config.yaml"
    args = _make_args(profile="realtime-gemini", config_path=str(cfg_path))
    rc = cli.cmd_setup(args)
    assert rc == 0
    # Config is written alongside; .env lives in the same parent directory
    env_path = tmp_path / ".env"
    assert env_path.exists(), "expected .env to be created next to config.yaml"
    env_txt = env_path.read_text()
    assert "HERMES_S2S_MONKEYPATCH_DISCORD=1" in env_txt
    # Also verify the s2s block was merged into config.yaml
    data = yaml.safe_load(cfg_path.read_text())
    assert data["s2s"]["mode"] == "realtime"
    assert data["s2s"]["realtime"]["provider"] == "gemini-live"


def test_setup_realtime_idempotent_env_append(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_path = tmp_path / "config.yaml"
    args = _make_args(profile="realtime-gemini", config_path=str(cfg_path))
    cli.cmd_setup(args)
    cli.cmd_setup(args)
    env_txt = (tmp_path / ".env").read_text()
    assert env_txt.count("HERMES_S2S_MONKEYPATCH_DISCORD=1") == 1


def test_setup_realtime_warns_on_missing_api_key(capsys, tmp_path, monkeypatch):
    # Run twice: once with the key set, once unset. Diff the [✓]/[✗] mark on
    # the GEMINI_API_KEY line to prove the warning actually reflects state
    # (a substring like "GEMINI_API_KEY" alone is printed regardless).
    monkeypatch.setenv("HOME", str(tmp_path))
    args = _make_args(
        profile="realtime-gemini",
        dry_run=True,
        config_path=str(tmp_path / "config.yaml"),
    )

    monkeypatch.setenv("GEMINI_API_KEY", "test-key-set")
    cli.cmd_setup(args)
    out_set = capsys.readouterr().out

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    cli.cmd_setup(args)
    out_unset = capsys.readouterr().out

    # Both runs print the readiness checklist mentioning the env var name +
    # remediation URL, so those substrings appear in BOTH outputs.
    assert "GEMINI_API_KEY" in out_set
    assert "GEMINI_API_KEY" in out_unset
    assert "aistudio.google.com" in out_unset

    # The actual differentiator is the unicode mark next to the line:
    # set -> [✓], unset -> [✗]. Find each and assert the mark differs.
    def _mark_for_line(text: str, label: str) -> str:
        for line in text.splitlines():
            if label in line and ("[\u2713]" in line or "[\u2717]" in line):
                return "set" if "[\u2713]" in line else "unset"
        return "missing"

    assert _mark_for_line(out_set, "GEMINI_API_KEY") == "set"
    assert _mark_for_line(out_unset, "GEMINI_API_KEY") == "unset"

