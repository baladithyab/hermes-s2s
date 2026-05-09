"""Pre-flight readiness check for hermes-s2s (ADR-0009 §2).

Runs a comprehensive set of checks across six categories:
    - configuration        (s2s.mode + provider)
    - python_deps          (moonshine_onnx, kokoro, scipy, websockets, discord, soundfile)
    - system_deps          (ffmpeg, libopus, espeak-ng)
    - api_keys             (GEMINI_API_KEY, OPENAI_API_KEY — active-provider-sensitive)
    - hermes_integration   (HERMES_S2S_MONKEYPATCH_DISCORD, DISCORD_BOT_TOKEN, DISCORD_ALLOWED_USERS)
    - backend_connectivity (5s WS probe to the configured realtime backend, opt-out via probe=False)

All expensive/optional imports are lazy — this module itself only imports the
standard library at load time.
"""

from __future__ import annotations

import asyncio
import ctypes.util
import importlib.util
import json
import os
import shutil
import sys
from typing import Any, Dict, List, Optional

# Status constants
_PASS = "pass"
_WARN = "warn"
_FAIL = "fail"
_SKIP = "skip"

# ANSI color codes
_C_GREEN = "\033[32m"
_C_YELLOW = "\033[33m"
_C_RED = "\033[31m"
_C_DIM = "\033[2m"
_C_RESET = "\033[0m"

_CATEGORIES = [
    "configuration",
    "python_deps",
    "system_deps",
    "api_keys",
    "hermes_integration",
    "backend_connectivity",
]


def _check(
    category: str,
    name: str,
    status: str,
    message: str,
    remediation: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "category": category,
        "name": name,
        "status": status,
        "message": message,
        "remediation": remediation,
    }


# ---------------------------------------------------------------------------
# Individual check groups
# ---------------------------------------------------------------------------


def _configuration_checks(cfg: Any) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    mode = getattr(cfg, "mode", "cascaded")
    checks.append(
        _check(
            "configuration",
            "s2s.mode",
            _PASS,
            f"s2s.mode = {mode}",
            None,
        )
    )
    if mode == "realtime":
        provider = getattr(cfg, "realtime_provider", "") or "(unset)"
        checks.append(
            _check(
                "configuration",
                "s2s.realtime.provider",
                _PASS if provider and provider != "(unset)" else _WARN,
                f"s2s.realtime.provider = {provider}",
                None
                if provider and provider != "(unset)"
                else "Set s2s.realtime.provider in ~/.hermes/config.yaml",
            )
        )
    elif mode == "s2s-server":
        endpoint = getattr(cfg, "server_endpoint", "")
        checks.append(
            _check(
                "configuration",
                "s2s.s2s_server.endpoint",
                _PASS,
                f"s2s.s2s_server.endpoint = {endpoint}",
                None,
            )
        )
    else:
        # cascaded
        stt = getattr(getattr(cfg, "stt", None), "provider", "")
        tts = getattr(getattr(cfg, "tts", None), "provider", "")
        checks.append(
            _check(
                "configuration",
                "s2s.pipeline.stt",
                _PASS,
                f"stt.provider = {stt or '(unset)'}",
                None,
            )
        )
        checks.append(
            _check(
                "configuration",
                "s2s.pipeline.tts",
                _PASS,
                f"tts.provider = {tts or '(unset)'}",
                None,
            )
        )
    return checks


def _python_dep_checks() -> List[Dict[str, Any]]:
    # (module_name, pip_hint, required_for_label)
    deps = [
        ("moonshine_onnx", "pip install hermes-s2s[local-stt]", "local STT"),
        ("kokoro", "pip install hermes-s2s[local-tts]", "local TTS"),
        ("scipy", "pip install scipy", "audio resampling"),
        ("websockets", "pip install hermes-s2s[realtime]", "realtime backends"),
        ("discord", "pip install discord.py", "Discord voice bridge"),
        ("soundfile", "pip install soundfile", "audio I/O"),
    ]
    out: List[Dict[str, Any]] = []
    for mod, pip_hint, label in deps:
        try:
            present = importlib.util.find_spec(mod) is not None
        except (ValueError, ImportError):
            present = False
        out.append(
            _check(
                "python_deps",
                mod,
                _PASS if present else _WARN,
                f"{mod} {'installed' if present else 'NOT installed'} ({label})",
                None if present else pip_hint,
            )
        )
    return out


def _system_dep_checks() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    ffmpeg = shutil.which("ffmpeg")
    out.append(
        _check(
            "system_deps",
            "ffmpeg",
            _PASS if ffmpeg else _WARN,
            f"ffmpeg {'in PATH at ' + ffmpeg if ffmpeg else 'not found in PATH'}",
            None if ffmpeg else "sudo apt install ffmpeg  # or brew install ffmpeg",
        )
    )
    try:
        opus = ctypes.util.find_library("opus")
    except Exception:  # noqa: BLE001
        opus = None
    out.append(
        _check(
            "system_deps",
            "libopus",
            _PASS if opus else _WARN,
            f"libopus {'found (' + str(opus) + ')' if opus else 'not found'}",
            None if opus else "sudo apt install libopus0  # or brew install opus",
        )
    )
    espeak = shutil.which("espeak-ng") or shutil.which("espeak")
    out.append(
        _check(
            "system_deps",
            "espeak-ng",
            _PASS if espeak else _WARN,
            f"espeak-ng {'in PATH at ' + espeak if espeak else 'not found'}",
            None if espeak else "sudo apt install espeak-ng  # (needed for kokoro)",
        )
    )
    return out


def _api_key_checks(cfg: Any) -> List[Dict[str, Any]]:
    """Per ADR-0009: fail if the active realtime provider's key is missing."""
    out: List[Dict[str, Any]] = []
    mode = getattr(cfg, "mode", "cascaded")
    provider = (getattr(cfg, "realtime_provider", "") or "").lower()
    gemini_set = bool(os.environ.get("GEMINI_API_KEY"))
    openai_set = bool(os.environ.get("OPENAI_API_KEY"))

    gemini_required = mode == "realtime" and "gemini" in provider
    openai_required = mode == "realtime" and "openai" in provider

    if gemini_set:
        klen = len(os.environ.get("GEMINI_API_KEY", ""))
        out.append(
            _check(
                "api_keys",
                "GEMINI_API_KEY",
                _PASS,
                f"GEMINI_API_KEY set ({klen} chars)",
                None,
            )
        )
    else:
        out.append(
            _check(
                "api_keys",
                "GEMINI_API_KEY",
                _FAIL if gemini_required else _WARN,
                "GEMINI_API_KEY not set"
                + (" (required for active gemini-live backend)" if gemini_required else " (optional)"),
                "Get a key at https://aistudio.google.com/apikey then set it in ~/.hermes/.env",
            )
        )
    if openai_set:
        klen = len(os.environ.get("OPENAI_API_KEY", ""))
        out.append(
            _check(
                "api_keys",
                "OPENAI_API_KEY",
                _PASS,
                f"OPENAI_API_KEY set ({klen} chars)",
                None,
            )
        )
    else:
        out.append(
            _check(
                "api_keys",
                "OPENAI_API_KEY",
                _FAIL if openai_required else _WARN,
                "OPENAI_API_KEY not set"
                + (" (required for active openai-realtime backend)" if openai_required else " (optional)"),
                "Get a key at https://platform.openai.com/api-keys then set it in ~/.hermes/.env",
            )
        )
    return out


def _hermes_integration_checks(cfg: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    mode = getattr(cfg, "mode", "cascaded")
    patch_flag = os.environ.get("HERMES_S2S_MONKEYPATCH_DISCORD", "") == "1"
    if patch_flag:
        out.append(
            _check(
                "hermes_integration",
                "HERMES_S2S_MONKEYPATCH_DISCORD",
                _PASS,
                "HERMES_S2S_MONKEYPATCH_DISCORD = 1",
                None,
            )
        )
    else:
        status = _WARN if mode == "realtime" else _PASS
        out.append(
            _check(
                "hermes_integration",
                "HERMES_S2S_MONKEYPATCH_DISCORD",
                status,
                "HERMES_S2S_MONKEYPATCH_DISCORD not set"
                + (" (required for realtime voice in Discord)" if mode == "realtime" else ""),
                "Append HERMES_S2S_MONKEYPATCH_DISCORD=1 to ~/.hermes/.env"
                if mode == "realtime"
                else None,
            )
        )
    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    out.append(
        _check(
            "hermes_integration",
            "DISCORD_BOT_TOKEN",
            _PASS if bot_token else _WARN,
            "DISCORD_BOT_TOKEN " + ("set" if bot_token else "not set"),
            None if bot_token else "Add DISCORD_BOT_TOKEN=... to ~/.hermes/.env",
        )
    )
    allowed = os.environ.get("DISCORD_ALLOWED_USERS", "")
    if allowed:
        n_users = len([x for x in allowed.split(",") if x.strip()])
        out.append(
            _check(
                "hermes_integration",
                "DISCORD_ALLOWED_USERS",
                _PASS,
                f"DISCORD_ALLOWED_USERS set ({n_users} user(s))",
                None,
            )
        )
    else:
        out.append(
            _check(
                "hermes_integration",
                "DISCORD_ALLOWED_USERS",
                _WARN,
                "DISCORD_ALLOWED_USERS not set",
                "Add DISCORD_ALLOWED_USERS=<your_user_id> to ~/.hermes/.env",
            )
        )
    return out


async def _probe_backend_async(cfg: Any, timeout: float = 5.0) -> Dict[str, Any]:
    """Instantiate the configured realtime backend and probe WS connectivity.

    Lazy-imports resolve_realtime + the backend module so importing doctor.py
    never drags the realtime extra. Catches *all* exceptions — the only
    outcomes are pass/fail with a descriptive message.
    """
    try:
        from hermes_s2s.registry import resolve_realtime
        from hermes_s2s import providers as _providers

        # Ensure built-in realtime backends are registered (idempotent).
        try:
            _providers.register_builtin_realtime_providers()
        except Exception:  # noqa: BLE001
            pass
        provider = (getattr(cfg, "realtime_provider", "") or "").lower()
        options = dict(getattr(cfg, "realtime_options", {}) or {})
        # Strip the top-level 'provider' field before passing to factory.
        options.pop("provider", None)
        backend = resolve_realtime(provider, options)
    except Exception as exc:  # noqa: BLE001
        return {
            "status": _FAIL,
            "message": f"Could not instantiate realtime backend: {exc}",
            "remediation": "pip install hermes-s2s[realtime]  (or check provider name)",
        }

    try:
        await asyncio.wait_for(
            backend.connect(
                "pre-flight doctor probe (read-only)",
                getattr(cfg, "realtime_options", {}).get("voice") or "Aoede",
                [],
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        try:
            await backend.close()
        except Exception:  # noqa: BLE001
            pass
        return {
            "status": _FAIL,
            "message": f"Backend connect timed out after {timeout}s",
            "remediation": "Check network connectivity and API-key validity",
        }
    except Exception as exc:  # noqa: BLE001
        try:
            await backend.close()
        except Exception:  # noqa: BLE001
            pass
        return {
            "status": _FAIL,
            "message": f"Backend connect failed: {exc}",
            "remediation": "Verify API key is valid and has realtime-model access",
        }

    # Connected — close and report pass.
    try:
        await asyncio.wait_for(backend.close(), timeout=2.0)
    except Exception:  # noqa: BLE001
        pass
    return {
        "status": _PASS,
        "message": "Backend WS connected successfully within timeout",
        "remediation": None,
    }


def _backend_connectivity_checks(cfg: Any, probe: bool) -> List[Dict[str, Any]]:
    mode = getattr(cfg, "mode", "cascaded")
    if mode != "realtime":
        return [
            _check(
                "backend_connectivity",
                "realtime_probe",
                _SKIP,
                f"skipped — mode = {mode} (no realtime backend to probe)",
                None,
            )
        ]
    if not probe:
        return [
            _check(
                "backend_connectivity",
                "realtime_probe",
                _SKIP,
                "skipped — --no-probe",
                None,
            )
        ]
    provider = (getattr(cfg, "realtime_provider", "") or "").lower()
    # Required key must be present to even attempt.
    if "gemini" in provider and not os.environ.get("GEMINI_API_KEY"):
        return [
            _check(
                "backend_connectivity",
                "realtime_probe",
                _SKIP,
                "skipped — GEMINI_API_KEY not set",
                "Set GEMINI_API_KEY in ~/.hermes/.env",
            )
        ]
    if "openai" in provider and not os.environ.get("OPENAI_API_KEY"):
        return [
            _check(
                "backend_connectivity",
                "realtime_probe",
                _SKIP,
                "skipped — OPENAI_API_KEY not set",
                "Set OPENAI_API_KEY in ~/.hermes/.env",
            )
        ]
    try:
        res = asyncio.run(_probe_backend_async(cfg, timeout=5.0))
    except Exception as exc:  # noqa: BLE001
        res = {
            "status": _FAIL,
            "message": f"Probe dispatcher raised: {exc}",
            "remediation": None,
        }
    return [
        _check(
            "backend_connectivity",
            f"realtime_probe[{provider}]",
            res["status"],
            res["message"],
            res.get("remediation"),
        )
    ]


# ---------------------------------------------------------------------------
# Runner + formatters
# ---------------------------------------------------------------------------


def run_doctor(probe: bool = True) -> Dict[str, Any]:
    """Run all checks and return a structured report."""
    checks: List[Dict[str, Any]] = []
    # Lazy import so `from hermes_s2s.doctor import run_doctor` stays cheap.
    try:
        from hermes_s2s.config import load_config

        cfg = load_config()
    except Exception as exc:  # noqa: BLE001
        checks.append(
            _check(
                "configuration",
                "load_config",
                _FAIL,
                f"Could not load ~/.hermes/config.yaml: {exc}",
                "Run `hermes s2s setup` to create a valid config",
            )
        )
        return {"overall_status": _FAIL, "checks": checks}

    checks.extend(_configuration_checks(cfg))
    checks.extend(_python_dep_checks())
    checks.extend(_system_dep_checks())
    checks.extend(_api_key_checks(cfg))
    checks.extend(_hermes_integration_checks(cfg))
    checks.extend(_backend_connectivity_checks(cfg, probe=probe))

    # Overall: fail > warn > pass (ignore skip).
    overall = _PASS
    for c in checks:
        if c["status"] == _FAIL:
            overall = _FAIL
            break
        if c["status"] == _WARN:
            overall = _WARN
    return {"overall_status": overall, "checks": checks}


def _paint(s: str, color: str, tty: bool) -> str:
    if not tty:
        return s
    return f"{color}{s}{_C_RESET}"


def format_human(report: Dict[str, Any]) -> str:
    """Pretty-printed output (ADR-0009 §2 box drawing UI)."""
    tty = sys.stdout.isatty()
    bar = "━" * 55
    lines: List[str] = []
    lines.append(bar)
    lines.append("hermes-s2s — readiness check")
    lines.append(bar)
    lines.append("")

    # Group by category; always emit all 6 category headers for consistency.
    grouped: Dict[str, List[Dict[str, Any]]] = {c: [] for c in _CATEGORIES}
    for c in report.get("checks", []):
        grouped.setdefault(c["category"], []).append(c)

    labels = {
        "configuration": "Configuration",
        "python_deps": "Python dependencies",
        "system_deps": "System dependencies",
        "api_keys": "API keys",
        "hermes_integration": "Hermes integration",
        "backend_connectivity": "Backend connectivity",
    }

    for cat in _CATEGORIES:
        lines.append(f"{labels[cat]}:")
        if not grouped.get(cat):
            lines.append("  (no checks)")
            lines.append("")
            continue
        for c in grouped[cat]:
            status = c["status"]
            if status == _PASS:
                glyph = _paint("✓", _C_GREEN, tty)
            elif status == _WARN:
                glyph = _paint("⚠", _C_YELLOW, tty)
            elif status == _FAIL:
                glyph = _paint("✗", _C_RED, tty)
            else:
                glyph = _paint("•", _C_DIM, tty)
            line = f"  {glyph} {c['name']:<30} {c['message']}"
            lines.append(line)
            if c.get("remediation"):
                lines.append(
                    f"     {_paint('→', _C_DIM, tty)} {c['remediation']}"
                )
        lines.append("")

    # Summary
    n_pass = sum(1 for c in report["checks"] if c["status"] == _PASS)
    n_warn = sum(1 for c in report["checks"] if c["status"] == _WARN)
    n_fail = sum(1 for c in report["checks"] if c["status"] == _FAIL)
    n_skip = sum(1 for c in report["checks"] if c["status"] == _SKIP)
    lines.append(bar)
    overall = report.get("overall_status", _PASS)
    overall_color = {_PASS: _C_GREEN, _WARN: _C_YELLOW, _FAIL: _C_RED}.get(
        overall, _C_RESET
    )
    lines.append(
        f"Overall: {_paint(overall.upper(), overall_color, tty)} "
        f"— {n_pass} pass, {n_warn} warn, {n_fail} fail, {n_skip} skip"
    )
    lines.append(bar)
    return "\n".join(lines)


def format_json(report: Dict[str, Any]) -> str:
    return json.dumps(report, indent=2, default=str)
