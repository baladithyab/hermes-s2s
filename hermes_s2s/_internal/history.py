"""History payload builder for realtime voice sessions.

Why this exists
---------------
When a user runs ``/voice join`` after chatting with ARIA in a Discord
text thread, the realtime backend (Gemini Live, OpenAI Realtime) is
born with **only the system prompt**. The model has no idea what was
just discussed in text, so it starts every voice call from zero —
users perceive this as ARIA having amnesia at the moment they switch
to voice.

This module fetches the prior conversation from Hermes core's
``SessionDB`` and shapes it into an OpenAI-format ``[{role, content},
...]`` list that backends inject as context-only turns (Gemini Live
``clientContent.turnComplete=true``, OpenAI Realtime
``conversation.item.create``) right after their setup handshake and
before the first audio frame.

Module placement
----------------
``_internal/history.py`` per architecture review §3 — this is bridge
glue, not user-facing voice API. The function reaches into Hermes
core internals (``SessionDB``, ``SessionStore._entries``) which by
convention live in ``_internal/``.

Design discipline
-----------------
- **Voice-mirror dedup**: turns whose content starts with the
  ``[voice]`` magic prefix written by ``TranscriptMirror`` get
  filtered out by default. Without this, on rejoin the model would
  see its own prior voice utterances re-injected as text — feels
  like talking to itself. See research/18 §4.4 + UX critique §2.

- **4-tier session-id fallback** per architecture review §5: don't
  silently default to None when ``adapter.session_store._entries``
  changes shape upstream. Cascade through public getters first,
  private dict second, DB lookup third, Discord REST last-resort.
  Each tier wrapped; only ALL-failures is a warning.

- **Defensive read**: if SessionDB is locked / file moved /
  schema-evolves mid-flight, return ``[]`` not raise. Voice still
  works without history.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Magic prefix that TranscriptMirror writes for voice turns. Used to
# dedup voice turns when injecting history on rejoin.
# NOTE: TranscriptMirror today writes `**[Voice]** @user: ...` /
# `**[Voice]** ARIA: ...` per voice/transcript.py:170-177. The dedup
# prefix matches that format.
_VOICE_MIRROR_PREFIX = "**[Voice]**"


def build_history_payload(
    session_db: Any,
    session_id: str,
    *,
    max_turns: int = 20,
    max_tokens: int = 8000,
    skip_voice_metadata: bool = True,
) -> List[Dict[str, Any]]:
    """Build OpenAI-format history list for realtime backends.

    Returns a list of ``{"role": "user"|"assistant", "content": str}``
    dicts. Empty list on error (db lock, missing session, etc.) — never
    raises so voice always boots.

    Filtering pipeline:
        1. Drop ``role in {"system", "tool", "function"}`` (we already
           pass system prompt through ``ConnectOptions.system_prompt``;
           tool roles confuse realtime models).
        2. If ``skip_voice_metadata=True``, drop turns with content
           matching the voice-mirror prefix (rejoin dedup).
        3. Coerce structured ``content`` (OpenAI multimodal list) to
           plain text by joining ``{"type":"text","text":...}`` parts.
        4. Drop empty / whitespace-only content.
        5. Truncate from oldest end until rendered text fits
           ``max_tokens`` budget (heuristic ``len // 4`` chars per
           token to avoid tokenizer dependency).
        6. Take last ``max_turns`` after token-budget truncation.

    Parameters
    ----------
    session_db
        ``hermes_state.SessionDB`` instance. Caller is responsible for
        lifecycle (we do NOT close it).
    session_id
        SQLite session row id. Pass ``None`` / empty string to
        short-circuit return.
    max_turns
        Hard cap on number of turns reaching the wire. Default 20.
    max_tokens
        Soft cap on rendered text size. Default 8000.
    skip_voice_metadata
        Whether to drop turns identified as voice-mirror writes (default
        True). Set False to debug rejoin behavior.

    Returns
    -------
    list of dict
        Possibly empty. Each dict has at minimum ``role`` and ``content``
        (str). Caller maps these to provider-specific shapes.
    """
    if not session_id:
        return []

    try:
        raw = session_db.get_messages_as_conversation(session_id)
    except Exception as exc:  # noqa: BLE001 - defensive against DB issues
        logger.debug(
            "history.build_history_payload: get_messages_as_conversation "
            "failed for session_id=%s: %s; returning empty",
            session_id,
            exc,
        )
        return []

    if not raw:
        return []

    filtered: List[Dict[str, Any]] = []
    for msg in raw:
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _extract_text(msg.get("content"))
        if not text or not text.strip():
            continue
        if skip_voice_metadata and text.lstrip().startswith(_VOICE_MIRROR_PREFIX):
            continue
        filtered.append({"role": role, "content": text})

    # Token-budget truncation: drop oldest until under budget.
    # Cheap heuristic — avoid tokenizer dep.
    rendered_chars = sum(len(m["content"]) for m in filtered)
    char_budget = max_tokens * 4
    while filtered and rendered_chars > char_budget:
        dropped = filtered.pop(0)
        rendered_chars -= len(dropped["content"])

    # Final turn cap: last N
    if len(filtered) > max_turns:
        filtered = filtered[-max_turns:]

    return filtered


def _extract_text(content: Any) -> str:
    """Flatten OpenAI-format content into a plain string.

    Handles three shapes:
    - str (already plain text)
    - list of dicts (multimodal: extract ``{"type":"text","text":...}`` parts)
    - None / other (returns empty string)
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return " ".join(parts)
    # Unknown shape — best-effort string
    try:
        return str(content)
    except Exception:  # noqa: BLE001
        return ""


# --- Session-id resolution: 4-tier fallback cascade ----------------- #


def resolve_session_id_for_thread(
    adapter: Any,
    *,
    thread_id: int,
    user_id: int,
    platform: str = "discord",
) -> Optional[str]:
    """Resolve a Hermes session_id from a Discord thread context.

    Architecture review §5 mandates this be a 4-tier cascade with
    explicit failure logging at the right level. We do NOT just default
    to ``None`` on any miss — the user's voice agent losing context
    should be observable, not silent.

    Cascade order
    -------------
    1. ``adapter.session_store.get(session_key)`` — public getter,
       if Hermes core ever exposes one. Today, none — falls through.
    2. ``adapter.session_store._entries[session_key]`` — current
       implementation path (private; trapped in try/except).
    3. ``session_db.get_session_by_title(session_key)`` — DB lookup
       fallback (hermes_state.py:1050). NOT exercised in 0.4.2 — the
       session_key isn't necessarily the title. Reserved for v0.5.0.
    4. Returns None — voice still boots, no history injected.

    Discord REST history (research/18 §3 "tier 4") is NOT implemented
    here — that's a different pathway entirely (fetch text messages
    from Discord, not from SessionDB). Reserve for v0.5.0 if
    SessionStore moves to LRU eviction and tier-2 starts missing
    on legitimate sessions.

    Parameters
    ----------
    adapter
        Hermes Discord adapter (gateway/platforms/discord.py
        ``DiscordAdapter`` instance). Must have ``session_store``.
    thread_id
        Discord thread or channel id used by the voice session.
    user_id
        Discord user id of the user who triggered the voice join.
        Used to construct the synthetic ``SessionSource``.
    platform
        Always ``"discord"`` for now; parameter reserved for
        cross-platform reuse.

    Returns
    -------
    str | None
        Session id if resolved, else None.
    """
    store = getattr(adapter, "session_store", None)
    if store is None:
        logger.debug(
            "resolve_session_id_for_thread: adapter has no session_store"
        )
        return None

    # Build a synthetic SessionSource matching what the Discord adapter
    # would emit for a voice join in this thread. Discord builds
    # ``chat_id=thread_id, thread_id=thread_id, chat_type="thread"``
    # (see gateway/platforms/discord.py:3481-3488). For thread chat_type
    # with thread_sessions_per_user=False (default), build_session_key
    # at gateway/session.py:642-657 ignores user_id — so the key is
    # ``agent:main:discord:thread:<thread_id>:<thread_id>``.
    try:
        from gateway.session import SessionSource  # type: ignore

        try:
            from gateway.types import Platform  # type: ignore
        except ImportError:
            from gateway.session import Platform  # type: ignore

        source = SessionSource(
            platform=Platform.DISCORD,
            chat_id=str(thread_id),
            chat_type="thread",
            thread_id=str(thread_id),
            user_id=str(user_id),
            user_name=str(user_id),
        )
        session_key = store._generate_session_key(source)  # noqa: SLF001
        logger.debug(
            "resolve_session_id_for_thread: synthesized session_key=%r",
            session_key,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "resolve_session_id_for_thread: synthetic source/_generate_session_key "
            "failed: %s",
            exc,
        )
        return None

    # Tier 1: public getter (not implemented upstream as of v0.4.2)
    getter = getattr(store, "get", None)
    if callable(getter):
        try:
            entry = getter(session_key)
            if entry is not None:
                sid = getattr(entry, "session_id", None)
                if sid:
                    return str(sid)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "resolve_session_id_for_thread: store.get(%r) raised: %s",
                session_key,
                exc,
            )
            # fall through

    # Tier 2: private _entries dict (today's path)
    try:
        entries = getattr(store, "_entries", None)
        if entries is not None:
            entry = entries.get(session_key)
            if entry is not None:
                sid = getattr(entry, "session_id", None)
                if sid:
                    return str(sid)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "resolve_session_id_for_thread: _entries lookup failed: %s",
            exc,
        )

    # Tier 3: DB title lookup (reserved for v0.5.0; session_key isn't
    # always the title — would require a new SessionDB.get_by_session_key
    # method upstream)
    # Skipped intentionally for v0.4.2.

    # All tiers exhausted — log at warn so observability surfaces this
    # without spamming when the user just hasn't chatted in this thread.
    logger.info(
        "resolve_session_id_for_thread: no session found for thread_id=%s "
        "user_id=%s platform=%s — voice will start without text history",
        thread_id,
        user_id,
        platform,
    )
    return None


def get_or_create_adapter_session_db(adapter: Any) -> Any:
    """Get the cached ``SessionDB`` instance, constructing on first use.

    Per architecture review §2: avoid re-instantiating SessionDB per
    VC join (each instance opens its own SQLite connection — ~5ms
    schema-reconcile tax on the join-critical path). Cache on the
    adapter instance.

    Returns ``None`` if SessionDB can't be imported (test env).
    """
    cached = getattr(adapter, "_s2s_session_db", None)
    if cached is not None:
        return cached
    try:
        from hermes_state import SessionDB  # type: ignore

        db = SessionDB()
        adapter._s2s_session_db = db  # cache for next join
        return db
    except Exception as exc:  # noqa: BLE001 - test env may lack hermes_state
        logger.debug(
            "get_or_create_adapter_session_db: SessionDB unavailable: %s",
            exc,
        )
        return None


__all__ = [
    "build_history_payload",
    "resolve_session_id_for_thread",
    "get_or_create_adapter_session_db",
]
