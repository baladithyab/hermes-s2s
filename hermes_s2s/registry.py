"""Provider registry for hermes-s2s.

Each provider category (stt, tts, realtime, pipeline) is a name → factory map.
Built-in providers register themselves at plugin load time; users can register
their own via the public `register_*` functions in __init__.py.

Provider names are case-insensitive (stored lowercase). Last-write-wins so a
user-installed provider with the same name as a built-in transparently overrides it.
"""

from __future__ import annotations

from typing import Any, Callable, Dict


# Each registry maps name -> factory(config_dict) -> provider instance.
_STT_REGISTRY: Dict[str, Callable[[Dict[str, Any]], Any]] = {}
_TTS_REGISTRY: Dict[str, Callable[[Dict[str, Any]], Any]] = {}
_REALTIME_REGISTRY: Dict[str, Callable[[Dict[str, Any]], Any]] = {}
_PIPELINE_REGISTRY: Dict[str, Callable[[Dict[str, Any]], Any]] = {}


def _key(name: str) -> str:
    return (name or "").lower().strip()


def register_stt(name: str, factory: Callable[[Dict[str, Any]], Any]) -> None:
    """Register an STT provider factory."""
    _STT_REGISTRY[_key(name)] = factory


def register_tts(name: str, factory: Callable[[Dict[str, Any]], Any]) -> None:
    """Register a TTS provider factory."""
    _TTS_REGISTRY[_key(name)] = factory


def register_realtime(name: str, factory: Callable[[Dict[str, Any]], Any]) -> None:
    """Register a realtime duplex backend factory (Gemini Live, OpenAI Realtime, …)."""
    _REALTIME_REGISTRY[_key(name)] = factory


def register_pipeline(name: str, factory: Callable[[Dict[str, Any]], Any]) -> None:
    """Register a full-pipeline (one-shot) backend factory.

    Pipeline backends own audio-in to audio-out in a single call — used for
    s2s-server mode where transcription, generation, and synthesis all happen
    inside an external process.
    """
    _PIPELINE_REGISTRY[_key(name)] = factory


def resolve_stt(name: str, config: Dict[str, Any]) -> Any:
    factory = _STT_REGISTRY.get(_key(name))
    if factory is None:
        available = ", ".join(sorted(_STT_REGISTRY)) or "(none registered)"
        raise ValueError(f"Unknown STT provider '{name}'. Available: {available}")
    return factory(config)


def resolve_tts(name: str, config: Dict[str, Any]) -> Any:
    factory = _TTS_REGISTRY.get(_key(name))
    if factory is None:
        available = ", ".join(sorted(_TTS_REGISTRY)) or "(none registered)"
        raise ValueError(f"Unknown TTS provider '{name}'. Available: {available}")
    return factory(config)


def resolve_realtime(name: str, config: Dict[str, Any]) -> Any:
    factory = _REALTIME_REGISTRY.get(_key(name))
    if factory is None:
        available = ", ".join(sorted(_REALTIME_REGISTRY)) or "(none registered)"
        raise ValueError(f"Unknown realtime backend '{name}'. Available: {available}")
    return factory(config)


def resolve_pipeline(name: str, config: Dict[str, Any]) -> Any:
    factory = _PIPELINE_REGISTRY.get(_key(name))
    if factory is None:
        available = ", ".join(sorted(_PIPELINE_REGISTRY)) or "(none registered)"
        raise ValueError(f"Unknown pipeline backend '{name}'. Available: {available}")
    return factory(config)


def list_registered() -> Dict[str, list[str]]:
    """Snapshot of the registries — used by the status tool."""
    return {
        "stt": sorted(_STT_REGISTRY),
        "tts": sorted(_TTS_REGISTRY),
        "realtime": sorted(_REALTIME_REGISTRY),
        "pipeline": sorted(_PIPELINE_REGISTRY),
    }
