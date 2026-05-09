"""STT CLI shim — `hermes-s2s-stt`.

Hermes invokes this shim via its command-provider mechanism (see ADR-0004).
Heavy deps (moonshine_onnx, torch) are loaded only inside the provider
implementation; this file only imports argparse + the registry.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hermes-s2s-stt",
        description="Transcribe an audio file to text via a hermes-s2s STT provider.",
    )
    p.add_argument("--provider", required=True, choices=["moonshine", "s2s-server"])
    p.add_argument("--input", required=True, help="Input audio file (wav/flac/ogg)")
    p.add_argument(
        "--output",
        default=None,
        help="Output text file path (default: <input>.txt next to the audio)",
    )
    p.add_argument("--model", choices=["tiny", "base"], default="tiny",
                   help="Moonshine model size")
    p.add_argument("--device", choices=["cuda", "cpu"], default="cpu",
                   help="Preferred inference device (advisory)")
    p.add_argument("--endpoint", default=None, help="s2s-server HTTP endpoint")
    return p


def _build_opts(args: argparse.Namespace) -> dict:
    if args.provider == "moonshine":
        return {"model": args.model, "device": args.device}
    opts: dict = {}
    if args.endpoint:
        opts["endpoint"] = args.endpoint
    return opts


def _resolve_output_paths(args: argparse.Namespace) -> list[Path]:
    """Resolve the list of paths to write the transcript to.

    Contract (defensive double-write — see P1-A/P1-B in wave-0.2.0 cold review):

    * When ``--output`` is omitted, write to BOTH:
        - ``Path(input).with_suffix('.txt')`` — basename-stripped (``clip.wav`` → ``clip.txt``)
        - ``Path(input + '.txt')`` — sticky, suffix-preserved (``clip.wav.txt``)

    * When ``--output`` IS given, write to the requested output AND ALSO to
      the audio-sibling basename-stripped ``.txt`` for safety.

    Duplicates are filtered while preserving order so callers only see each
    unique path once.
    """
    inp = Path(args.input)
    stripped = inp.with_suffix(".txt")
    sticky = inp.with_suffix(inp.suffix + ".txt") if inp.suffix else inp.with_suffix(".txt")

    if args.output:
        candidates = [Path(args.output), stripped]
    else:
        candidates = [stripped, sticky]

    seen: set[Path] = set()
    ordered: list[Path] = []
    for p in candidates:
        resolved = p
        if resolved not in seen:
            seen.add(resolved)
            ordered.append(resolved)
    return ordered


def main() -> int:
    args = _build_parser().parse_args()
    try:
        input_path = Path(args.input)
        if not input_path.is_file():
            raise FileNotFoundError(f"--input not found: {input_path}")

        # Register built-in providers (lazy — each factory late-imports its deps).
        from hermes_s2s.providers import register_builtin_stt_providers
        from hermes_s2s.registry import resolve_stt

        register_builtin_stt_providers()
        provider = resolve_stt(args.provider, _build_opts(args))
        result = provider.transcribe(str(input_path))

        if not result.get("success"):
            err = result.get("error") or "unknown STT failure"
            print(f"hermes-s2s-stt error: {err}", file=sys.stderr)
            return 1

        transcript = result.get("transcript", "")
        out_paths = _resolve_output_paths(args)
        for out_path in out_paths:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(transcript, encoding="utf-8")
            print(str(out_path))
        return 0
    except Exception as exc:  # noqa: BLE001 — shim must convert to exit code
        print(f"hermes-s2s-stt error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
