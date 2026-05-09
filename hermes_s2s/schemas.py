"""Tool schemas — what the LLM sees when deciding to call hermes-s2s tools."""

S2S_STATUS = {
    "name": "s2s_status",
    "description": (
        "Report the current speech-to-speech configuration: active mode "
        "(cascaded/realtime/s2s-server), per-stage providers (STT/TTS), "
        "registered backend names, and whether each backend has its required "
        "API key or local dependency available. Use this when the user asks "
        "'what voice mode am I in', 'is my local TTS set up', or to debug "
        "why voice replies are not working."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

S2S_SET_MODE = {
    "name": "s2s_set_mode",
    "description": (
        "Switch the active S2S mode for the current session. Mode must be one "
        "of 'cascaded' (STT-then-LLM-then-TTS, default), 'realtime' (native "
        "duplex via Gemini Live or GPT-4o Realtime), or 's2s-server' (delegate "
        "the full turn to an external streaming-speech-to-speech server). "
        "This does NOT modify ~/.hermes/config.yaml — it overrides per-session."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["cascaded", "realtime", "s2s-server"],
                "description": "S2S mode to activate for the current session.",
            },
        },
        "required": ["mode"],
    },
}

S2S_TEST_PIPELINE = {
    "name": "s2s_test_pipeline",
    "description": (
        "Smoke-test the configured S2S pipeline end-to-end using a short "
        "fixture audio file. Confirms the active STT can transcribe and the "
        "active TTS can synthesize. Returns timings and any errors. Use when "
        "the user asks 'is my voice setup working' or after changing "
        "providers."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Optional text to synthesize for the TTS smoke test.",
            },
        },
        "required": [],
    },
}
