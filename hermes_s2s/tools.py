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

    return _ok_json({
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
    })


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


# ---------------------------------------------------------------------------
# Slash command — /s2s
# ---------------------------------------------------------------------------

def handle_s2s_command(raw_args: str) -> str:
    """Implements the /s2s slash command.

    Examples:
        /s2s
        /s2s status
        /s2s mode realtime
        /s2s mode cascaded
    """
    parts = (raw_args or "").strip().split()
    if not parts or parts[0] in {"status", "show", "info"}:
        return s2s_status({})
    sub = parts[0]
    if sub == "mode" and len(parts) >= 2:
        return s2s_set_mode({"mode": parts[1]})
    if sub == "test":
        return s2s_test_pipeline({"text": " ".join(parts[1:]) or None})
    return (
        "Usage:\n"
        "  /s2s [status]              — show current S2S configuration\n"
        "  /s2s mode <cascaded|realtime|s2s-server> — switch mode for this session\n"
        "  /s2s test [text]           — smoke-test the configured TTS pipeline"
    )
