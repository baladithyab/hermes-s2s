"""Built-in providers for hermes-s2s.

Each registration is split by category so partial environments work — a user
who never enables realtime mode never imports websockets bridge code, etc.
"""

from __future__ import annotations

import logging

from ..registry import register_pipeline, register_realtime, register_stt, register_tts

logger = logging.getLogger(__name__)


def register_builtin_stt_providers() -> None:
    """Register the bundled STT providers."""
    from .stt.moonshine import make_moonshine
    register_stt("moonshine", make_moonshine)
    # `s2s-server` STT delegates to an external streaming-speech-to-speech
    # server's /asr endpoint when only the STT stage should be local.
    try:
        from .stt.s2s_server import make_s2s_server_stt
        register_stt("s2s-server", make_s2s_server_stt)
    except Exception as exc:
        logger.debug("s2s-server STT not registered: %s", exc)


def register_builtin_tts_providers() -> None:
    """Register the bundled TTS providers."""
    from .tts.kokoro import make_kokoro
    register_tts("kokoro", make_kokoro)
    try:
        from .tts.s2s_server import make_s2s_server_tts
        register_tts("s2s-server", make_s2s_server_tts)
    except Exception as exc:
        logger.debug("s2s-server TTS not registered: %s", exc)


def register_builtin_realtime_providers() -> None:
    """Register the bundled realtime duplex backends."""
    try:
        from .realtime.gemini_live import make_gemini_live
        register_realtime("gemini-live", make_gemini_live)
    except Exception as exc:
        logger.debug("gemini-live realtime not registered: %s", exc)
    try:
        from .realtime.openai_realtime import make_openai_realtime
        # Register multiple aliases — users may set any of these as
        # ``s2s.realtime.provider``. The factory passes the resolved
        # config sub-block (which contains the actual ``model`` field)
        # into the OpenAI backend, so the alias choice doesn't pin
        # the model — it just routes to the same factory.
        register_realtime("openai-realtime", make_openai_realtime)
        register_realtime("gpt-realtime", make_openai_realtime)
        register_realtime("gpt-realtime-1.5", make_openai_realtime)
        register_realtime("gpt-realtime-2", make_openai_realtime)
        register_realtime("gpt-realtime-mini", make_openai_realtime)
    except Exception as exc:
        logger.debug("openai realtime not registered: %s", exc)


def register_builtin_pipeline_providers() -> None:
    """Register full-pipeline (one-shot) backends."""
    try:
        from .pipeline.s2s_server import make_s2s_server_pipeline
        register_pipeline("s2s-server", make_s2s_server_pipeline)
    except Exception as exc:
        logger.debug("s2s-server pipeline not registered: %s", exc)
