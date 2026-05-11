"""Realtime backend abstraction.

A duplex backend consumes audio chunks from the user and produces audio chunks
back, end-to-end inside the model's session. Tool calls surface as events the
gateway routes to Hermes's tool dispatcher; results are injected back via
`inject_tool_result`.

Concrete implementations: gemini_live.GeminiLiveBackend, openai_realtime.OpenAIRealtimeBackend.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, List, Literal, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass
class RealtimeEvent:
    """Tagged-union event emitted from `recv_events`."""
    type: Literal[
        "audio_chunk",
        "transcript_partial",
        "transcript_final",
        "tool_call",
        "session_resumed",
        "session_cap",
        "error",
    ]
    payload: dict


@runtime_checkable
class RealtimeBackend(Protocol):
    """Duplex realtime model interface.

    Implementations: GeminiLiveBackend, OpenAIRealtimeBackend, and any future
    third-party realtime backend (xAI Voice, AWS Nova Sonic, Mistral Voice...).

    The 0.4.2+ protocol accepts either:
      - positional: ``connect(system_prompt, voice, tools)`` (back-compat)
      - dataclass:  ``connect(ConnectOptions(...))``

    Concrete backends route both shapes through ``_connect_with_opts``.
    """

    async def connect(self, *args: Any, **kwargs: Any) -> None: ...
    async def send_audio_chunk(self, pcm_chunk: bytes, sample_rate: int) -> None: ...
    async def send_activity_start(self) -> None: ...
    async def send_activity_end(self) -> None: ...
    async def recv_events(self) -> AsyncIterator[RealtimeEvent]: ...
    async def inject_tool_result(self, call_id: str, result: str) -> None: ...
    async def send_filler_audio(self, text: str) -> None: ...
    async def interrupt(self) -> None: ...
    async def close(self) -> None: ...


from hermes_s2s import __version__ as _pkg_version


class _BaseRealtimeBackend:
    """Common machinery for concrete realtime backends.

    0.4.2: provides the ConnectOptions shim. Concrete subclasses override
    ``_connect_with_opts(opts)`` (the real implementation) AND inherit
    ``connect(*args, **kwargs)`` (the public-API shim that adapts both
    positional and dataclass call shapes).

    History gating: ``_history_injection_complete`` is an ``asyncio.Event``
    set after ``connect`` finishes injecting any provided history. Pumps
    that send live audio chunks should ``await`` this event before sending
    so live speech doesn't interleave into the history sequence (red-team
    P0-7). Subclasses MUST set this event in their ``_connect_with_opts``
    before returning.
    """

    NAME = "base"

    def __init__(self, **kwargs: Any) -> None:
        self.config = kwargs
        # 0.4.2: gating event for history-injection-vs-live-audio race.
        # Lazy-create to avoid binding to wrong event loop in tests.
        self._history_injection_complete: Optional[Any] = None

    # ------------------------------------------------------------------
    # 0.4.2: connect() shim — accepts ConnectOptions OR positional triple
    # ------------------------------------------------------------------

    async def connect(self, *args: Any, **kwargs: Any) -> None:
        """Public connect entry point. Adapts call shape to ``_connect_with_opts``.

        Accepts:
          - ``connect(opts: ConnectOptions)``
          - ``connect(system_prompt, voice, tools)`` (legacy positional)
          - ``connect(system_prompt, voice, tools, history=[...])``
        """
        from hermes_s2s.voice.connect_options import ConnectOptions

        if len(args) == 1 and isinstance(args[0], ConnectOptions):
            opts = args[0]
        elif len(args) >= 3:
            # Legacy positional: (system_prompt, voice, tools, **kwargs)
            opts = ConnectOptions.from_positional(
                args[0], args[1], args[2], **kwargs
            )
        elif "system_prompt" in kwargs and "voice" in kwargs and "tools" in kwargs:
            opts = ConnectOptions.from_positional(
                kwargs.pop("system_prompt"),
                kwargs.pop("voice"),
                kwargs.pop("tools"),
                **kwargs,
            )
        else:
            raise TypeError(
                f"{type(self).__name__}.connect: expected ConnectOptions or "
                "(system_prompt, voice, tools) positional"
            )
        await self._connect_with_opts(opts)

    async def _connect_with_opts(self, opts: Any) -> None:
        """Real connect implementation. Override in subclasses."""
        raise NotImplementedError(
            f"{self.NAME} realtime backend is a stub in this {_pkg_version} release. "
            "Track progress at https://github.com/baladithyab/hermes-s2s — "
            "PRs welcome."
        )

    async def send_audio_chunk(self, *a: Any, **kw: Any) -> None:
        raise NotImplementedError

    async def send_activity_start(self) -> None:
        # Default no-op: backends with server-side VAD that handles Discord-style
        # bursty input correctly (e.g. OpenAI Realtime) can leave this unimplemented.
        # Manual-VAD backends (Gemini Live with AAD disabled) override.
        return None

    async def send_activity_end(self) -> None:
        # See send_activity_start docstring.
        return None

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
