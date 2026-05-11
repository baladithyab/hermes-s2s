# hermes-s2s — Configurable Speech-to-Speech for Hermes Agent

> A standalone Hermes plugin that gives Discord, Telegram, and CLI voice mode the **full S2S backend matrix**: native realtime models (Gemini Live, GPT-4o Realtime), cloud cascaded pipelines, hybrid local+cloud, and fully-local pipelines (your own Moonshine+vLLM+Kokoro server). Mix and match per-stage. Configurable per-call.

## What's new in 0.5.0

**`/s2s` rich configuration UI** (see ADR-0015,
`docs/plans/wave-0.5.0-s2s-configure.md`, and
[`docs/HOWTO-S2S-CONFIGURE.md`](docs/HOWTO-S2S-CONFIGURE.md)):

- **`/s2s configure` Discord rich panel** — ephemeral message with
  select menus (mode + realtime / STT / TTS providers) and Test /
  Reset / Refresh buttons. Selections persist immediately to
  `~/.hermes/.s2s_mode_overrides.json`.
- **Discord subcommand surface** — `/s2s mode | status | provider |
  test | doctor | reset | configure` as proper Discord slash
  subcommands.
- **Telegram `/s2s`** — inline-keyboard configuration on Telegram with
  the same mode + provider matrix.
- **CLI `/s2s`** — full subcommand router with pretty-printed status
  (no more raw JSON in your terminal).
- **Per-channel provider overrides** — `realtime_provider`,
  `stt_provider`, `tts_provider` join `mode` in the override store.
  Run `gpt-realtime-2` in one channel and `cascaded/moonshine+kokoro`
  in another without restarting the bot.
- **Backwards-compatible storage.** 0.4.x bare-string entries on disk
  auto-lift to dict shape on first read; no migration tool needed.

After upgrading: `hermes gateway restart` so Discord re-syncs the new
command Group, then `/s2s configure` in any channel to verify.

## What's new in 0.4.0

**Voice-mode rearchitecture** (see `docs/plans/wave-0.4.0-rearchitecture.md`,
ADRs 0010–0014, and research-13/14/15):

- **Four modes, one switch.** `s2s.mode ∈ {cascaded, pipeline, realtime, s2s-server}`.
  The legacy boolean `s2s.mode: duplex` is gone — the 0.4.0 config auto-translates on
  first load and a one-shot migrator (`python -m hermes_s2s.migrate_0_4`) writes the
  new shape to disk. See the migration note below.
- **Plugin-owned `/s2s` slash command.** In Discord, type
  `/s2s mode:<choice>` to switch the voice backend for this session
  without editing YAML (`mode:cascaded`, `mode:pipeline`,
  `mode:realtime`, `mode:s2s-server`). The change is session-scoped —
  it doesn't rewrite your `~/.hermes/config.yaml`. Global defaults
  still come from config.
- **Thread mirroring.** When `/voice join` or `/s2s` is invoked **from a thread**,
  the bot reuses that thread for transcripts. Invoked from a **plain channel**,
  the bot auto-creates a public thread (auto-archive: 1 day) and mirrors all
  STT transcripts + assistant replies into it. Forum parents fall back to
  the parent channel. Thread-name and starter-message templates are
  configurable under `s2s.voice.thread_name_template` /
  `s2s.voice.thread_starter_message`.
- **Voice meta-commands** — say the wakeword, then a full natural
  phrase. The built-in grammar (see `hermes_s2s/voice/meta.py`)
  recognises:
  - `"hey aria, start a new session"` (or `"begin"` / `"open a new chat"`)
  - `"hey aria, compress the context"` (or `"condense"` / `"summarize"`)
  - `"hey aria, title this session as <title>"` (or `"name this chat to …"`)
  - `"hey aria, branch off here"` (or `"fork from this point"`)
  - `"hey aria, clear the context"` (or `"reset the session"`)
  - `"hey aria, stop"` (or `"cancel"` / `"hush"` / `"quiet"`) — interrupts TTS
  These match on full phrases, not bare verbs — `"hey aria new"` won't
  match; you need `"hey aria, start a new session"`. Six verbs in
  0.4.0; `resume` is deferred to 0.4.1. The wakeword defaults to
  `"hey aria"` and is configurable under `s2s.voice.wakeword`.
- **Voice persona overlay + prompt-injection defense.** Voice mode now
  layers a short persona prompt over Hermes's base system prompt, with
  a hard-coded prompt-injection-defense block that refuses overrides of
  the "ignore previous instructions" / "you are now …" family from
  within a spoken transcript. See ADR-0013.
- **2-bucket tool-export policy (provisional for 0.4.0).** Voice tools
  are split into always-on (`hermes_meta_*` + a small allow-list:
  `web_search`, `vision_analyze`, `text_to_speech`, `clarify`) and
  deny-listed (everything else, fail-closed), enforced by a CI fence
  that imports the canonical core-tools list at test time. The third
  `ask` bucket with a synchronous voice-confirm flow ("ARIA wants to
  read FILENAME — say yes or no") is deferred to 0.4.1. See
  ADR-0014 "0.4.0 implementation note".

**Migration path.** The old `s2s.mode: duplex` boolean is silently
auto-translated to `s2s.mode: realtime` on first load (sentinel guard
prevents re-translation). For a clean one-shot rewrite run:

```bash
python -m hermes_s2s.migrate_0_4            # rewrite in place (creates .bak)
python -m hermes_s2s.migrate_0_4 --dry-run  # show the diff, write nothing
python -m hermes_s2s.migrate_0_4 --rollback # restore from .bak
```

See ADR-0014 for the full translation table.

## What this is

Hermes ships built-in voice mode with a fixed cascaded STT → LLM → TTS pipeline and a small set of providers. `hermes-s2s` extends that with:

- **Realtime duplex backends** — Gemini Live, GPT-4o Realtime / gpt-4o-mini-realtime
- **Per-stage configurability** — local STT + cloud LLM + local TTS, any combination
- **Local pipeline integration** — wraps your own [streaming-speech-to-speech](https://huggingface.co/spaces/Codeseys/streaming-speech-to-speech-demo) (or compatible) server as a one-shot duplex backend
- **Provider registry** — Moonshine, Kokoro, faster-whisper, ElevenLabs, OpenAI, Groq, and any custom CLI-wrapped provider
- **Modes** — `cascaded` (STT-then-LLM-then-TTS), `realtime` (native duplex WS), `s2s-server` (full-pipeline external server)

## Why a plugin instead of a fork

Hermes built-in voice mode is great for the common case. This plugin is for power users who want:

- Sub-300ms latency via realtime models or local vLLM pipelines
- Strict privacy (everything local) or strict cost control (free local + cheap cloud LLM)
- Per-call or per-channel backend switching
- Their own infrastructure (HuggingFace S2S server, local GPU rig) plugged in

Forking Hermes for this would diverge the codebase. A plugin opts users in cleanly.

## 30-second install

```bash
hermes plugins install baladithyab/hermes-s2s
~/.hermes/hermes-agent/venv/bin/python3 -m pip install -e ~/.hermes/plugins/hermes-s2s'[all]'
hermes plugins enable hermes-s2s
hermes s2s setup realtime-gemini
hermes s2s doctor
```

`hermes plugins install` clones the repo into `~/.hermes/plugins/hermes-s2s/`
where Hermes can discover it. The `pip install -e` step uses **Hermes' own
venv python** (not your shell's pip) so the plugin's deps land where the
`hermes` CLI can actually import them. This is the most common install
gotcha — `pip install` from your shell may install into a different env.

The doctor command tells you what's missing (API keys, system deps).
Once it's all green, restart `hermes gateway` and `/voice join` in any
Discord VC the bot has access to.

> **Note on `hermes s2s setup`:** profile is positional (`hermes s2s setup
> realtime-gemini`) because Hermes' top-level `--profile` flag would
> otherwise eat our subcommand's `--profile`. The flag form
> `hermes s2s setup --use-profile realtime-gemini` also works.

### Other profiles

| Profile | What you get | $/30min |
|---|---|---|
| realtime-gemini | Gemini Live half-cascade | $0.06 |
| realtime-openai | gpt-realtime premium | $1.44 |
| realtime-openai-mini | gpt-realtime-mini | $0.45 |
| local-all | Moonshine + Kokoro (cascaded) | electricity only |

Full profile matrix + per-OS system deps: [docs/INSTALL.md](https://github.com/baladithyab/hermes-s2s/blob/main/docs/INSTALL.md).
End-to-end voice-mode walkthrough + troubleshooting: [docs/HOWTO-VOICE-MODE.md](https://github.com/baladithyab/hermes-s2s/blob/main/docs/HOWTO-VOICE-MODE.md).

> **What this changes in your Hermes config**
>
> `hermes s2s setup` doesn't fork Hermes — it writes standard Hermes command-provider entries that invoke the `hermes-s2s-tts` / `hermes-s2s-stt` console scripts shipped by this package. See [ADR-0004](docs/adrs/0004-command-provider-interception.md) for the integration rationale.
>
> ```yaml
> # ~/.hermes/config.yaml (excerpt, written by `hermes s2s setup`)
> tts:
>   provider: hermes-s2s-kokoro
>   providers:
>     hermes-s2s-kokoro:
>       type: command
>       command: "hermes-s2s-tts --provider kokoro --voice af_heart --output {output_path} --text-file {input_path}"
>       output_format: wav
> ```
>
> ```bash
> # ~/.hermes/.env (appended by `hermes s2s setup`)
> HERMES_LOCAL_STT_COMMAND='hermes-s2s-stt --provider moonshine --model tiny --input {input_path} --output {output_path}'
> ```

For the full mechanics of each path, worked examples, and troubleshooting, see [docs/HOWTO-VOICE-MODE.md](docs/HOWTO-VOICE-MODE.md).

## Architecture

Three top-level modes, six provider categories:

```
                          ┌─ s2s.mode: cascaded ──────────────┐
                          │   STT → AIAgent (Hermes LLM) → TTS │
                          │   Each stage independently         │
                          │   configurable, mix local/cloud    │
                          └────────────────────────────────────┘
                          ┌─ s2s.mode: realtime ──────────────┐
 Discord/Telegram/CLI ─▶  │   Discord-Opus ↔ Realtime WS       │
        Voice              │   (Gemini Live / OpenAI Realtime)  │
                          │   Tool calls bridged to Hermes     │
                          └────────────────────────────────────┘
                          ┌─ s2s.mode: s2s-server ────────────┐
                          │   WS to your own server            │
                          │   (full pipeline lives there)      │
                          │   Lowest latency on local hardware │
                          └────────────────────────────────────┘
```

## Backend matrix

| Mode | STT | LLM | TTS | Latency | $/30min | When |
|------|-----|-----|-----|---------|---------|------|
| cascaded | groq | (Hermes) | edge | ~1.5s | ~$0.05 | Cheap cloud, default |
| cascaded | local | (Hermes) | elevenlabs | ~1.2s | ~$0.10 | Privacy-in, premium voice-out |
| cascaded | moonshine | (Hermes) | kokoro | ~0.6s | $0 | Fully local, free |
| cascaded | openai | (Hermes) | openai | ~1.0s | ~$0.15 | All cloud, OpenAI ecosystem |
| realtime | — | gemini-live half-cascade | — | ~600ms | $0.06 | Cheapest realtime |
| realtime | — | gpt-realtime | — | ~700ms | $1.44 | Premium voice quality |
| realtime | — | gpt-realtime-mini | — | ~700ms | $0.45 | Mid-tier realtime |
| s2s-server | (server) | (server) | (server) | ~250ms | electricity | Local v6 pipeline |

## Status

**0.4.0 (current):** voice-mode rearchitecture. Four explicit modes
(`cascaded` / `pipeline` / `realtime` / `s2s-server`), a plugin-owned
`/s2s mode:<choice>` slash command, automatic Discord-thread mirroring
for transcripts, six wakeword-prefixed voice meta-commands
(`"hey aria, start a new session"` / `"… compress the context"` /
`"… title this session as X"` / `"… branch off here"` / `"… stop"` /
`"… clear the context"`), a voice persona overlay with
prompt-injection defense, and a fail-closed 2-bucket tool-export
policy enforced by CI (the 3-bucket posture with a synchronous
voice-confirm flow is deferred to 0.4.1 — see ADR-0014 "0.4.0
implementation note"). Legacy `s2s.mode: duplex` auto-translates to
`s2s.mode: realtime` on first load; see the 0.4.0 section above for
the full migration path. Design: ADRs 0010–0014 and
`docs/design-history/research/13-*`, `14-*`, `15-*`.

**0.3.2:** plug-and-play install + `hermes s2s doctor` diagnostic + backend filler audio. `hermes s2s setup --profile realtime-gemini` writes the full realtime config + monkey-patch flag in one shot; `hermes s2s doctor` runs a readiness check over config, Python deps, system deps, API keys, and an optional backend-WS probe. Backend filler audio (`send_filler_audio`) now lands for both Gemini Live and OpenAI Realtime so slow tool calls no longer produce dead air. See [docs/INSTALL.md](https://github.com/baladithyab/hermes-s2s/blob/main/docs/INSTALL.md) for the install profile matrix and [docs/HOWTO-VOICE-MODE.md](https://github.com/baladithyab/hermes-s2s/blob/main/docs/HOWTO-VOICE-MODE.md#diagnosing-problems-with-hermes-s2s-doctor) for the doctor walkthrough.

**0.3.1:** realtime audio actually flows through Discord. With `HERMES_S2S_MONKEYPATCH_DISCORD=1` + `s2s.mode: realtime`, `/voice join` bridges a Discord VC end-to-end through Gemini Live or OpenAI Realtime — incoming 48 kHz stereo Opus is decoded, resampled, and streamed to the backend; responses come back as 20 ms / 3 840-byte frames into `discord.AudioSource`. Tool calls round-trip through Hermes's dispatcher with 5 s soft / 30 s hard timeouts. See [docs/HOWTO-REALTIME-DISCORD.md](docs/HOWTO-REALTIME-DISCORD.md) to set it up and [scripts/smoke_realtime.py](scripts/smoke_realtime.py) to validate your API keys standalone.

- [x] Project scaffold + plugin manifest
- [x] Config schema + provider registry interfaces
- [x] Moonshine STT provider (local) — ported from Hermes feat/aria-voice
- [x] Kokoro TTS provider (local) — ported from Hermes feat/aria-voice
- [x] `hermes-s2s-tts` / `hermes-s2s-stt` CLI shims (0.2.0)
- [x] `hermes s2s setup` interactive wizard (0.2.0)
- [x] `s2s-server` provider — HTTP REST (0.2.0), WebSocket pipeline (0.3.0)
- [x] **Realtime backends: Gemini Live, GPT-4o Realtime wire protocol** (0.3.0)
- [x] **Discord audio bridge actually moves frames** (0.3.1 — see ADR-0007)
- [x] **Tool-call bridging with timeouts** (0.3.1 — see ADR-0008)
- [x] **Plug-and-play install + `hermes s2s doctor`** (0.3.2 — see ADR-0009)
- [x] **Per-backend filler-audio implementation** (0.3.2)
- [x] **Four explicit modes + `/s2s mode:<choice>` slash + thread mirroring + meta-commands + persona overlay** (0.4.0 — ADRs 0010–0014)
- [ ] Multi-user VC mixing (0.4.1)
- [ ] Filler-audio cancellation when the tool result arrives early (0.4.1)
- [ ] Voice meta-command `resume` verb (0.4.1)
- [ ] 3-bucket tool-export (`ask` bucket with synchronous voice-confirm flow) (0.4.1 — see ADR-0014 "0.4.0 implementation note")
- [ ] Daemon mode to eliminate per-call model-load cost (0.4.x)
- [ ] Telegram realtime duplex (0.5.0)
- [ ] PyPI publish

> **Known issues in 0.3.2:**
>
> - **Single-user only.** Discord's `AudioSink.write` fires per-user-per-frame; the bridge picks the first active speaker and ignores the rest. Multi-party mixing is scoped to 0.4.0.
> - **Filler audio is fire-and-forget.** `send_filler_audio("let me check on that")` now plays, but if the tool result lands mid-phrase, the filler is NOT interrupted — you'll hear both. Cancellation is scoped to 0.4.0.
> - **Realtime requires the monkey-patch flag.** `HERMES_S2S_MONKEYPATCH_DISCORD=1` must be set explicitly; the `realtime-*` setup profiles append this automatically, but if you're editing `.env` by hand you still need the line.

See `docs/ROADMAP.md` for the milestone breakdown.

## Compatibility

- **Hermes Agent**: ≥ 0.X (verify against your installed version)
- **Python**: 3.11+
- **OS**: Linux, WSL2, macOS — Windows native untested
- **GPU**: Optional. CUDA for Moonshine/Kokoro acceleration; both work on CPU.

## License

Apache-2.0. Bundled provider code (Moonshine, Kokoro, etc.) keeps its own upstream licenses.
