---
name: hermes-s2s
description: "Configure and operate the hermes-s2s plugin — switch S2S modes (cascaded/realtime/s2s-server), pick per-stage providers, run smoke tests, troubleshoot Discord VC voice."
version: 0.3.0
author: Codeseys
license: Apache-2.0
metadata:
  hermes:
    tags: [voice, s2s, discord, plugin]
    related_skills: [hermes-agent]
---

# hermes-s2s

You're operating the `hermes-s2s` plugin — a Hermes extension that adds configurable speech-to-speech to voice mode. It lives in `~/.hermes/plugins/hermes-s2s/` (or wherever the user installed it).

## When to load this skill

- User asks "what voice mode am I in" / "switch to realtime" / "use my local TTS".
- Voice replies are silent or wrong — debugging path.
- Setting up Discord VC voice for the first time with cloud or local backends.
- User asks about Gemini Live, GPT-4o Realtime, Moonshine, Kokoro, or `streaming-speech-to-speech` server integration in the context of Hermes voice.

## Three modes, one switch

```
s2s.mode: cascaded     # STT → Hermes LLM → TTS  (default, max flexibility)
s2s.mode: realtime     # native duplex (Gemini Live, GPT-4o Realtime)
s2s.mode: s2s-server   # external pipeline server (full v6 stack)
```

Set globally in `~/.hermes/config.yaml` under the `s2s:` key. Override per session with `/s2s mode <name>` or `hermes s2s mode <name>`.

## Common config recipes

### Recipe 1 — Fully local, free, fast (cascaded)

```yaml
s2s:
  mode: cascaded
  pipeline:
    stt:
      provider: moonshine
      moonshine: { model: tiny }
    tts:
      provider: kokoro
      kokoro: { voice: af_heart, lang_code: a }
```

Requires `pip install hermes-s2s[local-all]`. ~600ms round-trip on a discrete GPU. Zero per-minute cost.

### Recipe 2 — Premium voice, cheap-ish realtime (realtime/Gemini)

```yaml
s2s:
  mode: realtime
  realtime:
    provider: gemini-live
    gemini_live:
      model: gemini-live-2.5-flash
      voice: Aoede
```

Requires `GEMINI_API_KEY`. ~$0.06 per 30 min, native barge-in.

### Recipe 3 — Hybrid privacy-on-input (cascaded mixed)

```yaml
s2s:
  mode: cascaded
  pipeline:
    stt: { provider: moonshine, moonshine: { model: base } }
    tts: { provider: elevenlabs, elevenlabs: { voice_id: "..." } }
```

Local STT (transcript never leaves the machine), cloud LLM via Hermes, cloud TTS for premium voice. Requires `ELEVENLABS_API_KEY`.

### Recipe 4 — External S2S server (lowest latency)

```yaml
s2s:
  mode: s2s-server
  s2s_server:
    endpoint: ws://localhost:8000/ws
    auto_launch: false
```

Requires running https://github.com/codeseys/streaming-speech-to-speech (or compatible) server. ~250ms turn latency on RTX 5090.

## Slash commands

```
/s2s                  # show config + readiness
/s2s status           # same
/s2s mode realtime    # switch mode for this session only
/s2s test             # run a TTS smoke test, write to ~/.hermes/voice-memos/
/s2s test "hello"     # specific text
```

## CLI commands

```bash
hermes s2s status
hermes s2s mode realtime
hermes s2s test --text "hermes voice check"
```

## Tools the LLM can call

- `s2s_status` — full config + dependency readiness JSON.
- `s2s_set_mode` — switch mode for the active session.
- `s2s_test_pipeline` — run a TTS smoke test.

## Troubleshooting

### "Voice replies stopped working after I changed config"

```bash
hermes s2s status                     # check active providers
hermes s2s test --text "test"         # synthesize to a file
ls -lh ~/.hermes/voice-memos/         # verify file was written
```

### "Discord VC bot joins but doesn't hear me"

Same checklist as built-in voice (hermes-s2s does not replace Discord auth):
- `DISCORD_ALLOWED_USERS` includes your numeric user ID
- You're not server-muted
- `Connect` + `Speak` + `Use Voice Activity` permissions on the bot
- Speaking SSRC mapping requires you to vocalize once after the bot joins

### "Moonshine says 'package not installed'"

```bash
pip install hermes-s2s[moonshine]
# or pip install moonshine-onnx directly
```

### "Kokoro says 'package not installed'"

```bash
pip install hermes-s2s[kokoro]
sudo apt install espeak-ng       # (Ubuntu/Debian) phonemizer backend
brew install espeak-ng           # (macOS)
```

### "Realtime mode not implemented yet"

That's expected for realtime Discord audio bridging in 0.3.0 — the bridge is setup-plumbing only (version gate, monkey-patch site, config branch). Your Discord bot will continue to use Hermes's built-in STT -> text -> TTS path; Gemini Live / OpenAI Realtime audio routing lands in 0.3.1. The realtime backend classes themselves (`gemini-live`, `gpt-realtime-mini`) are implemented — they work today for non-Discord use cases (CLI, tests, future platforms). Track progress at https://github.com/codeseys/hermes-s2s. Use `cascaded` or `s2s-server` for Discord voice in the meantime.

## Architecture (one paragraph)

Each S2S mode resolves to one or more provider factories from a registry (`hermes_s2s/registry.py`). Built-in factories register at plugin load time (`providers/__init__.py`). The plugin entry-point (`hermes_s2s.__init__:register`) wires three tools, one slash command, one CLI subcommand tree, and this skill into Hermes. Cascaded mode delegates STT→LLM→TTS through the existing Hermes voice pipeline by writing audio files and reading them back; realtime mode (TBD) opens a duplex WebSocket and bridges Discord-decoded PCM both ways; s2s-server mode (TBD) routes the full turn through an external streaming-speech-to-speech server.

## Pitfalls

- **Hermes built-in voice mode is still active** — `hermes-s2s` adds providers; it does not replace `tools/transcription_tools.py` and `tools/tts_tool.py`. If a user has `stt.provider: groq` set in `voice.stt.provider` (Hermes built-in) AND `s2s.pipeline.stt.provider: moonshine` (this plugin), the built-in wins for built-in voice mode. The 0.2.0 command-provider shims (`hermes-s2s-tts` / `hermes-s2s-stt`) route cascaded mode through this plugin; realtime Discord audio bridging is setup-plumbing only in 0.3.0 and completes in 0.3.1.
- **GPU contention with gaming** — local pipeline (Moonshine + Kokoro) shares VRAM with the user's RTX 5090. Document expectation; offer `device: cpu` overrides in config.
- **espeak-ng** — Kokoro's phonemizer backend on Linux. Already a Hermes voice-mode dep for NeuTTS; safe to assume it's present.
- **WSL audio I/O** — there's no audio device in WSL by default. CLI mic mode does NOT work; only Discord/Telegram bot voice (network audio) works. Document.
