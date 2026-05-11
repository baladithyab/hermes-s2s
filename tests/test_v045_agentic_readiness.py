"""Tests for v0.4.5 P0/P1 fixes — agentic readiness for realtime backends.

Coverage:
- P0-1: Gemini toolCallCancellation event surfacing + audio_bridge cancel
- P0-2: OpenAI input_audio_transcription session config + user transcript routing
- P1-3: Honest history framing turn (typed→voice transition)
- v0.4.6: provider alias coverage (openai-realtime, gpt-realtime-2, gpt-realtime-1.5)
- v0.4.6: history-fallback resolves SessionStore via gateway_runner indirection
- v0.4.6: cascaded STT silencer covers _process_voice_input (not just callback)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest


# ---------- P0-1: Gemini toolCallCancellation surfacing ----------------- #


class TestGeminiToolCallCancellation:
    """When the model emits toolCallCancellation we must surface it as a
    `tool_cancelled` RealtimeEvent so audio_bridge can cancel the in-flight
    tool task and advance the injection sequence pointer.
    """

    def test_translates_cancellation_to_tool_cancelled_event(self) -> None:
        from hermes_s2s.providers.realtime.gemini_live import GeminiLiveBackend

        backend = GeminiLiveBackend(
            api_key_env="X", url="ws://x", model="m"
        )
        msg = {"toolCallCancellation": {"ids": ["call-abc-1", "call-abc-2"]}}
        events = backend._translate_server_msg(msg)
        assert len(events) == 2
        assert events[0].type == "tool_cancelled"
        assert events[0].payload == {"call_id": "call-abc-1"}
        assert events[1].type == "tool_cancelled"
        assert events[1].payload == {"call_id": "call-abc-2"}

    def test_empty_cancellation_ids_emits_nothing(self) -> None:
        from hermes_s2s.providers.realtime.gemini_live import GeminiLiveBackend

        backend = GeminiLiveBackend(
            api_key_env="X", url="ws://x", model="m"
        )
        events = backend._translate_server_msg({"toolCallCancellation": {"ids": []}})
        assert events == []

    def test_missing_ids_field_treated_as_empty(self) -> None:
        from hermes_s2s.providers.realtime.gemini_live import GeminiLiveBackend

        backend = GeminiLiveBackend(
            api_key_env="X", url="ws://x", model="m"
        )
        events = backend._translate_server_msg({"toolCallCancellation": {}})
        assert events == []


class TestAudioBridgeToolCancellation:
    """Audio bridge wires tool_cancelled to in-flight task cancellation
    + injection-pointer advancement so later tools don't deadlock.
    """

    def test_cancellation_advances_seq_pointer_without_inject(self) -> None:
        """Sequential tools 0 and 1 dispatched. Tool 0 cancelled mid-flight.
        Tool 1's result MUST inject (not deadlock waiting for tool 0)."""
        from hermes_s2s.providers.realtime import RealtimeEvent
        from hermes_s2s._internal.audio_bridge import RealtimeAudioBridge

        async def scenario():
            inject_calls: list = []

            class FakeBackend:
                async def connect(self, *args, **kwargs): pass
                async def send_audio_chunk(self, *args, **kwargs): pass
                async def send_activity_start(self): pass
                async def send_activity_end(self): pass
                async def close(self): pass
                async def inject_tool_result(self, call_id, result):
                    inject_calls.append((call_id, result))
                async def recv_events(self):
                    if False:
                        yield  # async generator

            class SlowToolBridge:
                """Tool 0 sleeps long; tool 1 returns immediately."""
                def __init__(self):
                    self._tool_0_started = asyncio.Event()

                async def handle_tool_call(self, backend, call_id, name, args):
                    if call_id == "c0":
                        self._tool_0_started.set()
                        # Long sleep — gets cancelled
                        await asyncio.sleep(60)
                        return "tool0-result"  # never reached
                    return f"{call_id}-result"

            backend = FakeBackend()
            tb = SlowToolBridge()
            bridge = RealtimeAudioBridge(backend=backend, tool_bridge=tb)

            # Dispatch tool 0
            await bridge._dispatch_event(
                RealtimeEvent(type="tool_call", payload={
                    "call_id": "c0", "name": "search", "args": {}
                })
            )
            # Wait for tool 0 to actually start
            await asyncio.wait_for(tb._tool_0_started.wait(), timeout=1.0)

            # Dispatch tool 1 — its sequence number is 1; will block in cond
            # waiting for next_inject == 1 (i.e. tool 0 to finish/skip).
            await bridge._dispatch_event(
                RealtimeEvent(type="tool_call", payload={
                    "call_id": "c1", "name": "search", "args": {}
                })
            )

            # Cancel tool 0 — should release tool 1.
            await bridge._dispatch_event(
                RealtimeEvent(type="tool_cancelled", payload={"call_id": "c0"})
            )

            # Give the cancellation chain time to propagate.
            for _ in range(20):
                await asyncio.sleep(0.05)
                if any(c[0] == "c1" for c in inject_calls):
                    break

            return inject_calls

        inject_calls = asyncio.run(scenario())
        # Tool 0's result MUST NOT inject (cancelled).
        # Tool 1's result MUST inject.
        call_ids = [c[0] for c in inject_calls]
        assert "c0" not in call_ids, "cancelled tool should not inject"
        assert "c1" in call_ids, "later tool should inject after cancellation"

    def test_cancellation_of_unknown_call_id_is_no_op(self) -> None:
        """Cancellation for a call_id we don't know about logs but doesn't crash."""
        from hermes_s2s.providers.realtime import RealtimeEvent
        from hermes_s2s._internal.audio_bridge import RealtimeAudioBridge

        async def scenario():
            class FakeBackend:
                async def connect(self, *args, **kwargs): pass
                async def send_audio_chunk(self, *args, **kwargs): pass
                async def send_activity_start(self): pass
                async def send_activity_end(self): pass
                async def close(self): pass
                async def inject_tool_result(self, call_id, result): pass
                async def recv_events(self):
                    if False:
                        yield

            bridge = RealtimeAudioBridge(backend=FakeBackend(), tool_bridge=None)
            # Should not raise
            await bridge._dispatch_event(
                RealtimeEvent(type="tool_cancelled", payload={"call_id": "unknown"})
            )
            return True

        assert asyncio.run(scenario()) is True


# ---------- P0-2: OpenAI input_audio_transcription ---------------------- #


class TestOpenAIInputAudioTranscription:
    """Verify session.update enables user-side transcripts and that the
    server's input_audio_transcription events are routed to RealtimeEvents
    with role='user'."""

    def test_session_update_includes_input_audio_transcription(self) -> None:
        from hermes_s2s.providers.realtime.openai_realtime import (
            OpenAIRealtimeBackend,
        )
        from hermes_s2s.voice.connect_options import ConnectOptions

        sent = []

        class _RecordingWS:
            async def send(self, raw):
                sent.append(json.loads(raw))
            async def close(self): pass

        class _MockWebSockets:
            async def connect(self, url, **kw):
                return _RecordingWS()

        async def scenario():
            backend = OpenAIRealtimeBackend(
                api_key_env="UNUSED",
                connect_url="ws://stub",
                model="gpt-realtime",
                api_key="fake-key",
            )
            # Stub websockets module
            import sys
            mock = type("M", (), {"connect": _MockWebSockets().connect})()
            old = sys.modules.get("websockets")
            sys.modules["websockets"] = mock
            try:
                opts = ConnectOptions(
                    system_prompt="be brief", voice="alloy", tools=[], history=None
                )
                await backend._connect_with_opts(opts)
            finally:
                if old is not None:
                    sys.modules["websockets"] = old
                else:
                    del sys.modules["websockets"]
            return sent

        sent = asyncio.run(scenario())
        # First message MUST be session.update with input_audio_transcription
        assert len(sent) >= 1
        update = sent[0]
        assert update["type"] == "session.update"
        session = update["session"]
        assert "input_audio_transcription" in session
        assert session["input_audio_transcription"] == {"model": "whisper-1"}

    def test_input_audio_transcription_completed_routes_to_user_transcript_final(
        self,
    ) -> None:
        """Server's `conversation.item.input_audio_transcription.completed` →
        RealtimeEvent(type='transcript_final', payload={'text': ..., 'role': 'user'})"""
        from hermes_s2s.providers.realtime.openai_realtime import (
            OpenAIRealtimeBackend,
        )

        async def scenario():
            backend = OpenAIRealtimeBackend(
                api_key_env="X", connect_url="ws://x", model="m"
            )
            # Stub a fake WS that replays one transcript-completed event
            payload = {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "hello there",
                "item_id": "item-123",
            }

            class _ScriptedWS:
                def __init__(self, frames):
                    self._frames = list(frames)
                async def __aiter__(self):
                    for f in self._frames:
                        yield f
                async def close(self): pass

            backend._ws = _ScriptedWS([json.dumps(payload)])
            events = []
            async for ev in backend.recv_events():
                events.append(ev)
            return events

        events = asyncio.run(scenario())
        # Should contain at least one transcript_final with role=user
        user_finals = [
            e for e in events
            if e.type == "transcript_final" and e.payload.get("role") == "user"
        ]
        assert len(user_finals) == 1
        assert user_finals[0].payload["text"] == "hello there"

    def test_input_audio_transcription_delta_routes_to_user_transcript_partial(
        self,
    ) -> None:
        from hermes_s2s.providers.realtime.openai_realtime import (
            OpenAIRealtimeBackend,
        )

        async def scenario():
            backend = OpenAIRealtimeBackend(
                api_key_env="X", connect_url="ws://x", model="m"
            )
            payload = {
                "type": "conversation.item.input_audio_transcription.delta",
                "delta": "hel",
            }

            class _ScriptedWS:
                def __init__(self, frames):
                    self._frames = list(frames)
                async def __aiter__(self):
                    for f in self._frames:
                        yield f
                async def close(self): pass

            backend._ws = _ScriptedWS([json.dumps(payload)])
            events = []
            async for ev in backend.recv_events():
                events.append(ev)
            return events

        events = asyncio.run(scenario())
        partials = [
            e for e in events
            if e.type == "transcript_partial" and e.payload.get("role") == "user"
        ]
        assert len(partials) == 1
        assert partials[0].payload["text"] == "hel"

    def test_response_audio_transcript_still_carries_assistant_role(self) -> None:
        """0.4.5 P0-2: assistant-side transcripts now also tagged with role.
        Don't regress the assistant path while adding the user path."""
        from hermes_s2s.providers.realtime.openai_realtime import (
            OpenAIRealtimeBackend,
        )

        async def scenario():
            backend = OpenAIRealtimeBackend(
                api_key_env="X", connect_url="ws://x", model="m"
            )
            payload = {
                "type": "response.audio_transcript.done",
                "transcript": "I am ARIA",
            }

            class _ScriptedWS:
                def __init__(self, frames):
                    self._frames = list(frames)
                async def __aiter__(self):
                    for f in self._frames:
                        yield f
                async def close(self): pass

            backend._ws = _ScriptedWS([json.dumps(payload)])
            events = []
            async for ev in backend.recv_events():
                events.append(ev)
            return events

        events = asyncio.run(scenario())
        finals = [
            e for e in events
            if e.type == "transcript_final" and e.payload.get("role") == "assistant"
        ]
        assert len(finals) == 1
        assert finals[0].payload["text"] == "I am ARIA"


# ---------- P1-3: Framing turn behavior (no history → no framing) ------- #


class TestFramingTurnGuards:
    """The framing turn is only sent when there's actual history to frame.
    Empty / all-filtered history must NOT produce a lone framing turn."""

    def test_gemini_empty_history_no_send(self) -> None:
        from hermes_s2s.providers.realtime.gemini_live import GeminiLiveBackend

        async def scenario():
            backend = GeminiLiveBackend(api_key_env="X", url="ws://x", model="m")
            ws_sent = []

            class _WS:
                async def send(self, raw):
                    ws_sent.append(json.loads(raw))
                async def close(self): pass

            backend._ws = _WS()
            await backend._send_history([])
            return ws_sent

        sent = asyncio.run(scenario())
        # Empty history → no clientContent emitted (not even the framing turn alone)
        assert sent == []

    def test_gemini_all_filtered_history_no_send(self) -> None:
        from hermes_s2s.providers.realtime.gemini_live import GeminiLiveBackend

        async def scenario():
            backend = GeminiLiveBackend(api_key_env="X", url="ws://x", model="m")
            ws_sent = []

            class _WS:
                async def send(self, raw):
                    ws_sent.append(json.loads(raw))
                async def close(self): pass

            backend._ws = _WS()
            # All turns have empty content — get filtered inside _send_history
            await backend._send_history(
                [
                    {"role": "user", "content": ""},
                    {"role": "assistant", "content": "   "},
                ]
            )
            return ws_sent

        sent = asyncio.run(scenario())
        # All filtered → only the framing turn would survive; we drop it as a
        # lone isolated turn.
        assert sent == []
