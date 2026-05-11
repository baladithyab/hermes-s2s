"""Tests for realtime history injection (S2: voice context memory).

Covers:
- _internal/history.py: build_history_payload + session_id resolution
- providers/realtime/gemini_live._send_history: clientContent shape + closer
- providers/realtime/openai_realtime._send_history: per-turn events
- _BaseRealtimeBackend connect() shim: positional vs ConnectOptions

Plan: docs/plans/wave-0.4.2-clicks-history-quickwins.md (S2).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from hermes_s2s._internal.history import (
    build_history_payload,
    resolve_session_id_for_thread,
)
from hermes_s2s.voice.connect_options import ConnectOptions


# ---------- build_history_payload ------------------------------------ #


class _FakeSessionDB:
    """In-memory stub of hermes_state.SessionDB for testing."""

    def __init__(self, messages: list = None, raise_on_get: bool = False) -> None:
        self._messages = messages or []
        self._raise = raise_on_get

    def get_messages_as_conversation(self, session_id: str) -> list:
        if self._raise:
            raise RuntimeError("simulated DB error")
        return list(self._messages)


class TestBuildHistoryPayload:
    def test_empty_session_id_returns_empty(self) -> None:
        db = _FakeSessionDB([{"role": "user", "content": "hi"}])
        assert build_history_payload(db, "") == []
        assert build_history_payload(db, None) == []  # type: ignore[arg-type]

    def test_db_error_returns_empty_no_raise(self) -> None:
        db = _FakeSessionDB(raise_on_get=True)
        assert build_history_payload(db, "session-1") == []

    def test_filters_system_role(self) -> None:
        db = _FakeSessionDB(
            [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hey"},
            ]
        )
        out = build_history_payload(db, "s1")
        roles = [m["role"] for m in out]
        assert "system" not in roles
        assert roles == ["user", "assistant"]

    def test_filters_tool_role(self) -> None:
        db = _FakeSessionDB(
            [
                {"role": "user", "content": "hi"},
                {"role": "tool", "content": "tool result"},
                {"role": "function", "content": "fn result"},
                {"role": "assistant", "content": "ok"},
            ]
        )
        out = build_history_payload(db, "s1")
        assert all(m["role"] in ("user", "assistant") for m in out)
        assert len(out) == 2

    def test_filters_empty_content(self) -> None:
        db = _FakeSessionDB(
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": ""},
                {"role": "user", "content": "   "},
                {"role": "assistant", "content": "ok"},
            ]
        )
        out = build_history_payload(db, "s1")
        assert len(out) == 2
        assert out[0]["content"] == "hi"
        assert out[1]["content"] == "ok"

    def test_voice_mirror_dedup(self) -> None:
        """Turns from voice transcript mirror are filtered (rejoin dedup)."""
        db = _FakeSessionDB(
            [
                {"role": "user", "content": "regular text turn"},
                {"role": "user", "content": "**[Voice]** @user: hi from voice"},
                {"role": "assistant", "content": "**[Voice]** ARIA: hi back"},
                {"role": "assistant", "content": "regular text reply"},
            ]
        )
        out = build_history_payload(db, "s1", skip_voice_metadata=True)
        contents = [m["content"] for m in out]
        assert "regular text turn" in contents
        assert "regular text reply" in contents
        assert not any("[Voice]" in c for c in contents)

    def test_voice_mirror_dedup_disabled_keeps_voice_turns(self) -> None:
        db = _FakeSessionDB(
            [
                {"role": "user", "content": "**[Voice]** @user: hi"},
            ]
        )
        out = build_history_payload(db, "s1", skip_voice_metadata=False)
        assert len(out) == 1

    def test_max_turns_caps_count(self) -> None:
        msgs = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"}
            for i in range(50)
        ]
        db = _FakeSessionDB(msgs)
        out = build_history_payload(db, "s1", max_turns=10)
        assert len(out) == 10
        # Should be the LAST 10
        assert out[-1]["content"] == "msg 49"

    def test_max_tokens_truncates_oldest(self) -> None:
        # Each msg is 100 chars; token budget 50 → ~200 chars budget
        msgs = [
            {"role": "user", "content": "x" * 100},
            {"role": "assistant", "content": "y" * 100},
            {"role": "user", "content": "z" * 100},
        ]
        db = _FakeSessionDB(msgs)
        out = build_history_payload(db, "s1", max_tokens=50)
        # 50 tokens * 4 chars/token = 200 char budget
        # Drops oldest until rendered <= 200
        rendered = sum(len(m["content"]) for m in out)
        assert rendered <= 200

    def test_multimodal_content_coerced_to_text(self) -> None:
        db = _FakeSessionDB(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look at this"},
                        {"type": "image_url", "image_url": "x"},
                        {"type": "text", "text": "what is it"},
                    ],
                }
            ]
        )
        out = build_history_payload(db, "s1")
        assert len(out) == 1
        assert "look at this" in out[0]["content"]
        assert "what is it" in out[0]["content"]


# ---------- resolve_session_id_for_thread ---------------------------- #


class TestResolveSessionId:
    def test_no_session_store_returns_none(self) -> None:
        adapter = MagicMock(spec=[])  # no session_store attr
        result = resolve_session_id_for_thread(
            adapter, thread_id=123, user_id=456
        )
        assert result is None

    def test_returns_none_on_synthesis_failure(self) -> None:
        # No SessionSource importable in test env → falls through gracefully
        adapter = MagicMock()
        adapter.session_store._generate_session_key = MagicMock(
            side_effect=RuntimeError("boom")
        )
        result = resolve_session_id_for_thread(
            adapter, thread_id=123, user_id=456
        )
        assert result is None

    def test_resolves_via_entries(self) -> None:
        """Tier-2 path: _entries dict lookup."""
        adapter = MagicMock()
        # Simulate _generate_session_key returning a known key
        adapter.session_store._generate_session_key = MagicMock(
            return_value="key-abc"
        )
        # Tier-1 (public getter) absent
        adapter.session_store.get = None
        # Tier-2: _entries has the key
        entry = MagicMock()
        entry.session_id = "session-xyz"
        adapter.session_store._entries = {"key-abc": entry}

        # Mock the SessionSource import path so synthesis succeeds.
        # We skip this if gateway.session not importable.
        try:
            from gateway.session import SessionSource  # noqa: F401
        except ImportError:
            pytest.skip("gateway.session not importable (test env)")

        result = resolve_session_id_for_thread(
            adapter, thread_id=123, user_id=456
        )
        assert result == "session-xyz"


class TestFindMostRecentThreadSession:
    """0.4.4 fallback for joining VC from a non-thread channel."""

    def test_returns_none_with_no_session_store(self) -> None:
        from hermes_s2s._internal.history import (
            find_most_recent_thread_session_id,
        )

        adapter = MagicMock(spec=[])
        result = find_most_recent_thread_session_id(adapter)
        assert result is None

    def test_returns_none_when_no_thread_sessions_exist(self) -> None:
        """Only DM/group entries — no thread match."""
        from hermes_s2s._internal.history import (
            find_most_recent_thread_session_id,
        )

        adapter = MagicMock()
        # No thread keys
        adapter.session_store._entries = {
            "agent:main:discord:dm:111": MagicMock(
                session_id="dm-1", chat_type="dm"
            ),
        }
        result = find_most_recent_thread_session_id(adapter)
        assert result is None

    def test_picks_most_recent_thread(self) -> None:
        """Multiple thread sessions — most recent wins."""
        from hermes_s2s._internal.history import (
            find_most_recent_thread_session_id,
        )
        import datetime as _dt

        now = _dt.datetime.utcnow()
        adapter = MagicMock()
        # Three threads at different ages
        old = MagicMock(
            session_id="old-thread",
            chat_type="thread",
            updated_at=now - _dt.timedelta(hours=10),
        )
        recent = MagicMock(
            session_id="recent-thread",
            chat_type="thread",
            updated_at=now - _dt.timedelta(minutes=5),
        )
        oldest = MagicMock(
            session_id="oldest-thread",
            chat_type="thread",
            updated_at=now - _dt.timedelta(hours=20),
        )
        adapter.session_store._entries = {
            "agent:main:discord:thread:111:111": old,
            "agent:main:discord:thread:222:222": recent,
            "agent:main:discord:thread:333:333": oldest,
        }
        result = find_most_recent_thread_session_id(adapter)
        assert result is not None
        thread_id, sid = result
        assert sid == "recent-thread"
        assert thread_id == 222

    def test_skips_stale_sessions_beyond_max_age(self) -> None:
        """Sessions older than max_age_hours don't qualify."""
        from hermes_s2s._internal.history import (
            find_most_recent_thread_session_id,
        )
        import datetime as _dt

        now = _dt.datetime.utcnow()
        adapter = MagicMock()
        adapter.session_store._entries = {
            "agent:main:discord:thread:111:111": MagicMock(
                session_id="stale",
                chat_type="thread",
                updated_at=now - _dt.timedelta(hours=48),
            ),
        }
        result = find_most_recent_thread_session_id(
            adapter, max_age_hours=24.0
        )
        assert result is None

    def test_handles_iso_string_updated_at(self) -> None:
        """Real SessionEntry serialises updated_at as ISO string."""
        from hermes_s2s._internal.history import (
            find_most_recent_thread_session_id,
        )
        import datetime as _dt

        now_iso = _dt.datetime.utcnow().isoformat()
        adapter = MagicMock()
        adapter.session_store._entries = {
            "agent:main:discord:thread:111:111": MagicMock(
                session_id="iso-test",
                chat_type="thread",
                updated_at=now_iso,
            ),
        }
        result = find_most_recent_thread_session_id(adapter)
        assert result is not None
        assert result[1] == "iso-test"

    def test_filters_non_discord_platforms(self) -> None:
        """Only discord-platform thread keys are considered."""
        from hermes_s2s._internal.history import (
            find_most_recent_thread_session_id,
        )
        import datetime as _dt

        now = _dt.datetime.utcnow()
        adapter = MagicMock()
        adapter.session_store._entries = {
            "agent:main:telegram:thread:111:111": MagicMock(
                session_id="tg-thread",
                chat_type="thread",
                updated_at=now,
            ),
        }
        result = find_most_recent_thread_session_id(
            adapter, platform="discord"
        )
        assert result is None


# ---------- _BaseRealtimeBackend connect() shim ---------------------- #


class TestConnectShim:
    def test_dataclass_call_passes_through(self) -> None:
        """connect(opts) must reach _connect_with_opts unchanged."""
        from hermes_s2s.providers.realtime import _BaseRealtimeBackend

        captured = {}

        class _TestBackend(_BaseRealtimeBackend):
            NAME = "test"

            async def _connect_with_opts(self, opts):
                captured["opts"] = opts

        backend = _TestBackend()
        opts_in = ConnectOptions(
            system_prompt="hi",
            voice="Aoede",
            tools=[],
            history=[{"role": "user", "content": "ctx"}],
        )
        asyncio.run(backend.connect(opts_in))
        assert captured["opts"] is opts_in
        assert captured["opts"].history == [{"role": "user", "content": "ctx"}]

    def test_positional_call_back_compat(self) -> None:
        """Pre-v0.4.2 callers passed (system_prompt, voice, tools) positional."""
        from hermes_s2s.providers.realtime import _BaseRealtimeBackend

        captured = {}

        class _TestBackend(_BaseRealtimeBackend):
            NAME = "test"

            async def _connect_with_opts(self, opts):
                captured["opts"] = opts

        backend = _TestBackend()
        # Legacy call shape — must not raise
        asyncio.run(backend.connect("hi", "Aoede", [{"name": "tool1"}]))
        assert captured["opts"].system_prompt == "hi"
        assert captured["opts"].voice == "Aoede"
        assert captured["opts"].tools == [{"name": "tool1"}]
        assert captured["opts"].history is None

    def test_positional_with_history_kwarg(self) -> None:
        """0.4.2: ``connect(prompt, voice, tools, history=[...])`` works."""
        from hermes_s2s.providers.realtime import _BaseRealtimeBackend

        captured = {}

        class _TestBackend(_BaseRealtimeBackend):
            NAME = "test"

            async def _connect_with_opts(self, opts):
                captured["opts"] = opts

        backend = _TestBackend()
        asyncio.run(
            backend.connect(
                "hi",
                "Aoede",
                [],
                history=[{"role": "user", "content": "x"}],
            )
        )
        assert captured["opts"].history == [{"role": "user", "content": "x"}]


# ---------- Gemini _send_history ------------------------------------- #


class _RecordingWS:
    """In-memory stub WS that records sent JSON frames."""

    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def recv(self) -> str:
        return '{"setupComplete": {}}'

    async def close(self) -> None:
        pass


class TestGeminiSendHistory:
    def test_history_clientContent_frame_shape(self) -> None:
        from hermes_s2s.providers.realtime.gemini_live import GeminiLiveBackend

        async def scenario():
            backend = GeminiLiveBackend(
                api_key_env="UNUSED", url="ws://stub", model="gemini-test"
            )
            ws = _RecordingWS()
            backend._ws = ws
            history = [
                {"role": "user", "content": "what's the weather"},
                {"role": "assistant", "content": "sunny and 70"},
                {"role": "user", "content": "thanks"},
            ]
            await backend._send_history(history)
            return ws.sent

        sent = asyncio.run(scenario())
        assert len(sent) == 1
        cc = sent[0]
        assert "clientContent" in cc
        assert cc["clientContent"]["turnComplete"] is True
        turns = cc["clientContent"]["turns"]
        # 0.4.5 P1-3: framing turn at index 0, then history, then closer.
        assert turns[0]["role"] == "user"
        assert "Switching from typed conversation to voice" in turns[0]["parts"][0]["text"]
        # History turns: indices 1, 2, 3
        assert turns[1]["role"] == "user"
        assert turns[1]["parts"][0]["text"] == "what's the weather"
        assert turns[2]["role"] == "model"
        assert turns[2]["parts"][0]["text"] == "sunny and 70"
        assert turns[3]["role"] == "user"
        assert turns[3]["parts"][0]["text"] == "thanks"
        # Last turn was user → synthetic model closer appended
        assert turns[-1]["role"] == "model"
        assert "Voice mode active" in turns[-1]["parts"][0]["text"]

    def test_history_ending_in_assistant_no_synthetic_closer(self) -> None:
        """If history ends in assistant, no closer needed."""
        from hermes_s2s.providers.realtime.gemini_live import GeminiLiveBackend

        async def scenario():
            backend = GeminiLiveBackend(api_key_env="X", url="ws://x", model="m")
            ws = _RecordingWS()
            backend._ws = ws
            history = [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ]
            await backend._send_history(history)
            return ws.sent[0]["clientContent"]["turns"]

        turns = asyncio.run(scenario())
        # 0.4.5 P1-3: framing turn (user) + 2 history turns = 3, no closer.
        assert len(turns) == 3
        assert turns[0]["role"] == "user"  # framing
        assert "typed conversation to voice" in turns[0]["parts"][0]["text"]
        assert turns[-1]["role"] == "model"
        assert turns[-1]["parts"][0]["text"] == "hello"

    def test_empty_history_no_send(self) -> None:
        from hermes_s2s.providers.realtime.gemini_live import GeminiLiveBackend

        async def scenario():
            backend = GeminiLiveBackend(api_key_env="X", url="ws://x", model="m")
            ws = _RecordingWS()
            backend._ws = ws
            await backend._send_history([])
            return ws.sent

        sent = asyncio.run(scenario())
        assert sent == []

    def test_history_with_only_filtered_turns_no_send(self) -> None:
        """If all turns get filtered out, no clientContent emitted."""
        from hermes_s2s.providers.realtime.gemini_live import GeminiLiveBackend

        async def scenario():
            backend = GeminiLiveBackend(api_key_env="X", url="ws://x", model="m")
            ws = _RecordingWS()
            backend._ws = ws
            # All-empty content — gets filtered
            await backend._send_history(
                [
                    {"role": "user", "content": ""},
                    {"role": "assistant", "content": "   "},
                ]
            )
            return ws.sent

        sent = asyncio.run(scenario())
        assert sent == []


# ---------- OpenAI _send_history ------------------------------------- #


class TestOpenAISendHistory:
    def test_per_turn_conversation_item_create(self) -> None:
        from hermes_s2s.providers.realtime.openai_realtime import (
            OpenAIRealtimeBackend,
        )

        async def scenario():
            backend = OpenAIRealtimeBackend(
                api_key_env="UNUSED",
                connect_url="ws://stub",
                model="gpt-realtime",
            )
            ws = _RecordingWS()
            backend._ws = ws
            backend._send_lock = asyncio.Lock()
            history = [
                {"role": "user", "content": "ping"},
                {"role": "assistant", "content": "pong"},
                {"role": "user", "content": "again"},
            ]
            await backend._send_history(history)
            return ws.sent

        sent = asyncio.run(scenario())
        # 0.4.5 P1-3: framing turn + 3 history turns = 4 conversation.item.create events
        assert len(sent) == 4
        for ev in sent:
            assert ev["type"] == "conversation.item.create"
            assert ev["item"]["type"] == "message"
        # Framing turn first
        assert sent[0]["item"]["role"] == "user"
        assert "typed conversation to voice" in sent[0]["item"]["content"][0]["text"]
        # Then real history with role + content_type mapping
        assert sent[1]["item"]["role"] == "user"
        assert sent[1]["item"]["content"][0]["type"] == "input_text"
        assert sent[2]["item"]["role"] == "assistant"
        assert sent[2]["item"]["content"][0]["type"] == "text"

    def test_no_response_create_emitted(self) -> None:
        """CRITICAL: must NOT emit response.create or model speaks unprompted."""
        from hermes_s2s.providers.realtime.openai_realtime import (
            OpenAIRealtimeBackend,
        )

        async def scenario():
            backend = OpenAIRealtimeBackend(
                api_key_env="X", connect_url="ws://x", model="m"
            )
            ws = _RecordingWS()
            backend._ws = ws
            backend._send_lock = asyncio.Lock()
            await backend._send_history(
                [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                ]
            )
            return ws.sent

        sent = asyncio.run(scenario())
        types = [ev["type"] for ev in sent]
        assert "response.create" not in types

    def test_unknown_role_skipped(self) -> None:
        from hermes_s2s.providers.realtime.openai_realtime import (
            OpenAIRealtimeBackend,
        )

        async def scenario():
            backend = OpenAIRealtimeBackend(
                api_key_env="X", connect_url="ws://x", model="m"
            )
            ws = _RecordingWS()
            backend._ws = ws
            backend._send_lock = asyncio.Lock()
            await backend._send_history(
                [
                    {"role": "user", "content": "hi"},
                    {"role": "weird", "content": "skipme"},
                    {"role": "assistant", "content": "hello"},
                ]
            )
            return ws.sent

        sent = asyncio.run(scenario())
        # 0.4.5 P1-3: framing turn + 2 valid history turns = 3 (weird role dropped)
        assert len(sent) == 3


# ---------- ConnectOptions reaches Gemini's tool-disclaimer suffix --- #


class TestPersonaSuffix:
    def test_history_present_appends_tool_disclaimer(self) -> None:
        """When history is non-empty, _build_setup adds the tool-disclaimer."""
        from hermes_s2s.providers.realtime.gemini_live import GeminiLiveBackend

        backend = GeminiLiveBackend(
            api_key_env="X", url="ws://x", model="m"
        )
        # Without history
        setup = backend._build_setup("base prompt", [], with_history=False)
        prompt = setup["systemInstruction"]["parts"][0]["text"]
        assert "cannot call tools" not in prompt

        # With history
        setup = backend._build_setup("base prompt", [], with_history=True)
        prompt = setup["systemInstruction"]["parts"][0]["text"]
        assert "cannot call tools" in prompt
        assert "completed work" in prompt


# ---------- send_audio_chunk gates on history-injection event ------- #


class TestSendAudioGating:
    """Pre-merge review caught: _history_injection_complete was set in
    connect() but NEVER awaited in send_audio_chunk. Live audio would
    interleave into history sequence corrupting server state.

    These tests fence that send_audio_chunk waits on the event.
    """

    def test_gemini_send_audio_blocks_until_history_complete(self) -> None:
        from hermes_s2s.providers.realtime.gemini_live import GeminiLiveBackend

        async def scenario():
            backend = GeminiLiveBackend(
                api_key_env="X", url="ws://x", model="m"
            )
            # Set up: gating event present but NOT set (history mid-injection)
            backend._history_injection_complete = asyncio.Event()
            ws = _RecordingWS()
            backend._ws = ws

            # Fire send_audio_chunk; should NOT complete (blocked on event)
            send_task = asyncio.create_task(
                backend.send_audio_chunk(b"\x00\x00" * 1000, 16000)
            )

            # Give it a tick — must still be pending
            await asyncio.sleep(0.05)
            assert not send_task.done(), (
                "send_audio_chunk should block on _history_injection_complete"
            )

            # Now set the event — task should complete
            backend._history_injection_complete.set()
            await asyncio.wait_for(send_task, timeout=1.0)
            assert send_task.done()
            # Verify the audio frame was actually sent post-gate.
            sent_types = [s.get("realtimeInput", {}) for s in ws.sent]
            assert any("mediaChunks" in t or "audio" in t for t in sent_types)

        asyncio.run(scenario())

    def test_openai_send_audio_blocks_until_history_complete(self) -> None:
        from hermes_s2s.providers.realtime.openai_realtime import (
            OpenAIRealtimeBackend,
        )

        async def scenario():
            backend = OpenAIRealtimeBackend(
                api_key_env="X", connect_url="ws://x", model="m"
            )
            backend._history_injection_complete = asyncio.Event()
            ws = _RecordingWS()
            backend._ws = ws
            backend._send_lock = asyncio.Lock()

            send_task = asyncio.create_task(
                backend.send_audio_chunk(b"\x00\x00" * 1000, 24000)
            )

            await asyncio.sleep(0.05)
            assert not send_task.done(), (
                "send_audio_chunk should block on _history_injection_complete"
            )

            backend._history_injection_complete.set()
            await asyncio.wait_for(send_task, timeout=1.0)
            assert send_task.done()
            # Verify input_audio_buffer.append was sent post-gate.
            sent_types = [s.get("type") for s in ws.sent]
            assert "input_audio_buffer.append" in sent_types

        asyncio.run(scenario())

    def test_send_audio_no_gate_when_event_is_none(self) -> None:
        """If event is None (legacy / not initialized), don't block."""
        from hermes_s2s.providers.realtime.gemini_live import GeminiLiveBackend

        async def scenario():
            backend = GeminiLiveBackend(api_key_env="X", url="ws://x", model="m")
            backend._history_injection_complete = None
            ws = _RecordingWS()
            backend._ws = ws
            # Should complete immediately (no gate)
            await asyncio.wait_for(
                backend.send_audio_chunk(b"\x00\x00" * 1000, 16000),
                timeout=1.0,
            )

        asyncio.run(scenario())
