"""Tests for hermes_s2s.migrate_0_4 — config translation + CLI.

Covers translate_config (pure), apply/rollback/dry-run drivers, and the
auto-translate-on-first-load fallback wired into
``hermes_s2s.config.load_config`` (M5.1 part 2).
"""

from __future__ import annotations

import copy
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from hermes_s2s import migrate_0_4


# ---------------------------------------------------------------------------
# translate_config — pure function tests
# ---------------------------------------------------------------------------


def _sample_realtime_config() -> dict:
    return {
        "model": "anthropic/claude-opus-4.7",
        "provider": "openrouter",
        "s2s": {
            "mode": "realtime",
            "realtime": {
                "provider": "gemini-live",
                "gemini_live": {
                    "model": "gemini-2.5-flash-native-audio-latest",
                    "voice": "Aoede",
                    "language_code": "en-US",
                    "system_prompt": "You are ARIA.",
                },
            },
        },
    }


def test_translate_simple_realtime_config():
    cfg = _sample_realtime_config()
    new_cfg, _warns = migrate_0_4.translate_config(cfg)
    assert new_cfg["s2s"]["voice"]["default_mode"] == "realtime"
    # Provider block preserved verbatim.
    gl = new_cfg["s2s"]["realtime"]["gemini_live"]
    assert gl["model"] == "gemini-2.5-flash-native-audio-latest"
    assert gl["voice"] == "Aoede"
    assert new_cfg["s2s"]["realtime"]["provider"] == "gemini-live"


def test_translate_cascaded_config():
    cfg = {"s2s": {"mode": "cascaded"}}
    new_cfg, _ = migrate_0_4.translate_config(cfg)
    assert new_cfg["s2s"]["voice"]["default_mode"] == "cascaded"


def test_translate_idempotent():
    cfg = {"s2s": {"voice": {"default_mode": "realtime"}}}
    new_cfg, warnings = migrate_0_4.translate_config(cfg)
    assert new_cfg["s2s"]["voice"]["default_mode"] == "realtime"
    assert any("already migrated" in w for w in warnings)


def test_translate_preserves_unrelated_keys():
    cfg = {
        "model": "anthropic/claude-opus-4.7",
        "provider": "openrouter",
        "terminal": {"theme": "dark", "profile": "x"},
        "s2s": {"mode": "realtime"},
    }
    new_cfg, _ = migrate_0_4.translate_config(cfg)
    assert new_cfg["model"] == "anthropic/claude-opus-4.7"
    assert new_cfg["provider"] == "openrouter"
    assert new_cfg["terminal"] == {"theme": "dark", "profile": "x"}


def test_translate_adds_voice_defaults():
    cfg = {"s2s": {"mode": "cascaded"}}
    new_cfg, _ = migrate_0_4.translate_config(cfg)
    voice = new_cfg["s2s"]["voice"]
    assert voice["wakeword"] == "hey aria"
    assert "{user}" in voice["thread_name_template"]
    assert "{date" in voice["thread_name_template"]


def test_translate_does_not_mutate_input():
    cfg = _sample_realtime_config()
    before = copy.deepcopy(cfg)
    migrate_0_4.translate_config(cfg)
    assert cfg == before, "translate_config must not mutate its input"


# ---------------------------------------------------------------------------
# CLI driver: dry-run / apply / rollback
# ---------------------------------------------------------------------------


def _write_fixture(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """\
            model: anthropic/claude-opus-4.7
            provider: openrouter
            s2s:
              mode: realtime
              realtime:
                provider: gemini-live
                gemini_live:
                  model: gemini-2.5-flash-native-audio-latest
                  voice: Aoede
                  language_code: en-US
                  system_prompt: 'You are ARIA.'
            """
        )
    )


def test_dry_run_does_not_write(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    _write_fixture(cfg)
    original = cfg.read_text()
    rc = migrate_0_4.dry_run_migration(cfg)
    assert rc == 0
    assert cfg.read_text() == original, "dry-run must not modify the config file"
    # No backup, no sentinel.
    assert not list(tmp_path.glob("config.yaml.bak.0_4_*"))
    assert not (tmp_path / migrate_0_4.SENTINEL_NAME).exists()
    captured = capsys.readouterr()
    # Diff goes to stderr.
    assert "s2s.voice.default_mode" in captured.err or "voice:" in captured.err


def test_apply_writes_atomic_with_backup(tmp_path):
    cfg = tmp_path / "config.yaml"
    _write_fixture(cfg)
    original_bytes = cfg.read_bytes()

    rc = migrate_0_4.apply_migration(cfg, hermes_home=tmp_path)
    assert rc == 0

    # Backup exists and matches original.
    backups = list(tmp_path.glob("config.yaml.bak.0_4_*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original_bytes

    # New file is migrated.
    new_loaded = yaml.safe_load(cfg.read_text())
    assert new_loaded["s2s"]["voice"]["default_mode"] == "realtime"
    # Provider block preserved.
    assert new_loaded["s2s"]["realtime"]["gemini_live"]["voice"] == "Aoede"


def test_apply_writes_sentinel(tmp_path):
    cfg = tmp_path / "config.yaml"
    _write_fixture(cfg)
    migrate_0_4.apply_migration(cfg, hermes_home=tmp_path)
    sentinel = tmp_path / migrate_0_4.SENTINEL_NAME
    assert sentinel.exists()
    content = sentinel.read_text().strip()
    assert len(content) >= 10  # at least YYYY-MM-DDTh


def test_rollback_restores_from_backup(tmp_path):
    cfg = tmp_path / "config.yaml"
    _write_fixture(cfg)
    original_bytes = cfg.read_bytes()

    migrate_0_4.apply_migration(cfg, hermes_home=tmp_path)
    assert cfg.read_bytes() != original_bytes  # migration changed content

    rc = migrate_0_4.rollback_migration(cfg, hermes_home=tmp_path)
    assert rc == 0
    assert cfg.read_bytes() == original_bytes
    # Sentinel removed after rollback.
    assert not (tmp_path / migrate_0_4.SENTINEL_NAME).exists()


def test_rollback_errors_when_no_backup(tmp_path):
    cfg = tmp_path / "config.yaml"
    _write_fixture(cfg)
    rc = migrate_0_4.rollback_migration(cfg, hermes_home=tmp_path)
    assert rc != 0


def test_cli_dry_run_smoke(tmp_path):
    """End-to-end smoke of `python -m hermes_s2s.migrate_0_4 --dry-run`."""
    cfg = tmp_path / "config.yaml"
    _write_fixture(cfg)
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_s2s.migrate_0_4", "--dry-run", "--config", str(cfg)],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert proc.returncode == 0, proc.stderr
    assert cfg.read_text().startswith("model: anthropic"), "config unchanged by dry-run"


# ---------------------------------------------------------------------------
# Auto-translate-on-first-load (wired in hermes_s2s.config)
# ---------------------------------------------------------------------------


def test_auto_translate_on_load_when_old_config(tmp_path, monkeypatch):
    from hermes_s2s import config as s2s_config

    cfg = tmp_path / "config.yaml"
    _write_fixture(cfg)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    # Force the config module to resolve fresh paths from HERMES_HOME.
    monkeypatch.setattr(s2s_config, "HERMES_HOME", tmp_path)
    monkeypatch.setattr(s2s_config, "CONFIG_PATH", cfg)

    loaded = s2s_config.load_config()
    # Migrated on disk.
    on_disk = yaml.safe_load(cfg.read_text())
    assert on_disk["s2s"]["voice"]["default_mode"] == "realtime"
    # Sentinel written.
    assert (tmp_path / migrate_0_4.SENTINEL_NAME).exists()
    # S2SConfig returned with mode preserved.
    assert loaded.mode == "realtime"


def test_auto_translate_skips_when_sentinel_exists(tmp_path, monkeypatch):
    from hermes_s2s import config as s2s_config

    cfg = tmp_path / "config.yaml"
    _write_fixture(cfg)
    (tmp_path / migrate_0_4.SENTINEL_NAME).write_text("2025-01-01T00:00:00\n")

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(s2s_config, "HERMES_HOME", tmp_path)
    monkeypatch.setattr(s2s_config, "CONFIG_PATH", cfg)

    before = cfg.read_text()
    s2s_config.load_config()
    assert cfg.read_text() == before, (
        "auto-translate must be a no-op when the sentinel already exists"
    )
