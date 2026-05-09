"""s2s-server full-pipeline backend — STUB.

A pipeline backend owns the full audio-in to audio-out turn inside an
external process. The reference target is
https://github.com/codeseys/streaming-speech-to-speech — its FastAPI WS
server speaks a JSON+binary protocol (turn_start / turn_end / asr_result /
asr_partial / tts_chunk frames).

When mode == "s2s-server", Hermes voice mode hands the user's audio chunks
straight to the server and re-emits the server's TTS chunks. The server
runs the full Moonshine + vLLM + Kokoro stack with all v6 optimizations.
This means Hermes's normal LLM call is BYPASSED for the turn — the local
model speaks. Useful when low-latency conversation matters more than
Hermes-side tool access.

Stage-only modes (`stt.provider: s2s-server`, `tts.provider: s2s-server`)
delegate just one stage; Hermes's LLM and the other stage stay in play.
See providers/stt/s2s_server.py and providers/tts/s2s_server.py.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict


class S2SServerPipeline:
    """Full-duplex pipeline backed by an external WS server.

    Stub for 0.1.0. Real impl will use the `websockets` package to:
      1. Open WS, send turn_start.
      2. Stream Float32 PCM frames inbound.
      3. Receive asr_partial, asr_result, tts_chunk events.
      4. Yield outbound audio + transcript via an async iterator.
      5. Close on turn_end / cancel.
    """

    def __init__(self, endpoint: str, health_url: str = "", auto_launch: bool = False) -> None:
        self.endpoint = endpoint
        self.health_url = health_url
        self.auto_launch = auto_launch

    async def converse(
        self,
        audio_in: AsyncIterator[bytes],
        sample_rate: int,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Stub. Yields {"type": "audio_chunk"|"transcript", ...} events."""
        raise NotImplementedError(
            "s2s-server pipeline backend is a stub in 0.1.0. "
            "Run your streaming-speech-to-speech server independently for now and "
            "use stage-only mode (stt.provider: s2s-server / tts.provider: s2s-server). "
            "Track progress at https://github.com/codeseys/hermes-s2s."
        )


def make_s2s_server_pipeline(config: Dict[str, Any]) -> S2SServerPipeline:
    cfg = config or {}
    return S2SServerPipeline(
        endpoint=cfg.get("endpoint") or "ws://localhost:8000/ws",
        health_url=cfg.get("health_url") or "http://localhost:8000/health",
        auto_launch=bool(cfg.get("auto_launch", False)),
    )
