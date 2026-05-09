"""Audio bridge: routes Discord voice audio to a realtime backend and back.

Implements ADR-0007 (audio-bridge frame-callback hook) and research/09
(frame timing & cadence). This module is the owner of the realtime backend
session during a voice call; it marshals audio between:

- Discord VoiceReceiver (push side, running on a non-asyncio thread) ->
  backend.send_audio_chunk (resampled to backend rate).
- backend.recv_events (async iterator) -> BridgeBuffer output frames
  (resampled to 48 kHz stereo s16le, sliced to 20 ms frames, held fractional
  remainder per research/09 §3).

The QueuedPCMSource (see ``discord_audio.py``) pulls 3840-byte frames
synchronously from the buffer on discord.py's player thread.

Lazy imports: ``scipy`` (resample dep) and ``discord`` are never imported
at module load.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from typing import Any, Optional

from hermes_s2s.audio.resample import resample_pcm

logger = logging.getLogger(__name__)

# 20 ms @ 48 kHz stereo s16le = 48000 * 2 ch * 2 B * 0.020 s = 3840 B
FRAME_BYTES = 3840
SILENCE_FRAME = b"\x00" * FRAME_BYTES

# Discord receive format (decoded Opus): 48 kHz stereo s16le
DISCORD_SAMPLE_RATE = 48000
DISCORD_CHANNELS = 2

# Backend-type → (input_rate, output_rate) default mapping. Used when the
# backend instance doesn't expose explicit ``input_sample_rate`` /
# ``output_sample_rate`` attributes.
_BACKEND_RATE_DEFAULTS: dict[str, tuple[int, int]] = {
    "gemini-live": (16000, 24000),
    "gemini_live": (16000, 24000),
    "openai-realtime": (24000, 24000),
    "openai_realtime": (24000, 24000),
}
_DEFAULT_BACKEND_INPUT_RATE = 16000
_DEFAULT_BACKEND_OUTPUT_RATE = 24000

# Backpressure on the receive -> asyncio boundary. Each slot is one decoded
# Discord frame (~3840 B). 50 frames = ~1 s of audio; we drop oldest beyond.
INPUT_QUEUE_MAX = 50


class BridgeBuffer:
    """Thread-safe audio bridge buffer.

    Input side (Discord -> backend):
        - ``push_input(user_id, pcm)``: called from the discord.py receive
          thread. Uses a ``queue.Queue`` (thread-safe, synchronous) with
          drop-oldest backpressure when full.
        - ``pop_input()``: async coroutine for the bridge loop; awaits the
          next chunk using ``loop.run_in_executor`` on a blocking ``get``.

    Output side (backend -> Discord):
        - ``push_output(pcm)``: async; appends to an internal bytearray under
          a lock, slices into complete 20 ms frames, and holds the trailing
          fractional remainder for the next call (per research/09 §3).
        - ``read_frame()``: sync, called from the AudioSource thread; returns
          the next complete frame or 3840 bytes of silence on underflow.
          NEVER returns b"" (which would terminate Discord playback).
    """

    def __init__(self, input_max: int = INPUT_QUEUE_MAX) -> None:
        self._input_max = input_max
        self._input_q: queue.Queue[tuple[int, bytes]] = queue.Queue(
            maxsize=input_max
        )
        # Output side: lock-guarded frame deque + remainder bytearray.
        self._output_lock = threading.Lock()
        self._output_frames: list[bytes] = []
        self._output_remainder = bytearray()
        # Diagnostics counters.
        self._dropped_input = 0
        self._underflows = 0
        self._output_drops = 0

    # ---------------- input side (sync, thread-safe) ----------------

    def push_input(self, user_id: int, pcm: bytes) -> None:
        """Called from the Discord receive thread. Never blocks.

        On overflow, drops the OLDEST chunk (per research/09 §5 —
        freshest audio is what the model needs for live conversation).
        """
        try:
            self._input_q.put_nowait((user_id, pcm))
        except queue.Full:
            # Drop oldest, then retry. Use try/except because another
            # consumer could have drained between calls.
            try:
                self._input_q.get_nowait()
                self._dropped_input += 1
            except queue.Empty:  # pragma: no cover - race
                pass
            try:
                self._input_q.put_nowait((user_id, pcm))
            except queue.Full:  # pragma: no cover - pathological
                self._dropped_input += 1

    async def pop_input(
        self, poll_interval: float = 0.005
    ) -> tuple[int, bytes]:
        """Async: await next input chunk via a cooperative poll loop.

        We intentionally do NOT use ``run_in_executor(queue.get)`` because
        cancelling the wrapping asyncio task doesn't abort the blocking
        ``get()`` — the executor thread would stay wedged on the queue
        until something is pushed, leaving the bridge unable to shut down
        cleanly. A short-interval non-blocking poll is cheap (<0.2%% CPU)
        and cancels immediately at ``await asyncio.sleep``.
        """
        while True:
            try:
                return self._input_q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(poll_interval)

    def pop_input_nowait(self) -> Optional[tuple[int, bytes]]:
        """Non-blocking pop; returns None if empty."""
        try:
            return self._input_q.get_nowait()
        except queue.Empty:
            return None

    # ---------------- output side ----------------

    def push_output(self, pcm: bytes) -> int:
        """Append backend-produced audio; slice into complete 20 ms frames.

        The fractional remainder (bytes beyond the last whole frame) is
        held in ``self._output_remainder`` and prepended on the next call.
        This is the standard jitter-buffer pattern (pipecat, livekit-rtc);
        zero-padding mid-stream would inject audible clicks.

        Returns the number of complete frames produced by this call.
        """
        if not pcm:
            return 0
        frames_added = 0
        with self._output_lock:
            self._output_remainder.extend(pcm)
            while len(self._output_remainder) >= FRAME_BYTES:
                frame = bytes(self._output_remainder[:FRAME_BYTES])
                del self._output_remainder[:FRAME_BYTES]
                self._output_frames.append(frame)
                frames_added += 1
        return frames_added

    def read_frame(self) -> bytes:
        """Sync: called from the discord.py player thread on a 20 ms cadence.

        Returns the next queued frame, or 3840 bytes of silence on underflow.
        NEVER returns b"" — that would terminate playback per research/07.
        """
        with self._output_lock:
            if self._output_frames:
                return self._output_frames.pop(0)
            self._underflows += 1
        return SILENCE_FRAME

    # ---------------- diagnostics ----------------

    @property
    def dropped_input(self) -> int:
        return self._dropped_input

    @property
    def underflows(self) -> int:
        return self._underflows

    @property
    def queued_output_frames(self) -> int:
        with self._output_lock:
            return len(self._output_frames)

    @property
    def queued_input_chunks(self) -> int:
        return self._input_q.qsize()


def _backend_type_name(backend: Any) -> str:
    """Heuristic: derive a backend kind tag from the instance for rate lookup."""
    for attr in ("NAME", "name", "kind"):
        val = getattr(backend, attr, None)
        if isinstance(val, str) and val:
            return val.lower().replace("_", "-")
    return type(backend).__name__.lower()


def _resolve_backend_rates(backend: Any) -> tuple[int, int]:
    """Return ``(input_rate, output_rate)`` for a backend instance.

    Priority: explicit attributes on the instance > hardcoded mapping by
    backend-name > module defaults (16k/24k).
    """
    explicit_in = getattr(backend, "input_sample_rate", None)
    explicit_out = getattr(backend, "output_sample_rate", None)
    if isinstance(explicit_in, int) and isinstance(explicit_out, int):
        return explicit_in, explicit_out
    name = _backend_type_name(backend)
    for key, (in_r, out_r) in _BACKEND_RATE_DEFAULTS.items():
        if key in name:
            return (
                explicit_in if isinstance(explicit_in, int) else in_r,
                explicit_out if isinstance(explicit_out, int) else out_r,
            )
    return (
        explicit_in if isinstance(explicit_in, int) else _DEFAULT_BACKEND_INPUT_RATE,
        explicit_out if isinstance(explicit_out, int) else _DEFAULT_BACKEND_OUTPUT_RATE,
    )


class RealtimeAudioBridge:
    """Owns a ``BridgeBuffer`` + realtime backend + asyncio bridge task.

    Typical lifecycle (see ADR-0007)::

        bridge = RealtimeAudioBridge(backend=be, tool_bridge=tb)
        await bridge.start()
        # ...while the voice call is active:
        #   voice_receiver.set_frame_callback(bridge.on_user_frame)
        #   voice_client.play(QueuedPCMSource(bridge.buffer))
        await bridge.close()

    The bridge runs two concurrent coroutines inside ``bridge_loop``:
      * ``_pump_input``: drains the buffer's input queue, resamples each
        frame from 48 kHz stereo to the backend's mono input rate, and calls
        ``backend.send_audio_chunk``.
      * ``_pump_output``: async-iterates ``backend.recv_events()``; on each
        ``audio_chunk`` event, resamples the chunk to 48 kHz stereo s16le
        and calls ``buffer.push_output``. Routes ``tool_call`` events to
        the optional ``tool_bridge``.
    """

    def __init__(
        self,
        backend: Any,
        tool_bridge: Optional[Any] = None,
        *,
        buffer: Optional[BridgeBuffer] = None,
        input_queue_max: int = INPUT_QUEUE_MAX,
    ) -> None:
        self.backend = backend
        self.tool_bridge = tool_bridge
        self.buffer = buffer if buffer is not None else BridgeBuffer(
            input_max=input_queue_max
        )
        in_rate, out_rate = _resolve_backend_rates(backend)
        self._backend_input_rate = in_rate
        self._backend_output_rate = out_rate
        self._task: Optional[asyncio.Task[None]] = None
        self._children: list[asyncio.Task[Any]] = []
        self._closed = False
        self._stop_event: Optional[asyncio.Event] = None

    # ---------------- public API ----------------

    async def start(self) -> None:
        """Start the bridge loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._closed = False
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._bridge_loop(), name="hermes-s2s.bridge_loop"
        )

    async def close(self) -> None:
        """Cancel the bridge loop, close the backend. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._stop_event is not None:
            self._stop_event.set()
        # Cancel the supervisor task (which will in turn cancel its children).
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # Also cancel any stragglers.
        for child in self._children:
            if not child.done():
                child.cancel()
        for child in self._children:
            try:
                await child
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._children.clear()
        # Close the backend.
        close = getattr(self.backend, "close", None)
        if close is not None:
            try:
                res = close()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:  # noqa: BLE001
                logger.exception("backend close raised")

    def on_user_frame(self, user_id: int, pcm: bytes) -> None:
        """Called from the Discord receive thread. Thread-safe & non-blocking."""
        self.buffer.push_input(user_id, pcm)

    # ---------------- bridge loop ----------------

    async def _bridge_loop(self) -> None:
        """Supervise the two pump coroutines."""
        in_task = asyncio.create_task(self._pump_input(), name="bridge.pump_input")
        out_task = asyncio.create_task(self._pump_output(), name="bridge.pump_output")
        self._children = [in_task, out_task]
        try:
            await asyncio.gather(in_task, out_task, return_exceptions=True)
        except asyncio.CancelledError:
            for t in self._children:
                if not t.done():
                    t.cancel()
            raise
        finally:
            # Make sure children are really settled before we return.
            for t in self._children:
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass

    async def _pump_input(self) -> None:
        """Drain input queue -> resample to backend rate -> backend.send_audio_chunk."""
        while True:
            try:
                _user_id, pcm = await self.buffer.pop_input()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("pop_input failed")
                await asyncio.sleep(0.005)
                continue
            try:
                if self._backend_input_rate == DISCORD_SAMPLE_RATE:
                    # Same-rate path still needs stereo→mono mixdown.
                    resampled = resample_pcm(
                        pcm,
                        src_rate=DISCORD_SAMPLE_RATE,
                        dst_rate=self._backend_input_rate,
                        src_channels=DISCORD_CHANNELS,
                        dst_channels=1,
                    )
                else:
                    resampled = resample_pcm(
                        pcm,
                        src_rate=DISCORD_SAMPLE_RATE,
                        dst_rate=self._backend_input_rate,
                        src_channels=DISCORD_CHANNELS,
                        dst_channels=1,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("resample input failed")
                continue
            try:
                await self.backend.send_audio_chunk(
                    resampled, self._backend_input_rate
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("backend.send_audio_chunk failed")

    async def _pump_output(self) -> None:
        """Consume backend events; route audio + tool_call."""
        try:
            events = self.backend.recv_events()
        except Exception:  # noqa: BLE001
            logger.exception("backend.recv_events() raised on invocation")
            return

        # recv_events() is an async iterator. It may be either an async-def
        # function returning an async iterator (as per the Protocol) or an
        # async generator. Both are accepted.
        try:
            async for event in events:
                await self._dispatch_event(event)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("bridge output pump failed")

    async def _dispatch_event(self, event: Any) -> None:
        etype = getattr(event, "type", None)
        payload = getattr(event, "payload", {}) or {}
        if etype == "audio_chunk":
            pcm = payload.get("pcm") or payload.get("audio") or b""
            if not pcm:
                return
            rate = payload.get("sample_rate", self._backend_output_rate)
            try:
                frame_pcm = resample_pcm(
                    pcm,
                    src_rate=rate,
                    dst_rate=DISCORD_SAMPLE_RATE,
                    src_channels=1,
                    dst_channels=DISCORD_CHANNELS,
                )
            except Exception:  # noqa: BLE001
                logger.exception("resample output failed")
                return
            self.buffer.push_output(frame_pcm)
        elif etype == "tool_call":
            call_id = payload.get("call_id") or payload.get("id", "")
            name = payload.get("name", "")
            args = payload.get("args") or payload.get("arguments") or {}
            if self.tool_bridge is None:
                logger.warning(
                    "tool_call received but no tool_bridge configured "
                    "(call_id=%s, name=%s); skipping.",
                    call_id,
                    name,
                )
                return
            try:
                await self.tool_bridge.handle_tool_call(
                    self.backend, call_id, name, args
                )
            except Exception:  # noqa: BLE001
                logger.exception("tool_bridge.handle_tool_call raised")
        elif etype == "error":
            logger.warning("backend error event: %r", payload)
        # transcript_partial / transcript_final / session_resumed: ignored
        # for 0.3.1. Future: forward to Hermes session log.
