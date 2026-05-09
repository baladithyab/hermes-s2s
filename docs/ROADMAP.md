# Roadmap

## 0.1.0 (initial scaffold — ✅ this commit)
- [x] Plugin skeleton (manifest, pyproject, README, ROADMAP)
- [x] Provider registry (stt / tts / realtime / pipeline)
- [x] Config schema (`S2SConfig`, `~/.hermes/config.yaml` `s2s:` block)
- [x] Built-in STT: **Moonshine** (local, MIT)
- [x] Built-in TTS: **Kokoro** (local, Apache-2.0)
- [x] Built-in stage providers: **s2s-server** STT + TTS (HTTP delegation)
- [x] Stubs: realtime-gemini, realtime-openai, s2s-server pipeline
- [x] Tools: `s2s_status`, `s2s_set_mode`, `s2s_test_pipeline`
- [x] Slash command `/s2s`, CLI `hermes s2s ...`
- [x] Skill `hermes-s2s`

## 0.2.0 — Local-pipeline integration (next)
- [ ] Wire stage providers into Hermes built-in `tools/transcription_tools.py` / `tools/tts_tool.py` via `pre_tool_call` hooks (no fork required)
  *Decision pending:* hooks vs registering ourselves as Hermes-native providers via dynamic dispatch
- [ ] `hermes s2s serve` — convenience launcher for `streaming-speech-to-speech` server
- [ ] s2s-server pipeline backend (full WS protocol)
- [ ] WS protocol versioning + handshake
- [ ] `s2s.fallback_chain` config — auto-fail to next provider if primary errors
- [ ] Tests: full cascaded smoke against fixtures

## 0.3.0 — Realtime
- [ ] `RealtimeBackend` protocol + audio-resampling utility
- [ ] **gemini-live** backend (half-cascade by default; native-audio opt-in)
- [ ] **gpt-realtime / gpt-realtime-mini** backend
- [ ] Tool-call bridge: realtime backend tool_call → Hermes dispatcher → inject result
- [ ] Session resumption (Gemini >15min, OpenAI 30min cap reconnect)
- [ ] Discord voice integration: decode-side Opus → backend, encode-side backend → Opus
- [ ] CLI mic integration via PortAudio

## 0.4.0 — Polish
- [ ] True barge-in (cascaded mode VAD-during-TTS cancels)
- [ ] Voice-aware tool verbalization (numbers, code, URLs, lists)
- [ ] Long-tool filler audio ("let me check on that…")
- [ ] Multi-party VC handling (per-user STT, optional wakeword)
- [ ] Persona overlay (voice-only response shaping)
- [ ] Observability: per-call latency / cost dashboards

## 0.5.0+ — Distribution
- [ ] PyPI publish (`pip install hermes-s2s`)
- [ ] NixOS recipe
- [ ] Demo video, blog post, "from zero to Discord VC AI in 5 minutes"

## Out of scope
- Telegram group voice calls (different stack, pytgcalls / TDLib)
- Voice cloning (delegate to ElevenLabs's existing API)
- Multilingual auto-detect (set `lang_code` per-call)
