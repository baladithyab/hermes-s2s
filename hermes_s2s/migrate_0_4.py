"""Migration script: hermes-s2s 0.3.x → 0.4.0 config shape.

Translates the old top-level ``s2s.mode`` string into the new
``s2s.voice.default_mode`` namespace while preserving every other key
(including provider-specific blocks like ``s2s.realtime.gemini_live``
which are still consumed by the runtime).

Usage::

    python -m hermes_s2s.migrate_0_4              # apply (default)
    python -m hermes_s2s.migrate_0_4 --dry-run    # print diff only
    python -m hermes_s2s.migrate_0_4 --rollback   # restore latest backup
    python -m hermes_s2s.migrate_0_4 --config PATH

Atomic-write pattern (tmp → fsync → rename) protects against disk-full
corruption. A sibling backup ``config.yaml.bak.0_4_<timestamp>`` is
written before the swap, and a sentinel file
``<HERMES_HOME>/.s2s_migrated_0_4`` records the migration timestamp to
prevent the auto-translate-on-first-load path (see
``hermes_s2s.config``) from firing twice.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml


__all__ = [
    "translate_config",
    "apply_migration",
    "rollback_migration",
    "dry_run_migration",
    "SENTINEL_NAME",
    "BACKUP_PREFIX",
    "get_hermes_home_path",
    "get_config_path",
    "atomic_write_yaml",
    "write_sentinel",
    "find_latest_backup",
]


SENTINEL_NAME = ".s2s_migrated_0_4"
BACKUP_PREFIX = ".bak.0_4_"

DEFAULT_WAKEWORD = "hey aria"
DEFAULT_THREAD_NAME_TEMPLATE = "🎤 {user} — {date:%Y-%m-%d %H:%M}"

#: Map legacy 0.3.x ``s2s.mode`` values to the 0.4.0 ``VoiceMode``
#: vocabulary. 0.3.x called the realtime path ``duplex``; feeding that
#: verbatim into ``s2s.voice.default_mode`` would produce a value the
#: 0.4.0 ``VoiceMode`` enum rejects. Any value not listed here passes
#: through unchanged (so genuinely-unknown modes still surface at
#: runtime rather than being silently rewritten). See Phase-8 final
#: shippability review P0-3.
_LEGACY_MODE_MAP: dict[str, str] = {
    "duplex": "realtime",       # 0.3.x name for the realtime path
    "cascaded": "cascaded",
    "realtime": "realtime",
    "s2s-server": "s2s-server",
    "s2s_server": "s2s-server",  # underscore variant from early 0.3.x
    "pipeline": "pipeline",
}


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def get_hermes_home_path() -> Path:
    """Return ``HERMES_HOME`` honoring ``hermes_constants`` when present.

    Falls back to the env var / ``~/.hermes`` for standalone installs.
    """
    try:  # pragma: no cover - import path varies per environment
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home())
    except Exception:
        home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        return Path(home)


def get_config_path(override: Path | None = None) -> Path:
    if override is not None:
        return override
    return get_hermes_home_path() / "config.yaml"


def get_sentinel_path(hermes_home: Path | None = None) -> Path:
    return (hermes_home or get_hermes_home_path()) / SENTINEL_NAME


# ---------------------------------------------------------------------------
# Core translation
# ---------------------------------------------------------------------------


def translate_config(config: dict) -> tuple[dict, list[str]]:
    """Translate a 0.3.x config dict to 0.4.0 shape.

    Returns ``(new_config, warnings)``. Idempotent: an already-migrated
    config returns a deep copy and a single "already migrated" warning.

    Rules:
    - ``s2s.mode`` (string) → ``s2s.voice.default_mode`` (preserved value).
    - All other ``s2s.*`` subtrees (``realtime``, ``realtime.gemini_live``,
      ``realtime.openai``, ``pipeline``, ``s2s_server``, etc.) stay where
      they are — the runtime still consumes them.
    - ``s2s.voice`` gains defaults for ``wakeword`` and
      ``thread_name_template`` when absent.
    - Non-s2s top-level keys (``model``, ``provider``, ``terminal`` …)
      are preserved verbatim.
    """
    warnings: list[str] = []
    new_config = copy.deepcopy(config) if isinstance(config, dict) else {}

    s2s = new_config.get("s2s")
    if not isinstance(s2s, dict):
        s2s = {}
        new_config["s2s"] = s2s

    voice = s2s.get("voice")
    if not isinstance(voice, dict):
        voice = {}

    already_has_default_mode = "default_mode" in voice and voice["default_mode"]

    if already_has_default_mode:
        warnings.append("config already migrated (s2s.voice.default_mode present)")
    else:
        old_mode = s2s.get("mode")
        if isinstance(old_mode, str) and old_mode.strip():
            # Normalise legacy 0.3.x names to the 0.4.0 VoiceMode vocabulary
            # before copying into s2s.voice.default_mode. 0.3.x called the
            # realtime path "duplex"; copying that verbatim would produce a
            # default_mode value VoiceMode rejects at construction time
            # (Phase-8 final shippability review P0-3).
            new_mode = _LEGACY_MODE_MAP.get(old_mode, old_mode)
            if new_mode != old_mode:
                warnings.append(
                    f"translated legacy mode '{old_mode}' to '{new_mode}'"
                )
            voice["default_mode"] = new_mode
            warnings.append(
                "s2s.mode is deprecated; migrated to s2s.voice.default_mode"
            )
        # else: no old mode, no new mode — leave unset (downstream defaults cascaded).

    # Fill 0.4.0 voice defaults (non-destructive: only set if missing).
    if "wakeword" not in voice:
        voice["wakeword"] = DEFAULT_WAKEWORD
    if "thread_name_template" not in voice:
        voice["thread_name_template"] = DEFAULT_THREAD_NAME_TEMPLATE

    s2s["voice"] = voice

    # Leave ``s2s.mode`` in place for two minor versions per research-15 §8.1
    # (runtime continues to honor it). Flag it in warnings if present.
    if isinstance(s2s.get("mode"), str):
        warnings.append(
            "deprecated key still present: s2s.mode (removable after 0.4.x)"
        )

    return new_config, warnings


# ---------------------------------------------------------------------------
# Atomic write + sentinel + backup helpers
# ---------------------------------------------------------------------------


def atomic_write_yaml(path: Path, data: dict) -> None:
    """Write ``data`` to ``path`` atomically (tmp → fsync → rename).

    Creates parent directories when absent. Safe against disk-full
    mid-write (the rename is the only visible mutation).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmpf:
            tmpf.write(payload)
            tmpf.flush()
            os.fsync(tmpf.fileno())
        os.replace(tmp_name, path)
    except Exception:
        # Clean up the tmp file on failure; propagate the error.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_sentinel(hermes_home: Path | None = None) -> Path:
    """Write ``<HERMES_HOME>/.s2s_migrated_0_4`` with an ISO timestamp."""
    path = get_sentinel_path(hermes_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z") or time.strftime("%Y-%m-%dT%H:%M:%S")
    path.write_text(stamp + "\n", encoding="utf-8")
    return path


def _backup_name(config_path: Path, timestamp: str | None = None) -> Path:
    ts = timestamp or time.strftime("%Y%m%dT%H%M%S")
    return config_path.with_name(config_path.name + BACKUP_PREFIX + ts)


def find_latest_backup(config_path: Path) -> Path | None:
    """Return the most recent ``.bak.0_4_*`` sibling of ``config_path``."""
    parent = config_path.parent
    if not parent.is_dir():
        return None
    prefix = config_path.name + BACKUP_PREFIX
    candidates = [p for p in parent.iterdir() if p.name.startswith(prefix)]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


# ---------------------------------------------------------------------------
# High-level ops (apply / dry-run / rollback)
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
    except Exception as exc:
        raise RuntimeError(f"failed to parse {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise RuntimeError(f"{path} does not contain a top-level mapping")
    return loaded


def _dump_yaml(data: dict) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _color(text: str, code: str) -> str:
    if not sys.stderr.isatty():
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def _print_diff(old: dict, new: dict, label_old: str, label_new: str) -> None:
    old_lines = _dump_yaml(old).splitlines(keepends=True)
    new_lines = _dump_yaml(new).splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines, new_lines, fromfile=label_old, tofile=label_new, n=3
    )
    for line in diff:
        if line.startswith("+") and not line.startswith("+++"):
            sys.stderr.write(_color(line, "32"))
        elif line.startswith("-") and not line.startswith("---"):
            sys.stderr.write(_color(line, "31"))
        elif line.startswith("@@"):
            sys.stderr.write(_color(line, "36"))
        else:
            sys.stderr.write(line)


def dry_run_migration(config_path: Path) -> int:
    if not config_path.exists():
        print(f"No config at {config_path} — nothing to migrate.", file=sys.stderr)
        return 0
    loaded = _load_yaml(config_path)
    new_cfg, warnings = translate_config(loaded)
    _print_diff(loaded, new_cfg, str(config_path), str(config_path) + " (migrated)")
    for w in warnings:
        print(f"# warning: {w}", file=sys.stderr)
    print("# dry-run: no files written", file=sys.stderr)
    return 0


def apply_migration(
    config_path: Path, hermes_home: Path | None = None
) -> int:
    if not config_path.exists():
        print(f"No config at {config_path} — nothing to migrate.", file=sys.stderr)
        return 0
    loaded = _load_yaml(config_path)
    new_cfg, warnings = translate_config(loaded)

    # Backup the original.
    backup_path = _backup_name(config_path)
    shutil.copy2(config_path, backup_path)

    # Atomic write of migrated contents.
    atomic_write_yaml(config_path, new_cfg)

    # Sentinel (guards the auto-load path from firing again).
    sentinel_path = write_sentinel(hermes_home)

    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    print(f"Migrated. Backup: {backup_path}")
    print(f"Sentinel:   {sentinel_path}")
    return 0


def rollback_migration(
    config_path: Path, hermes_home: Path | None = None
) -> int:
    backup = find_latest_backup(config_path)
    if backup is None:
        print(
            f"No backup found (looked for {config_path.name}{BACKUP_PREFIX}* in "
            f"{config_path.parent}).",
            file=sys.stderr,
        )
        return 2

    # Atomic restore: copy backup to a sibling tmp, fsync, rename.
    fd, tmp_name = tempfile.mkstemp(
        prefix=config_path.name + ".", suffix=".restore", dir=str(config_path.parent)
    )
    os.close(fd)
    try:
        shutil.copy2(backup, tmp_name)
        # fsync to make sure the content hits disk before rename.
        with open(tmp_name, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp_name, config_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    # Remove the sentinel so the next load re-migrates if desired.
    sentinel_path = get_sentinel_path(hermes_home)
    if sentinel_path.exists():
        try:
            sentinel_path.unlink()
        except OSError:
            pass

    print(f"Rolled back {config_path} from {backup}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m hermes_s2s.migrate_0_4",
        description="Migrate ~/.hermes/config.yaml from hermes-s2s 0.3.x to 0.4.0.",
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the diff to stderr and exit without writing",
    )
    group.add_argument(
        "--rollback",
        action="store_true",
        help="Restore the most recent .bak.0_4_* backup",
    )
    p.add_argument(
        "--config",
        default=None,
        help="Path to config.yaml (default: $HERMES_HOME/config.yaml)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    cfg_path = get_config_path(Path(args.config)) if args.config else get_config_path()
    hermes_home = cfg_path.parent if args.config else get_hermes_home_path()

    if args.dry_run:
        return dry_run_migration(cfg_path)
    if args.rollback:
        return rollback_migration(cfg_path, hermes_home)
    return apply_migration(cfg_path, hermes_home)


if __name__ == "__main__":
    raise SystemExit(main())
