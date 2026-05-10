"""Voice-mode dispatch primitives for hermes-s2s 0.4.0.

This package houses the mode enum, router, session protocol, and
concrete session subclasses. WAVE 1a / M1.1 (this commit) lands the
mode enum + router; M1.2 adds the VoiceSession protocol; WAVE 1b adds
the four concrete session classes; WAVE 1c adds the factory +
capability gate.
"""

from .modes import ModeRouter, ModeSpec, VoiceMode

__all__ = [
    "VoiceMode",
    "ModeSpec",
    "ModeRouter",
]
