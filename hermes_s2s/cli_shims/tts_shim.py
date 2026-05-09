"""TTS CLI shim — `hermes-s2s-tts`.

Hermes invokes this shim via its command-provider mechanism (see ADR-0004).
Heavy deps (kokoro, torch) are loaded only inside the provider implementation;
this file only imports argparse + the registry.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hermes-s2s-tts",
        description="Synthesize text to an audio file via a hermes-s2s TTS provider.",
    )
    p.add_argument("--provider", required=True, choices=["kokoro", "s2s-server"])
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--text", help="Inline text to synthesize")
    src.add_argument("--text-file", help="Path to a UTF-8 text file")
    p.add_argument("--output", required=True, help="Output audio path (.wav recommended)")
    p.add_argument("--voice", default="af_heart", help="Voice id (provider-specific)")
    p.add_argument("--lang-code", default="a", help="Kokoro language code (default: a)")
    p.add_argument("--speed", type=float, default=1.0, help="Speaking speed multiplier")
    p.add_argument("--endpoint", default=None, help="s2s-server HTTP endpoint")
    return p


def _read_text(args: argparse.Namespace) -> str:
    if args.text is not None:
        return args.text
    path = Path(args.text_file)
    if not path.is_file():
        raise FileNotFoundError(f"--text-file not found: {path}")
    return path.read_text(encoding="utf-8")


def _build_opts(args: argparse.Namespace) -> dict:
    if args.provider == "kokoro":
        return {"voice": args.voice, "lang_code": args.lang_code, "speed": args.speed}
    # s2s-server
    opts: dict = {}
    if args.endpoint:
        opts["endpoint"] = args.endpoint
    return opts


def main() -> int:
    args = _build_parser().parse_args()
    try:
        # Register built-in providers (lazy — each factory late-imports its deps).
        from hermes_s2s.providers import register_builtin_tts_providers
        from hermes_s2s.registry import resolve_tts

        register_builtin_tts_providers()
        text = _read_text(args)
        provider = resolve_tts(args.provider, _build_opts(args))
        provider.synthesize(text, args.output)
        return 0
    except Exception as exc:  # noqa: BLE001 — shim must convert to exit code
        print(f"hermes-s2s-tts error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
