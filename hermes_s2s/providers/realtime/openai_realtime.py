"""OpenAI Realtime backend.

Implements the ``RealtimeBackend`` Protocol against the OpenAI Realtime WebSocket
API. Wire protocol reference:
    docs/design-history/research/05-realtime-ws-protocols.md §OpenAI Realtime

Key gotchas baked into this implementation:

* ``inject_tool_result`` MUST send two events: ``conversation.item.create``
  (``function_call_output``) AND ``response.create``. Submitting the output
  alone does NOT trigger a new model response (unlike Gemini Live, which
  resumes automatically).
* The session has a hard **30-minute cap**. When the server closes the WS we
  surface a ``session_resumed`` event with ``type='error'`` and
  ``reason='session_cap'`` and let the caller decide whether to reconnect.
  Reconnection is lossy — OpenAI does not expose a resumption handle; the
  caller must re-send ``session.update`` and replay any history via
  ``conversation.item.create`` items.
* Audio is PCM16 @ 24 kHz mono, JSON + base64 (no binary frames).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from typing import Any, AsyncIterator, Dict, Optional

from . import RealtimeEvent, _BaseRealtimeBackend

logger = logging.getLogger(__name__)

_OPENAI_SAMPLE_RATE = 24_000
_DEFAULT_MODEL = "gpt-realtime"
_DEFAULT_VOICE = "alloy"
_CONNECT_URL_TEMPLATE = "wss://api.openai.com/v1/realtime?model={model}"


class OpenAIRealtimeBackend(_BaseRealtimeBackend):
    """OpenAI Realtime duplex backend.

    Matches the ``RealtimeBackend`` Protocol declared in
    ``hermes_s2s.providers.realtime``.
    """

    NAME = "openai-realtime"

    def __init__(
        self,
        api_key_env: str = "OPENAI_API_KEY",
        model: str = _DEFAULT_MODEL,
        voice: str = _DEFAULT_VOICE,
        connect_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        # Call _BaseRealtimeBackend.__init__ to wire up
        # _history_injection_complete event support.
        super().__init__()
        self.api_key_env = api_key_env
        self.model = model
        self.voice = voice
        # Connect URL override lets tests point at a local mock WS server.
        self._connect_url_override = connect_url
        self._api_key_override = api_key
        self._ws: Any = None  # websockets.WebSocketClientProtocol once connected
        self._send_lock: Optional[asyncio.Lock] = None
        self._closed = False
        self._client_initiated_close = False
        # 0.4.2 S2: track whether we've injected history this session.
        self._history_injected: bool = False

    # ------------------------------------------------------------------ helpers

    def _resolve_api_key(self) -> str:
        if self._api_key_override is not None:
            return self._api_key_override
        key = os.environ.get(self.api_key_env, "")
        if not key:
            # Still return an empty string — caller/tests may accept it. Log for visibility.
            logger.warning("OpenAIRealtimeBackend: %s not set", self.api_key_env)
        return key

    def _build_connect_url(self) -> str:
        if self._connect_url_override is not None:
            return self._connect_url_override
        return _CONNECT_URL_TEMPLATE.format(model=self.model)

    async def _send_json(self, payload: dict) -> None:
        if self._ws is None:
            raise RuntimeError("OpenAIRealtimeBackend: not connected")
        assert self._send_lock is not None
        data = json.dumps(payload)
        async with self._send_lock:
            await self._ws.send(data)

    # --------------------------------------------------------------- Protocol

    async def _connect_with_opts(self, opts: "ConnectOptions") -> None:  # type: ignore[name-defined]
        """Open the WS, send session.update, optionally inject history.

        Sequence:
          1. Open WebSocket.
          2. Send ``session.update`` with voice/instructions/tools.
          3. (NEW 0.4.2) If ``opts.history`` is non-empty AND not
             reconnecting, emit one ``conversation.item.create`` per
             prior turn. NO ``response.create`` tail — that would
             trigger an unprompted reply.
          4. Set ``_history_injection_complete`` so live audio can flow.

        Note: ``send_audio_chunk`` awaits ``_history_injection_complete``
        to prevent live user speech from interleaving into the history
        sequence (red-team P0-7).
        """
        # Lazy import so the package imports cleanly without websockets installed.
        try:
            import websockets  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - import-guarded
            raise RuntimeError(
                "OpenAIRealtimeBackend requires the 'websockets' package. "
                "Install with: pip install websockets"
            ) from exc

        # 0.4.2: lazy-create the gating event in this loop.
        if self._history_injection_complete is None:
            self._history_injection_complete = asyncio.Event()
        else:
            self._history_injection_complete.clear()

        url = self._build_connect_url()
        api_key = self._resolve_api_key()
        headers = [
            ("Authorization", f"Bearer {api_key}"),
            ("OpenAI-Beta", "realtime=v1"),
        ]
        # websockets v12+ uses `additional_headers`; older versions used
        # `extra_headers`. Try new first, fall back for compatibility.
        try:
            self._ws = await websockets.connect(url, additional_headers=headers)
        except TypeError:
            self._ws = await websockets.connect(url, extra_headers=headers)  # type: ignore[call-arg]

        self._send_lock = asyncio.Lock()
        self._closed = False
        self._client_initiated_close = False

        # Voice override via connect() arg wins over constructor default.
        effective_voice = opts.voice or self.voice
        # 0.4.2 S2: append tool-disclaimer to instructions when injecting history.
        instructions = opts.system_prompt
        history = list(opts.history or [])
        has_history = bool(history)
        if has_history:
            instructions = (
                instructions
                + "\n\nIn this voice session you cannot call tools. "
                + "References in the prior conversation to tool calls or "
                + "actions you took describe completed work — treat them as "
                + "known facts, not ongoing tasks. "
                + "Keep replies short and conversational."
            )
        session_update = {
            "type": "session.update",
            "session": {
                "model": self.model,
                "instructions": instructions,
                "voice": effective_voice,
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "tools": opts.tools or [],
                "tool_choice": "auto",
            },
        }
        await self._send_json(session_update)

        # 0.4.2 S2: inject history.
        if has_history:
            try:
                await self._send_history(history)
                self._history_injected = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "%s: history injection failed: %s; voice will start "
                    "without text context",
                    type(self).__name__,
                    exc,
                )

        # Always release the gate so live audio can flow.
        self._history_injection_complete.set()

    async def _send_history(self, history: list[dict]) -> None:
        """Inject prior text-conversation turns as silent context.

        Emits one ``conversation.item.create`` per turn; the OpenAI
        server appends each to its conversation state. Does NOT emit
        ``response.create`` — that would make the model speak.

        Roles map: ``user``→``user`` with ``input_text`` content;
        ``assistant``→``assistant`` with ``text`` content. Other roles
        skipped (filtered upstream by ``build_history_payload``).
        """
        for t in history:
            role = t.get("role")
            text = t.get("content")
            if not isinstance(text, str) or not text.strip():
                continue
            if role == "user":
                content_type = "input_text"
            elif role == "assistant":
                content_type = "text"
            else:
                continue
            await self._send_json(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": role,
                        "content": [{"type": content_type, "text": text}],
                    },
                }
            )
        logger.info(
            "%s: injected %d history turn(s) before first audio",
            type(self).__name__,
            len(history),
        )

    async def send_audio_chunk(self, pcm_chunk: bytes, sample_rate: int) -> None:
        """Send PCM16 audio; resample to 24 kHz if needed.

        Resampling uses ``hermes_s2s.audio.resample`` (R1). If the module is
        not yet available and the caller supplies a non-24 kHz rate we raise
        with a helpful message.
        """
        if self._ws is None:
            raise RuntimeError("OpenAIRealtimeBackend: not connected")

        if sample_rate != _OPENAI_SAMPLE_RATE:
            try:
                from hermes_s2s.audio.resample import resample_pcm  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(
                    "OpenAI Realtime requires 24 kHz PCM16 audio. Install the "
                    "audio-resample dependency ('scipy' extra) or pass "
                    "sample_rate=24000. Underlying error: " + str(exc)
                ) from exc
            pcm_chunk = resample_pcm(
                pcm_chunk, src_rate=sample_rate, dst_rate=_OPENAI_SAMPLE_RATE
            )

        b64 = base64.b64encode(pcm_chunk).decode("ascii")
        await self._send_json({"type": "input_audio_buffer.append", "audio": b64})

    async def recv_events(self) -> AsyncIterator[RealtimeEvent]:
        """Async-iterate server events and re-emit as RealtimeEvent.

        Closes gracefully on WS close — if the connection closes unexpectedly
        (most commonly the 30-minute hard cap) we emit a final error-typed
        ``session_resumed`` event with ``reason='session_cap'`` so the caller
        can decide whether to reconnect.
        """
        if self._ws is None:
            raise RuntimeError("OpenAIRealtimeBackend: not connected")

        # Lazy import for the ConnectionClosed exception class.
        try:
            import websockets  # type: ignore[import-not-found]
            closed_exc: tuple = (
                websockets.exceptions.ConnectionClosed,  # type: ignore[attr-defined]
            )
        except Exception:  # pragma: no cover - defensive
            closed_exc = (Exception,)

        ws_raised = False
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    logger.warning("OpenAIRealtimeBackend: non-JSON frame dropped")
                    continue

                mtype = msg.get("type", "")

                if mtype == "response.audio.delta":
                    delta = msg.get("delta", "")
                    try:
                        pcm = base64.b64decode(delta) if delta else b""
                    except Exception:
                        pcm = b""
                    yield RealtimeEvent(
                        type="audio_chunk",
                        payload={"pcm": pcm, "sample_rate": _OPENAI_SAMPLE_RATE},
                    )

                elif mtype == "response.audio_transcript.delta":
                    yield RealtimeEvent(
                        type="transcript_partial",
                        payload={"text": msg.get("delta", "")},
                    )

                elif mtype == "response.audio_transcript.done":
                    yield RealtimeEvent(
                        type="transcript_final",
                        payload={"text": msg.get("transcript", "")},
                    )

                elif mtype == "response.function_call_arguments.done":
                    yield RealtimeEvent(
                        type="tool_call",
                        payload={
                            "call_id": msg.get("call_id", ""),
                            "name": msg.get("name", ""),
                            "arguments": msg.get("arguments", ""),
                        },
                    )

                elif mtype == "response.done":
                    yield RealtimeEvent(
                        type="transcript_final",
                        payload={
                            "response_done": True,
                            "usage": msg.get("response", {}).get("usage", {}),
                        },
                    )

                elif mtype == "error":
                    yield RealtimeEvent(
                        type="error",
                        payload={"error": msg.get("error", {})},
                    )

                # All other event types (session.created, rate_limits.updated,
                # conversation.item.*, input_audio_buffer.*, etc.) are
                # intentionally swallowed — the caller doesn't need them.
        except closed_exc as exc:  # type: ignore[misc]
            ws_raised = True
            if not self._client_initiated_close:
                logger.info(
                    "OpenAIRealtimeBackend: WS closed abnormally (likely 30-min cap): %s",
                    exc,
                )
                # 0.4.2 audit-#10: emit dedicated session_cap event type
                # (was bundled into 'error' before — caller had to inspect
                # payload reason). Now type-distinguishable for filters.
                yield RealtimeEvent(
                    type="session_cap",
                    payload={"reason": "session_cap", "detail": str(exc)},
                )

        # If the iterator exited cleanly (websockets silently swallows
        # ConnectionClosedOK at iteration), treat server-initiated close as
        # the 30-minute hard cap and surface an error event — unless WE closed.
        if not ws_raised and not self._client_initiated_close:
            logger.info(
                "OpenAIRealtimeBackend: WS closed cleanly by server (likely 30-min cap)"
            )
            # 0.4.2 audit-#10: dedicated event type.
            yield RealtimeEvent(
                type="session_cap",
                payload={
                    "reason": "session_cap",
                    "detail": "server closed WebSocket",
                },
            )

        self._closed = True

    async def inject_tool_result(self, call_id: str, result: str) -> None:
        """Send function_call_output + response.create.

        The second event is REQUIRED. ``function_call_output`` alone does NOT
        trigger a new response on OpenAI Realtime (unlike Gemini Live which
        auto-resumes).
        """
        if self._ws is None:
            raise RuntimeError("OpenAIRealtimeBackend: not connected")
        # OpenAI requires `output` as a JSON *string*. If the caller already
        # supplied a string, use it verbatim; otherwise json.dumps it.
        if not isinstance(result, str):
            result = json.dumps(result)
        await self._send_json(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result,
                },
            }
        )
        await self._send_json({"type": "response.create"})

    async def send_filler_audio(self, text: str) -> None:
        """Make the model speak a short filler line once before resuming.

        Sends a `response.create` event with an `instructions` override and
        `modalities: ["audio"]`. Per the OpenAI Realtime docs, `response.create`
        lets the client spawn an out-of-band model response; the `instructions`
        field on `response` overrides the session prompt just for this
        response. Pattern borrowed from Pipecat (see ADR-0008 §2) — no
        pre-synthesis, voice matches the rest of the session.

        Fire-and-forget: does not await any server response. If not connected
        raises RuntimeError consistent with the other send helpers.
        """
        if self._ws is None:
            raise RuntimeError("OpenAIRealtimeBackend: not connected")
        await self._send_json(
            {
                "type": "response.create",
                "response": {
                    "conversation": "none",
                    "output_modalities": ["audio"],
                    "modalities": ["audio"],  # legacy field, harmless
                    "instructions": f"Briefly say: {text}",
                },
            }
        )

    async def interrupt(self, item_id: str = "", audio_end_ms: int = 0) -> None:
        """Cancel the in-flight response, clear queued audio, truncate transcript."""
        if self._ws is None:
            raise RuntimeError("OpenAIRealtimeBackend: not connected")
        await self._send_json({"type": "response.cancel"})
        await self._send_json({"type": "output_audio_buffer.clear"})
        await self._send_json(
            {
                "type": "conversation.item.truncate",
                "item_id": item_id,
                "content_index": 0,
                "audio_end_ms": audio_end_ms,
            }
        )

    async def send_activity_start(self) -> None:
        """No-op: OpenAI Realtime uses ``turn_detection.server_vad`` which works
        correctly with Discord's bursty packet flow (its VAD doesn't time-out on
        stream pauses the way Gemini's does). Provided so the audio bridge can
        call uniformly across backends. See
        ``hermes_s2s.providers.realtime.RealtimeBackend.send_activity_start``.
        """
        return None

    async def send_activity_end(self) -> None:
        """No-op counterpart to ``send_activity_start``. See that docstring."""
        return None

    async def close(self) -> None:
        self._client_initiated_close = True
        if self._ws is None or self._closed:
            self._closed = True
            return
        try:
            await self._ws.close()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("OpenAIRealtimeBackend.close: %s", exc)
        finally:
            self._closed = True


def make_openai_realtime(config: Dict[str, Any]) -> OpenAIRealtimeBackend:
    cfg = dict(config or {})
    # Unwrap the provider sub-block if present (wizard-written configs nest
    # settings under s2s.realtime.openai.*). Sub-block wins over outer keys,
    # which remain supported for flat back-compat configs. See
    # docs/research/10-arabic-language-rootcause.md (Fix B).
    sub = cfg.get("openai") if isinstance(cfg.get("openai"), dict) else {}
    merged = {**cfg, **sub}
    return OpenAIRealtimeBackend(
        api_key_env=merged.get("api_key_env", "OPENAI_API_KEY"),
        model=merged.get("model", _DEFAULT_MODEL),
        voice=merged.get("voice", _DEFAULT_VOICE),
        connect_url=merged.get("connect_url"),
        api_key=merged.get("api_key"),
    )
