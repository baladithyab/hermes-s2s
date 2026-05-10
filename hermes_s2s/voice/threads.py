"""ThreadResolver — resolves a voice-session target Discord thread.

Per ADR-0012 (voice thread co-management) and research-14 §5/§6:

* Invoked-in-thread (`/voice join` from within a thread) → reuse that thread.
* Invoked-in-channel → auto-create a public thread with a 60-minute
  auto-archive so user STT + ARIA TTS replies can be mirrored as text
  alongside the voice call.
* Invoked-in-forum (or any parent that can't host a plain public thread)
  → return ``None`` so the caller falls back to Hermes core's existing
  forum-thread path (``_send_to_forum``).

This module is imported lazily by ``_internal/discord_bridge.py`` so the
plugin still loads cleanly on environments where ``discord.py`` is not
installed — the discord imports happen inside ``resolve()``, not at module
load time.

Security notes (Phase-8 review P1-F4):
    The starter message explicitly announces that the transcript thread
    is **public** so bystanders in the parent channel understand the
    privacy boundary before speaking into the VC. Operators can disable
    the warning by setting ``s2s.voice.thread_starter_message`` to an
    empty string (deprecation-warned at `resolve()` time).
"""

from __future__ import annotations

import logging
import string
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)


_DEFAULT_NAME_TEMPLATE = "🎤 {user} — {date:%Y-%m-%d %H:%M}"
_DEFAULT_STARTER_MESSAGE = (
    "🎤 Voice transcript will appear here. **This thread is public** — "
    "anyone in #{parent_channel_name} can see it. "
    "Type `/voice leave` or leave the VC to end the call."
)


class _SafeFormatter(string.Formatter):
    """A ``str.format`` variant that tolerates missing keys.

    Missing ``{user}``/``{date:…}`` fields render as an empty string
    instead of raising ``KeyError``. Used so an operator misconfiguring
    the template with an unknown placeholder won't crash the
    voice-channel-join code path.
    """

    def get_value(  # type: ignore[override]
        self, key: Any, args: Any, kwargs: Any
    ) -> Any:
        if isinstance(key, str):
            return kwargs.get(key, "")
        try:
            return super().get_value(key, args, kwargs)
        except (IndexError, KeyError):
            return ""


class ThreadResolver:
    """Resolve ``(adapter, event, voice_channel)`` → ``target_thread_id``.

    Construction is cheap — the class holds template strings only. The
    heavy work (discord API calls) happens in :meth:`resolve`, which is
    awaited from the ``join_voice_channel`` monkey-patch before Hermes
    core's runner snapshots ``event.source``.
    """

    def __init__(self, config: dict) -> None:
        voice_cfg = (config or {}).get("s2s", {}).get("voice", {}) or {}
        self.name_template: str = voice_cfg.get(
            "thread_name_template", _DEFAULT_NAME_TEMPLATE
        )
        # Distinguish "unset" (None) from "explicitly blanked" ("") so we
        # can emit a one-time deprecation warning for operators who
        # silenced the public-thread notice on purpose.
        self.starter_message: Optional[str] = voice_cfg.get(
            "thread_starter_message", _DEFAULT_STARTER_MESSAGE
        )
        self._formatter = _SafeFormatter()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def resolve(
        self, adapter: Any, event: Any, voice_channel: Any
    ) -> Optional[int]:
        """Return the thread_id to mirror transcripts into, or ``None``.

        ``None`` means "caller should fall back to the non-thread path"
        — currently forum parents or any failure to create a public
        thread. The caller must treat ``None`` as best-effort, not as
        an error — voice still works without a transcript mirror.
        """
        source = getattr(event, "source", None)

        # (1) Event already has a thread_id — reuse it.
        thread_id = getattr(source, "thread_id", None)
        if thread_id:
            try:
                return int(thread_id)
            except (TypeError, ValueError):
                logger.debug(
                    "hermes-s2s.threads: event.source.thread_id=%r not int-coercible; "
                    "falling through",
                    thread_id,
                )

        # (2) Invoked from within a thread (chat_type=='thread').
        chat_type = getattr(source, "chat_type", None)
        if chat_type == "thread":
            chat_id = getattr(source, "chat_id", None)
            if chat_id:
                try:
                    return int(chat_id)
                except (TypeError, ValueError):
                    logger.debug(
                        "hermes-s2s.threads: chat_type=='thread' but chat_id=%r "
                        "not int-coercible",
                        chat_id,
                    )

        # (3) Invoked from a plain channel — need to create a thread.
        chat_id = getattr(source, "chat_id", None)
        if not chat_id:
            logger.debug(
                "hermes-s2s.threads: event.source has no chat_id; cannot resolve "
                "parent channel for thread auto-create"
            )
            return None

        client = getattr(adapter, "_client", None)
        if client is None:
            logger.debug(
                "hermes-s2s.threads: adapter has no _client attribute; "
                "cannot resolve parent channel"
            )
            return None

        try:
            parent = client.get_channel(int(chat_id))
        except (TypeError, ValueError):
            logger.debug(
                "hermes-s2s.threads: chat_id=%r not int-coercible", chat_id
            )
            return None
        if parent is None:
            logger.debug(
                "hermes-s2s.threads: client.get_channel(%s) returned None", chat_id
            )
            return None

        # (4) Forum parents — caller falls back to forum-thread path.
        try:
            import discord  # type: ignore[import-not-found]

            ForumChannel = getattr(discord, "ForumChannel", None)
            ChannelType = getattr(discord, "ChannelType", None)
        except Exception:  # pragma: no cover — defensive
            logger.debug(
                "hermes-s2s.threads: discord module not importable; skipping "
                "thread auto-create"
            )
            return None

        if ForumChannel is not None and isinstance(parent, ForumChannel):
            logger.debug(
                "hermes-s2s.threads: parent channel is ForumChannel; caller will "
                "fall back to forum path"
            )
            return None

        # (5) Create a public thread.
        user_display = getattr(source, "user_display_name", None) or "user"
        rendered_name = self._render_name(user_display)

        if ChannelType is None or not hasattr(parent, "create_thread"):
            logger.debug(
                "hermes-s2s.threads: parent channel has no create_thread() / "
                "ChannelType unavailable; skipping auto-create"
            )
            return None

        try:
            thread = await parent.create_thread(
                name=rendered_name,
                type=ChannelType.public_thread,
                auto_archive_duration=60,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes-s2s.threads: create_thread() failed on parent=%s: %s",
                getattr(parent, "id", "?"),
                exc,
            )
            return None

        # (6) Starter message — Phase-8 security P1-F4 public-thread warning.
        starter = self.starter_message
        if starter:
            parent_name = getattr(parent, "name", "") or ""
            try:
                body = starter.format(parent_channel_name=parent_name)
            except Exception:  # noqa: BLE001
                # Operator-provided template with a bad placeholder —
                # fall back to raw text rather than crash.
                body = starter
            try:
                await thread.send(body)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "hermes-s2s.threads: starter-message send failed on "
                    "new thread %s: %s",
                    getattr(thread, "id", "?"),
                    exc,
                )
        elif starter == "":
            logger.warning(
                "hermes-s2s.threads: thread_starter_message is empty; "
                "public-thread privacy notice suppressed (deprecated; "
                "will be required in a future release)"
            )

        # (7) Mark on adapter's participation tracker if present.
        tracker = getattr(adapter, "_threads", None)
        if tracker is not None and hasattr(tracker, "mark"):
            try:
                tracker.mark(str(thread.id))
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "hermes-s2s.threads: _threads.mark failed: %s", exc
                )

        try:
            return int(thread.id)
        except (TypeError, ValueError):  # pragma: no cover — defensive
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _render_name(self, user_display: str) -> str:
        """Render ``self.name_template`` with ``user`` + ``date`` bound."""
        try:
            return self._formatter.format(
                self.name_template,
                user=user_display,
                date=datetime.now(),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "hermes-s2s.threads: name-template render failed (%s); "
                "falling back to default",
                exc,
            )
            return self._formatter.format(
                _DEFAULT_NAME_TEMPLATE,
                user=user_display,
                date=datetime.now(),
            )


__all__ = ["ThreadResolver"]
