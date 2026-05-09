"""Gemini Live realtime backend — STUB.

Roadmap:
  - WebSocket: wss://generativelanguage.googleapis.com/ws/.../BidiGenerateContent
  - Audio: PCM16 16kHz in, PCM16 24kHz out.
  - Models: gemini-live-2.5-flash (half-cascade, ~$0.06/30min)
            gemini-2.5-flash-native-audio (~$0.36/30min)
            gemini-3.1-flash-live (preview)
  - Voices: 30+ (Aoede, Charon, Fenrir, Kore, Puck, ...)
  - Sessions: ~15min audio-only, extensible via session-resumption tokens.
  - Tool calling: native function-calling format.

See plugins/aria/docs/research/02-realtime-apis.md (in the original Hermes
fork branch) for cited research notes.
"""

from __future__ import annotations

from typing import Any, Dict

from . import _BaseRealtimeBackend


class GeminiLiveBackend(_BaseRealtimeBackend):
    NAME = "gemini-live"


def make_gemini_live(config: Dict[str, Any]) -> GeminiLiveBackend:
    cfg = config or {}
    return GeminiLiveBackend(
        api_key_env="GEMINI_API_KEY",
        model=cfg.get("model", "gemini-live-2.5-flash"),
        voice=cfg.get("voice", "Aoede"),
    )
