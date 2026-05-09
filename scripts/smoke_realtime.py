#!/usr/bin/env python3
"""Smoke-test a hermes-s2s realtime backend end-to-end.

Standalone script (not a pytest test). Connects to Gemini Live or
OpenAI Realtime, sends either a synthesized 440Hz tone or a WAV you
provide, collects the audio response, and writes it to a WAV file.

Run from the repo root:

    python3 scripts/smoke_realtime.py --provider gemini-live
    python3 scripts/smoke_realtime.py --provider gpt-realtime-mini --input my.wav

Env keys (one of):
    GEMINI_API_KEY  — for --provider gemini-live
    OPENAI_API_KEY  — for --provider gpt-realtime / gpt-realtime-mini

Uses ONLY the public hermes-s2s API (registry.resolve_realtime +
audio.resample.resample_pcm). No internal imports.

Exit 0 on success, 1 on any error (message on stderr).
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import struct
import sys
import time
import wave
from pathlib import Path
from typing import List, Tuple

# Ensure `import hermes_s2s` works when running from the repo root without install
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from hermes_s2s.audio.resample import resample_pcm  # noqa: E402
from hermes_s2s.providers import register_builtin_realtime_providers  # noqa: E402
from hermes_s2s.registry import resolve_realtime  # noqa: E402

# Per-provider defaults: (api_key_env, default_model, default_voice, backend_sample_rate)
_PROVIDER_DEFAULTS = {
    "gemini-live": ("GEMINI_API_KEY", "gemini-live-2.5-flash", "Aoede", 16000),
    "gpt-realtime": ("OPENAI_API_KEY", "gpt-realtime", "alloy", 24000),
    "gpt-realtime-mini": ("OPENAI_API_KEY", "gpt-4o-mini-realtime-preview", "alloy", 24000),
}

_SYSTEM_PROMPT = "You are a helpful voice assistant. Respond briefly."


def _synthesize_tone(freq_hz: float = 440.0, secs: float = 1.0, rate: int = 16000) -> bytes:
    """Generate a mono s16le PCM 440Hz tone — stand-in input when no WAV is given."""
    n = int(rate * secs)
    samples = [int(0.25 * 32767 * math.sin(2 * math.pi * freq_hz * i / rate)) for i in range(n)]
    return struct.pack("<" + "h" * n, *samples)


def _load_wav(path: Path) -> Tuple[bytes, int, int]:
    """Return (pcm_bytes, sample_rate, channels) from a WAV. Refuses non-PCM."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"{path}: only 16-bit PCM supported (got {w.getsampwidth()*8}-bit)")
        return w.readframes(w.getnframes()), w.getframerate(), w.getnchannels()


def _write_wav(path: Path, pcm: bytes, rate: int, channels: int = 1) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


async def _run_smoke(args: argparse.Namespace) -> int:
    provider = args.provider
    env_key, default_model, default_voice, backend_rate = _PROVIDER_DEFAULTS[provider]

    api_key = os.environ.get(env_key)
    if not api_key:
        print(f"error: {env_key} not set in environment", file=sys.stderr)
        return 1

    # Resolve backend through the public registry
    register_builtin_realtime_providers()
    cfg = {}
    if args.model:
        cfg["model"] = args.model
    if args.voice:
        cfg["voice"] = args.voice
    backend = resolve_realtime(provider, cfg)

    # Prepare input audio
    if args.input:
        in_path = Path(args.input)
        if not in_path.is_file():
            print(f"error: input WAV not found: {in_path}", file=sys.stderr)
            return 1
        pcm, src_rate, src_channels = _load_wav(in_path)
        print(f"loaded {in_path} ({len(pcm)} bytes, {src_rate} Hz, {src_channels} ch)")
    else:
        pcm = _synthesize_tone(rate=backend_rate)
        src_rate, src_channels = backend_rate, 1
        print(f"synthesized 1s 440Hz tone ({len(pcm)} bytes @ {src_rate} Hz)")

    # Resample / mix down to backend-expected rate + mono if needed
    if src_rate != backend_rate or src_channels != 1:
        pcm = resample_pcm(
            pcm,
            src_rate=src_rate,
            dst_rate=backend_rate,
            src_channels=src_channels,
            dst_channels=1,
        )
        print(f"resampled → {backend_rate} Hz mono ({len(pcm)} bytes)")

    voice = args.voice or default_voice

    # Connect
    t0 = time.perf_counter()
    await backend.connect(_SYSTEM_PROMPT, voice, [])
    connect_ms = (time.perf_counter() - t0) * 1000
    print(f"connected to {provider} in {connect_ms:.0f}ms")

    # Send input audio (one shot — backend chunks internally)
    await backend.send_audio_chunk(pcm, backend_rate)

    # Collect audio for --duration-secs
    chunks: List[bytes] = []
    first_audio_ms: float = 0.0
    total_bytes = 0
    t_send = time.perf_counter()
    deadline = t_send + float(args.duration_secs)
    response_rate = backend_rate  # will be overwritten by first audio_chunk payload

    async def _drain() -> None:
        nonlocal first_audio_ms, total_bytes, response_rate
        async for ev in backend.recv_events():
            if ev.type == "audio_chunk":
                audio = ev.payload.get("audio", b"") or b""
                if audio:
                    if not chunks:
                        first_audio_ms = (time.perf_counter() - t_send) * 1000
                    chunks.append(audio)
                    total_bytes += len(audio)
                    response_rate = ev.payload.get("sample_rate", response_rate) or response_rate
            elif ev.type == "error":
                print(f"backend error: {ev.payload}", file=sys.stderr)
                return

    try:
        await asyncio.wait_for(_drain(), timeout=max(1.0, float(args.duration_secs)))
    except asyncio.TimeoutError:
        pass  # expected — we just want the first N seconds of audio
    finally:
        try:
            await asyncio.wait_for(backend.close(), timeout=5.0)
        except Exception:  # noqa: BLE001
            pass

    # Write response
    out_path = Path(args.output)
    if chunks:
        _write_wav(out_path, b"".join(chunks), response_rate, channels=1)
        print(f"wrote response to {out_path} ({total_bytes} bytes @ {response_rate} Hz)")
    else:
        print("warning: no audio_chunk events received — output WAV not written", file=sys.stderr)

    # Summary
    print("--- smoke summary ---")
    print(f"provider              : {provider}")
    print(f"connect_ms            : {connect_ms:.0f}")
    print(f"first_audio_chunk_ms  : {first_audio_ms:.0f}" if first_audio_ms else
          "first_audio_chunk_ms  : n/a (no audio)")
    print(f"total_audio_bytes     : {total_bytes}")
    print(f"audio_chunks_received : {len(chunks)}")
    return 0 if chunks else 1


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--provider", required=True, choices=list(_PROVIDER_DEFAULTS),
                   help="Realtime backend to test")
    p.add_argument("--input", help="Path to input WAV (default: synthesize a 1s 440Hz tone)")
    p.add_argument("--output", default="smoke_response.wav", help="Where to write the response WAV")
    p.add_argument("--duration-secs", type=int, default=5,
                   help="How long to collect response audio (default: 5s)")
    p.add_argument("--model", help="Override the default model id for the chosen provider")
    p.add_argument("--voice", help="Override the default voice for the chosen provider")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        return asyncio.run(_run_smoke(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
