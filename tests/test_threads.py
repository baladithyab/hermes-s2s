"""Tests for ThreadResolver + TranscriptMirror (W3a) and the audio_bridge
transcript plumbing + discord_bridge wire-up (W3b).

W3a covers:
  * ThreadResolver.resolve() — reuse / auto-create / forum / template render
  * TranscriptMirror — formatting, rate limit, overflow, failure swallow

W3b covers:
  * RealtimeAudioBridge._dispatch_event routes transcript events to the
    registered _transcript_sink (role-aware)
  * join_voice_channel_wrapped resolves the thread BEFORE snapshotting
    event.source (M3.4 smoke)
"""

from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes_s2s.voice.threads import ThreadResolver
from hermes_s2s.voice.transcript import TranscriptMirror


# ---------------------------------------------------------------------------
# Discord stub — just enough of the module surface that ThreadResolver
# imports without pulling in real discord.py.
# ---------------------------------------------------------------------------


class _DummyForumChannel:  # pragma: no cover — marker class
    pass


class _DummyChannelType:  # pragma: no cover — marker class
    public_thread = "public_thread"


def _install_discord_stub() -> None:
    """Ensure ``import discord`` inside ThreadResolver yields predictable types.

    We don't want to accidentally pick up a real discord.py install from
    the test environment; a tiny stub module gives every test a stable
    ForumChannel / ChannelType to compare against.
    """
    stub = types.ModuleType("discord")
    stub.ForumChannel = _DummyForumChannel  # type: ignore[attr-defined]
    stub.ChannelType = _DummyChannelType  # type: ignore[attr-defined]
    sys.modules["discord"] = stub


_install_discord_stub()


# ---------------------------------------------------------------------------
# ThreadResolver tests
# ---------------------------------------------------------------------------


def _make_event(
    *,
    thread_id: str | None = None,
    chat_type: str = "channel",
    chat_id: str = "12345",
    user_display_name: str = "alice",
) -> SimpleNamespace:
    source = SimpleNamespace(
        thread_id=thread_id,
        chat_type=chat_type,
        chat_id=chat_id,
        user_display_name=user_display_name,
    )
    return SimpleNamespace(source=source)


@pytest.mark.asyncio
async def test_resolver_reuses_existing_thread() -> None:
    resolver = ThreadResolver({})
    event = _make_event(thread_id="777")
    adapter = SimpleNamespace(_client=MagicMock())
    result = await resolver.resolve(adapter, event, voice_channel=None)
    assert result == 777
    # Must NOT have consulted the client — we short-circuited.
    adapter._client.get_channel.assert_not_called()


@pytest.mark.asyncio
async def test_resolver_reuses_chat_type_thread() -> None:
    resolver = ThreadResolver({})
    event = _make_event(chat_type="thread", chat_id="555")
    adapter = SimpleNamespace(_client=MagicMock())
    result = await resolver.resolve(adapter, event, voice_channel=None)
    assert result == 555
    adapter._client.get_channel.assert_not_called()


@pytest.mark.asyncio
async def test_resolver_creates_thread_in_channel() -> None:
    """Plain parent channel → resolver creates a public thread and marks it."""
    resolver = ThreadResolver(
        {"s2s": {"voice": {"thread_starter_message": "hi {parent_channel_name}"}}}
    )

    # Mock thread returned by create_thread.
    thread = MagicMock()
    thread.id = 999
    thread.send = AsyncMock()

    parent = MagicMock()
    parent.id = 42
    parent.name = "general"
    parent.create_thread = AsyncMock(return_value=thread)
    # Force "not a forum" — we use a Mock that is NOT a _DummyForumChannel.

    client = MagicMock()
    client.get_channel.return_value = parent

    tracker = MagicMock()  # ThreadParticipationTracker stand-in.
    adapter = SimpleNamespace(_client=client, _threads=tracker)

    event = _make_event(chat_id="42", user_display_name="Alice")
    result = await resolver.resolve(adapter, event, voice_channel=None)

    assert result == 999
    parent.create_thread.assert_awaited_once()
    # Starter message posted with parent name interpolated.
    thread.send.assert_awaited_once_with("hi general")
    # Marked on tracker with str(thread.id).
    tracker.mark.assert_called_once_with("999")


@pytest.mark.asyncio
async def test_resolver_handles_forum_parent() -> None:
    resolver = ThreadResolver({})

    forum = _DummyForumChannel()  # isinstance check will match.
    client = MagicMock()
    client.get_channel.return_value = forum
    adapter = SimpleNamespace(_client=client)

    event = _make_event(chat_id="1")
    result = await resolver.resolve(adapter, event, voice_channel=None)
    assert result is None


@pytest.mark.asyncio
async def test_resolver_renders_template_with_user_and_date() -> None:
    """The template's {user} + {date:%Y} placeholders are both interpolated."""
    resolver = ThreadResolver(
        {"s2s": {"voice": {"thread_name_template": "Test {user} {date:%Y}"}}}
    )
    thread = MagicMock(id=1)
    thread.send = AsyncMock()
    parent = MagicMock()
    parent.name = "p"
    parent.create_thread = AsyncMock(return_value=thread)

    client = MagicMock()
    client.get_channel.return_value = parent
    adapter = SimpleNamespace(_client=client)
    event = _make_event(chat_id="1", user_display_name="Alice")

    await resolver.resolve(adapter, event, voice_channel=None)

    # The create_thread call name= kwarg is our rendered template.
    call = parent.create_thread.await_args
    name = call.kwargs["name"]
    # current year will be the 4-digit year of "now".
    from datetime import datetime

    assert name.startswith("Test Alice ")
    assert name.endswith(str(datetime.now().year))


@pytest.mark.asyncio
async def test_resolver_skips_starter_when_empty() -> None:
    """Empty starter_message → thread.send is NOT called."""
    resolver = ThreadResolver(
        {"s2s": {"voice": {"thread_starter_message": ""}}}
    )
    thread = MagicMock(id=1)
    thread.send = AsyncMock()
    parent = MagicMock()
    parent.name = "p"
    parent.create_thread = AsyncMock(return_value=thread)

    client = MagicMock()
    client.get_channel.return_value = parent
    adapter = SimpleNamespace(_client=client)
    event = _make_event(chat_id="1")

    await resolver.resolve(adapter, event, voice_channel=None)
    thread.send.assert_not_awaited()


# ---------------------------------------------------------------------------
# TranscriptMirror tests
# ---------------------------------------------------------------------------


def _mirror_with_channel() -> tuple[TranscriptMirror, MagicMock]:
    """Build a mirror whose adapter._client.get_channel returns a Mock channel."""
    channel = MagicMock()
    channel.send = AsyncMock()
    client = MagicMock()
    client.get_channel.return_value = channel
    adapter = SimpleNamespace(_client=client)
    return TranscriptMirror(adapter), channel


@pytest.mark.asyncio
async def test_mirror_send_user_format() -> None:
    mirror, channel = _mirror_with_channel()
    await mirror.send(channel_id=1, role="user", text="hello")
    channel.send.assert_awaited_once_with("**[Voice]** @user: hello")


@pytest.mark.asyncio
async def test_mirror_send_assistant_format() -> None:
    mirror, channel = _mirror_with_channel()
    await mirror.send(channel_id=1, role="assistant", text="hi there")
    channel.send.assert_awaited_once_with("**[Voice]** ARIA: hi there")


@pytest.mark.asyncio
async def test_mirror_rate_limit() -> None:
    """16 sends inside one 5s window → only the first 5 reach channel.send."""
    mirror, channel = _mirror_with_channel()
    for i in range(16):
        await mirror.send(channel_id=1, role="assistant", text=f"msg{i}")
    # Only 5 tokens are available in a fresh 5s window.
    assert channel.send.await_count == 5


@pytest.mark.asyncio
async def test_mirror_handles_send_failure() -> None:
    """channel.send raising must NOT propagate."""
    mirror, channel = _mirror_with_channel()
    channel.send.side_effect = RuntimeError("boom")
    # Should not raise.
    await mirror.send(channel_id=1, role="assistant", text="x")
    channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_mirror_overflow_warning_throttled(caplog) -> None:
    """Pushing >50 overflows produces exactly one WARNING per 60s."""
    import logging as _logging

    mirror, channel = _mirror_with_channel()
    caplog.set_level(_logging.WARNING, logger="hermes_s2s.voice.transcript")

    # Burn the 5-token rate-limit window.
    for _ in range(5):
        await mirror.send(channel_id=7, role="assistant", text="primed")
    assert channel.send.await_count == 5

    # Now 60 more sends within the same window — all queue, first 50 fill
    # the bounded queue, the remaining 10 overflow. Queue-full starts at
    # item #51 and we expect exactly ONE warning log for the first
    # 60-second window.
    for i in range(60):
        await mirror.send(channel_id=7, role="assistant", text=f"x{i}")

    overflow_warns = [
        r for r in caplog.records
        if "overflow on channel" in r.getMessage()
    ]
    assert len(overflow_warns) == 1


# ---------------------------------------------------------------------------
# W3b — audio_bridge transcript routing
# ---------------------------------------------------------------------------


def _make_realtime_event(etype: str, payload: dict) -> SimpleNamespace:
    """Mimic hermes_s2s.providers.base.RealtimeEvent duck-typed surface."""
    return SimpleNamespace(type=etype, payload=payload)


@pytest.mark.asyncio
async def test_audio_bridge_routes_transcript_partial_user_to_sink() -> None:
    from hermes_s2s._internal.audio_bridge import RealtimeAudioBridge

    # Minimal stub backend with the rates we need.
    backend = SimpleNamespace(input_rate=16000, output_rate=24000)
    bridge = RealtimeAudioBridge(backend=backend)

    sink = MagicMock()
    bridge._transcript_sink = sink

    event = _make_realtime_event(
        "transcript_partial", {"text": "hi", "role": "user"}
    )
    await bridge._dispatch_event(event)
    sink.assert_called_once_with(role="user", text="hi", final=False)


@pytest.mark.asyncio
async def test_audio_bridge_routes_transcript_partial_assistant_to_sink() -> None:
    from hermes_s2s._internal.audio_bridge import RealtimeAudioBridge

    backend = SimpleNamespace(input_rate=16000, output_rate=24000)
    bridge = RealtimeAudioBridge(backend=backend)
    sink = MagicMock()
    bridge._transcript_sink = sink

    event = _make_realtime_event(
        "transcript_partial", {"text": "hello", "role": "assistant"}
    )
    await bridge._dispatch_event(event)
    sink.assert_called_once_with(role="assistant", text="hello", final=False)


@pytest.mark.asyncio
async def test_audio_bridge_routes_transcript_final_to_sink() -> None:
    from hermes_s2s._internal.audio_bridge import RealtimeAudioBridge

    backend = SimpleNamespace(input_rate=16000, output_rate=24000)
    bridge = RealtimeAudioBridge(backend=backend)
    sink = MagicMock()
    bridge._transcript_sink = sink

    event = _make_realtime_event(
        "transcript_final", {"role": "assistant"}
    )
    await bridge._dispatch_event(event)
    sink.assert_called_once_with(role="assistant", text="", final=True)


@pytest.mark.asyncio
async def test_audio_bridge_skips_transcript_when_no_sink() -> None:
    """With _transcript_sink=None the dispatch is a silent no-op."""
    from hermes_s2s._internal.audio_bridge import RealtimeAudioBridge

    backend = SimpleNamespace(input_rate=16000, output_rate=24000)
    bridge = RealtimeAudioBridge(backend=backend)
    assert bridge._transcript_sink is None

    event = _make_realtime_event(
        "transcript_partial", {"text": "x", "role": "user"}
    )
    # Must not raise.
    await bridge._dispatch_event(event)


# ---------------------------------------------------------------------------
# W3b — discord_bridge resolver wire-up smoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_join_voice_resolves_thread_before_source_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The monkey-patched join_voice_channel calls ThreadResolver first.

    We don't run the full bridge wiring here — just assert that the
    resolver is invoked with the adapter/event/channel triple and that
    the resulting thread_id is copied onto event.source.thread_id +
    chat_type before ``_install_bridge_on_adapter`` would be reached.
    """
    from hermes_s2s._internal import discord_bridge as db_mod

    resolved_calls: list = []

    class _FakeResolver:
        def __init__(self, cfg: dict) -> None:
            self._cfg = cfg

        async def resolve(self, adapter, event, channel):  # type: ignore[no-untyped-def]
            resolved_calls.append((adapter, event, channel))
            return 42

    monkeypatch.setattr(db_mod, "ThreadResolver", _FakeResolver, raising=False)

    # _install_bridge_on_adapter is a no-op for this test — we only care
    # about the pre-snapshot mutation.
    monkeypatch.setattr(
        db_mod,
        "_install_bridge_on_adapter",
        lambda adapter, channel, ctx=None: None,
    )

    # The wrapped join_voice_channel is an inner function created by
    # _install_via_monkey_patch. We exercise the helper directly instead.
    source = SimpleNamespace(thread_id=None, chat_type="channel", chat_id="1")
    event = SimpleNamespace(source=source)
    adapter = SimpleNamespace(_client=MagicMock(), _current_event=event)
    channel = MagicMock()

    await db_mod._resolve_and_mirror_thread(adapter, channel)

    assert resolved_calls, "ThreadResolver.resolve was not called"
    assert source.thread_id == "42"
    assert source.chat_type == "thread"
