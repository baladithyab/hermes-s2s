"""Tests for :mod:`hermes_s2s.config.load_config` error handling.

Phase-8 final security review P0-4 caught that ``load_config`` had a
broad ``except Exception`` that silently returned an empty config for
YAML syntax errors, permission errors, and anything else. Result: a
user's typo'd config would boot a fresh empty config and they'd have
no idea why their settings didn't apply.

The fix narrows the except-return-empty path to ``FileNotFoundError``
only (fresh-install case) and raises on parse/IO errors. These tests
fence that behaviour.
"""

from __future__ import annotations

import pytest
import yaml

from hermes_s2s.config import S2SConfig, load_config


def test_missing_config_returns_empty(tmp_path):
    """A non-existent config file is a fresh-install signal, not an error."""
    cfg = load_config(tmp_path / "does-not-exist.yaml")
    assert isinstance(cfg, S2SConfig)
    # Defaults apply; mode resolves to the dataclass default.
    assert cfg.mode == "cascaded"


def test_yaml_syntax_error_raises_with_clear_message(tmp_path, caplog):
    """YAML parse errors must raise (not silently return empty).

    Regression fence for P0-4. Previously the broad except ate the
    exception and users got a silent empty config on a typo; now we
    log clearly and raise so the error surfaces at startup.
    """
    broken = tmp_path / "config.yaml"
    # Unclosed bracket — guaranteed YAML parse failure.
    broken.write_text("s2s:\n  voice:\n    default_mode: [realtime\n", encoding="utf-8")

    with caplog.at_level("ERROR", logger="hermes_s2s.config"):
        with pytest.raises(yaml.YAMLError):
            load_config(broken)

    # Error log must name the path so the user knows which file to fix.
    assert any(
        str(broken) in rec.getMessage() for rec in caplog.records
    ), f"expected config path in error log, got: {[r.getMessage() for r in caplog.records]}"


def test_empty_yaml_file_returns_empty_config(tmp_path):
    """An empty YAML file is not an error — it just yields an empty config."""
    empty = tmp_path / "config.yaml"
    empty.write_text("", encoding="utf-8")
    cfg = load_config(empty)
    assert isinstance(cfg, S2SConfig)
    assert cfg.mode == "cascaded"


def test_yaml_top_level_list_does_not_raise(tmp_path):
    """Non-dict top-level YAML is handled by S2SConfig.from_dict (not a parse error).

    This is defensive: a user could accidentally write ``- item`` at
    top-level; YAML parses that successfully as a list, and
    ``S2SConfig.from_dict`` coerces non-dict inputs to ``{}``. This
    path should NOT raise — only genuine parse failures should.
    """
    listy = tmp_path / "config.yaml"
    listy.write_text("- a\n- b\n", encoding="utf-8")
    cfg = load_config(listy)
    assert isinstance(cfg, S2SConfig)
