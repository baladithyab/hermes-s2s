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
from typing import Any, Callable, Optional

import numpy as np

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


# ---------------------------------------------------------------------------
# Active-bridge registry (P1-2).
#
# The Hermes gateway has no direct access to the ``RealtimeAudioBridge``
# instance created deep inside ``discord_bridge.py``. A module-level
# registry is the narrowest seam that lets the ``s2s_status`` LLM tool
# surface ``bridge.stats()`` without plumbing an extra argument through
# every layer. Guarded by a threading.Lock because ``start()`` runs in an
# asyncio loop but ``get_active_bridge()`` may be called from any thread
# (including the Discord player thread).
# ---------------------------------------------------------------------------
_ACTIVE_BRIDGE: "Optional[RealtimeAudioBridge]" = None
_ACTIVE_BRIDGE_LOCK = threading.Lock()


def get_active_bridge() -> "Optional[RealtimeAudioBridge]":
    """Return the currently-active ``RealtimeAudioBridge``, or ``None``.

    There is at most ONE active bridge per process (realtime voice is
    single-call in 0.3.2 — see README §Known issues). Returns ``None``
    before ``start()`` completes or after ``close()`` has cleared the slot.
    """
    with _ACTIVE_BRIDGE_LOCK:
        return _ACTIVE_BRIDGE


def _set_active_bridge(bridge: "Optional[RealtimeAudioBridge]") -> None:
    global _ACTIVE_BRIDGE
    with _ACTIVE_BRIDGE_LOCK:
        _ACTIVE_BRIDGE = bridge


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
        # G3 (BACKLOG-0.3.2 F5): debounced warn + per-frame emission counters.
        # P2-fix-B: use a monotonically-advancing next-warn threshold rather
        # than a modulo check, so the warn fires reliably even if multiple
        # drops land in the same ``push_input`` call (the modulo variant would
        # skip the boundary when counters leapt from e.g. 99 to 101).
        self._dropped_input_warn_threshold = 100
        self._next_drop_warn_at = self._dropped_input_warn_threshold
        self._frames_emitted = 0
        self._frames_underflow = 0
        # 0.4.2 S1 Fix B: track silence-to-audio transitions for raised-cosine
        # fade-in on the first non-silence frame after underflow. Eliminates
        # the reply-onset pop reported by users. Fade ramp is precomputed so
        # ``read_frame`` stays O(frame_size) on the hot path.
        # silence_fade_ms can be 0 to disable; clamped 0-50 by AudioConfig.
        self._last_was_silence: bool = True  # opening state: no audio yet
        self._silence_fade_ms: int = 5  # config-driven; 0 disables
        self._fade_envelope_int16: Optional[np.ndarray] = None
        self._build_fade_envelope()

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
                self._maybe_warn_dropped_input()
            except queue.Empty:  # pragma: no cover - race
                pass
            try:
                self._input_q.put_nowait((user_id, pcm))
            except queue.Full:  # pragma: no cover - pathological
                self._dropped_input += 1
                self._maybe_warn_dropped_input()

    def _maybe_warn_dropped_input(self) -> None:
        """P2-fix-B: emit a warning each time ``_dropped_input`` crosses the
        next debounce boundary. Monotonically advances the threshold so
        missed boundaries (race between two +=1 bumps) still fire once.
        """
        while self._dropped_input >= self._next_drop_warn_at:
            logger.warning(
                "BridgeBuffer: %d input frames dropped so far "
                "(queue capacity=%d)",
                self._dropped_input,
                self._input_max,
            )
            self._next_drop_warn_at += self._dropped_input_warn_threshold

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

        0.4.2 S1 Fix B: applies a raised-cosine fade-in to the first
        non-silence frame after a silence run, eliminating reply-onset
        pops. Fade duration is configurable via AudioConfig.
        """
        with self._output_lock:
            if self._output_frames:
                frame = self._output_frames.pop(0)
                # Fade-in if this is the first non-silence frame after silence.
                if self._last_was_silence and self._fade_envelope_int16 is not None:
                    frame = self._apply_fade_in(frame)
                self._last_was_silence = False
                self._frames_emitted += 1
                return frame
            self._underflows += 1
            self._frames_underflow += 1
            self._last_was_silence = True
        return SILENCE_FRAME

    # 0.4.2 S1 Fix B helpers ------------------------------------------- #

    def clear_output(self) -> int:
        """Drop all queued output frames + held remainder. Used on barge-in.

        0.4.2 S3 (audit #21): when the user starts a new utterance,
        any in-flight ARIA reply is being abandoned. Without this,
        Discord plays out ~300ms of stale audio (the queued frames)
        before the new reply starts — perceived as ARIA still talking
        for a beat after the user interrupts.

        Returns the number of frames dropped (for diagnostics).
        """
        with self._output_lock:
            dropped = len(self._output_frames)
            self._output_frames.clear()
            self._output_remainder.clear()
            self._output_drops += dropped
            # After a clear, the buffer is logically silent again — so
            # the next non-silence frame will get faded in (Fix B).
            self._last_was_silence = True
        return dropped

    def _build_fade_envelope(self) -> None:
        """Precompute raised-cosine fade-in ramp as int16 multipliers (Q15).

        At 48kHz stereo, a 5ms fade = 240 stereo frames = 480 int16 samples.
        Stored as a numpy array of length ``2 * fade_samples`` (interleaved
        L/R) of float32 multipliers in [0, 1]. Applied per-sample in
        ``_apply_fade_in``.

        Set ``_silence_fade_ms = 0`` to disable (envelope set to None).
        """
        if self._silence_fade_ms <= 0:
            self._fade_envelope_int16 = None
            return
        # 48kHz * fade_ms/1000 = samples per channel; * 2 channels interleaved
        fade_samples_per_ch = int(48_000 * self._silence_fade_ms / 1000)
        # Raised-cosine: 0.5 * (1 - cos(pi * t)) over t in [0, 1]
        t = np.linspace(0.0, 1.0, fade_samples_per_ch, endpoint=False)
        envelope = 0.5 * (1.0 - np.cos(np.pi * t)).astype(np.float32)
        # Interleave for stereo: each per-channel sample maps to L,R
        self._fade_envelope_int16 = np.repeat(envelope, 2)

    def set_silence_fade_ms(self, fade_ms: int) -> None:
        """Reconfigure fade-in length (called by bridge after AudioConfig load).

        Clamps to [0, 50]; 0 disables fade-in entirely.
        """
        clamped = max(0, min(50, int(fade_ms)))
        if clamped == self._silence_fade_ms:
            return
        with self._output_lock:
            self._silence_fade_ms = clamped
            self._build_fade_envelope()

    def _apply_fade_in(self, frame: bytes) -> bytes:
        """Apply precomputed raised-cosine fade to leading samples of frame.

        Frame is 3840 bytes = 1920 int16 samples = 960 stereo samples.
        Fade envelope length is ``min(envelope_len, 1920)`` int16 samples.
        Samples beyond the envelope length are unchanged (full amplitude).
        """
        if self._fade_envelope_int16 is None:
            return frame
        env = self._fade_envelope_int16
        arr = np.frombuffer(frame, dtype=np.int16).copy()
        n = min(len(env), len(arr))
        # Multiply leading samples by envelope; preserve int16 dtype.
        leading = arr[:n].astype(np.float32) * env[:n]
        arr[:n] = np.clip(leading, -32768, 32767).astype(np.int16)
        return arr.tobytes()

    # ---------------- diagnostics ----------------

    def stats(self) -> dict:
        """Return a snapshot of buffer diagnostics (G3 / BACKLOG-0.3.2 F5)."""
        with self._output_lock:
            queue_depth_out = len(self._output_frames)
        return {
            "dropped_input": self._dropped_input,
            "dropped_output": self._output_drops,
            "queue_depth_in": self._input_q.qsize(),
            "queue_depth_out": queue_depth_out,
            "frames_emitted": self._frames_emitted,
            "frames_underflow": self._frames_underflow,
        }

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
        system_prompt: str = "You are a helpful voice assistant.",
        voice: Optional[str] = None,
        tools: Optional[list] = None,
    ) -> None:
        self.backend = backend
        self.tool_bridge = tool_bridge
        self.buffer = buffer if buffer is not None else BridgeBuffer(
            input_max=input_queue_max
        )
        in_rate, out_rate = _resolve_backend_rates(backend)
        self._backend_input_rate = in_rate
        self._backend_output_rate = out_rate
        self._system_prompt = system_prompt
        self._voice = voice
        self._tools = list(tools) if tools is not None else []
        self._task: Optional[asyncio.Task[None]] = None
        self._children: list[asyncio.Task[Any]] = []
        self._closed = False
        self._stop_event: Optional[asyncio.Event] = None
        self._tool_tasks: set[asyncio.Task[Any]] = set()
        # 0.4.2 Manual VAD state — for backends that disable server-side VAD
        # (Gemini Live with automaticActivityDetection.disabled=True). For
        # backends with their own VAD (OpenAI Realtime), the activity_start/
        # activity_end calls are no-ops, but we still maintain this state so
        # tests and stats are uniform across backends.
        # See docs/plans/wave-0.4.2-manual-vad.md.
        self._activity_open: bool = False
        self._activity_starts_sent: int = 0
        self._activity_ends_sent: int = 0
        self._last_input_frame_monotonic: float = 0.0
        self._silence_gap_s: float = 0.8  # commit utterance after this much quiet
        # Tool-call ordering primitives (created lazily on first tool_call
        # so they bind to the running loop, not the constructor's loop).
        self._tool_seq_lock: Optional[asyncio.Lock] = None
        self._tool_seq_cond: Optional[asyncio.Condition] = None
        self._tool_seq_next_dispatch = 0
        self._tool_seq_next_inject = 0
        # W3b M3.3: optional transcript sink. Set externally by
        # discord_bridge._attach_realtime_to_voice_client when a thread
        # + TranscriptMirror are available. Signature:
        #   sink(*, role: str, text: str, final: bool) -> None
        # Synchronous — the sink is expected to schedule its own async
        # work (see voice.transcript.TranscriptMirror.schedule_send).
        self._transcript_sink: Optional[Callable[..., None]] = None

        # 0.4.2 S1: streaming-resampler cache for output audio
        # (backend rate -> Discord 48 kHz). Replaces stateless
        # resample_pcm calls in _dispatch_event audio_chunk branch
        # which produced audible chunk-boundary clicks at realistic
        # variable-length Gemini Live chunks. See:
        #   docs/research/17-audio-clicks-rootcause.md
        #   docs/plans/wave-0.4.2-clicks-history-quickwins.md (S1)
        # Reset on: stop(), reconnect, barge-in, activity_start. Cache
        # is lazily populated; instances kept across reset() so we
        # only allocate once per (in_rate, out_rate, channels) triple.
        try:
            from hermes_s2s.audio.streaming_resample import ResamplerCache

            self._out_resampler_cache: Optional["ResamplerCache"] = ResamplerCache()
        except ImportError:
            # soxr not installed; fall back to stateless resample_pcm.
            # Logged once to avoid heartbeat spam.
            logger.warning(
                "hermes-s2s: soxr not installed; using stateless resample "
                "(audible chunk-boundary clicks expected). "
                "Install with: pip install soxr"
            )
            self._out_resampler_cache = None

    # ---------------- public API ----------------

    async def start(self) -> None:
        """Start the bridge loop. Idempotent.

        Connects the backend BEFORE spawning pump tasks — the pumps rely on
        a live session (send_audio_chunk / recv_events both assume the WS
        is open). Connection errors propagate so the caller can log & abort
        cleanly instead of silently feeding frames into a closed socket.
        """
        if self._task is not None and not self._task.done():
            return
        # Connect first so the pumps have a live session when they start.
        await self.backend.connect(
            self._system_prompt, self._voice, self._tools
        )
        self._closed = False
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._bridge_loop(), name="hermes-s2s.bridge_loop"
        )
        # P1-2: publish this bridge as the active one so s2s_status can
        # surface bridge.stats() via ``get_active_bridge()``.
        _set_active_bridge(self)

    async def close(self) -> None:
        """Cancel the bridge loop, close the backend. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._stop_event is not None:
            self._stop_event.set()
        # 0.4.2 S1: drop streaming-resampler state on session end so a
        # subsequent restart starts with fresh filter state.
        self._reset_out_resamplers()
        # 0.4.2: if we're shutting down with an open activity, emit a final
        # activity_end so Gemini flushes the in-flight utterance instead of
        # holding it server-side. Best-effort; backend may already be torn
        # down. The default Protocol method is a no-op for non-manual-VAD
        # backends.
        if self._activity_open:
            try:
                await self.backend.send_activity_end()
                self._activity_open = False
                self._activity_ends_sent += 1
            except Exception:  # noqa: BLE001
                logger.debug(
                    "stop(): final send_activity_end failed (backend may be gone)",
                    exc_info=True,
                )
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
        # Cancel any in-flight tool tasks (their own cleanup in tool_bridge
        # will settle tool_task cancellation; this just drains the bridge-
        # side wrapper tasks so close() doesn't leak them).
        for t in list(self._tool_tasks):
            if not t.done():
                t.cancel()
        for t in list(self._tool_tasks):
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tool_tasks.clear()
        # Close the backend.
        close = getattr(self.backend, "close", None)
        if close is not None:
            try:
                res = close()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:  # noqa: BLE001
                logger.exception("backend close raised")
        # P1-2: clear the active-bridge slot if it still points at us (a
        # newer bridge may already have replaced it in a hypothetical
        # multi-call future).
        if get_active_bridge() is self:
            _set_active_bridge(None)

    def on_user_frame(self, user_id: int, pcm: bytes) -> None:
        """Called from the Discord receive thread. Thread-safe & non-blocking."""
        self.buffer.push_input(user_id, pcm)

    def stats(self) -> dict:
        """Return a snapshot of bridge + buffer diagnostics.

        Delegates to ``self.buffer.stats()`` and augments with bridge-level
        counters (G3 / BACKLOG-0.3.2 F5).
        """
        import time as _time

        s = dict(self.buffer.stats())
        s["backend_input_rate"] = self._backend_input_rate
        s["backend_output_rate"] = self._backend_output_rate
        s["closed"] = self._closed
        s["tool_tasks_in_flight"] = len(self._tool_tasks)
        # 0.4.2 manual-VAD diagnostics.
        s["activity_open"] = self._activity_open
        s["activity_starts_sent"] = self._activity_starts_sent
        s["activity_ends_sent"] = self._activity_ends_sent
        if self._last_input_frame_monotonic > 0:
            s["time_since_last_frame_s"] = round(
                _time.monotonic() - self._last_input_frame_monotonic, 3
            )
        else:
            s["time_since_last_frame_s"] = None
        # 0.4.2 S1: streaming resampler observability.
        if self._out_resampler_cache is not None:
            s["out_resamplers_cached"] = len(self._out_resampler_cache)
        else:
            s["out_resamplers_cached"] = -1  # soxr unavailable
        # 0.4.2 audit-#42: surface history injection state so live verify
        # ("did history actually load?") is objectively checkable via
        # /s2s_status, not vibes-based.
        history_injected = bool(getattr(self.backend, "_history_injected", False))
        s["history_injected"] = history_injected
        s["realtime_voice"] = getattr(self.backend, "voice", None)
        s["realtime_model"] = getattr(self.backend, "model", None)
        return s

    def _reset_out_resamplers(self) -> None:
        """Reset all output streaming-resampler filter state.

        Called when the audio stream is logically discontinuous:
        ``close()``, barge-in, new user activity_start. Keeps the
        cached instances around (they're cheap to reuse) but flushes
        their internal FIR delay lines so the next chunk starts fresh.
        """
        if self._out_resampler_cache is not None:
            try:
                self._out_resampler_cache.clear()
            except Exception:  # noqa: BLE001 - defensive
                logger.exception("ResamplerCache.clear() failed")

    # ---------------- bridge loop ----------------

    async def _bridge_loop(self) -> None:
        """Supervise the two pump coroutines."""
        in_task = asyncio.create_task(self._pump_input(), name="bridge.pump_input")
        out_task = asyncio.create_task(self._pump_output(), name="bridge.pump_output")
        # G3+: periodic stats heartbeat (every 10s) so live debugging doesn't
        # need DEBUG-level logs. Cheap and silent if nothing is happening.
        stats_task = asyncio.create_task(self._stats_heartbeat(), name="bridge.stats_heartbeat")
        # 0.4.2 silence watchdog — drives activity_end after _silence_gap_s of
        # no input frames. Necessary for backends with server-side VAD disabled
        # (Gemini Live in manual-VAD mode); harmless otherwise (the backend's
        # send_activity_end is a no-op).
        watchdog_task = asyncio.create_task(
            self._silence_watchdog(), name="bridge.silence_watchdog"
        )
        self._children = [in_task, out_task, stats_task, watchdog_task]
        try:
            await asyncio.gather(
                in_task, out_task, stats_task, watchdog_task, return_exceptions=True
            )
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

    async def _silence_watchdog(self) -> None:
        """Drive activity_end after a configurable gap of no input frames.

        Polls every 100ms. When activity is open and the last input frame was
        more than ``self._silence_gap_s`` ago, emits ``backend.send_activity_end``
        and clears the open flag. The next input frame will reopen with
        ``send_activity_start``.

        For backends with a working server-side VAD (OpenAI Realtime), the
        ``send_activity_*`` calls are no-ops so the watchdog still runs
        harmlessly. For Gemini Live in manual-VAD mode (``automaticActivity\
        Detection.disabled=True``) this is required to commit each utterance —
        the server otherwise waits indefinitely.
        """
        import time as _time

        try:
            while True:
                await asyncio.sleep(0.1)
                if not self._activity_open:
                    continue
                if self._last_input_frame_monotonic <= 0:
                    continue
                gap = _time.monotonic() - self._last_input_frame_monotonic
                if gap < self._silence_gap_s:
                    continue
                # Quiet long enough — close the activity window.
                try:
                    await self.backend.send_activity_end()
                    self._activity_open = False
                    self._activity_ends_sent += 1
                    logger.info(
                        "hermes-s2s: silence watchdog closed activity "
                        "(gap=%.2fs after %d frames; starts=%d ends=%d)",
                        gap,
                        # buffer counts dropped frames as well, but a useful
                        # lower bound for "frames in this utterance" is the
                        # cumulative emitted count delta — we don't track that
                        # per-utterance so just log the totals.
                        self.buffer.stats().get("frames_emitted", 0),
                        self._activity_starts_sent,
                        self._activity_ends_sent,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.exception("backend.send_activity_end failed")
                    # Don't clear _activity_open on error — let the next
                    # successful send do it.
        except asyncio.CancelledError:
            raise

    async def _stats_heartbeat(self) -> None:
        """Log bridge stats every 10s while running. Cheap, INFO level.

        Surfaces frame-flow data without requiring DEBUG logs. If you see
        ``frames_in=0`` after speaking for 5+ seconds, the input shim never
        delivered frames. If ``frames_in>0`` but ``frames_out=0``, the
        backend isn't producing audio (check setup config / VAD).
        """
        prev_in = -1
        prev_emitted = -1
        prev_underflow = -1
        try:
            while True:
                await asyncio.sleep(10.0)
                try:
                    s = self.stats()
                except Exception:  # noqa: BLE001
                    continue
                # Only log when something actually changed since the last
                # heartbeat — keeps idle bridges from spamming.
                if (
                    s.get("queue_depth_in", 0) > 0
                    or s.get("frames_emitted", 0) != prev_emitted
                    or s.get("frames_underflow", 0) != prev_underflow
                    or s.get("dropped_input", 0) > 0
                ):
                    logger.info(
                        "bridge stats: q_in=%s q_out=%s frames_emitted=%s "
                        "frames_underflow=%s dropped_in=%s tool_tasks=%s",
                        s.get("queue_depth_in"),
                        s.get("queue_depth_out"),
                        s.get("frames_emitted"),
                        s.get("frames_underflow"),
                        s.get("dropped_input"),
                        s.get("tool_tasks_in_flight"),
                    )
                    prev_in = s.get("queue_depth_in", 0)
                    prev_emitted = s.get("frames_emitted", 0)
                    prev_underflow = s.get("frames_underflow", 0)
        except asyncio.CancelledError:
            raise

    async def _pump_input(self) -> None:
        """Drain input queue -> resample to backend rate -> backend.send_audio_chunk.

        On every successful pop_input we (a) update _last_input_frame_monotonic
        (drives the silence watchdog that fires activity_end) and (b) emit
        activity_start if no activity is currently open. Both are wired for
        the manual-VAD case (Gemini Live with AAD disabled); for backends with
        a working server-side VAD over Discord input the activity_* calls are
        no-ops and this is just bookkeeping.
        """
        import time as _time

        while True:
            try:
                _user_id, pcm = await self.buffer.pop_input()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("pop_input failed")
                await asyncio.sleep(0.005)
                continue

            # Mark new utterance start if we don't have one open.
            if not self._activity_open:
                try:
                    await self.backend.send_activity_start()
                    self._activity_open = True
                    self._activity_starts_sent += 1
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.exception("backend.send_activity_start failed")
                # 0.4.2 S1: new user utterance = abandon any in-flight
                # ARIA output. Reset output resampler filter state so the
                # next chunk doesn't start with stale FIR taps from the
                # previous (now-cancelled) reply.
                self._reset_out_resamplers()
                # 0.4.2 S3 (audit #21): clear buffered output frames so
                # the user doesn't hear ~300ms of stale ARIA audio after
                # interrupting. Fade-in on next non-silence frame keeps
                # the resumption clean (Fix B already wired).
                dropped = self.buffer.clear_output()
                if dropped > 0:
                    logger.debug(
                        "barge-in: dropped %d queued output frames "
                        "(~%dms of stale audio)",
                        dropped,
                        dropped * 20,  # 20ms per frame at 48kHz stereo
                    )
            self._last_input_frame_monotonic = _time.monotonic()

            try:
                # resample_pcm handles equal-rate as a fast path and also
                # performs the stereo→mono mixdown Discord input always
                # requires, so one call covers both branches.
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
                # 0.4.2 S1: prefer streaming resampler if soxr is available.
                # Path: backend mono PCM s16le @ rate -> mono PCM s16le @ 48k
                # -> stereo upmix (numpy.repeat). soxr is mono-channel for
                # this stream (we don't waste CPU running stereo through
                # the same filter twice).
                if self._out_resampler_cache is not None:
                    rs = self._out_resampler_cache.get(rate, DISCORD_SAMPLE_RATE, 1)
                    mono_48k = rs.process(pcm)
                    if not mono_48k:
                        # Filter delay swallowed this chunk (first ~1ms of
                        # stream). Will surface in next chunk; not a click.
                        return
                    # Upmix mono int16 -> stereo by interleaving each sample.
                    # numpy.repeat with axis=0 (frames axis) duplicates the
                    # frame, then reshape to interleave L/R = same.
                    mono_arr = np.frombuffer(mono_48k, dtype=np.int16)
                    stereo = np.repeat(mono_arr, 2)
                    frame_pcm = stereo.tobytes()
                else:
                    # Fallback path: stateless resample_pcm. Logged once at
                    # init when soxr is missing.
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
            # ADR-0008 §3: multiple tool_calls from a single turn run in
            # PARALLEL; their results MUST be injected back in the order
            # the model emitted them. Dispatch as a task so a slow tool
            # doesn't stall subsequent events, and chain in-order result
            # injection via an asyncio.Lock-serialized injector.
            task = asyncio.create_task(
                self._run_and_inject_tool(call_id, name, args),
                name=f"bridge.tool_call[{call_id or name}]",
            )
            self._tool_tasks.add(task)
            task.add_done_callback(self._tool_tasks.discard)
        elif etype == "error":
            logger.warning("backend error event: %r", payload)
        # W3b M3.3 — route realtime transcripts to the optional sink
        # installed by discord_bridge when a thread is available. The
        # backend emits ONE event type for both roles, distinguished by
        # payload['role']:
        #   RealtimeEvent(type="transcript_partial",
        #                 payload={"text": ..., "role": "user"|"assistant"})
        #   RealtimeEvent(type="transcript_final",
        #                 payload={"role": "user"|"assistant"})    # no text
        # Do NOT access event.text / event.final as attributes — those
        # are not attributes on RealtimeEvent; they live inside payload.
        elif etype == "transcript_partial" and self._transcript_sink:
            role = payload.get("role", "assistant")
            text = payload.get("text", "")
            if text:
                try:
                    self._transcript_sink(role=role, text=text, final=False)
                except Exception:  # noqa: BLE001 — sink must not break pump
                    logger.exception("transcript sink raised on partial")
        elif etype == "transcript_final" and self._transcript_sink:
            role = payload.get("role", "assistant")
            try:
                self._transcript_sink(role=role, text="", final=True)
            except Exception:  # noqa: BLE001 — sink must not break pump
                logger.exception("transcript sink raised on final")
        # session_resumed: still ignored for 0.4.0.

    async def _run_and_inject_tool(
        self, call_id: str, name: str, args: dict
    ) -> None:
        """Run a tool_call in parallel with others, inject result in emission order.

        Each dispatched tool gets a sequence number; a shared injection
        gate ensures inject_tool_result calls happen in the original order
        even if later-dispatched tools finish first.
        """
        # Claim emission order under a lock so concurrent dispatches serialize
        # their sequence assignment correctly. Lazily create loop-bound
        # primitives on first call.
        if self._tool_seq_lock is None:
            self._tool_seq_lock = asyncio.Lock()
            self._tool_seq_cond = asyncio.Condition()
        async with self._tool_seq_lock:
            my_seq = self._tool_seq_next_dispatch
            self._tool_seq_next_dispatch += 1

        try:
            result = await self.tool_bridge.handle_tool_call(
                self.backend, call_id, name, args
            )
        except Exception:  # noqa: BLE001
            logger.exception("tool_bridge.handle_tool_call raised")
            # Advance the injection pointer even on failure so later tools
            # aren't blocked waiting for a result that will never arrive.
            async with self._tool_seq_cond:
                while self._tool_seq_next_inject != my_seq:
                    await self._tool_seq_cond.wait()
                self._tool_seq_next_inject += 1
                self._tool_seq_cond.notify_all()
            return

        # Wait until it's our turn to inject (preserves emission order).
        async with self._tool_seq_cond:
            while self._tool_seq_next_inject != my_seq:
                await self._tool_seq_cond.wait()
            try:
                await self.backend.inject_tool_result(call_id, result)
            except Exception:  # noqa: BLE001
                logger.exception("backend.inject_tool_result raised")
            self._tool_seq_next_inject += 1
            self._tool_seq_cond.notify_all()
