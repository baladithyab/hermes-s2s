# HOWTO — Realtime Voice on Discord (hermes-s2s 0.3.1)

End-to-end setup for routing a Discord voice channel through a realtime
speech-to-speech backend (Gemini Live or OpenAI Realtime). This is the
"audio actually flows" release — earlier 0.3.0 shipped the wire protocol
but left the Discord bridge as a warning log.

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
        model: gemini-live-2.5-flash
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

Full step-by-step walkthrough in [scripts/smoke_discord.md](https://github.com/codeseys/hermes-s2s/blob/main/scripts/smoke_discord.md).

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

From [research/02-realtime-apis.md §cost+latency table](https://github.com/codeseys/hermes-s2s/blob/main/docs/design-history/research/02-realtime-apis.md):

| Backend | Latency (first audio) | $/30 min |
|---|---|---|
| gemini-live (half-cascade) | ~500 ms | $0.06 |
| gpt-realtime-mini | ~350 ms | $0.45 |
| gpt-realtime (GA) | ~350 ms | $1.44 |

Add ~50–150 ms for Discord Opus decode + bridge resample + network uplink
round-trip. In practice users on a 20 ms home-broadband RTT report
**700 ms – 1.2 s** end-of-speech → start-of-response on gemini-live.

## Reporting bugs

https://github.com/codeseys/hermes-s2s/issues — include the output of
`scripts/smoke_realtime.py` for the same provider so we can tell backend
issues from bridge issues.
