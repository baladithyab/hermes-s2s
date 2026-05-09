"""s2s-server TTS — delegate synthesis to an external server's /tts endpoint.

POSTs JSON `{"text": "...", "voice": "..."}` and expects a binary audio
response (WAV preferred). Convert downstream if a different output format
is requested.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)


class S2SServerTTS:
    def __init__(
        self,
        endpoint: str,
        voice: str = "af_heart",
        timeout: float = 30.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.voice = voice
        self.timeout = float(timeout)

    def synthesize(self, text: str, output_path: str | Path) -> str:
        try:
            import httpx  # type: ignore
        except ImportError as exc:
            raise ImportError(f"httpx required for s2s-server TTS: {exc}") from exc

        url = self.endpoint if self.endpoint.endswith("/tts") else f"{self.endpoint}/tts"
        resp = httpx.post(
            url,
            json={"text": text, "voice": self.voice},
            timeout=self.timeout,
        )
        resp.raise_for_status()

        output_path = str(output_path)
        wav_path = output_path
        if not output_path.endswith(".wav"):
            wav_path = output_path.rsplit(".", 1)[0] + ".wav"
        with open(wav_path, "wb") as f:
            f.write(resp.content)

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
                os.rename(wav_path, output_path)

        return output_path


def make_s2s_server_tts(config: Dict[str, Any]) -> S2SServerTTS:
    cfg = config or {}
    return S2SServerTTS(
        endpoint=cfg.get("endpoint") or "http://localhost:8000/tts",
        voice=cfg.get("voice") or "af_heart",
        timeout=cfg.get("timeout", 30.0),
    )
