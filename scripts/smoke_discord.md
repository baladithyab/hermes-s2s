# Smoke Test Plan — Discord realtime voice (0.3.1)

> Manual test procedure for verifying that `HERMES_S2S_MONKEYPATCH_DISCORD=1`
> plus `s2s.mode: realtime` actually bridges audio between a Discord voice
> channel and a realtime backend (Gemini Live or OpenAI Realtime).
>
> **This is a manual test.** CI cannot cover it — it requires a live Discord
> bot token, a server, a voice channel, and network access to the realtime
> backend. The automated smoke harness (`scripts/smoke_realtime.py`) covers
> backend connectivity only.

## Prerequisites

- [ ] A Discord bot application with a token (https://discord.com/developers/applications)
- [ ] The bot invited to a server you control, with `Connect` + `Speak` voice permissions
- [ ] A voice channel in that server and your user ID in `DISCORD_ALLOWED_USERS`
- [ ] One of: `GEMINI_API_KEY` (free tier OK) or `OPENAI_API_KEY` with Realtime access
- [ ] Linux or WSL2 host with `libopus` installed (`sudo apt install libopus0`)
- [ ] Python 3.11+ and hermes-s2s installed: `pip install 'hermes-s2s[realtime,local-all]'`
- [ ] Hermes Agent core installed and configured with the Discord platform enabled

## Setup

### 1. Env vars

```bash
export DISCORD_BOT_TOKEN='…'
export DISCORD_ALLOWED_USERS='111122223333'   # your Discord user ID
export GEMINI_API_KEY='…'                     # or OPENAI_API_KEY
export HERMES_S2S_MONKEYPATCH_DISCORD=1       # REQUIRED — bridge no-ops without this
```

### 2. `~/.hermes/config.yaml`

```yaml
plugins:
  hermes-s2s:
    mode: realtime
    realtime:
      provider: gemini-live           # or gpt-realtime-mini
      gemini-live:
        model: gemini-live-2.5-flash
        voice: Aoede
```

### 3. Sanity-check backend before touching Discord

```bash
cd /path/to/hermes-s2s
python3 scripts/smoke_realtime.py --provider gemini-live --duration-secs 5
```

Must exit `0` and write `smoke_response.wav` before you proceed. If this
fails your API key or network is broken — Discord won't help you here.

## Smoke procedure (7 steps)

1. **Start Hermes Agent** in a terminal where you can watch stdout:
   `hermes run` (or your normal launcher). Confirm the log contains
   `hermes-s2s plugin v0.3.1 registered` and `monkey-patching Discord voice bridge`.
2. **Verify bot is online** in Discord — green dot on the bot user in the member list.
3. **Join a voice channel** yourself (as a human user, with a working mic).
4. **Ask the bot to join**: type `/voice join` in any text channel the bot can see.
   Expected: bot appears in the VC within ~2s; log line `RealtimeAudioBridge started for guild …`.
5. **Speak a short prompt**: say "Hello, can you hear me?" aloud, then go silent for ~2s.
   Expected: within ~1–2s you hear the bot's voice respond through the VC.
6. **Ask a tool-requiring question** (if you have tools registered), e.g. "What time is it?"
   Expected: response arrives within soft timeout (5s). If the tool is slow, you may hear
   a brief filler gap — the bridge logs `tool_call dispatched: <name>` and
   `tool_call completed: <name> in Xs`.
7. **Leave cleanly**: `/voice leave`. Bot disconnects. Log shows
   `RealtimeAudioBridge closed for guild …` with no `Task was destroyed but it is pending` warnings.

## Expected output

**Console (Hermes log):**

```
hermes-s2s plugin v0.3.1 registered
monkey-patching Discord voice bridge (HERMES_S2S_MONKEYPATCH_DISCORD=1)
RealtimeAudioBridge started for guild 1234567890 backend=gemini-live
gemini-live: connected in 412ms
RealtimeAudioBridge closed for guild 1234567890
```

**In Discord:** bot joins the VC, plays back audio responses with <1.5s perceived latency,
disconnects cleanly on `/voice leave`.

**`smoke_response.wav`:** 16-bit PCM, 24 kHz (OpenAI) or 24 kHz (Gemini), duration
roughly equal to `--duration-secs` minus network latency. Open it in any audio
player — you should hear the bot reading the assistant response.

## Known issues / troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Bot joins but is silent | `HERMES_S2S_MONKEYPATCH_DISCORD` not set (bridge is no-op) | `export HERMES_S2S_MONKEYPATCH_DISCORD=1` then restart |
| `ImportError: scipy` | Discord audio needs resampling 48k→16k/24k | `pip install 'hermes-s2s[audio]'` |
| `No module named 'websockets'` | realtime extra missing | `pip install 'hermes-s2s[realtime]'` |
| Bot plays audio but cuts off mid-word | Buffer underflow under jitter | Expected on low-bandwidth uplinks — report with log output |
| Multiple users talking → garbled | 0.3.1 is **single-user only** (documented limitation) | Use a 1-on-1 VC for 0.3.1; multi-party mix is 0.4.0 |
| `Task was destroyed but it is pending` on `/voice leave` | Cleanup race | File an issue with the full log |
| Tool runs >5s, no filler audio | `send_filler_audio` is a stub in 0.3.1 backends | Cosmetic; result still arrives. Filler impl is 0.3.2 |

## How to report bugs

Open an issue at **https://github.com/baladithyab/hermes-s2s/issues** with:

1. The exact command/config that reproduces (redact keys).
2. Hermes Agent version (`hermes --version`) + hermes-s2s version (`hermes s2s status`).
3. Last ~50 lines of the Hermes log.
4. Output of `scripts/smoke_realtime.py --provider <same provider>` — proves
   whether the problem is backend-side or Discord-bridge-side.
5. Python version (`python3 --version`) and OS (`uname -a`).
