"""Wakeword-anchored meta-command grammar for voice mode (WAVE 4a M4.1).

``MetaCommandSink`` recognizes a small, stable set of spoken meta-commands
(``/new``, ``/compress``, ``/title``, ``/branch``, ``stop_speaking``,
``/clear``) that users can issue mid-conversation to control the session
without routing them through the LLM.

Design notes (per docs/plans/wave-0.4.0-rearchitecture.md WAVE 4a,
ADR-0014, research-15 §3):

- Wakeword-anchored: an utterance MUST begin with the wakeword (default
  ``hey aria``) to be considered for meta-command matching. This prevents
  substring false positives like "I should start a new feature on the
  dashboard" from triggering ``/new``.
- Filler-token tolerance: up to three fillers (``um``, ``uh``, ``like``,
  ``you know``, ``please``, ``just``) between the wakeword and the verb
  are stripped before matching.
- Trailing-text tolerant on ``/new``: trailing text after the
  "new session" phrase is captured as ``extra`` and the dispatcher uses
  it as a ``/title`` hint.
- 80-char cap on user-supplied captures (title, extra) enforced at the
  regex level.
- Stable rules, no LLM matcher — predictable latency, no probability of
  runaway side-effects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# Fillers tolerated between wakeword and verb. ``you know`` is a two-word
# filler and must be listed first in the alternation so the regex engine
# doesn't greedily eat "you" as a standalone token and leave "know" behind.
_FILLERS = r"(?:you know|um|uh|like|please|just)"

# At most three fillers between wakeword and verb. Whitespace between
# fillers is collapsed.
_FILLER_PREFIX = rf"(?:{_FILLERS}\s+){{0,3}}"


@dataclass
class MetaMatch:
    """Result of matching an utterance against the meta-command grammar.

    Attributes:
        verb: Canonical verb. One of ``'new'``, ``'compress'``, ``'title'``,
            ``'branch'``, ``'stop_speaking'``, ``'clear'``.
        args: Structured arguments parsed from the utterance (e.g.
            ``{'title': 'ARIA brainstorm'}`` or
            ``{'extra': 'about React performance'}``).
        original_text: The original (unlowercased, unstripped) utterance.
        consumed: Whether the caller should consume the utterance and
            refrain from routing it to the LLM. Always ``True`` for now;
            retained as a field so future non-consuming "hint" matches
            can opt-out without breaking callers.
    """

    verb: str
    args: dict[str, str] = field(default_factory=dict)
    original_text: str = ""
    consumed: bool = True


class MetaCommandSink:
    """Regex-based meta-command matcher.

    Usage::

        sink = MetaCommandSink(wakeword="hey aria")
        match = sink.match("hey aria start a new session about React")
        if match is not None:
            # match.verb == 'new'
            # match.args == {'extra': 'about React'}
            ...
    """

    def __init__(self, wakeword: str = "hey aria") -> None:
        self._wakeword = wakeword.strip().lower()
        # Patterns compiled lazily on first ``match()`` call — keeps
        # import cheap and avoids work for callers that never match.
        self._patterns: Optional[list[tuple[str, re.Pattern[str]]]] = None

    # ------------------------------------------------------------------
    # Pattern table
    # ------------------------------------------------------------------
    def _compile_patterns(self) -> list[tuple[str, re.Pattern[str]]]:
        """Compile the ordered pattern table.

        Order matters: the first pattern to match wins. In particular,
        ``stop_speaking`` is intentionally placed near the end so that
        a future "stop the session" phrasing wouldn't accidentally
        preempt a more specific verb — all currently-defined specific
        verbs have their own anchors and cannot collide with ``stop``.
        """

        # Each capture cap uses ``.{1,80}`` to enforce the 80-char
        # user-supplied arg cap at the regex level.
        raw: list[tuple[str, str]] = [
            # /new — trailing ``extra`` optional (used as /title hint).
            (
                "new",
                r"^(?:start|begin|open)\s+(?:a\s+|an\s+)?new\s+"
                r"(?:session|chat|conversation)"
                r"(?:\b(?P<extra>.{0,80}))?$",
            ),
            # /compress
            (
                "compress",
                r"^(?:compress|condense|summarize)\s+(?:the\s+|my\s+)?context\b.*$",
            ),
            # /title <title> — anchored; captures 1-80 chars.
            (
                "title",
                r"^(?:title|name)\s+(?:this|the)\s+(?:session|chat)\s+"
                r"(?:as\s+|to\s+)?(?P<title>.{1,80})$",
            ),
            # /branch
            (
                "branch",
                r"^(?:branch|fork)\s+(?:off\s+|from\s+)?"
                r"(?:here|now|this point)\b.*$",
            ),
            # /clear (alias for /new) — placed BEFORE stop_speaking so
            # "clear the context" wins over any future "clear" stop-like
            # reading; also more specific than the bare stop verb.
            (
                "clear",
                r"^(?:clear|reset)\s+(?:the\s+|my\s+)?(?:context|session)\b.*$",
            ),
            # stop_speaking — interrupts current TTS / realtime audio
            # output. Does NOT dispatch a slash command.
            (
                "stop_speaking",
                r"^(?:stop|cancel|hush|quiet)\b.*$",
            ),
        ]

        return [
            (verb, re.compile(pattern, re.IGNORECASE))
            for verb, pattern in raw
        ]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def match(self, text: str) -> Optional[MetaMatch]:
        """Try to match ``text`` as a meta-command.

        Returns ``None`` if the utterance does not begin with the
        wakeword, or if no pattern matches after wakeword + filler
        stripping.
        """

        if not text:
            return None

        original_text = text
        stripped = text.strip()
        if not stripped:
            return None

        # Wakeword check — must be at the start of the utterance.
        # Case-insensitive compare but we preserve original casing in
        # the remainder so user-supplied captures (title, extra) keep
        # the original case.
        lowered = stripped.lower()
        if not lowered.startswith(self._wakeword):
            return None

        # Strip wakeword. Require a word boundary immediately after so
        # a wakeword of "hey aria" does not accidentally match
        # "hey arianas".
        after_wake = stripped[len(self._wakeword):]
        if after_wake and not after_wake[0].isspace():
            return None
        remainder = after_wake.strip()
        if not remainder:
            return None

        # Strip up to three filler tokens between wakeword and verb.
        # Case-insensitive replacement; captures later use ``re.IGNORECASE``
        # on the per-pattern match so casing in the remainder is preserved.
        remainder = re.sub(
            rf"^{_FILLER_PREFIX}",
            "",
            remainder,
            count=1,
            flags=re.IGNORECASE,
        ).strip()
        if not remainder:
            return None

        # Lazy-compile patterns.
        if self._patterns is None:
            self._patterns = self._compile_patterns()

        for verb, pattern in self._patterns:
            m = pattern.match(remainder)
            if m is None:
                continue

            args: dict[str, str] = {}
            groups = m.groupdict()
            if verb == "new":
                extra = (groups.get("extra") or "").strip()
                if extra:
                    args["extra"] = extra
            elif verb == "title":
                title = (groups.get("title") or "").strip()
                # Defensive: title should be non-empty (the regex requires
                # at least 1 char, but strip could yield empty if capture
                # was all whitespace).
                if title:
                    args["title"] = title
                else:
                    # Title pattern matched but captured whitespace only —
                    # treat as no match.
                    continue

            return MetaMatch(
                verb=verb,
                args=args,
                original_text=original_text,
                consumed=True,
            )

        return None


__all__ = ["MetaMatch", "MetaCommandSink"]
