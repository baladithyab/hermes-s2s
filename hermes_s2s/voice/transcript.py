"""TranscriptMirror — post voice-session transcripts to a Discord thread.

Per ADR-0012 + research-14 §5 + Phase-8 security P1-F5:

* **Rate-limited**: 5 sends per 5-second window per channel (well under
  Discord's 5/2s REST limit).
* **Bounded overflow queue**: 50 items per channel. Excess items are
  dropped silently; a single warning log per 60-second window per
  channel surfaces sustained overflow without flooding the log.
* **Fire-and-forget**: failures are logged, never raised, so a flaky
  network or a stale thread can't crash the voice session.

Wiring:
    * Cascaded mode gets transcript mirroring "for free" from Hermes
      core's existing text-reply path at ``run.py:9298-9301``; this
      mirror is only invoked for **realtime** mode, where the backend
      emits ``transcript_partial`` / ``transcript_final`` events that
      otherwise get dropped at ``audio_bridge.py:~616``.
    * ``discord_bridge.py`` constructs a ``TranscriptMirror``, bakes in
      the target ``channel_id`` via ``functools.partial``, and assigns
      the resulting callable to ``RealtimeAudioBridge._transcript_sink``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Deque, Dict, Tuple

logger = logging.getLogger(__name__)


# Token-bucket limits (channel-scoped).
_RATE_LIMIT_MAX = 5
_RATE_LIMIT_WINDOW_SEC = 5.0

# Overflow queue bounds.
_QUEUE_MAX = 50

# Throttle overflow-warning logs: at most one per 60s per channel.
_OVERFLOW_WARN_WINDOW_SEC = 60.0


class TranscriptMirror:
    """Post voice transcripts to a Discord channel/thread with backpressure.

    Instances are cheap to construct; the state is per-mirror, so one
    mirror per active voice session is the intended usage. Passing the
    same ``TranscriptMirror`` across multiple channels works — state is
    keyed on ``channel_id`` internally — but is not required.
    """

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter
        # channel_id → (window_start_monotonic, count_in_window)
        self._rate_windows: Dict[int, Tuple[float, int]] = {}
        # channel_id → deque of (role, text, final) awaiting flush
        self._queues: Dict[int, Deque[Tuple[str, str, bool]]] = {}
        # channel_id → monotonic-time of last overflow warn emitted
        self._overflow_warn_ts: Dict[int, float] = {}

    # ------------------------------------------------------------------
    # Sync entry point — the realtime bridge runs inside an asyncio loop
    # but the transcript sink may be invoked from either sync or async
    # code paths depending on where in the pump the event arrives.
    # ------------------------------------------------------------------

    def schedule_send(
        self,
        *,
        channel_id: int,
        role: str,
        text: str,
        final: bool = False,
    ) -> None:
        """Schedule :meth:`send` on the running event loop.

        Thread- and sync-context safe. If no loop is running in this
        thread we still best-effort dispatch via
        ``asyncio.run_coroutine_threadsafe`` targeting the adapter's
        loop (if the adapter exposes ``_loop``).
        """
        coro = self.send(
            channel_id=channel_id, role=role, text=text, final=final
        )
        # 0.4.2 audit-#5: ``asyncio.get_event_loop()`` is deprecated in 3.12
        # (DeprecationWarning) and will raise in 3.14 when called from a
        # thread without a running loop — exactly our case (player thread).
        # Use get_running_loop() inside try/except + adapter loop fallback.
        loop = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            # Hot path — fire and forget on the running loop.
            loop.call_soon_threadsafe(asyncio.create_task, coro)
            return

        # Fallback: try the adapter's loop.
        adapter_loop = getattr(self._adapter, "_loop", None)
        if adapter_loop is not None and adapter_loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(coro, adapter_loop)
                return
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "hermes-s2s.transcript: run_coroutine_threadsafe on "
                    "adapter loop failed: %s",
                    exc,
                )

        # Last-resort: we can't schedule; close the coroutine so Python
        # doesn't warn about "coroutine was never awaited".
        coro.close()
        logger.warning(
            "hermes-s2s.transcript: no running event loop to schedule send "
            "(channel_id=%s); dropping",
            channel_id,
        )

    # ------------------------------------------------------------------
    # Async entry point — formats, rate-limits, and sends.
    # ------------------------------------------------------------------

    async def send(
        self,
        channel_id: int,
        role: str,
        text: str,
        final: bool = False,
    ) -> None:
        """Format + send one transcript utterance to ``channel_id``.

        * ``role == "user"`` → ``**[Voice]** @{user}: {text}``
          (user name is a placeholder in 0.4.0; the 0.4.1 work will
          thread the Discord user identity through.)
        * ``role == "assistant"`` → ``**[Voice]** ARIA: {text}``
        * ``final=True`` with empty text is a turn-complete marker and
          is a no-op in 0.4.0.
        """
        # Skip empty final markers — they carry no information the
        # thread needs today. Keeping the interface open so a future
        # "end-of-turn chip" UI can hook in without a breaking change.
        if final and not text:
            return

        if not text:
            return

        body = self._format(role, text)

        # --- Rate-limit: token-bucket per channel ---
        if not self._consume_token(channel_id):
            # Over the limit → queue (or drop if queue is full).
            queue = self._queues.setdefault(channel_id, deque())
            if len(queue) >= _QUEUE_MAX:
                self._warn_overflow(channel_id)
                return
            queue.append((role, text, final))
            return

        # --- Before sending, drain any queued items that now fit ---
        await self._flush_queue(channel_id)

        await self._deliver(channel_id, body)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _format(self, role: str, text: str) -> str:
        if role == "user":
            # 0.4.0: placeholder user identity. Research-14 §6 covers
            # the 0.4.1 path that threads the Discord user display name
            # through from the voice-join event.
            return f"**[Voice]** @user: {text}"
        # Default (and assistant) → ARIA voice line.
        return f"**[Voice]** ARIA: {text}"

    def _consume_token(self, channel_id: int) -> bool:
        """Return True if we may send now; False if over the limit."""
        now = time.monotonic()
        window = self._rate_windows.get(channel_id)
        if window is None or (now - window[0]) >= _RATE_LIMIT_WINDOW_SEC:
            # Fresh window.
            self._rate_windows[channel_id] = (now, 1)
            return True
        start, count = window
        if count < _RATE_LIMIT_MAX:
            self._rate_windows[channel_id] = (start, count + 1)
            return True
        return False

    async def _flush_queue(self, channel_id: int) -> None:
        """Drain queued utterances while we still have tokens this window."""
        queue = self._queues.get(channel_id)
        if not queue:
            return
        # We already consumed one token for the current call in
        # :meth:`send`; drain from the queue for any additional tokens
        # the current window still permits. _consume_token is the
        # source of truth for "do I have capacity now?".
        while queue and self._consume_token(channel_id):
            role, text, final = queue.popleft()
            if final and not text:
                continue
            body = self._format(role, text)
            await self._deliver(channel_id, body)

    def _warn_overflow(self, channel_id: int) -> None:
        """Log a throttled overflow warning — once per 60s per channel."""
        now = time.monotonic()
        last = self._overflow_warn_ts.get(channel_id, 0.0)
        if (now - last) < _OVERFLOW_WARN_WINDOW_SEC:
            return
        self._overflow_warn_ts[channel_id] = now
        logger.warning(
            "hermes-s2s.transcript: overflow on channel=%s — queue full "
            "(max %d); dropping excess transcript lines (warning throttled "
            "to 1/%.0fs)",
            channel_id,
            _QUEUE_MAX,
            _OVERFLOW_WARN_WINDOW_SEC,
        )

    async def _deliver(self, channel_id: int, body: str) -> None:
        """Resolve channel via adapter and await ``channel.send``."""
        client = getattr(self._adapter, "_client", None)
        if client is None:
            logger.debug(
                "hermes-s2s.transcript: adapter has no _client; drop send to %s",
                channel_id,
            )
            return
        try:
            channel = client.get_channel(int(channel_id))
        except (TypeError, ValueError):
            logger.debug(
                "hermes-s2s.transcript: channel_id=%r not int-coercible",
                channel_id,
            )
            return
        if channel is None:
            logger.debug(
                "hermes-s2s.transcript: client.get_channel(%s) returned None",
                channel_id,
            )
            return
        try:
            await channel.send(body)
        except Exception as exc:  # noqa: BLE001 — never raise
            logger.warning(
                "hermes-s2s.transcript: channel.send failed on %s: %s",
                channel_id,
                exc,
            )


__all__ = ["TranscriptMirror"]
