"""Voice-mode dispatch primitives for hermes-s2s 0.4.0.

This package houses the mode enum, router, session protocol, and
concrete session subclasses. WAVE 1a (this module + ``modes.py`` +
``sessions.py``) delivers the shared skeleton; WAVE 1b adds the four
concrete session classes; WAVE 1c adds the factory + capability gate.

Public exports intentionally cover only the building blocks consumers
of this package need — concrete session classes and the factory are
imported from their own modules once they land.
"""

from .modes import ModeRouter, ModeSpec, VoiceMode
from .sessions import (
    AsyncExitStackBaseSession,
    InvalidTransition,
    SessionState,
    VoiceSession,
)

__all__ = [
    "VoiceMode",
    "ModeSpec",
    "ModeRouter",
    "VoiceSession",
    "AsyncExitStackBaseSession",
    "SessionState",
    "InvalidTransition",
]
