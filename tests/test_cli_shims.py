"""Tests for CLI shims hermes-s2s-tts / hermes-s2s-stt.

Strategy:
- Happy-path tests call `main()` in-process with monkeypatched providers so we
  can stub out the heavy deps (kokoro, moonshine_onnx) without ever loading them.
- Error-path tests use `subprocess.run` to exec the shim module end-to-end —
  this exercises argparse exit codes, the module-load path, and proves no
  heavy import fires at module import time (subprocess would blow up otherwise).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_shim(module: str, *args: str) -> subprocess.CompletedProcess:
    """Invoke a shim as a subprocess via `python -m`."""
    return subprocess.run(
        [sys.executable, "-m", module, *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )


# --------------------------------------------------------------------------- #
# TTS shim
# --------------------------------------------------------------------------- #

def test_tts_shim_happy_path_with_mocked_kokoro(tmp_path, monkeypatch):
    """Mock KokoroTTS.synthesize; ensure the shim calls it and exits 0."""
    from hermes_s2s.cli_shims import tts_shim
    from hermes_s2s.providers.tts import kokoro as kokoro_mod

    out_path = tmp_path / "out.wav"
    text_file = tmp_path / "in.txt"
    text_file.write_text("hello world", encoding="utf-8")

    calls = {}

    def fake_synthesize(self, text, output_path):
        calls["text"] = text
        calls["output_path"] = str(output_path)
        Path(output_path).write_bytes(b"RIFF-fake-wav")
        return str(output_path)

    monkeypatch.setattr(kokoro_mod.KokoroTTS, "synthesize", fake_synthesize)
    monkeypatch.setattr(
        sys, "argv",
        [
            "hermes-s2s-tts",
            "--provider", "kokoro",
            "--text-file", str(text_file),
            "--output", str(out_path),
            "--voice", "af_heart",
        ],
    )

    rc = tts_shim.main()
    assert rc == 0, "shim should exit 0 on happy path"
    assert calls["text"] == "hello world"
    assert calls["output_path"] == str(out_path)
    assert out_path.exists()


def test_tts_shim_missing_text_file_error(tmp_path):
    """Shim should exit non-zero with stderr when --text-file doesn't exist."""
    result = _run_shim(
        "hermes_s2s.cli_shims.tts_shim",
        "--provider", "kokoro",
        "--text-file", str(tmp_path / "does-not-exist.txt"),
        "--output", str(tmp_path / "out.wav"),
    )
    assert result.returncode != 0
    assert "hermes-s2s-tts error" in result.stderr


def test_tts_shim_invalid_provider_error(tmp_path):
    """argparse should reject unknown --provider choices."""
    text_file = tmp_path / "in.txt"
    text_file.write_text("hi", encoding="utf-8")

    result = _run_shim(
        "hermes_s2s.cli_shims.tts_shim",
        "--provider", "nonsense-provider",
        "--text-file", str(text_file),
        "--output", str(tmp_path / "out.wav"),
    )
    assert result.returncode != 0
    # argparse writes "invalid choice" to stderr
    assert "invalid choice" in result.stderr.lower() or "nonsense-provider" in result.stderr


# --------------------------------------------------------------------------- #
# STT shim
# --------------------------------------------------------------------------- #

def test_stt_shim_happy_path_with_mocked_moonshine(tmp_path, monkeypatch):
    """Mock MoonshineSTT.transcribe; ensure the shim writes the transcript."""
    from hermes_s2s.cli_shims import stt_shim
    from hermes_s2s.providers.stt import moonshine as moonshine_mod

    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF-fake-wav")
    out_path = tmp_path / "clip.txt"

    def fake_transcribe(self, file_path):
        return {"success": True, "transcript": "hello", "provider": "moonshine"}

    monkeypatch.setattr(moonshine_mod.MoonshineSTT, "transcribe", fake_transcribe)
    monkeypatch.setattr(
        sys, "argv",
        [
            "hermes-s2s-stt",
            "--provider", "moonshine",
            "--input", str(audio),
            "--output", str(out_path),
            "--model", "tiny",
        ],
    )

    rc = stt_shim.main()
    assert rc == 0
    assert out_path.read_text(encoding="utf-8") == "hello"


def test_stt_shim_missing_input_file_error(tmp_path):
    """Shim should exit non-zero when --input doesn't exist."""
    result = _run_shim(
        "hermes_s2s.cli_shims.stt_shim",
        "--provider", "moonshine",
        "--input", str(tmp_path / "nope.wav"),
        "--output", str(tmp_path / "out.txt"),
    )
    assert result.returncode != 0
    assert "hermes-s2s-stt error" in result.stderr
    assert "not found" in result.stderr.lower()


def test_stt_shim_s2s_server_connection_refused(tmp_path):
    """s2s-server provider should fail cleanly when the endpoint is unreachable."""
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF-fake-wav")
    out_path = tmp_path / "clip.txt"

    # Port 1 is typically unreachable; if not, a closed ephemeral-range port
    # will still raise ConnectError from httpx.
    result = _run_shim(
        "hermes_s2s.cli_shims.stt_shim",
        "--provider", "s2s-server",
        "--input", str(audio),
        "--output", str(out_path),
        "--endpoint", "http://127.0.0.1:1/asr",
    )
    assert result.returncode != 0
    assert "hermes-s2s-stt error" in result.stderr
    # Transcript file should NOT have been written on failure.
    assert not out_path.exists()
