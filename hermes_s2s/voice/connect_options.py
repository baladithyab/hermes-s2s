"""
ConnectOptions — forward-compatible parameter envelope for realtime backends.

Why this exists
---------------
Pre-0.4.2 ``_BaseRealtimeBackend.connect()`` took 3 positional kwargs:
``(system_prompt, voice, tools)``. v0.4.2 needs to add ``history``.
v0.5.0 will need ``tier``, ``confirm_policy``, ``language_code``,
``capability_probe``, etc. Threading 6+ kwargs through 4 layers
(factory → session → bridge → backend) is the smell architecture
review caught (research/critique-architecture §1).

This module introduces a frozen dataclass that bundles every connect
option in one place. The protocol becomes::

    async def connect(self, opts: ConnectOptions) -> None: ...

To preserve back-compat with existing tests (which call
``backend.connect(prompt, voice, tools)`` positionally), backends keep
a shim that adapts positional triples into a ``ConnectOptions``. That
adapter lives on ``_BaseRealtimeBackend`` (see
``providers/realtime/__init__.py``) so providers don't each duplicate it.

v0.5.0 adds tier/confirm fields here without touching any call site.
"""

from __future__ import annotations

import dataclasses
from typing import Any, List, Mapping, Optional


@dataclasses.dataclass(frozen=True)
class ConnectOptions:
    """Bundle of options passed to ``RealtimeBackend.connect``.

    Fields
    ------
    system_prompt
        Persona / system-level instruction string. Backends layer
        provider-specific anchors on top (e.g. Gemini's language
        anchor, OpenAI's tool-disclaimer suffix when ``history`` is
        non-empty).
    voice
        Voice ID. Provider-specific (Gemini ``"Aoede"``, OpenAI
        ``"alloy"`` / ``"verse"``).
    tools
        Hermes-format tool declarations. Empty list = no tools.
    history
        OpenAI-format ``[{"role", "content"}, ...]`` prior conversation
        turns. ``None`` = no history injection. ``[]`` is treated the
        same as ``None`` (no-op). Backends route this through their
        provider-specific history primitive
        (Gemini ``clientContent``, OpenAI ``conversation.item.create``).
    extras
        Provider-specific opaque options (mock URL overrides,
        experimental flags). Read by individual backends; unknown keys
        ignored.

    Reserved for v0.5.0 (not yet honored)
    -------------------------------------
    tier
        ``"read" | "act" | "meta" | "off"`` — tool exposure tier.
    confirm_policy
        Voice-confirm flow configuration for ASK-bucket tool calls.
    language_code
        ``"auto"`` enables provider auto-detection; otherwise a BCP-47
        code overrides the backend default.
    """

    system_prompt: str
    voice: str
    tools: List[dict]
    history: Optional[List[dict]] = None
    extras: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    # --- v0.5.0 reserved fields (parsed but not yet honored) ---
    tier: Optional[str] = None
    confirm_policy: Optional[Any] = None
    language_code: Optional[str] = None

    @classmethod
    def from_positional(
        cls,
        system_prompt: str,
        voice: str,
        tools: List[dict],
        **kw: Any,
    ) -> "ConnectOptions":
        """Build from the legacy positional triple plus optional kwargs.

        Used by ``_BaseRealtimeBackend.connect`` shim to keep
        pre-v0.4.2 callers working without modification.
        """
        return cls(
            system_prompt=system_prompt,
            voice=voice,
            tools=list(tools or []),
            history=kw.get("history"),
            extras=kw.get("extras", {}),
            tier=kw.get("tier"),
            confirm_policy=kw.get("confirm_policy"),
            language_code=kw.get("language_code"),
        )


__all__ = ["ConnectOptions"]
