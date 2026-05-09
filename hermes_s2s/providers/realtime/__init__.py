"""Realtime backend abstraction.

A duplex backend consumes audio chunks from the user and produces audio chunks
back, end-to-end inside the model's session. Tool calls surface as events the
gateway routes to Hermes's tool dispatcher; results are injected back via
`inject_tool_result`.

Concrete implementations: gemini_live.GeminiLiveBackend, openai_realtime.OpenAIRealtimeBackend.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Literal, Protocol, runtime_checkable


@dataclass
class RealtimeEvent:
    """Tagged-union event emitted from `recv_events`."""
    type: Literal[
        "audio_chunk",
        "transcript_partial",
        "transcript_final",
        "tool_call",
        "session_resumed",
        "error",
    ]
    payload: dict


@runtime_checkable
class RealtimeBackend(Protocol):
    """Duplex realtime model interface.

    Implementations: GeminiLiveBackend, OpenAIRealtimeBackend, and any future
    third-party realtime backend (xAI Voice, AWS Nova Sonic, Mistral Voice...).
    """

    async def connect(self, system_prompt: str, voice: str, tools: list[dict]) -> None: ...
    async def send_audio_chunk(self, pcm_chunk: bytes, sample_rate: int) -> None: ...
    async def recv_events(self) -> AsyncIterator[RealtimeEvent]: ...
    async def inject_tool_result(self, call_id: str, result: str) -> None: ...
    async def send_filler_audio(self, text: str) -> None: ...
    async def interrupt(self) -> None: ...
    async def close(self) -> None: ...


from hermes_s2s import __version__ as _pkg_version


class _BaseRealtimeBackend:
    """Stub base — not implemented yet. Subclasses should raise NotImplementedError
    with a helpful message until the backend is wired in.
    """

    NAME = "base"

    def __init__(self, **kwargs: Any) -> None:
        self.config = kwargs

    async def connect(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            f"{self.NAME} realtime backend is a stub in this {_pkg_version} release. "
            "Track progress at https://github.com/baladithyab/hermes-s2s — "
            "PRs welcome."
        )

    async def send_audio_chunk(self, *a: Any, **kw: Any) -> None:
        raise NotImplementedError

    async def recv_events(self) -> AsyncIterator[RealtimeEvent]:  # pragma: no cover - stub
        if False:  # noqa: SIM108 - keep async generator typing
            yield RealtimeEvent(type="error", payload={"message": "stub"})
        raise NotImplementedError

    async def inject_tool_result(self, *a: Any, **kw: Any) -> None:
        raise NotImplementedError

    async def send_filler_audio(self, text: str) -> None:
        raise NotImplementedError(
            f"{type(self).__name__}.send_filler_audio not implemented"
        )

    async def interrupt(self) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError
