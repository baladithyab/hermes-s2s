"""Config schema for hermes-s2s.

Loaded from `~/.hermes/config.yaml` under the top-level `s2s:` key. All fields
are optional; sensible defaults apply.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

import yaml


HERMES_HOME = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
CONFIG_PATH = HERMES_HOME / "config.yaml"


@dataclass
class StageConfig:
    """Config for one stage of a cascaded pipeline (stt or tts)."""
    provider: str = ""
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class S2SConfig:
    """Top-level S2S config block.

    See README.md for a fully annotated example.
    """
    mode: str = "cascaded"  # cascaded | realtime | s2s-server
    # Cascaded-mode pipeline stages
    stt: StageConfig = field(default_factory=StageConfig)
    tts: StageConfig = field(default_factory=StageConfig)
    # Realtime-mode backend choice
    realtime_provider: str = "gemini-live"
    realtime_options: Dict[str, Any] = field(default_factory=dict)
    # s2s-server backend
    server_endpoint: str = "ws://localhost:8000/ws"
    server_health_url: str = "http://localhost:8000/health"
    server_auto_launch: bool = False
    server_fallback_to: str = "pipeline"  # if server is down: pipeline | none
    # Raw block for downstream lookups
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "S2SConfig":
        if not isinstance(raw, dict):
            raw = {}
        pipeline = raw.get("pipeline", {}) or {}
        stt = pipeline.get("stt", {}) or {}
        tts = pipeline.get("tts", {}) or {}
        realtime = raw.get("realtime", {}) or {}
        server = raw.get("s2s_server", {}) or {}
        return cls(
            mode=str(raw.get("mode") or "cascaded").lower().strip(),
            stt=StageConfig(
                provider=str(stt.get("provider") or "moonshine"),
                options=stt,
            ),
            tts=StageConfig(
                provider=str(tts.get("provider") or "kokoro"),
                options=tts,
            ),
            realtime_provider=str(realtime.get("provider") or "gemini-live"),
            realtime_options=realtime,
            server_endpoint=str(server.get("endpoint") or "ws://localhost:8000/ws"),
            server_health_url=str(server.get("health_url") or "http://localhost:8000/health"),
            server_auto_launch=bool(server.get("auto_launch", False)),
            server_fallback_to=str(server.get("fallback_to") or "pipeline"),
            raw=raw,
        )

    def stage_options(self, stage: str) -> Dict[str, Any]:
        """Return per-provider options for the active stage."""
        if stage == "stt":
            cfg = self.stt
        elif stage == "tts":
            cfg = self.tts
        else:
            return {}
        # Provider-specific block lives at e.g. stt.moonshine.{...}
        provider_block = cfg.options.get(cfg.provider, {})
        return provider_block if isinstance(provider_block, dict) else {}


def load_config(path: Path | None = None) -> S2SConfig:
    """Load the S2S config from `~/.hermes/config.yaml`."""
    config_path = path or CONFIG_PATH
    if not config_path.exists():
        return S2SConfig.from_dict({})
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return S2SConfig.from_dict({})
    return S2SConfig.from_dict(data.get("s2s", {}) or {})
