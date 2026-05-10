"""Regression tests for v0.3.9 — wizard-nested config is read by runtime.

Covers Fixes A, B, and C from docs/research/10-arabic-language-rootcause.md:

* Fix A — ``discord_bridge._resolve_bridge_params`` unwraps the provider
  sub-block (``realtime_options['gemini_live']`` / ``'openai'``) so the
  anchored system prompt + voice the wizard writes actually reach the
  RealtimeAudioBridge constructor.
* Fix B — ``make_gemini_live`` and ``make_openai_realtime`` merge the
  sub-block over outer keys (sub-block wins) so nested configs work while
  flat back-compat configs keep working.
* Fix C — ``GeminiLiveBackend._build_setup`` prepends an explicit
  ``"Respond exclusively in <Language>."`` directive to ``systemInstruction``
  as defense in depth against Gemini Live's input-audio language drift.
"""

from __future__ import annotations

import pytest


# --------------------------------------------------------------------------- #
# Fix A — discord_bridge._resolve_bridge_params                               #
# --------------------------------------------------------------------------- #


def test_discord_bridge_unwraps_gemini_live_subblock():
    """Wizard-nested realtime_options[gemini_live].{system_prompt,voice} must
    be what ends up on RealtimeAudioBridge — NOT the outer/default value."""
    from hermes_s2s.config import S2SConfig
    from hermes_s2s._internal import discord_bridge

    cfg = S2SConfig.from_dict({
        "mode": "realtime",
        "realtime": {
            "provider": "gemini-live",
            "gemini_live": {
                "system_prompt": "MARKER-EN",
                "voice": "Puck",
                "model": "X",
                "language_code": "fr-FR",
            },
        },
    })

    system_prompt, voice = discord_bridge._resolve_bridge_params(cfg)
    assert system_prompt == "MARKER-EN"
    assert voice == "Puck"


def test_discord_bridge_falls_back_to_outer_for_flat_config():
    """Back-compat: pre-0.3.8 flat configs (no sub-block) still work."""
    from hermes_s2s.config import S2SConfig
    from hermes_s2s._internal import discord_bridge

    cfg = S2SConfig.from_dict({
        "mode": "realtime",
        "realtime": {
            "provider": "gemini-live",
            "system_prompt": "FLAT-PROMPT",
            "voice": "Aoede",
        },
    })

    system_prompt, voice = discord_bridge._resolve_bridge_params(cfg)
    assert system_prompt == "FLAT-PROMPT"
    assert voice == "Aoede"


def test_discord_bridge_uses_default_when_nothing_set():
    """With no prompt anywhere, the module _DEFAULT_SYSTEM_PROMPT wins — and
    it must mention English so Gemini Live can't pick Arabic from accent."""
    from hermes_s2s.config import S2SConfig
    from hermes_s2s._internal import discord_bridge

    cfg = S2SConfig.from_dict({
        "mode": "realtime",
        "realtime": {"provider": "gemini-live"},
    })
    system_prompt, voice = discord_bridge._resolve_bridge_params(cfg)
    assert system_prompt == discord_bridge._DEFAULT_SYSTEM_PROMPT
    assert "English" in system_prompt
    assert voice is None


# --------------------------------------------------------------------------- #
# Fix B — factory sub-block unwrap                                            #
# --------------------------------------------------------------------------- #


def test_make_gemini_live_unwraps_subblock():
    from hermes_s2s.providers.realtime.gemini_live import make_gemini_live

    backend = make_gemini_live({
        "provider": "gemini-live",
        "gemini_live": {"model": "X", "language_code": "fr-FR"},
    })
    assert backend.model == "X"
    assert backend.language_code == "fr-FR"


def test_make_gemini_live_outer_level_back_compat():
    """Flat config (no sub-block) still works — outer keys read directly."""
    from hermes_s2s.providers.realtime.gemini_live import make_gemini_live

    backend = make_gemini_live({"model": "Y", "language_code": "es-ES"})
    assert backend.model == "Y"
    assert backend.language_code == "es-ES"


def test_make_openai_realtime_unwraps_subblock():
    from hermes_s2s.providers.realtime.openai_realtime import make_openai_realtime

    backend = make_openai_realtime({
        "provider": "gpt-realtime",
        "openai": {"model": "M", "voice": "shimmer"},
    })
    assert backend.model == "M"
    assert backend.voice == "shimmer"


# --------------------------------------------------------------------------- #
# Fix C — _build_setup language anchor                                        #
# --------------------------------------------------------------------------- #


def test_build_setup_anchors_language():
    from hermes_s2s.providers.realtime.gemini_live import GeminiLiveBackend

    b = GeminiLiveBackend(language_code="fr-FR")
    setup = b._build_setup(system_prompt="hi", tools=[])
    text = setup["systemInstruction"]["parts"][0]["text"]
    assert text.startswith("Respond exclusively in French")
    assert text.endswith("hi")
    # Observability hook preserved.
    assert setup["outputAudioTranscription"] == {}


def test_build_setup_anchors_english_by_default():
    from hermes_s2s.providers.realtime.gemini_live import GeminiLiveBackend

    b = GeminiLiveBackend()  # default en-US
    setup = b._build_setup(system_prompt="system", tools=[])
    text = setup["systemInstruction"]["parts"][0]["text"]
    assert text.startswith("Respond exclusively in English")
