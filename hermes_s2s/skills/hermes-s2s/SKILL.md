---
name: hermes-s2s
description: "Configure and operate the hermes-s2s plugin — switch S2S modes (cascaded/pipeline/realtime/s2s-server), pick per-stage providers, run smoke tests, troubleshoot Discord VC voice, use /s2s slash command + voice meta-commands."
version: 0.4.0
author: Codeseys
license: Apache-2.0
metadata:
  hermes:
    tags: [voice, s2s, discord, plugin]
    related_skills: [hermes-agent]
---

# hermes-s2s

You're operating the `hermes-s2s` plugin — a Hermes extension that adds configurable speech-to-speech to voice mode. It lives in `~/.hermes/plugins/hermes-s2s/` (or wherever the user installed it).

## What changed in 0.4.0

- **Four modes** (was three): `cascaded | pipeline | realtime | s2s-server`.
  The pre-0.4 `s2s.mode: duplex` boolean is gone — the loader
  auto-translates to `realtime` on first load; a one-shot migrator
  (`python -m hermes_s2s.migrate_0_4`) writes the new shape.
- **Plugin-owned `/s2s` slash command** in Discord. Use this to pick
  modes / check readiness / run smoke TTS from chat.
- **Thread mirroring** in Discord: join from a thread → reuses it;
  join from a channel → auto-creates a public thread (auto-archive
  1 day) and mirrors transcripts.
- **Voice meta-commands** — say the wakeword (default `hey aria`)
  followed by a verb phrase for one of six verbs:
  `new | compress | title | branch | clear | stop_speaking`
  (e.g. `"hey aria, start a new session"`,
  `"hey aria, compress the context"`). Dispatched through the
  gateway's MetaDispatcher before the LLM sees the transcript.
- **Voice persona overlay + prompt-injection defense** — voice
  replies get a persona overlay and a hard-coded defense block that
  refuses "ignore previous instructions"-style payloads within a
  spoken transcript. See ADR-0013.
- **2-bucket tool-export policy** (provisional for 0.4.0): always-on
  default-exposed (`hermes_meta_*` + `web_search`, `vision_analyze`,
  `text_to_speech`, `clarify`) and deny-listed (everything else,
  fail-closed). Enforced by a CI fence. The third `ask` bucket
  (synchronous voice-confirm flow) is deferred to 0.4.1 — see
  ADR-0014 "0.4.0 implementation note".

Pointer to design: ADRs 0010–0014 under `docs/adrs/`, research notes
under `docs/design-history/research/13-*`, `14-*`, `15-*`, and the
full wave plan at `docs/plans/wave-0.4.0-rearchitecture.md`.

## When to load this skill

- User asks "what voice mode am I in" / "switch to realtime" / "use my local TTS".
- Voice replies are silent or wrong — debugging path.
- Setting up Discord VC voice for the first time with cloud or local backends.
- User asks about Gemini Live, GPT-4o Realtime, Moonshine, Kokoro, or `streaming-speech-to-speech` server integration in the context of Hermes voice.
- User asks about `/s2s`, voice meta-commands, or thread mirroring.

## Four modes, one switch (0.4.0+)

```
s2s.mode: cascaded     # STT → Hermes LLM → TTS  (default, max flexibility)
s2s.mode: pipeline     # Hermes's built-in tightly-coupled STT→LLM→TTS
s2s.mode: realtime     # native duplex (Gemini Live, GPT-4o Realtime)
s2s.mode: s2s-server   # external pipeline server (full v6 stack)
```

Set globally in `~/.hermes/config.yaml` under the `s2s:` key. Override per session with `/s2s mode:<choice>` (Discord) or `hermes s2s mode <name>` (CLI).

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
      model: gemini-2.5-flash-native-audio-latest
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

0.4.0 ships a deliberately small `/s2s` surface — exactly one
subcommand with one required choice:

```
/s2s mode:<choice>    # <choice> ∈ {cascaded, pipeline, realtime, s2s-server}
```

Session-scoped (in-memory for this guild+channel); does NOT write
`config.yaml`. Status / readiness and TTS smoke-test commands are
**not** on the 0.4.0 `/s2s` surface — use `hermes s2s doctor` and
`hermes s2s test --text "..."` from the CLI.

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
# or pip install useful-moonshine-onnx directly
```

### "Kokoro says 'package not installed'"

```bash
pip install hermes-s2s[kokoro]
sudo apt install espeak-ng       # (Ubuntu/Debian) phonemizer backend
brew install espeak-ng           # (macOS)
```

### "Realtime mode not implemented yet"

That's expected only if your plugin is older than 0.3.1 — the realtime Discord audio bridge shipped in 0.3.1. Upgrade to 0.3.1+ and set `HERMES_S2S_MONKEYPATCH_DISCORD=1` (see HOWTO-REALTIME-DISCORD.md) to route Discord VC audio through Gemini Live / OpenAI Realtime. The realtime backend classes themselves (`gemini-live`, `gpt-realtime-mini`) work today for non-Discord use cases too (CLI, tests, future platforms). Track progress at https://github.com/baladithyab/hermes-s2s. Use `cascaded` or `s2s-server` for Discord voice if you can't upgrade.

## Architecture (one paragraph)

Each S2S mode resolves to one or more provider factories from a registry (`hermes_s2s/registry.py`). Built-in factories register at plugin load time (`providers/__init__.py`). The plugin entry-point (`hermes_s2s.__init__:register`) wires three tools, one slash command, one CLI subcommand tree, and this skill into Hermes. Cascaded mode delegates STT→LLM→TTS through the existing Hermes voice pipeline by writing audio files and reading them back; realtime mode (TBD) opens a duplex WebSocket and bridges Discord-decoded PCM both ways; s2s-server mode (TBD) routes the full turn through an external streaming-speech-to-speech server.

## Pitfalls

- **Hermes built-in voice mode is still active** — `hermes-s2s` adds providers; it does not replace `tools/transcription_tools.py` and `tools/tts_tool.py`. If a user has `stt.provider: groq` set in `voice.stt.provider` (Hermes built-in) AND `s2s.pipeline.stt.provider: moonshine` (this plugin), the built-in wins for built-in voice mode. The 0.2.0 command-provider shims (`hermes-s2s-tts` / `hermes-s2s-stt`) route cascaded mode through this plugin; realtime Discord audio bridging shipped in 0.3.1.
- **GPU contention with gaming** — local pipeline (Moonshine + Kokoro) shares VRAM with the user's RTX 5090. Document expectation; offer `device: cpu` overrides in config.
- **espeak-ng** — Kokoro's phonemizer backend on Linux. Already a Hermes voice-mode dep for NeuTTS; safe to assume it's present.
- **WSL audio I/O** — there's no audio device in WSL by default. CLI mic mode does NOT work; only Discord/Telegram bot voice (network audio) works. Document.
