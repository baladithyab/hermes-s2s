"""`hermes s2s ...` CLI subcommand tree."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml

from . import tools as s2s_tools


# ---------------------------------------------------------------------------
# Profile definitions (tts + stt config blocks per ADR-0004)
# ---------------------------------------------------------------------------

_PROFILES = ["local-all", "hybrid-privacy", "cloud-cheap", "s2s-server", "custom"]

_LOCAL_STT_COMMAND = (
    "hermes-s2s-stt --provider moonshine --model tiny "
    "--output {output_path} --input {input_path}"
)
_S2S_STT_COMMAND = (
    "hermes-s2s-stt --provider s2s-server "
    "--output {output_path} --input {input_path}"
)
_ENV_MARKER = "# hermes-s2s setup: HERMES_LOCAL_STT_COMMAND"


def _profile_blocks(profile: str) -> tuple[dict, dict, str | None]:
    """Return (tts_block, stt_block, stt_env_command_or_None) for a profile."""
    if profile == "local-all":
        tts = {
            "provider": "hermes-s2s-kokoro",
            "providers": {
                "hermes-s2s-kokoro": {
                    "type": "command",
                    "command": (
                        "hermes-s2s-tts --provider kokoro --voice af_heart "
                        "--output {output_path} --text-file {input_path}"
                    ),
                    "output_format": "wav",
                }
            },
        }
        stt = {"provider": "local"}
        return tts, stt, _LOCAL_STT_COMMAND
    if profile == "hybrid-privacy":
        tts = {
            "provider": "elevenlabs",
            "providers": {"elevenlabs": {"type": "elevenlabs"}},
        }
        stt = {"provider": "local"}
        return tts, stt, _LOCAL_STT_COMMAND
    if profile == "cloud-cheap":
        tts = {
            "provider": "edge",
            "providers": {"edge": {"type": "edge"}},
        }
        stt = {"provider": "groq"}
        return tts, stt, None
    if profile == "s2s-server":
        tts = {
            "provider": "hermes-s2s-server",
            "providers": {
                "hermes-s2s-server": {
                    "type": "command",
                    "command": (
                        "hermes-s2s-tts --provider s2s-server "
                        "--output {output_path} --text-file {input_path}"
                    ),
                    "output_format": "wav",
                }
            },
        }
        stt = {"provider": "local"}
        return tts, stt, _S2S_STT_COMMAND
    raise ValueError(f"unknown profile: {profile}")


def _custom_prompt() -> tuple[dict, dict, str | None]:
    print("Custom profile — answer each prompt (blank = skip).")
    tts_provider = input("TTS provider name [hermes-s2s-kokoro]: ").strip() or "hermes-s2s-kokoro"
    tts_cmd = input(f"TTS command for {tts_provider} [leave blank for default kokoro]: ").strip()
    stt_provider = input("STT provider name [local]: ").strip() or "local"
    stt_cmd = input("HERMES_LOCAL_STT_COMMAND [blank to skip]: ").strip()
    tts: dict[str, Any] = {"provider": tts_provider}
    if tts_cmd:
        tts["providers"] = {
            tts_provider: {"type": "command", "command": tts_cmd, "output_format": "wav"}
        }
    stt = {"provider": stt_provider}
    return tts, stt, stt_cmd or None


# ---------------------------------------------------------------------------
# Dep detection + warnings
# ---------------------------------------------------------------------------


def _detect_missing(profile: str) -> list[str]:
    missing: list[str] = []
    if profile in ("local-all", "hybrid-privacy"):
        if importlib.util.find_spec("moonshine_onnx") is None:
            missing.append("moonshine_onnx (pip install hermes-s2s[local-stt])")
    if profile == "local-all":
        if importlib.util.find_spec("kokoro") is None:
            missing.append("kokoro (pip install hermes-s2s[local-tts])")
    if profile in ("local-all", "hybrid-privacy", "cloud-cheap", "s2s-server"):
        if shutil.which("ffmpeg") is None:
            missing.append("ffmpeg (system install)")
    if profile == "hybrid-privacy":
        if not os.environ.get("ELEVENLABS_API_KEY"):
            missing.append("ELEVENLABS_API_KEY env var")
    if profile == "cloud-cheap":
        if not os.environ.get("GROQ_API_KEY"):
            missing.append("GROQ_API_KEY env var")
    return missing


# ---------------------------------------------------------------------------
# Merge + write helpers
# ---------------------------------------------------------------------------


def _deep_merge(base: dict, overlay: dict) -> dict:
    for k, v in overlay.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _write_env_command(env_path: Path, command: str) -> bool:
    """Append HERMES_LOCAL_STT_COMMAND to .env idempotently. Returns True if written."""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing = env_path.read_text() if env_path.exists() else ""
    if _ENV_MARKER in existing:
        return False
    line = f"\n{_ENV_MARKER}\nHERMES_LOCAL_STT_COMMAND='{command}'\n"
    with env_path.open("a") as f:
        f.write(line)
    return True


# ---------------------------------------------------------------------------
# Argparse + dispatch
# ---------------------------------------------------------------------------


def setup_argparse(subparser: argparse.ArgumentParser) -> None:
    """Build the argparse tree for `hermes s2s`."""
    subs = subparser.add_subparsers(dest="s2s_command")
    subs.add_parser("status", help="Show current S2S configuration")
    p_mode = subs.add_parser("mode", help="Set S2S mode for the current shell session")
    p_mode.add_argument("mode", choices=["cascaded", "realtime", "s2s-server"])
    p_test = subs.add_parser("test", help="Smoke-test the configured TTS pipeline")
    p_test.add_argument("--text", default=None, help="Text to synthesize")
    p_setup = subs.add_parser(
        "setup", help="Interactively configure Hermes voice mode for hermes-s2s"
    )
    p_setup.add_argument(
        "--profile",
        choices=_PROFILES,
        default=None,
        help="Skip interactive prompt and pick a profile",
    )
    p_setup.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the config that would be written; don't modify files",
    )
    p_setup.add_argument(
        "--config-path",
        default=None,
        help="Path to config.yaml (default: ~/.hermes/config.yaml)",
    )
    subparser.set_defaults(func=dispatch)


def cmd_setup(args: argparse.Namespace) -> int:
    profile = args.profile
    if profile is None:
        print("Pick a profile:")
        for i, p in enumerate(_PROFILES, 1):
            print(f"  {i}) {p}")
        raw = input(f"Choice [1-{len(_PROFILES)}]: ").strip()
        try:
            profile = _PROFILES[int(raw) - 1]
        except (ValueError, IndexError):
            print(f"Invalid choice; defaulting to {_PROFILES[0]}", file=sys.stderr)
            profile = _PROFILES[0]

    if profile == "custom":
        tts_block, stt_block, stt_env_cmd = _custom_prompt()
    else:
        tts_block, stt_block, stt_env_cmd = _profile_blocks(profile)

    missing = _detect_missing(profile)
    if missing:
        print("WARNING: Missing dependencies for profile '%s':" % profile, file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        print("(You can install them later; setup will proceed.)", file=sys.stderr)

    new_config = {"tts": tts_block, "stt": stt_block}

    if args.dry_run:
        print("# dry-run: config that would be merged into config.yaml")
        print(yaml.safe_dump(new_config, sort_keys=False))
        if stt_env_cmd:
            print(f"# .env: HERMES_LOCAL_STT_COMMAND='{stt_env_cmd}'")
        return 0

    cfg_path = Path(args.config_path) if args.config_path else Path.home() / ".hermes" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if cfg_path.exists():
        loaded = yaml.safe_load(cfg_path.read_text()) or {}
        if isinstance(loaded, dict):
            existing = loaded
    merged = _deep_merge(existing, new_config)
    cfg_path.write_text(yaml.safe_dump(merged, sort_keys=False))
    print(f"Wrote {cfg_path}")

    if stt_env_cmd:
        env_path = cfg_path.parent / ".env"
        if _write_env_command(env_path, stt_env_cmd):
            print(f"Appended HERMES_LOCAL_STT_COMMAND to {env_path}")
        else:
            print(f"HERMES_LOCAL_STT_COMMAND already present in {env_path}")

    print("\nNext steps:")
    print("  1. Restart Hermes so it picks up the new config.")
    print("  2. In Hermes: /voice on")
    print("  3. Speak / try /voice tts 'hello world' to test.")
    return 0


def dispatch(args: argparse.Namespace) -> None:
    """Route `hermes s2s <subcommand>` to the right handler."""
    sub = getattr(args, "s2s_command", None)
    if sub == "setup":
        cmd_setup(args)
        return
    if sub == "mode":
        result = s2s_tools.s2s_set_mode({"mode": args.mode})
    elif sub == "test":
        result = s2s_tools.s2s_test_pipeline({"text": args.text})
    else:
        result = s2s_tools.s2s_status({})
    try:
        parsed = json.loads(result)
        print(json.dumps(parsed, indent=2))
    except Exception:
        print(result)
