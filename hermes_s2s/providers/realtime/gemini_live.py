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
        # 0.4.2 (audit #6): initialise here so any code path touching
        # recv_events before connect() doesn't AttributeError.
        self._pending_first_msg: Optional[Dict[str, Any]] = None
        # 0.4.2 S2: track whether we've injected history this session
        # (used by reconnect path — sessionResumption skips re-injection).
        self._history_injected: bool = False

    # ------------------------------------------------------------------ #
    # connect / close                                                    #
    # ------------------------------------------------------------------ #
    async def _connect_with_opts(self, opts: "ConnectOptions") -> None:  # type: ignore[name-defined]
        """Open the WS, send setup frame, optionally inject history.

        Sequence:
          1. Open WebSocket.
          2. Send ``setup`` frame.
          3. Wait for ``setupComplete``.
          4. (NEW 0.4.2) If ``opts.history`` is non-empty AND we are NOT
             reconnecting via session resumption, inject prior turns as
             a single ``clientContent.turnComplete=true`` frame.
          5. Set ``_history_injection_complete`` event so the input pump
             can start sending live audio. Always set even on the
             no-history path so audio isn't blocked.
        """
        # Lazy-import websockets — part of [realtime] extra, not core deps.
        try:
            import websockets  # type: ignore
        except ImportError as e:  # pragma: no cover - env-dependent
            raise NotImplementedError(
                "gemini-live backend requires the `websockets` package. "
                "Install with: pip install 'hermes-s2s[realtime]'"
            ) from e
        self._connect_module = websockets

        # 0.4.2: lazy-create the gating event in this loop (binding to
        # the right loop matters when tests call connect from multiple
        # loops across the suite).
        if self._history_injection_complete is None:
            self._history_injection_complete = asyncio.Event()
        else:
            self._history_injection_complete.clear()

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
        self.voice = opts.voice or self.voice

        # Decide whether to inject persona suffix (only when history is
        # non-empty — otherwise the suffix's "references to past tool calls"
        # disclaimer just clutters fresh sessions).
        history = list(opts.history or [])
        has_history = bool(history)

        setup = self._build_setup(
            opts.system_prompt, opts.tools or [], with_history=has_history
        )
        self._last_setup = setup
        await self._ws.send(json.dumps({"setup": setup}))

        # Wait for setupComplete — server MUST send it before accepting audio.
        # Some mock servers may not bother; tolerate that by not blocking forever.
        try:
            first = await asyncio.wait_for(self._ws.recv(), timeout=5.0)
            msg = json.loads(first)
            if "setupComplete" not in msg:
                # Buffer this message so recv_events can re-yield it.
                self._pending_first_msg = msg
            else:
                self._pending_first_msg = None
        except asyncio.TimeoutError:
            self._pending_first_msg = None
        except Exception:  # noqa: BLE001 - fixtures may differ
            self._pending_first_msg = None

        # 0.4.2 S2: inject history if present AND not reconnecting.
        # On session resumption (handle present), Gemini retains
        # server-side state — re-injecting turns the model already
        # produced confuses the conversation.
        if has_history and not self._session_handle:
            try:
                await self._send_history(history)
                self._history_injected = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "%s: history injection failed: %s; voice will start "
                    "without text context",
                    self.NAME,
                    exc,
                )

        # Always release the gate so live audio can flow.
        self._history_injection_complete.set()

    async def _send_history(self, history: List[Dict[str, Any]]) -> None:
        """Inject prior text-conversation turns as silent context.

        Sends a single ``BidiGenerateContentClientContent`` frame with
        role-mapped turns and ``turnComplete:true``. MUST be called
        AFTER setupComplete and BEFORE the first realtimeInput.audio
        frame (the input pump waits on ``_history_injection_complete``).

        0.4.5 P1-3: honest mode-transition framing.

        - Prepend a synthetic ``user`` turn naming the mode switch:
          "(Switching from typed conversation to voice. The conversation
          above was typed; what follows is spoken. Wait for me to speak
          before responding.)"
        - If the final history turn is ``user``, append a synthetic
          ``model`` turn that acknowledges the mode switch and stays
          silent: "Voice mode active. Listening." — replaces the older
          "(voice session starting)" closer (which Gemini sometimes read
          aloud as if it were a thinking-noise).

        Why both: Gemini's native-audio model reads the final turn as
        priming; without an assistant-role closer it sometimes greets
        the user verbally on session-open ("hey what's up?"). With one,
        it sits silent and waits for audio. Tying the framing to a
        spoken intro/outro pair (rather than the cryptic parenthetical)
        improves correctness when the model occasionally DOES decide to
        verbalize the closer.
        """
        if self._ws is None:
            return
        gemini_turns: List[Dict[str, Any]] = [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            "(Switching from typed conversation to voice. "
                            "The conversation above was typed; what follows "
                            "is spoken. Wait for me to speak before "
                            "responding.)"
                        )
                    }
                ],
            }
        ]
        for t in history:
            role = "user" if t.get("role") == "user" else "model"
            text = t.get("content")
            if not isinstance(text, str) or not text.strip():
                continue
            gemini_turns.append({"role": role, "parts": [{"text": text}]})
        # Bare history (no real turns survived filtering): drop the lone
        # framing turn rather than send a single isolated user turn.
        if len(gemini_turns) == 1:
            return
        # Ensure final turn is role="model" so Gemini doesn't speak. The
        # closer is conversational so it works whether the model stays
        # silent (preferred) or briefly verbalizes (acceptable degradation).
        if gemini_turns[-1]["role"] == "user":
            gemini_turns.append(
                {
                    "role": "model",
                    "parts": [{"text": "Voice mode active. Listening."}],
                }
            )
        msg = {
            "clientContent": {
                "turns": gemini_turns,
                "turnComplete": True,
            }
        }
        await self._ws.send(json.dumps(msg))
        logger.info(
            "%s: injected %d history turn(s) (~%d chars) before first audio",
            self.NAME,
            len(gemini_turns),
            sum(len(t["parts"][0]["text"]) for t in gemini_turns),
        )

    def _build_setup(
        self,
        system_prompt: str,
        tools: List[Dict[str, Any]],
        *,
        with_history: bool = False,
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
        # 0.4.2 S2: when injecting prior text history, append a tool-disclaimer
        # so the model treats prior tool-call references as completed work
        # rather than promising to do them now (UX critique §4). Voice
        # sessions today have no tools (tier system is v0.5.0), so any
        # history reference to tools is by definition stale.
        if with_history:
            anchored += (
                "\n\nIn this voice session you cannot call tools. "
                "References in the prior conversation to tool calls or "
                "actions you took describe completed work — treat them as "
                "known facts, not ongoing tasks. "
                "Keep replies short and conversational."
            )
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
                "automaticActivityDetection": {"disabled": True},
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
        """Send a PCM audio chunk. Resamples to 16kHz s16le mono if needed.

        0.4.2 S2 (red-team P0-7): if history injection is in flight,
        block here until ``_history_injection_complete`` so live audio
        frames don't interleave into the clientContent.turns sequence
        and trigger an unprompted Gemini response before the synthetic
        model closer is sent.
        """
        # Gate live audio behind history-injection completion. Event is
        # set immediately on connect() if no history; only gates on the
        # first chunk of a fresh session with non-empty history.
        if self._history_injection_complete is not None:
            await self._history_injection_complete.wait()

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

        # 0.4.5 P0-1: surface toolCallCancellation so audio_bridge can cancel
        # the in-flight _run_and_inject_tool task and advance _tool_seq_next_inject.
        # Before this fix, a model that abandoned a tool call mid-turn (Gemini
        # emits this when it decides not to use the result, e.g. user barge-in
        # or NON_BLOCKING tool race) would deadlock the injection sequence —
        # later tools could never inject because the cancelled call never
        # advanced the pointer.
        cancellation = msg.get("toolCallCancellation")
        if cancellation:
            for cancelled_id in cancellation.get("ids", []) or []:
                events.append(
                    RealtimeEvent(
                        type="tool_cancelled",
                        payload={"call_id": cancelled_id},
                    )
                )
            return events

        # goAway, usageMetadata, setupComplete — no events emitted.
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

        Useful as a barge-in primitive (server treats it as user-spoke-and-stopped
        in zero time). With AAD disabled in 0.4.2+ this is a degenerate case of
        the regular per-utterance bracket emitted by the audio bridge.
        """
        if self._ws is None:
            return
        await self.send_activity_start()
        await self.send_activity_end()

    async def send_activity_start(self) -> None:
        """Emit BidiGenerateContentRealtimeInput.activityStart.

        Required when ``automaticActivityDetection.disabled=true`` (our default
        from 0.4.2 onward). The audio bridge calls this when the first input
        frame of an utterance arrives. See
        https://ai.google.dev/gemini-api/docs/live-guide#disable-vad.
        """
        if self._ws is None:
            return
        await self._ws.send(json.dumps({"realtimeInput": {"activityStart": {}}}))

    async def send_activity_end(self) -> None:
        """Emit BidiGenerateContentRealtimeInput.activityEnd.

        Required when AAD is disabled — Gemini will not commit a turn until it
        receives this. The audio bridge's silence watchdog calls this after a
        configurable gap of no input frames.
        """
        if self._ws is None:
            return
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
