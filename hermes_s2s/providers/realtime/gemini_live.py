"""Gemini Live realtime backend.

Implements `RealtimeBackend` for Google's Gemini Live API (BidiGenerateContent).

Wire protocol: see docs/design-history/research/05-realtime-ws-protocols.md
§Gemini Live for the authoritative JSON shapes.

Key points:
- Connect: WS to generativelanguage.googleapis.com with `?key=<API_KEY>`.
- Setup: first frame is `BidiGenerateContentSetup` with model/voice/system/tools.
  Server replies `setupComplete` before accepting audio.
- Audio: PCM16 16kHz mono LE, base64 inside JSON text frames (no binary frames).
  Server audio out: PCM16 24kHz mono.
- Tools: Hermes's `{name, description, parameters}` JSON-schema → Gemini
  `{functionDeclarations: [{name, description, parameters}]}`.
- Tool round-trip: server emits `toolCall.functionCalls[]` with `id`; client
  sends `toolResponse.functionResponses[]` with matching `id`. Gemini then
  auto-resumes audio — no extra "continue" frame needed (unlike OpenAI).
- Session resumption: server streams `sessionResumptionUpdate.newHandle`;
  on reconnect, pass it back in `setup.sessionResumption.handle`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from typing import Any, AsyncIterator, Dict, List, Optional

from . import RealtimeEvent, _BaseRealtimeBackend

logger = logging.getLogger(__name__)

GEMINI_LIVE_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
    "?key={api_key}"
)


def _translate_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Translate Hermes tool-schema list → Gemini `tools` list.

    Input (Hermes / OpenAI-ish JSON-schema):
        [{"name": "search", "description": "...",
          "parameters": {"type": "object",
                         "properties": {...}, "required": [...]}}]

    Output (Gemini BidiGenerateContentSetup.tools):
        [{"functionDeclarations": [
            {"name": "search", "description": "...",
             "parameters": {"type": "object", "properties": {...},
                            "required": [...]}}]}]

    We pass `parameters` through verbatim — Gemini accepts lowercase JSON-schema
    types (`"string"`, `"object"`) as well as the canonical uppercase OpenAPI
    forms (`"STRING"`, `"OBJECT"`). If a tool dict has neither `parameters` nor
    `description`, those keys are omitted from the declaration.
    """
    if not tools:
        return []
    decls: List[Dict[str, Any]] = []
    for t in tools:
        decl: Dict[str, Any] = {"name": t["name"]}
        if t.get("description"):
            decl["description"] = t["description"]
        if t.get("parameters"):
            decl["parameters"] = t["parameters"]
        decls.append(decl)
    return [{"functionDeclarations": decls}]


class GeminiLiveBackend(_BaseRealtimeBackend):
    """Duplex realtime backend for Google Gemini Live.

    Construct via `make_gemini_live(config)`. Call `connect()` first, then
    interleave `send_audio_chunk` with `async for ev in recv_events()`.
    On `tool_call` events the caller runs the tool and replies via
    `inject_tool_result(call_id, result_str)`.
    """

    NAME = "gemini-live"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.api_key_env: str = kwargs.get("api_key_env", "GEMINI_API_KEY")
        self.model: str = kwargs.get("model", "gemini-2.5-flash-native-audio-latest")
        self.voice: str = kwargs.get("voice", "Aoede")
        self.language_code: str = kwargs.get("language_code", "en-US")
        # Allow tests to override the connect URL (mock WS).
        self._url_override: Optional[str] = kwargs.get("url")

        self._ws: Any = None
        self._connect_module: Any = None  # websockets.connect
        self._session_handle: Optional[str] = None
        self._last_setup: Optional[Dict[str, Any]] = None
        self._closed = False

    # ------------------------------------------------------------------ #
    # connect / close                                                    #
    # ------------------------------------------------------------------ #
    async def connect(
        self, system_prompt: str, voice: str, tools: List[Dict[str, Any]]
    ) -> None:
        """Open the WS and send the initial setup frame. Waits for setupComplete."""
        # Lazy-import websockets — part of [realtime] extra, not core deps.
        try:
            import websockets  # type: ignore
        except ImportError as e:  # pragma: no cover - env-dependent
            raise NotImplementedError(
                "gemini-live backend requires the `websockets` package. "
                "Install with: pip install 'hermes-s2s[realtime]'"
            ) from e
        self._connect_module = websockets

        if self._url_override:
            url = self._url_override
        else:
            api_key = os.environ.get(self.api_key_env)
            if not api_key:
                raise RuntimeError(
                    f"{self.NAME}: env var {self.api_key_env} is not set. "
                    f"Get a key at https://aistudio.google.com/apikey."
                )
            url = GEMINI_LIVE_URL.format(api_key=api_key)

        self._ws = await websockets.connect(url, max_size=None)
        self.voice = voice or self.voice

        setup = self._build_setup(system_prompt, tools)
        self._last_setup = setup
        await self._ws.send(json.dumps({"setup": setup}))

        # Wait for setupComplete — server MUST send it before accepting audio.
        # Some mock servers may not bother; tolerate that by not blocking forever.
        try:
            first = await asyncio.wait_for(self._ws.recv(), timeout=5.0)
            msg = json.loads(first)
            if "setupComplete" not in msg:
                # Buffer this message so recv_events can re-yield it.
                self._pending_first_msg: Optional[Dict[str, Any]] = msg
            else:
                self._pending_first_msg = None
        except asyncio.TimeoutError:
            self._pending_first_msg = None
        except Exception:  # noqa: BLE001 - fixtures may differ
            self._pending_first_msg = None

    def _build_setup(
        self, system_prompt: str, tools: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        # Defense-in-depth language anchor — Gemini Live native-audio models
        # auto-detect output language from input audio cues, which can pin
        # an entire session to a non-English language when the first user
        # turn is quiet/accented. Prepending an explicit directive to the
        # system prompt keeps the model in the configured language even if
        # the caller passes a weak/minimal system_prompt. See
        # docs/research/10-arabic-language-rootcause.md (Fix C).
        lang_code = (self.language_code or "en-US").split("-")[0].lower()
        _LANG_NAMES = {
            "en": "English",
            "es": "Spanish",
            "fr": "French",
            "de": "German",
            "it": "Italian",
            "pt": "Portuguese",
            "nl": "Dutch",
            "ja": "Japanese",
            "ko": "Korean",
            "zh": "Chinese",
            "ar": "Arabic",
            "hi": "Hindi",
            "ru": "Russian",
            "pl": "Polish",
            "tr": "Turkish",
            "sv": "Swedish",
            "da": "Danish",
            "no": "Norwegian",
            "fi": "Finnish",
            "cs": "Czech",
            "el": "Greek",
            "he": "Hebrew",
            "id": "Indonesian",
            "th": "Thai",
            "vi": "Vietnamese",
            "uk": "Ukrainian",
        }
        lang_name = _LANG_NAMES.get(lang_code, lang_code.upper())
        anchored = f"Respond exclusively in {lang_name}.\n\n{system_prompt}"
        setup: Dict[str, Any] = {
            "model": f"models/{self.model}" if "/" not in self.model else self.model,
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.voice}},
                    "languageCode": self.language_code,
                },
            },
            "systemInstruction": {"parts": [{"text": anchored}]},
            "realtimeInputConfig": {
                "automaticActivityDetection": {"disabled": False},
                "activityHandling": "START_OF_ACTIVITY_INTERRUPTS",
            },
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
            "contextWindowCompression": {"slidingWindow": {}},
        }
        translated = _translate_tools(tools or [])
        if translated:
            setup["tools"] = translated
        # Session resumption: include handle if we have one (for reconnect).
        if self._session_handle:
            setup["sessionResumption"] = {"handle": self._session_handle}
        else:
            setup["sessionResumption"] = {}
        return setup

    async def close(self) -> None:
        self._closed = True
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    # send / recv                                                        #
    # ------------------------------------------------------------------ #
    async def send_audio_chunk(self, pcm_chunk: bytes, sample_rate: int) -> None:
        """Send a PCM audio chunk. Resamples to 16kHz s16le mono if needed."""
        if self._ws is None:
            raise RuntimeError(f"{self.NAME}: not connected")

        data = pcm_chunk
        if sample_rate != 16000:
            # Lazy-import resample util ([audio] extra). If unavailable we fall
            # back to declaring the sample rate in the MIME — Gemini will
            # resample server-side but it's less efficient + lossier.
            try:
                from hermes_s2s.audio.resample import resample_pcm  # type: ignore

                data = resample_pcm(
                    pcm_chunk,
                    src_rate=sample_rate,
                    dst_rate=16000,
                    src_channels=1,
                    dst_channels=1,
                )
                mime = "audio/pcm;rate=16000"
            except ImportError:
                logger.debug(
                    "hermes_s2s.audio.resample not available; declaring src rate %d to server",
                    sample_rate,
                )
                mime = f"audio/pcm;rate={sample_rate}"
        else:
            mime = "audio/pcm;rate=16000"

        b64 = base64.b64encode(data).decode("ascii")
        msg = {"realtimeInput": {"audio": {"data": b64, "mimeType": mime}}}
        await self._ws.send(json.dumps(msg))

    async def recv_events(self) -> AsyncIterator[RealtimeEvent]:
        """Async-iterate server messages, translating to `RealtimeEvent`s."""
        if self._ws is None:
            raise RuntimeError(f"{self.NAME}: not connected")

        # Replay any buffered first-message captured during connect().
        if getattr(self, "_pending_first_msg", None):
            for ev in self._translate_server_msg(self._pending_first_msg):
                yield ev
            self._pending_first_msg = None

        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("gemini-live: non-JSON frame, skipping")
                    continue
                for ev in self._translate_server_msg(msg):
                    yield ev
        except Exception as e:  # noqa: BLE001
            if not self._closed:
                yield RealtimeEvent(type="error", payload={"message": str(e)})

    def _translate_server_msg(self, msg: Dict[str, Any]) -> List[RealtimeEvent]:
        """Fan a single Gemini message out to zero-or-more RealtimeEvents."""
        events: List[RealtimeEvent] = []

        # Error envelope — Gemini's `{error: {...}}` or Google RPC-style.
        if "error" in msg and "serverContent" not in msg:
            events.append(RealtimeEvent(type="error", payload=dict(msg["error"])))
            return events

        sc = msg.get("serverContent")
        if sc:
            # modelTurn.parts[] → audio_chunk + transcript_partial
            model_turn = sc.get("modelTurn") or {}
            for part in model_turn.get("parts", []) or []:
                inline = part.get("inlineData")
                if inline and "audio/pcm" in (inline.get("mimeType") or ""):
                    try:
                        audio = base64.b64decode(inline["data"])
                    except Exception:  # noqa: BLE001
                        audio = b""
                    events.append(
                        RealtimeEvent(
                            type="audio_chunk",
                            payload={
                                "audio": audio,
                                "mime_type": inline.get("mimeType", "audio/pcm;rate=24000"),
                                "sample_rate": 24000,
                            },
                        )
                    )
                if "text" in part:
                    events.append(
                        RealtimeEvent(
                            type="transcript_partial",
                            payload={"text": part["text"], "role": "assistant"},
                        )
                    )
            # Dedicated output transcript events (more common than inline text).
            out_tx = sc.get("outputTranscription")
            if out_tx and out_tx.get("text") is not None:
                events.append(
                    RealtimeEvent(
                        type="transcript_partial",
                        payload={"text": out_tx["text"], "role": "assistant"},
                    )
                )
            in_tx = sc.get("inputTranscription")
            if in_tx and in_tx.get("text") is not None:
                events.append(
                    RealtimeEvent(
                        type="transcript_partial",
                        payload={"text": in_tx["text"], "role": "user"},
                    )
                )
            if sc.get("turnComplete"):
                # Surface a final marker — caller may re-use transcript_final.
                events.append(
                    RealtimeEvent(type="transcript_final", payload={"turn_complete": True})
                )
            return events

        tool_call = msg.get("toolCall")
        if tool_call:
            for fc in tool_call.get("functionCalls", []) or []:
                events.append(
                    RealtimeEvent(
                        type="tool_call",
                        payload={
                            "call_id": fc.get("id"),
                            "name": fc.get("name"),
                            "args": fc.get("args", {}),
                        },
                    )
                )
            return events

        sru = msg.get("sessionResumptionUpdate")
        if sru:
            handle = sru.get("newHandle")
            if handle:
                self._session_handle = handle
            events.append(
                RealtimeEvent(
                    type="session_resumed",
                    payload={
                        "handle": handle,
                        "resumable": sru.get("resumable", True),
                    },
                )
            )
            return events

        # goAway, usageMetadata, setupComplete, toolCallCancellation — no events emitted.
        return events

    # ------------------------------------------------------------------ #
    # tool / control                                                     #
    # ------------------------------------------------------------------ #
    async def inject_tool_result(self, call_id: str, result: str) -> None:
        """Send `BidiGenerateContentToolResponse` with matching call_id.

        `result` is a free-form string (Hermes tool returns JSON-serialized
        strings) — we wrap it in the `{response: {result: <str>}}` envelope
        Gemini expects. Gemini auto-resumes audio after this frame.
        """
        if self._ws is None:
            raise RuntimeError(f"{self.NAME}: not connected")
        # If the tool already returned JSON, try to decode it so it shows up as
        # a structured response; otherwise wrap as a plain string.
        resp_payload: Any
        try:
            resp_payload = json.loads(result)
            if not isinstance(resp_payload, (dict, list)):
                resp_payload = {"result": result}
        except (ValueError, TypeError):
            resp_payload = {"result": result}

        msg = {
            "toolResponse": {
                "functionResponses": [
                    {"id": call_id, "response": resp_payload}
                ]
            }
        }
        await self._ws.send(json.dumps(msg))

    async def send_filler_audio(self, text: str) -> None:
        """Make the model speak `text` once before resuming.

        Sends a `BidiGenerateContentClientContent` frame with a single user
        turn and `turnComplete: true`. Per the Gemini Live `live-api` docs,
        this injects a one-shot turn the model reads aloud using the current
        voice/language configuration, then continues with whatever it was
        doing. Used by HermesToolBridge on soft-timeout (ADR-0008 §2) to
        avoid long silent gaps while a tool is running.

        Fire-and-forget: does not await any server response. If not connected
        raises RuntimeError consistent with `send_audio_chunk` /
        `inject_tool_result`.
        """
        if self._ws is None:
            raise RuntimeError(f"{self.NAME}: not connected")
        msg = {
            "clientContent": {
                "turns": [{"role": "user", "parts": [{"text": text}]}],
                "turnComplete": True,
            }
        }
        await self._ws.send(json.dumps(msg))

    async def interrupt(self) -> None:
        """Manual interrupt — send activityStart/activityEnd bracket.

        Only meaningful when `automaticActivityDetection.disabled=true`. With
        default VAD the server handles barge-in automatically.
        """
        if self._ws is None:
            return
        await self._ws.send(json.dumps({"realtimeInput": {"activityStart": {}}}))
        await self._ws.send(json.dumps({"realtimeInput": {"activityEnd": {}}}))

    # ------------------------------------------------------------------ #
    # reconnect helper (for session resumption)                          #
    # ------------------------------------------------------------------ #
    async def reconnect(self, system_prompt: str, tools: List[Dict[str, Any]]) -> None:
        """Close the current WS and re-open with the stored session handle."""
        await self.close()
        self._closed = False
        await self.connect(system_prompt, self.voice, tools)


def make_gemini_live(config: Dict[str, Any]) -> GeminiLiveBackend:
    cfg = dict(config or {})
    # Unwrap the provider sub-block if present (wizard-written configs nest
    # settings under s2s.realtime.gemini_live.*). Sub-block wins over outer
    # keys, which remain supported for flat back-compat configs. See
    # docs/research/10-arabic-language-rootcause.md (Fix B).
    sub = cfg.get("gemini_live") if isinstance(cfg.get("gemini_live"), dict) else {}
    merged = {**cfg, **sub}
    return GeminiLiveBackend(
        api_key_env=merged.get("api_key_env", "GEMINI_API_KEY"),
        model=merged.get("model", "gemini-2.5-flash-native-audio-latest"),
        voice=merged.get("voice", "Aoede"),
        language_code=merged.get("language_code", "en-US"),
        url=merged.get("url"),  # primarily for tests
    )
