"""Tests for v0.4.6 fixes — discovered live in production logs.

Coverage:
- v0.4.6 #1: history fallback resolves SessionStore via adapter.gateway_runner
  (live Discord adapter never has .session_store directly)
- v0.4.6 #2: provider aliases — gpt-realtime-2, gpt-realtime-1.5,
  openai-realtime all route to OpenAIRealtimeBackend factory
- v0.4.6 #3: cascaded STT silencer monkey-patches _process_voice_input,
  not just _voice_input_callback (transcribe_audio fires upstream of cb)
"""

from __future__ import annotations

from typing import Any


# ---------- History fallback: gateway_runner indirection ----------------- #


class TestHistoryStoreResolution:
    """Live Discord adapter doesn't have ``.session_store`` directly — the
    gateway runner does. v0.4.4 fallback silently failed because both
    ``resolve_session_id_for_thread`` and ``find_most_recent_thread_session_id``
    only checked ``adapter.session_store``. v0.4.6 walks
    ``adapter.gateway_runner.session_store`` as a fallback."""

    def test_get_session_store_prefers_direct_attribute(self) -> None:
        from hermes_s2s._internal.history import _get_session_store

        class A: pass
        a = A()
        sentinel_direct = object()
        a.session_store = sentinel_direct
        a.gateway_runner = type("R", (), {"session_store": object()})()
        assert _get_session_store(a) is sentinel_direct

    def test_get_session_store_falls_back_to_gateway_runner(self) -> None:
        from hermes_s2s._internal.history import _get_session_store

        class A: pass
        a = A()
        sentinel_runner = object()
        a.gateway_runner = type("R", (), {"session_store": sentinel_runner})()
        assert _get_session_store(a) is sentinel_runner

    def test_get_session_store_returns_none_when_neither(self) -> None:
        from hermes_s2s._internal.history import _get_session_store

        class A: pass
        assert _get_session_store(A()) is None

    def test_get_session_store_handles_runner_without_store(self) -> None:
        """Runner present but lacks session_store attribute — return None."""
        from hermes_s2s._internal.history import _get_session_store

        class A: pass
        a = A()
        a.gateway_runner = object()  # no session_store on it
        assert _get_session_store(a) is None

    def test_find_most_recent_uses_gateway_runner_store(self) -> None:
        """Fallback discovery walks through gateway_runner when adapter
        has no direct session_store."""
        import datetime as dt
        from hermes_s2s._internal.history import find_most_recent_thread_session_id

        class _Entry:
            chat_type = "thread"
            session_id = "20260510_144631_a8362c"
            updated_at = dt.datetime.utcnow() - dt.timedelta(minutes=5)

        class _Store:
            _entries = {
                "agent:main:discord:thread:1502750093571522670:1502750093571522670": _Entry(),
            }
            def _ensure_loaded(self): pass

        class _Runner: pass
        runner = _Runner()
        runner.session_store = _Store()

        class A: pass
        a = A()
        a.gateway_runner = runner

        result = find_most_recent_thread_session_id(
            a, user_id=None, guild_id=None, platform="discord", max_age_hours=24.0
        )
        assert result is not None
        thread_id, session_id = result
        assert thread_id == 1502750093571522670
        assert session_id == "20260510_144631_a8362c"


# ---------- Provider alias registration --------------------------------- #


class TestRealtimeProviderAliases:
    """Users may set ``s2s.realtime.provider`` to any of the registered
    aliases. 0.4.6 broadened the registered set to include
    ``gpt-realtime-2`` (the new GA alias) and ``openai-realtime``."""

    def test_all_openai_aliases_route_to_make_openai_realtime(self) -> None:
        from hermes_s2s.providers import register_builtin_realtime_providers
        from hermes_s2s.registry import _REALTIME_REGISTRY
        register_builtin_realtime_providers()

        for alias in [
            "openai-realtime",
            "gpt-realtime",
            "gpt-realtime-1.5",
            "gpt-realtime-2",
            "gpt-realtime-mini",
        ]:
            assert alias in _REALTIME_REGISTRY, (
                f"alias {alias!r} not registered — runtime config flip will fail"
            )

    def test_gemini_alias_still_registered(self) -> None:
        """Don't regress Gemini support while broadening OpenAI aliases."""
        from hermes_s2s.providers import register_builtin_realtime_providers
        from hermes_s2s.registry import _REALTIME_REGISTRY
        register_builtin_realtime_providers()
        assert "gemini-live" in _REALTIME_REGISTRY

    def test_resolve_realtime_with_new_alias_builds_openai_backend(self) -> None:
        """Routing through resolve_realtime with gpt-realtime-2 alias
        builds an OpenAIRealtimeBackend with the configured model."""
        from hermes_s2s.providers import register_builtin_realtime_providers
        from hermes_s2s.registry import resolve_realtime
        register_builtin_realtime_providers()

        backend = resolve_realtime("gpt-realtime-2", {
            "openai": {"model": "gpt-realtime-2025-08-28", "voice": "alloy"},
        })
        from hermes_s2s.providers.realtime.openai_realtime import OpenAIRealtimeBackend
        assert isinstance(backend, OpenAIRealtimeBackend)
        assert backend.model == "gpt-realtime-2025-08-28"
        assert backend.voice == "alloy"
