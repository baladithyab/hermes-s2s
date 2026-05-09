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


# --------------------------------------------------------------------------- #
# STT shim: defensive double-write output contract (P1-A / P1-B)
# --------------------------------------------------------------------------- #

def test_stt_shim_default_output_writes_both_sibling_forms(tmp_path, monkeypatch, capsys):
    """When --output is omitted, write to BOTH <name>.txt AND <name>.<ext>.txt.

    Belt-and-suspenders for either Hermes contract (basename-stripped vs.
    sticky-suffix). Also, both paths must be printed to stdout.
    """
    from hermes_s2s.cli_shims import stt_shim
    from hermes_s2s.providers.stt import moonshine as moonshine_mod

    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF-fake-wav")

    monkeypatch.setattr(
        moonshine_mod.MoonshineSTT,
        "transcribe",
        lambda self, path: {"success": True, "transcript": "hi there", "provider": "moonshine"},
    )
    monkeypatch.setattr(
        sys, "argv",
        [
            "hermes-s2s-stt",
            "--provider", "moonshine",
            "--input", str(audio),
        ],
    )

    rc = stt_shim.main()
    assert rc == 0
    stripped = tmp_path / "clip.txt"
    sticky = tmp_path / "clip.wav.txt"
    assert stripped.read_text(encoding="utf-8") == "hi there"
    assert sticky.read_text(encoding="utf-8") == "hi there"
    out = capsys.readouterr().out
    assert str(stripped) in out
    assert str(sticky) in out


def test_stt_shim_explicit_output_also_writes_audio_sibling(tmp_path, monkeypatch, capsys):
    """When --output is given, write to --output AND the audio-sibling .txt."""
    from hermes_s2s.cli_shims import stt_shim
    from hermes_s2s.providers.stt import moonshine as moonshine_mod

    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF-fake-wav")
    # Put --output somewhere far from the audio so we can prove we also wrote
    # the sibling form.
    requested = tmp_path / "subdir" / "transcript.out"

    monkeypatch.setattr(
        moonshine_mod.MoonshineSTT,
        "transcribe",
        lambda self, path: {"success": True, "transcript": "payload", "provider": "moonshine"},
    )
    monkeypatch.setattr(
        sys, "argv",
        [
            "hermes-s2s-stt",
            "--provider", "moonshine",
            "--input", str(audio),
            "--output", str(requested),
        ],
    )

    rc = stt_shim.main()
    assert rc == 0
    sibling = tmp_path / "clip.txt"  # basename-stripped audio-sibling
    assert requested.read_text(encoding="utf-8") == "payload"
    assert sibling.read_text(encoding="utf-8") == "payload"
    out = capsys.readouterr().out
    assert str(requested) in out
    assert str(sibling) in out


# --------------------------------------------------------------------------- #
# KokoroTTS: real KPipeline-shape tuple-unpack contract (P1-C)
# --------------------------------------------------------------------------- #

def test_kokoro_synthesize_unpacks_kpipeline_tuples(tmp_path, monkeypatch):
    """Exercise KokoroTTS.synthesize with a KPipeline-shaped mock.

    The real kokoro.KPipeline yields ``(graphemes, phonemes, audio)`` tuples.
    This test monkeypatches the actual import site in providers/tts/kokoro.py
    (``kokoro.KPipeline``) with a callable mock that yields that exact shape,
    then runs synthesize end-to-end (no stub on synthesize itself) to verify
    the tuple-unpack line (providers/tts/kokoro.py:62) stays wired correctly.
    """
    np = pytest.importorskip("numpy")
    pytest.importorskip("soundfile")

    # Provide a fake `kokoro` module with a KPipeline class that mimics the
    # real callable-pipeline shape. Install it BEFORE importing KokoroTTS so
    # the inline `from kokoro import KPipeline` picks it up.
    import types as _types

    class _FakeKPipeline:
        def __init__(self, *args, **kwargs):
            pass

        def __call__(self, text, voice=None, speed=1.0):
            # A tiny float32 buffer — just enough to write a valid WAV.
            buf = np.zeros(1200, dtype=np.float32)
            # Two chunks to exercise the concat path.
            yield ("g1", "p1", buf)
            yield ("g2", "p2", buf)

    fake_kokoro = _types.ModuleType("kokoro")
    fake_kokoro.KPipeline = _FakeKPipeline
    monkeypatch.setitem(sys.modules, "kokoro", fake_kokoro)

    # Clear cache so the fake pipeline is actually instantiated (otherwise a
    # previously-cached real pipeline could be reused between tests).
    from hermes_s2s.providers.tts import kokoro as kokoro_mod
    monkeypatch.setattr(kokoro_mod, "_KOKORO_CACHE", {})

    out_path = tmp_path / "out.wav"
    tts = kokoro_mod.KokoroTTS(voice="af_heart", lang_code="a", speed=1.0)
    returned = tts.synthesize("hello world", out_path)

    assert Path(returned) == out_path
    assert out_path.exists()
    assert out_path.stat().st_size > 0
