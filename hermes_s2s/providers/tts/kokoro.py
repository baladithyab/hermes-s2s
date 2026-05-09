"""Kokoro TTS provider (Hexgrad, Apache-2.0, local).

Ported from work originally on the Hermes feat/aria-voice branch.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

_KOKORO_CACHE: Dict[str, Any] = {}


class KokoroTTS:
    """TTS provider that wraps Kokoro KPipeline."""

    def __init__(
        self,
        voice: str = "af_heart",
        lang_code: str = "a",
        speed: float = 1.0,
    ) -> None:
        self.voice = voice
        self.lang_code = lang_code
        self.speed = float(speed)

    def synthesize(self, text: str, output_path: str | Path) -> str:
        """Synthesize `text` to `output_path`. Returns the final path written."""
        output_path = str(output_path)

        try:
            from kokoro import KPipeline  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "Kokoro selected but 'kokoro' package not installed. "
                "Install via: pip install hermes-s2s[kokoro]"
            ) from exc

        try:
            import numpy as np
            import soundfile as sf
        except ImportError as exc:
            raise ImportError(
                f"hermes-s2s requires soundfile + numpy (missing: {exc})"
            ) from exc

        cache_key = self.lang_code
        if cache_key not in _KOKORO_CACHE:
            logger.info("[Kokoro] Loading KPipeline: lang_code=%s", self.lang_code)
            _KOKORO_CACHE[cache_key] = KPipeline(lang_code=self.lang_code)
            logger.info("[Kokoro] KPipeline loaded")
        pipeline = _KOKORO_CACHE[cache_key]

        # Kokoro yields (graphemes, phonemes, audio) tuples per chunk.
        chunks = []
        for _, _, audio in pipeline(text, voice=self.voice, speed=self.speed):
            if audio is None:
                continue
            if hasattr(audio, "detach"):  # torch tensor
                audio = audio.detach().cpu().numpy()
            chunks.append(np.asarray(audio, dtype=np.float32))

        if not chunks:
            raise RuntimeError(
                f"Kokoro produced no audio for voice='{self.voice}' lang_code='{self.lang_code}'"
            )
        audio_out = np.concatenate(chunks, axis=0)

        # Kokoro outputs 24kHz. Write WAV; convert to mp3/ogg if requested.
        wav_path = output_path
        if not output_path.endswith(".wav"):
            wav_path = output_path.rsplit(".", 1)[0] + ".wav"

        sf.write(wav_path, audio_out, 24000)

        if wav_path != output_path:
            ffmpeg = shutil.which("ffmpeg")
            if ffmpeg:
                subprocess.run(
                    [ffmpeg, "-i", wav_path, "-y", "-loglevel", "error", output_path],
                    check=True,
                    timeout=30,
                )
                try:
                    os.remove(wav_path)
                except OSError:
                    pass
            else:
                # No ffmpeg — keep WAV at the requested path
                os.rename(wav_path, output_path)

        return output_path


def make_kokoro(config: Dict[str, Any]) -> KokoroTTS:
    """Factory used by the registry."""
    cfg = config or {}
    return KokoroTTS(
        voice=cfg.get("voice", "af_heart"),
        lang_code=cfg.get("lang_code", "a"),
        speed=float(cfg.get("speed", 1.0)),
    )
