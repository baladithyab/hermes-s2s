"""s2s-server STT — delegate transcription to an external streaming-speech-to-speech server's /asr endpoint.

The endpoint is expected to accept a multipart upload of an audio file and
return JSON `{"transcript": "...", "language": "en", ...}`. If your server
exposes a different shape, write a thin custom provider — see registry.register_stt.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)


class S2SServerSTT:
    def __init__(self, endpoint: str, timeout: float = 30.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = float(timeout)

    def transcribe(self, file_path: str | Path) -> Dict[str, Any]:
        try:
            import httpx  # type: ignore
        except ImportError as exc:
            return {"success": False, "transcript": "",
                    "error": f"httpx required for s2s-server STT: {exc}"}

        url = self.endpoint if self.endpoint.endswith("/asr") else f"{self.endpoint}/asr"
        try:
            with open(file_path, "rb") as f:
                resp = httpx.post(url, files={"audio": f}, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json() or {}
            return {
                "success": True,
                "transcript": str(data.get("transcript", "")).strip(),
                "provider": "s2s-server",
            }
        except FileNotFoundError:
            return {"success": False, "transcript": "", "error": f"Audio file not found: {file_path}"}
        except Exception as exc:
            logger.error("s2s-server STT failed: %s", exc, exc_info=True)
            return {"success": False, "transcript": "", "error": f"s2s-server STT failed: {exc}"}


def make_s2s_server_stt(config: Dict[str, Any]) -> S2SServerSTT:
    cfg = config or {}
    endpoint = cfg.get("endpoint") or "http://localhost:8000/asr"
    return S2SServerSTT(endpoint=endpoint, timeout=cfg.get("timeout", 30.0))
