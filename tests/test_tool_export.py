"""CI fence for the 3-bucket tool-export policy (WAVE 4b M4.4).

These tests make sure that every Hermes core tool known at 0.4.0 ship
time is classified into exactly one of ``DEFAULT_EXPOSED``, ``ASK``, or
``DENY`` in ``hermes_s2s._internal.tool_bridge``. If a new tool is added
to Hermes core without updating the buckets, ``build_tool_manifest``
would silently drop it (fail-closed by design) — this test catches that
at CI time so the bucket tables are kept in sync.

**Maintenance note:** The ``_CORE_TOOLS_AT_0_4_0`` constant is a
hardcoded snapshot of the tool names in
``~/.hermes/hermes-agent/toolsets.py`` at the time of the 0.4.0 release.
When Hermes core adds a new tool, append its name here AND add it to one
of the three buckets in ``tool_bridge.py``. Failing to update one but
not the other will cause this test to fail loudly (that's the point).
"""

from __future__ import annotations

import pytest

from hermes_s2s._internal.tool_bridge import ASK, DEFAULT_EXPOSED, DENY


# ---------------------------------------------------------------------------
# Snapshot of Hermes core tools at 0.4.0 ship time.
#
# Source: /home/codeseys/.hermes/hermes-agent/toolsets.py (_HERMES_CORE_TOOLS).
# Update this list AND the bucket tables in tool_bridge.py together when
# Hermes core adds tools. The test below will fail if this list diverges
# from the union of the three buckets.
# ---------------------------------------------------------------------------

_CORE_TOOLS_AT_0_4_0: list[str] = [
    # IO / filesystem / shell
    "read_file",
    "search_files",
    "write_file",
    "patch",
    "terminal",
    "process",
    "execute_code",
    # Web / search / vision
    "web_search",
    "vision_analyze",
    # Browser automation (CDP)
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_press",
    "browser_scroll",
    "browser_console",
    "browser_get_images",
    # Desktop / delegation / scheduling
    "computer_use",
    "delegate_task",
    "cronjob",
    # Memory / session
    "memory",
    "session_search",
    # Messaging / voice
    "send_message",
    "text_to_speech",
    "clarify",
    # Image generation
    "image_generate",
    "comfyui",
    # Home Assistant
    "ha_state_read",
    "ha_call_service",
    # Kanban
    "kanban_show",
    "kanban_list",
    "kanban_create",
    "kanban_complete",
    "kanban_block",
    "kanban_link",
    "kanban_comment",
    "kanban_heartbeat",
    # Skills
    "skill_manage",
    # Spotify
    "spotify_search",
    "spotify_play",
    "spotify_pause",
    # Integrations — read
    "feishu_doc_read",
    "xurl_read",
    "obsidian_read",
    "youtube_transcript",
    "arxiv_search",
    "gif_search",
    # Integrations — write
    "xurl_post",
    "xurl_dm",
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_every_core_tool_classified() -> None:
    """Every known core tool must appear in at least one bucket.

    This is the CI fence for the silent-grant security regression
    (Phase-8 P0-F1). If this test fails, do NOT paper over it — instead
    add the new tool name to the correct bucket (``DEFAULT_EXPOSED``,
    ``ASK``, or ``DENY``) in ``tool_bridge.py``. Tools that involve
    user-data reads or external-call cost go in ``ASK``; tools that
    mutate state or execute code go in ``DENY``.
    """
    classified = DEFAULT_EXPOSED | ASK | DENY
    unclassified = [t for t in _CORE_TOOLS_AT_0_4_0 if t not in classified]
    assert not unclassified, (
        f"The following core tools are not classified into any of "
        f"DEFAULT_EXPOSED/ASK/DENY in hermes_s2s/_internal/tool_bridge.py: "
        f"{unclassified}. Add each to the appropriate bucket — do NOT "
        f"leave them unclassified (build_tool_manifest fail-closes and "
        f"will silently drop them from the voice tool manifest)."
    )


def test_no_overlap_between_buckets() -> None:
    """No tool name may appear in more than one bucket."""
    pairs = [
        ("DEFAULT_EXPOSED", "ASK", DEFAULT_EXPOSED & ASK),
        ("DEFAULT_EXPOSED", "DENY", DEFAULT_EXPOSED & DENY),
        ("ASK", "DENY", ASK & DENY),
    ]
    overlaps = {(a, b): list(inter) for (a, b, inter) in pairs if inter}
    assert not overlaps, (
        f"Bucket overlap detected: {overlaps}. Each tool must be in "
        f"exactly one bucket."
    )


def test_buckets_are_sets_of_strings() -> None:
    """Sanity check: the three bucket constants are sets of non-empty strings."""
    for name, bucket in [
        ("DEFAULT_EXPOSED", DEFAULT_EXPOSED),
        ("ASK", ASK),
        ("DENY", DENY),
    ]:
        assert isinstance(bucket, set), f"{name} must be a set"
        for entry in bucket:
            assert isinstance(entry, str) and entry, (
                f"{name} contains a non-string or empty entry: {entry!r}"
            )
