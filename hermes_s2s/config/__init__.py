"""Config schema for hermes-s2s.

Loaded from `~/.hermes/config.yaml` under the top-level `s2s:` key. All fields
are optional; sensible defaults apply.

On first load against a pre-0.4.0 config (``s2s.mode`` present, no
``s2s.voice.default_mode``, no ``.s2s_migrated_0_4`` sentinel) this module
transparently migrates the config in-place via
:mod:`hermes_s2s.migrate_0_4` and logs a one-time deprecation warning.
The dedicated ``python -m hermes_s2s.migrate_0_4`` script is the
recommended path; this fallback exists so 0.3.x users don't need to run
anything manually.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

import yaml


logger = logging.getLogger(__name__)


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
        # Prefer 0.4.0 voice.default_mode when present; fall back to legacy s2s.mode.
        voice = raw.get("voice", {}) or {}
        mode_raw = voice.get("default_mode") or raw.get("mode") or "cascaded"
        return cls(
            mode=str(mode_raw).lower().strip(),
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

    # ------------------------------------------------------------------
    # v0.5.0 (Wave 1) — per-channel override builders
    # ------------------------------------------------------------------
    #
    # These ``with_*`` helpers return a shallow copy of the config with
    # one field swapped out. ``resolve_s2s_config_for_channel`` in
    # ``voice/factory.py`` chains them when applying the per-channel
    # record from ``S2SModeOverrideStore`` so the cached singleton from
    # ``load_config()`` is never mutated.
    #
    # ``S2SConfig`` is NOT a frozen dataclass (it carries a ``raw`` dict
    # users sometimes mutate post-load), so ``dataclasses.replace`` is
    # the safe shallow-copy primitive here.

    def with_mode(self, mode: str) -> "S2SConfig":
        """Return a copy with ``mode`` swapped."""
        import dataclasses as _dc

        return _dc.replace(self, mode=str(mode))

    def with_realtime_provider(self, provider: str) -> "S2SConfig":
        """Return a copy with ``realtime_provider`` swapped."""
        import dataclasses as _dc

        return _dc.replace(self, realtime_provider=str(provider))

    def with_stt_provider(self, provider: str) -> "S2SConfig":
        """Return a copy with the cascaded STT provider swapped.

        The nested :class:`StageConfig` is also copied so the original
        ``stt`` instance shared with the cached config is left intact.
        """
        import dataclasses as _dc

        new_stt = _dc.replace(self.stt, provider=str(provider))
        return _dc.replace(self, stt=new_stt)

    def with_tts_provider(self, provider: str) -> "S2SConfig":
        """Return a copy with the cascaded TTS provider swapped."""
        import dataclasses as _dc

        new_tts = _dc.replace(self.tts, provider=str(provider))
        return _dc.replace(self, tts=new_tts)


# ---------------------------------------------------------------------------
# Auto-translate fallback (M5.1 part 2)
# ---------------------------------------------------------------------------


_AUTO_MIGRATE_LOGGED = False


def _maybe_auto_migrate(
    config_path: Path, hermes_home: Path, data: Dict[str, Any]
) -> Dict[str, Any]:
    """Auto-translate a legacy 0.3.x config in place.

    Triggers only when: ``s2s.mode`` is present, ``s2s.voice.default_mode``
    is not, and ``<hermes_home>/.s2s_migrated_0_4`` doesn't exist. Writes
    the migrated config atomically, drops the sentinel, and logs a single
    deprecation warning. Silently becomes a no-op for any of:
    - config already on 0.4.0 shape
    - sentinel exists (user already ran the script or hit this path)
    - migration raises (returns the input data so load_config never fails)
    """
    global _AUTO_MIGRATE_LOGGED

    s2s = data.get("s2s") if isinstance(data, dict) else None
    if not isinstance(s2s, dict):
        return data

    voice = s2s.get("voice")
    already_migrated = isinstance(voice, dict) and voice.get("default_mode")
    if already_migrated:
        return data

    has_legacy_mode = isinstance(s2s.get("mode"), str) and s2s["mode"].strip()
    if not has_legacy_mode:
        return data

    try:
        from hermes_s2s import migrate_0_4
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("auto-migrate import failed: %s", exc)
        return data

    sentinel_path = hermes_home / migrate_0_4.SENTINEL_NAME
    if sentinel_path.exists():
        return data

    try:
        new_data, _warnings = migrate_0_4.translate_config(data)
        migrate_0_4.atomic_write_yaml(config_path, new_data)
        migrate_0_4.write_sentinel(hermes_home)
    except Exception as exc:  # pragma: no cover - best-effort
        logger.debug("auto-migrate write failed: %s", exc)
        return data

    if not _AUTO_MIGRATE_LOGGED:
        logger.warning(
            "hermes-s2s: migrated legacy s2s.mode -> s2s.voice.default_mode "
            "(config: %s). Sentinel: %s. Run "
            "`python -m hermes_s2s.migrate_0_4 --rollback` to revert.",
            config_path,
            sentinel_path,
        )
        _AUTO_MIGRATE_LOGGED = True

    return new_data


def load_config(path: Path | None = None) -> S2SConfig:
    """Load the S2S config from `~/.hermes/config.yaml`.

    Auto-translates legacy 0.3.x configs on first load (see
    :func:`_maybe_auto_migrate`).
    """
    config_path = path or CONFIG_PATH
    if not config_path.exists():
        # Fresh install / no config file — empty config is fine.
        return S2SConfig.from_dict({})
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        # Phase-8 final security review P0-4: previously the broad
        # except-and-return-empty path silently ate YAML syntax errors,
        # which meant a typo'd config would boot a fresh empty config
        # and the user had no idea why their settings didn't apply.
        # Surface it loud and raise — this is *their* config file,
        # they need to fix it.
        logger.error(
            "hermes-s2s: failed to parse YAML config at %s: %s",
            config_path,
            exc,
        )
        raise
    except OSError as exc:
        # Permission-denied / disk errors / unreadable files — same
        # principle: don't silently fall through to empty config, raise
        # so the user knows their config couldn't be read.
        logger.error(
            "hermes-s2s: failed to read config at %s: %s",
            config_path,
            exc,
        )
        raise

    if isinstance(data, dict):
        # Resolve HERMES_HOME fresh each call so tests can monkeypatch
        # the module-level HERMES_HOME constant.
        hermes_home = HERMES_HOME if path is None else config_path.parent
        data = _maybe_auto_migrate(config_path, hermes_home, data)
    else:
        # Non-dict top-level (e.g. a top-level YAML list) — treat as
        # empty so downstream .get() calls don't explode. This is not
        # a parse error; yaml.safe_load succeeded, the user just wrote
        # a list where we expected a mapping.
        data = {}

    return S2SConfig.from_dict(data.get("s2s", {}) or {})
