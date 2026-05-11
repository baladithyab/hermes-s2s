"""Pure-text formatters for the ``/s2s`` slash command.

Decoupled from discord.py / python-telegram-bot so unit tests don't need
either dependency installed. The Discord (``voice/slash.py``) and Telegram
(``voice/slash_telegram.py``, Wave 3) presenters import from here — that
way the two surfaces can't drift.

Every function here returns ``str`` and never touches I/O.

See ``docs/plans/wave-0.5.0-s2s-configure.md`` Task 2.1.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


__all__ = [
    "format_status",
    "format_help",
    "format_doctor_summary",
]


def format_status(
    *,
    active_mode: str,
    config_mode: str,
    realtime_provider: str,
    stt_provider: str,
    tts_provider: str,
    guild_id: int,
    channel_id: int,
    per_channel_record: Mapping[str, str],
) -> str:
    """Render the ``/s2s status`` reply as Markdown.

    ``per_channel_record`` is the dict the override store returned for this
    ``(guild_id, channel_id)`` — empty means "no channel override, using
    the global config".
    """
    has_override = bool(per_channel_record)
    header = f"**S2S status — guild `{guild_id}` channel `{channel_id}`**"
    mode_line = f"  • Active mode: `{active_mode}`"
    if has_override:
        mode_line += f" (this channel overrides global `{config_mode}`)"
    else:
        mode_line += " (global default)"
    lines = [
        header,
        mode_line,
        f"  • Realtime provider: `{realtime_provider}`",
        f"  • Cascaded STT: `{stt_provider}`",
        f"  • Cascaded TTS: `{tts_provider}`",
    ]
    if not has_override:
        lines.append("  • _no channel override — using global config_")
    else:
        keys = ", ".join(f"`{k}`" for k in sorted(per_channel_record))
        lines.append(f"  • Channel overrides set: {keys}")
    return "\n".join(lines)


def format_help() -> str:
    """Render the ``/s2s`` subcommand catalogue."""
    return (
        "**`/s2s` subcommands**\n"
        "  `/s2s configure` — open interactive panel\n"
        "  `/s2s status` — show active mode + providers\n"
        "  `/s2s mode <choice>` — switch mode for this channel\n"
        "  `/s2s provider <kind> <name>` — set a provider override\n"
        "  `/s2s test [text]` — TTS smoke test\n"
        "  `/s2s doctor` — preflight checks (deps, keys, WS handshake)\n"
        "  `/s2s reset` — clear this channel's overrides"
    )


def format_doctor_summary(report: Mapping[str, Any]) -> str:
    """Compact 3-5 line summary of a :func:`hermes_s2s.doctor.run_doctor_async`
    report, suitable for an ephemeral Discord reply.

    Shows overall status, pass/warn/fail counts, and the top 3 failing
    checks (with their ``remediation`` text when present). When nothing
    failed the "top failures" block is replaced with "_no failures_".
    """
    checks: Sequence[Mapping[str, Any]] = report.get("checks") or []
    overall = str(report.get("overall_status", "unknown"))

    counts = {"pass": 0, "warn": 0, "fail": 0, "skip": 0}
    failures: list[Mapping[str, Any]] = []
    for c in checks:
        status = str(c.get("status", "")).lower()
        if status in counts:
            counts[status] += 1
        if status == "fail":
            failures.append(c)

    icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}.get(overall.lower(), "•")
    lines = [
        f"{icon} **Doctor — overall: `{overall}`**",
        (
            f"  • {counts['pass']} pass · {counts['warn']} warn · "
            f"{counts['fail']} fail"
            + (f" · {counts['skip']} skip" if counts["skip"] else "")
        ),
    ]

    if not failures:
        lines.append("  • _no failures — all checks passed_")
    else:
        lines.append("  • Top failures:")
        for c in failures[:3]:
            name = c.get("name", "?")
            category = c.get("category", "?")
            message = c.get("message", "")
            remediation = c.get("remediation")
            entry = f"    — `{category}/{name}`: {message}"
            if remediation:
                entry += f"\n        → {remediation}"
            lines.append(entry)
        if len(failures) > 3:
            lines.append(f"  • …and {len(failures) - 3} more failure(s)")

    return "\n".join(lines)
