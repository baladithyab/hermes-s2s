"""Discord AudioSource subclass that pulls 20 ms frames from a BridgeBuffer.

The discord.py library ships its player on a dedicated ``threading.Thread``
(see research/07 and research/09). ``AudioSource.read()`` is invoked on that
thread on a steady 20 ms cadence and MUST return exactly 3840 bytes of
48 kHz s16le stereo PCM (or ``b""`` to stop playback — which we never want).

We lazy-import ``discord`` so this module can be imported in tests / CI
environments that don't have discord.py installed.
"""

from __future__ import annotations

from typing import Any

from hermes_s2s._internal.audio_bridge import BridgeBuffer

_cached_cls: Any = None


def _build_queued_pcm_source_cls() -> Any:
    """Lazy-build a subclass of ``discord.AudioSource``.

    Raises ImportError with a helpful message if discord.py is missing.
    The built class is cached so repeated instantiations share the type.
    """
    global _cached_cls
    if _cached_cls is not None:
        return _cached_cls
    try:
        import discord  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover - exercised when discord absent
        raise ImportError(
            "discord.py is required for QueuedPCMSource. "
            "Install Hermes (which depends on discord.py) or add "
            "'discord.py' to your environment."
        ) from e

    class _QueuedPCMSourceImpl(discord.AudioSource):
        """Concrete AudioSource backed by a BridgeBuffer."""

        def __init__(self, buffer: BridgeBuffer) -> None:
            self.buffer = buffer

        def read(self) -> bytes:  # called from discord.py player thread
            return self.buffer.read_frame()

        def is_opus(self) -> bool:
            return False

        def cleanup(self) -> None:
            return None

    _cached_cls = _QueuedPCMSourceImpl
    return _cached_cls


class QueuedPCMSource:
    """Factory wrapper — ``QueuedPCMSource(buffer)`` returns a real
    ``discord.AudioSource`` subclass instance with our behaviour.

    Using a factory (via ``__new__``) lets this module be imported without
    discord.py on the path; the dependency is only resolved at instantiation.
    """

    def __new__(cls, buffer: BridgeBuffer) -> Any:
        impl_cls = _build_queued_pcm_source_cls()
        return impl_cls(buffer)
