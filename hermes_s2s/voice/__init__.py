"""Voice-mode dispatch primitives for hermes-s2s 0.4.0.

This package houses the mode enum, router, session protocol, concrete
session subclasses, capability gate, and factory. WAVE 1a (this module
+ ``modes.py`` + ``sessions.py``) delivered the shared skeleton; WAVE 1b
added the four concrete session classes; WAVE 1c adds the factory and
capability gate.

Public exports intentionally cover only the building blocks consumers
of this package need — individual session classes are importable from
their own modules for callers that need direct access.
"""

from .capabilities import (
    CapabilityCheck,
    CapabilityError,
    ModeRequirements,
    check_requirements,
    requirements_for,
)
from .factory import VoiceSessionFactory
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
    "ModeRequirements",
    "CapabilityCheck",
    "CapabilityError",
    "check_requirements",
    "requirements_for",
    "VoiceSessionFactory",
]
