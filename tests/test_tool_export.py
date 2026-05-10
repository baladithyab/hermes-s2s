"""CI fence for the tool-export policy (WAVE 4b M4.4; 0.4.0 final-review P0-1).

These tests ensure that every Hermes core tool listed in the canonical
``_HERMES_CORE_TOOLS`` source-of-truth (``toolsets.py``) is classified
into exactly one of ``DEFAULT_EXPOSED``, ``ASK``, or ``DENY`` in
``hermes_s2s._internal.tool_bridge``.

**Pre-0.4.0 final-review regression:** the CI fence previously used a
hardcoded snapshot of the core-tools list that silently drifted from
reality (Phase-8 final review P0-1 / correctness #5 / security #1). The
hardcoded list named phantom tools that never existed (``comfyui``,
``spotify_play``, ``xurl_read``, ``ha_state_read``, …) and missed real
ones (``web_extract``, ``browser_back``, ``browser_cdp``, ``todo``,
``ha_list_entities``, …), so the fence passed vacuously.

**Current approach:** the fence imports ``_HERMES_CORE_TOOLS`` directly
from the Hermes core checkout at test time (lazy import via the
user's ``~/.hermes/hermes-agent`` path). If Hermes core is not
available on this host (e.g. CI runner without the core sidecar), the
test is skipped rather than papered-over — we'd rather catch the
drift on developer machines and release runners that have core
installed than silently green.

**Maintenance note:** when Hermes core adds or renames a tool, this
test will fail loudly (that's the point). Fix it by adding the new
tool name to the correct bucket in ``tool_bridge.py`` — do NOT
paper over the fence.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from hermes_s2s._internal.tool_bridge import ASK, DEFAULT_EXPOSED, DENY


# ---------------------------------------------------------------------------
# Lazy import of the canonical core-tools list.
# ---------------------------------------------------------------------------


def _load_core_tools() -> list[str]:
    """Import ``_HERMES_CORE_TOOLS`` from the Hermes core checkout.

    Searches a small, deterministic set of candidate paths (env override
    first, then the standard ``~/.hermes/hermes-agent`` location) and
    returns the list of tool names. Raises ``FileNotFoundError`` if no
    candidate path works — caller decides whether to skip or fail.
    """
    candidates: list[Path] = []
    env_override = os.environ.get("HERMES_CORE_PATH")
    if env_override:
        candidates.append(Path(env_override))
    candidates.append(Path.home() / ".hermes" / "hermes-agent")

    for candidate in candidates:
        toolsets_py = candidate / "toolsets.py"
        if toolsets_py.is_file():
            # Prepend so our import wins over anything else named
            # ``toolsets`` on sys.path. Pop it back off after importing
            # to avoid polluting other tests.
            inserted = str(candidate)
            sys.path.insert(0, inserted)
            try:
                # Defensive: if a stale ``toolsets`` module is cached
                # from a prior test run, evict it so we re-import from
                # the Hermes-core path.
                sys.modules.pop("toolsets", None)
                import toolsets as _core_toolsets  # type: ignore[import-not-found]

                return list(_core_toolsets._HERMES_CORE_TOOLS)
            finally:
                try:
                    sys.path.remove(inserted)
                except ValueError:
                    pass

    raise FileNotFoundError(
        "Hermes core checkout not found; set HERMES_CORE_PATH or install "
        "hermes-agent at ~/.hermes/hermes-agent"
    )


@pytest.fixture(scope="module")
def core_tools() -> list[str]:
    """Yield the canonical ``_HERMES_CORE_TOOLS`` list, or skip the module."""
    try:
        return _load_core_tools()
    except (FileNotFoundError, ImportError, AttributeError) as exc:
        pytest.skip(
            f"Hermes core not available; CI fence not enforceable on this "
            f"checkout ({exc})"
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_every_core_tool_classified(core_tools: list[str]) -> None:
    """Every known core tool must appear in at least one bucket.

    This is the CI fence for the silent-grant security regression. If
    this test fails, do NOT paper over it — add the new tool name to
    the correct bucket (``DEFAULT_EXPOSED``, ``ASK``, or ``DENY``) in
    ``tool_bridge.py``. Write/privileged/code-exec tools go in ``DENY``;
    pure-output tools with no user-data or cost amplification go in
    ``DEFAULT_EXPOSED``. The ``ASK`` bucket is provisionally empty for
    0.4.0 and will be repopulated in 0.4.1 once the voice-confirm flow
    lands — for 0.4.0, user-data-read tools (file reads, memory,
    session_search, HA reads, browser snapshots, skills listings) all
    live in ``DENY`` as a fail-closed posture.
    """
    classified = DEFAULT_EXPOSED | ASK | DENY
    unclassified = [t for t in core_tools if t not in classified]
    assert not unclassified, (
        f"The following core tools are not classified into any of "
        f"DEFAULT_EXPOSED/ASK/DENY in hermes_s2s/_internal/tool_bridge.py: "
        f"{unclassified}. Add each to the appropriate bucket — do NOT "
        f"leave them unclassified (build_tool_manifest fail-closes and "
        f"will silently drop them from the voice tool manifest)."
    )


def test_no_phantom_bucket_entries(core_tools: list[str]) -> None:
    """No bucket may contain a tool name that doesn't exist in core.

    Catches the other half of the drift class: bucket entries that
    refer to tools core never shipped (``ha_state_read``, ``comfyui``,
    ``spotify_play``, ``xurl_*``, …). These phantom entries create the
    illusion of coverage without actually guarding anything.
    """
    core_set = set(core_tools)
    classified = DEFAULT_EXPOSED | ASK | DENY
    phantoms = sorted(classified - core_set)
    assert not phantoms, (
        f"The following bucket entries in tool_bridge.py refer to tools "
        f"that do NOT exist in Hermes core's _HERMES_CORE_TOOLS: "
        f"{phantoms}. Remove them — they're dead weight that hides real "
        f"gaps."
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


def test_ask_bucket_empty_for_0_4_0() -> None:
    """0.4.0 ships ASK empty; voice-confirm flow deferred to 0.4.1.

    Per Phase-8 final review P0-2: the synchronous voice yes/no prompt
    that ASK was meant to guard isn't implemented. Until 0.4.1 lands
    that flow, ASK must stay empty (ASK candidates live in DENY as a
    fail-closed posture). When 0.4.1 repopulates ASK, update or delete
    this test.
    """
    assert ASK == set(), (
        f"ASK bucket must be empty for 0.4.0 (the voice-confirm flow "
        f"is deferred to 0.4.1 per ADR-0014 0.4.0 implementation note). "
        f"Current contents: {sorted(ASK)}"
    )
