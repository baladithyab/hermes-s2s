"""`hermes s2s ...` CLI subcommand tree."""

from __future__ import annotations

import argparse
import json

from . import tools as s2s_tools


def setup_argparse(subparser: argparse.ArgumentParser) -> None:
    """Build the argparse tree for `hermes s2s`."""
    subs = subparser.add_subparsers(dest="s2s_command")
    subs.add_parser("status", help="Show current S2S configuration")
    p_mode = subs.add_parser("mode", help="Set S2S mode for the current shell session")
    p_mode.add_argument("mode", choices=["cascaded", "realtime", "s2s-server"])
    p_test = subs.add_parser("test", help="Smoke-test the configured TTS pipeline")
    p_test.add_argument("--text", default=None, help="Text to synthesize")
    subparser.set_defaults(func=dispatch)


def dispatch(args: argparse.Namespace) -> None:
    """Route `hermes s2s <subcommand>` to the right handler."""
    sub = getattr(args, "s2s_command", None)
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
