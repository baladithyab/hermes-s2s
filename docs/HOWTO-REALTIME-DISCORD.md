# HOWTO — Realtime Voice on Discord (hermes-s2s 0.3.1)

End-to-end setup for routing a Discord voice channel through a realtime
speech-to-speech backend (Gemini Live or OpenAI Realtime). This is the
"audio actually flows" release — earlier 0.3.0 shipped the wire protocol
but left the Discord bridge as a warning log.

## What 0.4.0 adds on top

- **`/s2s` slash command** (plugin-owned). In a Discord channel, type
  `/s2s` for a picker + status view, `/s2s mode realtime` to switch
  modes for this session, and `/s2s test "hello"` to smoke-test TTS
  from chat. Installed automatically when
  `HERMES_S2S_MONKEYPATCH_DISCORD=1` is set. Session-scoped — does
  NOT rewrite `config.yaml`; edit YAML to persist across restarts.
- **Thread mirroring.** Invoked from a thread → reuses it. Invoked
  from a plain channel → auto-creates a public thread (auto-archive
  1 day) and mirrors STT transcripts + assistant replies into it.
  Forum parents fall back to the parent channel. Templates live at
  `s2s.voice.thread_name_template` /
  `s2s.voice.thread_starter_message`. The authoritative hook site
  is `AIAgentRunner._handle_voice_channel_join` — the plugin
  monkey-patches it so `event.source.thread_id` is set *before*
  Hermes snapshots the source for subsequent voice MessageEvents.
- **Voice meta-commands.** Say `hermes <verb>` in-VC to run one of
  six verbs hands-free: `new`, `compress`, `title`, `branch`, `stop`,
  `clear`. Detected by the `MetaCommandSink` wakeword grammar and
  dispatched through the gateway's `MetaDispatcher` *before* the LLM
  sees the transcript. Wakeword configurable at `s2s.voice.wakeword`.
  (`resume` is deferred to 0.4.1.)
- **Voice persona overlay + prompt-injection defense.** Voice replies
  get a short persona overlay and a hard-coded defense block that
  refuses "ignore previous instructions" / "you are now …" payloads
  appearing inside a spoken transcript. Defense-in-depth — the usual
  `DISCORD_ALLOWED_USERS` allow-list still gates who can talk to the
  bot. See ADR-0013.
- **Four modes, one switch.** `s2s.mode ∈ {cascaded, pipeline,
  realtime, s2s-server}` — the legacy `duplex` boolean is gone.
  Auto-translates to `realtime` on first load; run
  `python -m hermes_s2s.migrate_0_4` for a clean one-shot rewrite.

See ADRs 0010–0014 and `docs/design-history/research/13-*`,
`14-*`, `15-*` for the design rationale behind each of the above.

## What 0.3.1 actually does

- **Audio bridge moves frames end-to-end.** Incoming Discord 48 kHz stereo
  Opus → decoded PCM → resampled to the backend's expected rate (16 kHz mono
  for Gemini Live, 24 kHz mono for OpenAI Realtime) → `backend.send_audio_chunk`.
  Response audio comes back as `audio_chunk` events → resampled to 48 kHz stereo
  → sliced into 20 ms / 3 840-byte frames → `discord.AudioSource.read`.
- **Single-user only.** Discord's `AudioSink.write` fires per-user-per-frame.
  0.3.1 picks the first active speaker and ignores the rest. Multi-party
  mixing is 0.4.0.
- **Tool-call bridge with timeouts.** Realtime tool-call events are
  dispatched through Hermes's tool dispatcher with a 5 s soft timeout
  (would-be filler-audio hook) and 30 s hard timeout (cancels the task,
  injects an error result). Results truncated to 4 KB. See ADR-0008.
- **Clean lifecycle.** Bridge starts on `/voice join` and closes on
  `/voice leave` / disconnect. `bridge.close()` is idempotent — no leaked
  asyncio tasks.

## What 0.3.1 doesn't do yet

- **Multi-user mixing** in a busy VC — deferred to 0.4.0.
- **Real filler audio during slow tool calls** — `send_filler_audio` is
  a stub in both Gemini Live and OpenAI Realtime backends for 0.3.1
  (raises `NotImplementedError`, the bridge catches and carries on).
  Proper per-backend filler triggers land in 0.3.2.
- **Telegram realtime** — voice messages work in cascaded mode; true
  realtime duplex requires a different transport and is scoped out.
- **Voice cloning / custom voices** — use the built-in voice names the
  backends ship with.

## Install

```bash
pip install 'hermes-s2s[realtime,local-all]'
```

The `realtime` extra pulls `websockets`; `local-all` covers the audio
resampling deps (`scipy`) plus local Moonshine/Kokoro if you want to
fall back to cascaded mode. If you only ever use realtime and never
cascaded you can drop `local-all` down to just `[realtime,audio]`.

## Environment variables

| Variable | Required? | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | if using gemini-live | https://aistudio.google.com/apikey |
| `OPENAI_API_KEY` | if using gpt-realtime(-mini) | https://platform.openai.com/api-keys |
| `DISCORD_BOT_TOKEN` | yes | Discord bot token |
| `DISCORD_ALLOWED_USERS` | yes | Comma-separated Discord user IDs allowed to invoke voice |
| `HERMES_S2S_MONKEYPATCH_DISCORD` | **yes for realtime** | Must be `1` — without it the bridge is a no-op and Hermes's built-in cascaded voice handles VC |

## Config recipes

### Cheapest: Gemini Live half-cascade (~$0.06 / 30 min)

```yaml
plugins:
  hermes-s2s:
    mode: realtime
    realtime:
      provider: gemini-live
      gemini-live:
        model: gemini-2.5-flash-native-audio-latest
        voice: Aoede           # also: Puck, Charon, Kore, Fenrir
        language_code: en-US
```

### Mid-tier: OpenAI Realtime Mini (~$0.45 / 30 min)

```yaml
plugins:
  hermes-s2s:
    mode: realtime
    realtime:
      provider: gpt-realtime-mini
      gpt-realtime-mini:
        model: gpt-4o-mini-realtime-preview
        voice: alloy           # also: echo, shimmer, verse, …
```

### Premium: OpenAI Realtime GA (~$1.44 / 30 min)

```yaml
plugins:
  hermes-s2s:
    mode: realtime
    realtime:
      provider: gpt-realtime
      gpt-realtime:
        model: gpt-realtime
        voice: alloy
```

## Verify

1. `hermes s2s status` — should print the active mode (`realtime`) and
   the resolved backend in `registered_backends.realtime`.
2. `python3 scripts/smoke_realtime.py --provider gemini-live` — exits 0
   and writes `smoke_response.wav` with ~5 seconds of audio. If this
   fails, stop here and debug the backend before touching Discord.
3. In Discord: join a voice channel, `/voice join` → bot joins within
   ~2 s. Speak a short prompt. Response audio plays back through the VC
   within 1–2 s. `/voice leave` → bot disconnects cleanly.

Full step-by-step walkthrough in [scripts/smoke_discord.md](https://github.com/baladithyab/hermes-s2s/blob/main/scripts/smoke_discord.md).

## Troubleshooting

| Symptom | Fix |
|---|---|
| Bot joins VC but is completely silent | `export HERMES_S2S_MONKEYPATCH_DISCORD=1` and restart — without the flag the bridge is intentionally a no-op so 0.3.0 users keep their built-in cascaded voice. |
| `ImportError: scipy is required for audio resampling` | `pip install 'hermes-s2s[audio]'` — Discord 48 kHz → backend 16/24 kHz needs scipy. |
| `ModuleNotFoundError: websockets` | `pip install 'hermes-s2s[realtime]'` |
| `RuntimeError: gemini-live: env var GEMINI_API_KEY is not set` | Export the key in the same shell that launches Hermes — `.env` files are not auto-loaded. |
| OpenAI session ends abruptly after 30 min | OpenAI Realtime has a hard 30-minute session cap. Bridge surfaces this as a `session_resumed`/`error` event; user must rejoin VC. Documented in `openai_realtime.py`. |
| Bot audio cuts mid-word under jitter | Jitter buffer underflow — 0.3.1 ships a fixed 60 ms prefetch. Report with bandwidth measurements if this is chronic. |
| "Task was destroyed but it is pending" on disconnect | Cleanup race — file an issue with the full log. |

## Latency expectations

From [research/02-realtime-apis.md §cost+latency table](https://github.com/baladithyab/hermes-s2s/blob/main/docs/design-history/research/02-realtime-apis.md):

| Backend | Latency (first audio) | $/30 min |
|---|---|---|
| gemini-live (half-cascade) | ~500 ms | $0.06 |
| gpt-realtime-mini | ~350 ms | $0.45 |
| gpt-realtime (GA) | ~350 ms | $1.44 |

Add ~50–150 ms for Discord Opus decode + bridge resample + network uplink
round-trip. In practice users on a 20 ms home-broadband RTT report
**700 ms – 1.2 s** end-of-speech → start-of-response on gemini-live.

## Reporting bugs

https://github.com/baladithyab/hermes-s2s/issues — include the output of
`scripts/smoke_realtime.py` for the same provider so we can tell backend
issues from bridge issues.
