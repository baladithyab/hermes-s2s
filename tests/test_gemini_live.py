"""Tests for GeminiLiveBackend using the in-process mock WS server fixture.

Never calls the real Gemini API. The whole file skips cleanly if the
`websockets` package isn't installed (it's in the [realtime] extra).
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

pytest.importorskip("websockets")

from hermes_s2s.providers.realtime.gemini_live import (  # noqa: E402
    GeminiLiveBackend,
    _translate_tools,
    make_gemini_live,
)

pytestmark = pytest.mark.asyncio


def _audio_chunk_reply(sample_rate: int = 24000, text: str = "Hello there") -> dict:
    b64 = base64.b64encode(b"\x00\x01" * 100).decode("ascii")
    return {
        "serverContent": {
            "modelTurn": {
                "parts": [
                    {
                        "inlineData": {
                            "mimeType": f"audio/pcm;rate={sample_rate}",
                            "data": b64,
                        }
                    }
                ]
            },
            "outputTranscription": {"text": text},
        }
    }


async def _drain_events(backend, expected_count, timeout=2.0):
    """Collect up to `expected_count` events from the backend within `timeout`s."""
    collected = []

    async def _collector():
        async for ev in backend.recv_events():
            collected.append(ev)
            if len(collected) >= expected_count:
                return

    try:
        await asyncio.wait_for(_collector(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    return collected


# -------------------------------------------------------------------- #
# 1. Happy path: connect → setup → audio in → recv events → close      #
# -------------------------------------------------------------------- #
async def test_happy_connect_audio_roundtrip(mock_ws_server):
    server = await mock_ws_server()  # scripted mode
    server.add_reply({"setupComplete": {}})
    server.add_reply(_audio_chunk_reply(text="Hi!"))

    backend = make_gemini_live({"url": server.url})
    await backend.connect("You are a helpful assistant.", "Aoede", [])

    await server.wait_for_messages(1, timeout=2.0)
    sent_setup = json.loads(server.sent_messages[0])
    assert "setup" in sent_setup
    assert sent_setup["setup"]["model"].startswith("models/")
    assert (
        sent_setup["setup"]["generationConfig"]["speechConfig"]["voiceConfig"][
            "prebuiltVoiceConfig"
        ]["voiceName"]
        == "Aoede"
    )

    # Send one audio chunk at 16 kHz (no resample needed).
    await backend.send_audio_chunk(b"\x00\x00" * 1600, sample_rate=16000)
    await server.wait_for_messages(2, timeout=2.0)
    sent_audio = json.loads(server.sent_messages[1])
    assert "realtimeInput" in sent_audio
    assert sent_audio["realtimeInput"]["audio"]["mimeType"] == "audio/pcm;rate=16000"

    events = await _drain_events(backend, expected_count=2, timeout=2.0)
    types = [e.type for e in events]
    assert "audio_chunk" in types
    assert "transcript_partial" in types
    audio_ev = next(e for e in events if e.type == "audio_chunk")
    assert isinstance(audio_ev.payload["audio"], bytes)
    assert audio_ev.payload["sample_rate"] == 24000

    await backend.close()


# -------------------------------------------------------------------- #
# 2. Tool-call round trip                                              #
# -------------------------------------------------------------------- #
async def test_tool_call_roundtrip(mock_ws_server):
    server = await mock_ws_server()
    server.add_reply({"setupComplete": {}})
    server.add_proactive(
        {
            "toolCall": {
                "functionCalls": [
                    {"id": "abc", "name": "search", "args": {"q": "foo"}}
                ]
            }
        }
    )

    tools = [
        {
            "name": "search",
            "description": "Web search",
            "parameters": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        }
    ]

    backend = GeminiLiveBackend(url=server.url)
    await backend.connect("sys", "Aoede", tools)

    await server.wait_for_messages(1)
    setup_msg = json.loads(server.sent_messages[0])
    decls = setup_msg["setup"]["tools"][0]["functionDeclarations"]
    assert decls[0]["name"] == "search"
    assert decls[0]["parameters"]["properties"]["q"]["type"] == "string"

    events = await _drain_events(backend, expected_count=1, timeout=2.0)
    tool_ev = next(e for e in events if e.type == "tool_call")
    assert tool_ev.payload["call_id"] == "abc"
    assert tool_ev.payload["name"] == "search"
    assert tool_ev.payload["args"] == {"q": "foo"}

    await backend.inject_tool_result("abc", "result")

    await server.wait_for_messages(2, timeout=2.0)
    resp = json.loads(server.sent_messages[1])
    fr = resp["toolResponse"]["functionResponses"][0]
    assert fr["id"] == "abc"
    assert fr["response"] == {"result": "result"}

    await backend.close()


# -------------------------------------------------------------------- #
# 3. Session resumption: handle is stored and re-sent on reconnect     #
# -------------------------------------------------------------------- #
async def test_session_resumption(mock_ws_server):
    server = await mock_ws_server()
    server.add_reply({"setupComplete": {}})
    server.add_proactive(
        {"sessionResumptionUpdate": {"newHandle": "h1", "resumable": True}}
    )

    backend = GeminiLiveBackend(url=server.url)
    await backend.connect("sys", "Aoede", [])

    # Drain one event so the resumption update is processed.
    events = await _drain_events(backend, expected_count=1, timeout=2.0)
    assert any(e.type == "session_resumed" for e in events)
    assert backend._session_handle == "h1"

    await backend.close()

    # Simulate reconnect: new setup should include sessionResumption.handle='h1'.
    server.add_reply({"setupComplete": {}})
    await backend.connect("sys", "Aoede", [])

    # The second setup frame is the most recent "setup"-keyed message on server.
    setup_frames = [
        json.loads(m) for m in server.sent_messages if '"setup"' in m[:20]
    ]
    assert len(setup_frames) >= 2
    latest_setup = setup_frames[-1]["setup"]
    assert latest_setup["sessionResumption"]["handle"] == "h1"

    await backend.close()


# -------------------------------------------------------------------- #
# 4. Error event surfaces cleanly                                      #
# -------------------------------------------------------------------- #
async def test_error_event_surfaces(mock_ws_server):
    server = await mock_ws_server()
    server.add_reply({"setupComplete": {}})
    server.add_proactive({"error": {"message": "rate limited", "code": 429}})

    backend = GeminiLiveBackend(url=server.url)
    await backend.connect("sys", "Aoede", [])

    events = await _drain_events(backend, expected_count=1, timeout=2.0)
    errs = [e for e in events if e.type == "error"]
    assert errs, f"expected an error event, got types={[e.type for e in events]}"
    assert errs[0].payload.get("message") == "rate limited"

    await backend.close()


# -------------------------------------------------------------------- #
# bonus: unit-level sanity on the translator (no WS needed)            #
# -------------------------------------------------------------------- #
async def test_translate_tools_shape():
    out = _translate_tools(
        [
            {
                "name": "weather",
                "description": "get weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            }
        ]
    )
    assert out == [
        {
            "functionDeclarations": [
                {
                    "name": "weather",
                    "description": "get weather",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                }
            ]
        }
    ]
    assert _translate_tools([]) == []


# -------------------------------------------------------------------- #
# 5. send_filler_audio emits a clientContent text turn                 #
# -------------------------------------------------------------------- #
async def test_send_filler_audio_emits_text_content(mock_ws_server):
    """Verify the G1 filler-audio frame shape per Gemini Live live-api docs.

    Shape:
        {"clientContent": {"turns": [{"role": "user",
                                      "parts": [{"text": "hello world"}]}],
                           "turnComplete": true}}
    """
    server = await mock_ws_server()
    server.add_reply({"setupComplete": {}})

    backend = GeminiLiveBackend(url=server.url)
    await backend.connect("sys", "Aoede", [])

    # Drain the setup frame.
    await server.wait_for_messages(1, timeout=2.0)

    await backend.send_filler_audio("hello world")

    await server.wait_for_messages(2, timeout=2.0)
    filler = json.loads(server.sent_messages[1])

    assert "clientContent" in filler, filler
    cc = filler["clientContent"]
    assert cc["turnComplete"] is True
    turns = cc["turns"]
    assert len(turns) == 1
    assert turns[0]["role"] == "user"
    assert turns[0]["parts"][0]["text"] == "hello world"

    # Not-connected guard: close → raises RuntimeError.
    await backend.close()
    with pytest.raises(RuntimeError):
        await backend.send_filler_audio("ignored")
