"""JSON-Schema tool definitions for hermes_meta_* voice meta-commands.

Per WAVE 4b M4.3 (ADR-0014, research-15 §4):
These tools are ALWAYS exposed in the realtime tool manifest (not in any
bucket — see hermes_s2s._internal.tool_bridge.build_tool_manifest). They
let the voice LLM issue lifecycle commands against its own session:
new session, set title, compress context, branch session.

**Deferred to 0.4.1**: ``hermes_meta_resume_session`` — the user-pick
disambiguation flow (LLM guesses the wrong session from fuzzy voice query
→ loads private data into the wrong conversation) is a data-hazard that
needs a synchronous "which session?" voice-prompt flow. The ``ask``-bucket
pending-confirm infrastructure (W4a) is the foundation for resume in
0.4.1. Shipping 4 tools in 0.4.0.

The schemas are plain JSON Schema (draft-07 subset) with ``name``,
``description``, ``parameters`` keys — matching the shape used by the
OpenAI Realtime API and the Gemini Live API tool manifest.
"""

from __future__ import annotations

import copy

# ---------------------------------------------------------------------------
# Tool schemas — 4 tools for 0.4.0. Resume deferred to 0.4.1.
# ---------------------------------------------------------------------------

META_TOOLS: list[dict] = [
    {
        "name": "hermes_meta_new_session",
        "description": (
            "Start a new session, ending the current conversation context. "
            "Use this when the user says something like 'new chat' or "
            "'start over'."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "hermes_meta_title_session",
        "description": (
            "Set the title of the current session. Use this when the user "
            "says something like 'call this conversation X' or "
            "'title this session Y'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "The new session title (max 80 chars).",
                    "maxLength": 80,
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "hermes_meta_compress_context",
        "description": (
            "Compress the current conversation context to free up space. "
            "Use this when the user says something like 'summarize what "
            "we've talked about' or 'compress the context' — typically "
            "when the session is getting long."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "hermes_meta_branch_session",
        "description": (
            "Branch off the current session into a new conversation. The "
            "current session stays intact; a new session is forked from "
            "the current point with an optional name. Use this when the "
            "user says 'branch this' or 'let's explore a tangent'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Optional name for the new branch (max 80 chars).",
                    "maxLength": 80,
                },
            },
            "required": [],
        },
    },
]


def get_meta_tools() -> list[dict]:
    """Return a deep copy of META_TOOLS.

    Deep copy prevents callers (e.g. ``build_tool_manifest``) from
    mutating the module-level schema list by accident. The tool manifest
    is typically serialized into a backend request body, so hand out
    fresh dicts each call.
    """
    return copy.deepcopy(META_TOOLS)
