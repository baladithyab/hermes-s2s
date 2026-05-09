"""Tests for OpenAIRealtimeBackend.

Uses the ``mock_ws_server`` fixture from ``tests/conftest.py`` (owned by R2-A).

# requires mock_ws_server fixture from conftest.py written by R2-A; if missing,
# fixture will not resolve and tests will error — that is the expected pre-merge
# state per the wave-0.3.0 plan.

Fixture is a callable factory: ``server = await mock_ws_server(handler)``.
The returned ``_MockWSServer`` exposes:
    - ``server.url``           → ws://127.0.0.1:<port>
    - ``server.received``      → list[dict] of parsed JSON from the client
    - ``server.headers``       → dict of first-handshake request headers
    - ``server.connections``   → number of client connects seen
    - ``server.wait_for_messages(n, timeout)``

For test (d) we use the scripted mode (no handler) because we only want to
observe what the client sends.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

# Skip the whole module if websockets is missing — this backend is opt-in.
pytest.importorskip("websockets")

from hermes_s2s.providers.realtime.openai_realtime import OpenAIRealtimeBackend  # noqa: E402


pytestmark = pytest.mark.asyncio


# ----------------------------------------------------------------------- helpers


async def _drain_until(agen, predicate, timeout: float = 2.5):
    """Iterate ``agen`` until ``predicate(event)`` returns True, or timeout."""

    async def _inner():
        async for ev in agen:
            if predicate(ev):
                return ev
        return None

    return await asyncio.wait_for(_inner(), timeout=timeout)


# ----------------------------------------------------------------------- tests


async def test_happy_connect_and_audio_flow(mock_ws_server):
    """a. connect sends Authorization header + session.update; push audio;
    server emits response.audio.delta; backend surfaces audio_chunk; close."""

    pcm = b"\x01\x02" * 10
    done_audio = asyncio.Event()

    async def handler(ws):
        # 1. Read session.update
        await ws.recv()
        # 2. Read audio chunk
        await ws.recv()
        # 3. Emit one response.audio.delta
        await ws.send(
            json.dumps(
                {
                    "type": "response.audio.delta",
                    "delta": base64.b64encode(pcm).decode("ascii"),
                }
            )
        )
        done_audio.set()
        # Hold connection open until test closes it.
        try:
            await asyncio.wait_for(ws.recv(), timeout=3.0)
        except Exception:
            return

    server = await mock_ws_server(handler)

    backend = OpenAIRealtimeBackend(
        connect_url=server.url, api_key="sk-test-123", model="gpt-realtime"
    )
    await backend.connect(system_prompt="hello", voice="alloy", tools=[])

    await backend.send_audio_chunk(b"\xaa\xbb" * 20, sample_rate=24_000)

    got = await _drain_until(
        backend.recv_events(), lambda ev: ev.type == "audio_chunk"
    )
    assert got is not None
    assert got.payload["sample_rate"] == 24_000
    assert got.payload["pcm"] == pcm

    # Verify Authorization + OpenAI-Beta headers reached the server.
    hdrs_lower = {k.lower(): v for k, v in (server.headers or {}).items()}
    assert hdrs_lower.get("authorization") == "Bearer sk-test-123"
    assert hdrs_lower.get("openai-beta") == "realtime=v1"

    # Verify session.update shape.
    assert len(server.received) >= 2
    session_update = server.received[0]
    assert session_update["type"] == "session.update"
    assert session_update["session"]["voice"] == "alloy"
    assert session_update["session"]["input_audio_format"] == "pcm16"
    assert session_update["session"]["output_audio_format"] == "pcm16"
    assert session_update["session"]["instructions"] == "hello"

    audio_msg = server.received[1]
    assert audio_msg["type"] == "input_audio_buffer.append"
    assert audio_msg["audio"] == base64.b64encode(b"\xaa\xbb" * 20).decode("ascii")

    await backend.close()


async def test_tool_call_round_trip_sends_output_and_response_create(mock_ws_server):
    """b. Server emits response.function_call_arguments.done; backend yields
    tool_call; caller injects result; BOTH conversation.item.create AND
    response.create must be received by the server, in that order."""

    async def handler(ws):
        # 1. Read session.update
        await ws.recv()
        # 2. Push tool call
        await ws.send(
            json.dumps(
                {
                    "type": "response.function_call_arguments.done",
                    "response_id": "resp_1",
                    "item_id": "item_2",
                    "call_id": "call_xyz789",
                    "name": "get_weather",
                    "arguments": '{"city":"Paris"}',
                }
            )
        )
        # 3. Read the two follow-up messages from the client.
        try:
            await asyncio.wait_for(ws.recv(), timeout=3.0)
            await asyncio.wait_for(ws.recv(), timeout=3.0)
        except Exception:
            return
        # Keep socket alive briefly so tests can inspect state.
        try:
            await asyncio.wait_for(ws.recv(), timeout=1.0)
        except Exception:
            return

    server = await mock_ws_server(handler)

    backend = OpenAIRealtimeBackend(
        connect_url=server.url, api_key="sk-test", model="gpt-realtime"
    )
    await backend.connect(system_prompt="sys", voice="alloy", tools=[])

    tool_ev = await _drain_until(
        backend.recv_events(), lambda ev: ev.type == "tool_call"
    )
    assert tool_ev is not None
    assert tool_ev.payload["call_id"] == "call_xyz789"
    assert tool_ev.payload["name"] == "get_weather"
    assert tool_ev.payload["arguments"] == '{"city":"Paris"}'

    await backend.inject_tool_result(
        call_id="call_xyz789",
        result='{"tempC":14,"condition":"cloudy"}',
    )

    # Wait for session.update + conversation.item.create + response.create.
    await server.wait_for_messages(3, timeout=3.0)
    types = [m.get("type") for m in server.received]

    assert "conversation.item.create" in types, types
    assert "response.create" in types, types

    # Order: response.create MUST come AFTER conversation.item.create.
    create_idx = types.index("conversation.item.create")
    resp_idx = types.index("response.create")
    assert create_idx < resp_idx, (
        "response.create must come AFTER function_call_output — the OpenAI "
        "gotcha: function_call_output alone does NOT trigger a new response"
    )

    fco = server.received[create_idx]
    assert fco["item"]["type"] == "function_call_output"
    assert fco["item"]["call_id"] == "call_xyz789"
    assert fco["item"]["output"] == '{"tempC":14,"condition":"cloudy"}'

    await backend.close()


async def test_session_cap_close_surfaces_error_and_reconnect_works(mock_ws_server):
    """c. Server closes WS (simulate 30-min cap). Backend surfaces an error
    event with reason='session_cap'. Caller reconnects; second connect works."""

    connection_counter = {"n": 0}

    async def handler(ws):
        connection_counter["n"] += 1
        # Read session.update.
        await ws.recv()
        if connection_counter["n"] == 1:
            # Simulate the 30-min hard cap: server closes cleanly.
            await ws.close(code=1000)
        else:
            # Second connect: push one audio.delta then keep socket open.
            await ws.send(
                json.dumps(
                    {
                        "type": "response.audio.delta",
                        "delta": base64.b64encode(b"\x00\x01").decode("ascii"),
                    }
                )
            )
            try:
                await asyncio.wait_for(ws.recv(), timeout=2.0)
            except Exception:
                return

    server = await mock_ws_server(handler)

    backend = OpenAIRealtimeBackend(
        connect_url=server.url, api_key="sk-test", model="gpt-realtime"
    )
    await backend.connect(system_prompt="sys", voice="alloy", tools=[])

    err_ev = await _drain_until(
        backend.recv_events(),
        lambda ev: ev.type == "error" and ev.payload.get("reason") == "session_cap",
        timeout=3.0,
    )
    assert err_ev is not None
    assert err_ev.payload.get("reason") == "session_cap"
    await backend.close()

    # Caller decides reconnect is worth it (lossy: no resumption handle).
    backend2 = OpenAIRealtimeBackend(
        connect_url=server.url, api_key="sk-test", model="gpt-realtime"
    )
    await backend2.connect(system_prompt="sys", voice="alloy", tools=[])
    got = await _drain_until(
        backend2.recv_events(), lambda ev: ev.type == "audio_chunk", timeout=3.0
    )
    assert got is not None
    await backend2.close()

    assert connection_counter["n"] == 2
    assert server.connections == 2


async def test_interrupt_sends_cancel_clear_truncate(mock_ws_server):
    """d. interrupt() must send response.cancel + output_audio_buffer.clear +
    conversation.item.truncate, in that order."""

    # Use scripted mode (no handler) — we only care about what the client sends.
    server = await mock_ws_server()

    backend = OpenAIRealtimeBackend(
        connect_url=server.url, api_key="sk-test", model="gpt-realtime"
    )
    await backend.connect(system_prompt="sys", voice="alloy", tools=[])

    await backend.interrupt(item_id="item_1", audio_end_ms=1840)

    # session.update + response.cancel + output_audio_buffer.clear +
    # conversation.item.truncate == 4 messages.
    await server.wait_for_messages(4, timeout=3.0)
    types = [m.get("type") for m in server.received]

    assert "response.cancel" in types
    assert "output_audio_buffer.clear" in types
    assert "conversation.item.truncate" in types

    cancel_idx = types.index("response.cancel")
    clear_idx = types.index("output_audio_buffer.clear")
    trunc_idx = types.index("conversation.item.truncate")
    assert cancel_idx < clear_idx < trunc_idx

    trunc_msg = server.received[trunc_idx]
    assert trunc_msg["item_id"] == "item_1"
    assert trunc_msg["audio_end_ms"] == 1840
    assert trunc_msg["content_index"] == 0

    await backend.close()


async def test_send_filler_audio_emits_response_create(mock_ws_server):
    """e. send_filler_audio must emit a `response.create` with instructions
    override and audio modality. Shape per OpenAI Realtime docs:

        {"type": "response.create",
         "response": {"instructions": "Briefly say: thinking",
                      "modalities": ["audio"]}}

    Used by HermesToolBridge on soft-timeout (ADR-0008 §2).
    """
    # Scripted mode (no handler) — we only observe client frames.
    server = await mock_ws_server()

    backend = OpenAIRealtimeBackend(
        connect_url=server.url, api_key="sk-test", model="gpt-realtime"
    )
    await backend.connect(system_prompt="sys", voice="alloy", tools=[])

    # 1 frame so far (session.update from connect).
    await server.wait_for_messages(1, timeout=2.0)

    await backend.send_filler_audio("thinking")

    # session.update + response.create == 2 messages.
    await server.wait_for_messages(2, timeout=2.0)

    types = [m.get("type") for m in server.received]
    assert "response.create" in types, types

    rc_idx = types.index("response.create")
    rc = server.received[rc_idx]

    # Session.update from connect() must still come first and be untouched.
    assert server.received[0]["type"] == "session.update"
    assert rc_idx > 0

    response = rc.get("response") or {}
    assert response.get("conversation") == "none", (
        "OOB responses (out-of-band, not added to history) REQUIRE "
        "conversation='none' per GA gpt-realtime docs"
    )
    # GA field is 'output_modalities'; legacy field 'modalities' is kept for
    # back-compat with older server revisions. Either must be present + audio.
    assert (
        "output_modalities" in response or "modalities" in response
    ), response
    assert response.get("output_modalities") == ["audio"] or response.get(
        "modalities"
    ) == ["audio"]
    assert "thinking" in response.get("instructions", "")

    await backend.close()
