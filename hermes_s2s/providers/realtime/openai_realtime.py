"""OpenAI Realtime backend — STUB.

Roadmap:
  - WebSocket: wss://api.openai.com/v1/realtime
  - Audio: PCM16 24kHz both directions.
  - Models: gpt-realtime ($1.44/30min), gpt-realtime-mini / gpt-4o-mini-realtime ($0.45/30min)
  - Voices: alloy, echo, fable, onyx, nova, shimmer + new (cedar, marin)
  - Sessions: hard 30-min cap; backend tears down + reconnects on cap; system prompt + tool list re-sent.
  - Tool calling: native function-calling format.

See plugins/aria/docs/research/02-realtime-apis.md for cited research notes.
"""

from __future__ import annotations

from typing import Any, Dict

from . import _BaseRealtimeBackend


class OpenAIRealtimeBackend(_BaseRealtimeBackend):
    NAME = "openai-realtime"


def make_openai_realtime(config: Dict[str, Any]) -> OpenAIRealtimeBackend:
    cfg = config or {}
    # Default to mini for cost; user can override to gpt-realtime
    return OpenAIRealtimeBackend(
        api_key_env="OPENAI_API_KEY",
        model=cfg.get("model", "gpt-realtime-mini"),
        voice=cfg.get("voice", "cedar"),
    )
