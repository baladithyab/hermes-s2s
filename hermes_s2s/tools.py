"""Tool handlers + slash-command dispatcher for hermes-s2s."""

from __future__ import annotations

import json
import logging
import os
import shutil
from typing import Any, Dict

from .config import load_config
from .registry import list_registered

logger = logging.getLogger(__name__)


# Per-session mode overrides (in-memory only)
_SESSION_MODE_OVERRIDE: Dict[str, str] = {}


def _resolve_active_mode(session_id: str | None = None) -> str:
    """Active mode for the current session, falling back to config."""
    if session_id and session_id in _SESSION_MODE_OVERRIDE:
        return _SESSION_MODE_OVERRIDE[session_id]
    return load_config().mode


def _check_env(var: str) -> bool:
    return bool(os.environ.get(var))


def _ok_json(data: Any) -> str:
    return json.dumps(data, default=str)


def s2s_status(args: dict, **kwargs: Any) -> str:
    """LLM-callable: report active mode, providers, and readiness."""
    cfg = load_config()
    session_id = kwargs.get("task_id") or kwargs.get("session_id")
    active_mode = _resolve_active_mode(session_id)
    registered = list_registered()

    result: dict[str, Any] = {
        "active_mode": active_mode,
        "config_mode": cfg.mode,
        "session_overridden": session_id in _SESSION_MODE_OVERRIDE,
        "cascaded": {
            "stt_provider": cfg.stt.provider,
            "stt_options": cfg.stage_options("stt"),
            "tts_provider": cfg.tts.provider,
            "tts_options": cfg.stage_options("tts"),
        },
        "realtime": {
            "provider": cfg.realtime_provider,
            "gemini_key_set": _check_env("GEMINI_API_KEY"),
            "openai_key_set": _check_env("OPENAI_API_KEY"),
        },
        "s2s_server": {
            "endpoint": cfg.server_endpoint,
            "auto_launch": cfg.server_auto_launch,
        },
        "registered_backends": registered,
        "deps": {
            "moonshine_onnx": _module_available("moonshine_onnx"),
            "transformers": _module_available("transformers"),
            "kokoro": _module_available("kokoro"),
            "scipy": _module_available("scipy"),
            "websockets": _module_available("websockets"),
            "ffmpeg_in_path": shutil.which("ffmpeg") is not None,
        },
    }

    # P1-2: surface RealtimeAudioBridge.stats() when a bridge is active.
    try:
        from hermes_s2s._internal.audio_bridge import get_active_bridge

        bridge = get_active_bridge()
        if bridge is not None:
            try:
                result["bridge_stats"] = bridge.stats()
            except Exception as exc:  # noqa: BLE001
                result["bridge_stats_error"] = str(exc)
    except Exception:  # noqa: BLE001
        # audio_bridge import path is always present but be defensive.
        pass

    return _ok_json(result)


def _module_available(name: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def s2s_set_mode(args: dict, **kwargs: Any) -> str:
    """LLM-callable: switch S2S mode for this session only."""
    mode = (args or {}).get("mode", "").lower().strip()
    if mode not in {"cascaded", "realtime", "s2s-server"}:
        return _ok_json({"error": f"invalid mode '{mode}'. Must be cascaded|realtime|s2s-server"})
    session_id = kwargs.get("task_id") or kwargs.get("session_id") or "default"
    _SESSION_MODE_OVERRIDE[session_id] = mode
    return _ok_json({"ok": True, "session_mode": mode, "session_id": session_id})


def s2s_test_pipeline(args: dict, **kwargs: Any) -> str:
    """LLM-callable: run a smoke test on the configured cascaded pipeline."""
    text = (args or {}).get("text") or "hermes speech to speech smoke test"
    cfg = load_config()
    try:
        from .registry import resolve_tts
        tts = resolve_tts(cfg.tts.provider, cfg.stage_options("tts"))
    except Exception as exc:
        return _ok_json({"ok": False, "stage": "tts_resolve", "error": str(exc)})

    out_dir = os.path.expanduser("~/.hermes/voice-memos")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "s2s_smoke_test.wav")

    try:
        path = tts.synthesize(text, out_path)
        size = os.path.getsize(path) if os.path.exists(path) else 0
    except Exception as exc:
        return _ok_json({"ok": False, "stage": "tts_synthesize", "error": str(exc)})

    return _ok_json({
        "ok": True,
        "tts_provider": cfg.tts.provider,
        "stt_provider": cfg.stt.provider,
        "wrote": path,
        "bytes": size,
        "next_step": "play the file or rerun /voice join in your VC to confirm end-to-end",
    })


async def s2s_doctor(args: dict, **kwargs: Any) -> str:
    """LLM-callable: run the full pre-flight readiness check.

    Delegates to :func:`hermes_s2s.doctor.run_doctor_async`. The ``probe`` arg
    (default True) controls whether to open a 5 s WS probe to the configured
    realtime backend; set to False in CI or to avoid the ~$0.0001 probe cost.

    This handler is **async** (P0-1): the Hermes gateway invokes tool
    handlers inside a running event loop, so a synchronous ``asyncio.run``
    inside the probe would raise
    ``RuntimeError: asyncio.run() cannot be called from a running event loop``.
    """
    probe = True
    if isinstance(args, dict) and "probe" in args:
        probe = bool(args.get("probe"))
    from .doctor import run_doctor_async

    report = await run_doctor_async(probe=probe)
    return json.dumps(report, default=str)


# ---------------------------------------------------------------------------
# Slash command — /s2s
# ---------------------------------------------------------------------------

def handle_s2s_command(raw_args: str) -> str:
    """Implements the ``/s2s`` slash command for the CLI surface.

    Mirrors the Discord ``app_commands.Group`` subcommands so muscle memory
    carries across platforms. The CLI lacks a ``(guild_id, channel_id)``
    context, so provider overrides print a guidance message rather than
    writing to the per-channel override store — see Wave 4 / Task 4.1 in
    ``docs/plans/wave-0.5.0-s2s-configure.md`` and ADR-0015.

    Examples:
        /s2s
        /s2s status
        /s2s mode realtime
        /s2s provider realtime gpt-realtime-2
        /s2s test hello
        /s2s doctor
        /s2s reset
        /s2s configure
        /s2s help
    """
    parts = (raw_args or "").strip().split()

    # ``status`` (and no-arg) — pretty-printed, not raw JSON.
    if not parts or parts[0] in {"status", "show", "info"}:
        from .voice.slash_format import format_status

        payload = json.loads(s2s_status({}))
        return format_status(
            active_mode=payload["active_mode"],
            config_mode=payload["config_mode"],
            realtime_provider=payload["realtime"]["provider"],
            stt_provider=payload["cascaded"]["stt_provider"],
            tts_provider=payload["cascaded"]["tts_provider"],
            guild_id=0,
            channel_id=0,
            per_channel_record={},
        )

    sub = parts[0]

    if sub == "mode" and len(parts) >= 2:
        result = json.loads(s2s_set_mode({"mode": parts[1]}))
        if "error" in result:
            return f"❌ {result['error']}"
        return f"✅ Session mode → {result['session_mode']}"

    if sub == "provider" and len(parts) >= 3:
        kind, name = parts[1], parts[2]
        if kind not in {"realtime", "stt", "tts"}:
            return "Usage: /s2s provider <realtime|stt|tts> <name>"
        avail = list_registered().get(kind, [])
        if name not in avail:
            return (
                f"❌ Unknown {kind} provider '{name}'. "
                f"Available: {', '.join(avail)}"
            )
        # CLI lacks (guild_id, channel_id) for the per-channel override store,
        # so we can't persist this the way Discord does. Guide the user to
        # the global config instead — ADR-0015 acknowledges this as a 0.5.1
        # follow-up (session-scoped CLI overrides).
        return (
            f"⚠️  CLI provider override not yet plumbed (no per-channel context). "
            f"Edit ~/.hermes/config.yaml: s2s.{kind}.provider: {name}"
        )

    if sub == "test":
        text = " ".join(parts[1:]) or None
        result = json.loads(s2s_test_pipeline({"text": text}))
        if result.get("ok"):
            return f"✅ TTS OK — wrote {result['bytes']} bytes to {result['wrote']}"
        return f"❌ failed at {result.get('stage')}: {result.get('error')}"

    if sub == "doctor":
        # s2s_doctor is async; CLI handlers are sync. If we happen to be
        # inside an event loop, asyncio.run raises RuntimeError — fall back
        # to pointing the user at the dedicated `hermes s2s doctor` CLI
        # subcommand (which has its own async runner).
        import asyncio

        try:
            return asyncio.run(s2s_doctor({}))
        except RuntimeError:
            return (
                "Doctor must be called from a sync context. "
                "Use `hermes s2s doctor` instead."
            )

    if sub == "reset":
        # CLI reset = clear the in-memory session mode override.
        _SESSION_MODE_OVERRIDE.pop("default", None)
        return "✅ Cleared session override."

    if sub in {"configure", "help"}:
        from .voice.slash_format import format_help

        return format_help()

    # Unknown subcommand — surface help.
    from .voice.slash_format import format_help

    return f"Unknown subcommand `{sub}`.\n\n{format_help()}"
