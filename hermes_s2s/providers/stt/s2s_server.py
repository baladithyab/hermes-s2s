"""s2s-server STT — delegate transcription to an external streaming-speech-to-speech server's /asr endpoint.

The endpoint is expected to accept a multipart upload of an audio file and
return JSON `{"transcript": "...", "language": "en", ...}`. If your server
exposes a different shape, write a thin custom provider — see registry.register_stt.

NOTE: S2SServerUnavailable is defined per-file (stt + tts) rather than in a
shared _internal module to keep the provider surface flat and import-light —
callers typically only touch one side at a time.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)


class S2SServerUnavailable(RuntimeError):
    """Raised when the configured s2s-server endpoint is unreachable or unsupported."""


class S2SServerSTT:
    def __init__(self, endpoint: str, timeout: float = 30.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = float(timeout)
        parsed = urlparse(self.endpoint)
        if parsed.scheme in ("ws", "wss"):
            logger.warning(
                "s2s-server STT configured with %s:// endpoint; WebSocket pipeline "
                "mode is 0.3.0+. Falling back / raising on use.", parsed.scheme,
            )
            self._scheme = "ws"
        else:
            self._scheme = parsed.scheme or "http"

    def _base_url(self) -> str:
        parsed = urlparse(self.endpoint)
        return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))

    def health_check(self) -> Tuple[bool, str]:
        try:
            import httpx  # type: ignore
        except ImportError as exc:
            return False, f"httpx missing: {exc}"
        url = f"{self._base_url()}/health"
        try:
            resp = httpx.get(url, timeout=2.0)
            return (resp.status_code < 400, f"{resp.status_code}")
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def transcribe(self, file_path: str | Path) -> Dict[str, Any]:
        if self._scheme == "ws":
            raise S2SServerUnavailable(
                "WebSocket pipeline mode is 0.3.0+; use http(s):// endpoints for stage-only providers"
            )
        try:
            import httpx  # type: ignore
        except ImportError as exc:
            return {"success": False, "transcript": "",
                    "error": f"httpx required for s2s-server STT: {exc}"}

        url = self.endpoint if self.endpoint.endswith("/asr") else f"{self.endpoint}/asr"
        try:
            with open(file_path, "rb") as f:
                resp = httpx.post(url, files={"audio": f}, timeout=self.timeout)
        except FileNotFoundError:
            return {"success": False, "transcript": "", "error": f"Audio file not found: {file_path}"}
        except httpx.ConnectError as exc:
            raise S2SServerUnavailable(f"endpoint unreachable: {url} ({exc})") from exc
        except Exception as exc:
            logger.error("s2s-server STT failed: %s", exc, exc_info=True)
            return {"success": False, "transcript": "", "error": f"s2s-server STT failed: {exc}"}

        if resp.status_code >= 500:
            raise S2SServerUnavailable(f"endpoint unreachable: {url} ({resp.status_code})")
        try:
            resp.raise_for_status()
            data = resp.json() or {}
            return {
                "success": True,
                "transcript": str(data.get("transcript", "")).strip(),
                "provider": "s2s-server",
            }
        except Exception as exc:
            logger.error("s2s-server STT failed: %s", exc, exc_info=True)
            return {"success": False, "transcript": "", "error": f"s2s-server STT failed: {exc}"}


def make_s2s_server_stt(config: Dict[str, Any]) -> S2SServerSTT:
    cfg = config or {}
    endpoint = cfg.get("endpoint") or "http://localhost:8000/asr"
    return S2SServerSTT(endpoint=endpoint, timeout=cfg.get("timeout", 30.0))
