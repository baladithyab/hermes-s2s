# ARIA — Discord Voice Library Research

Research notes for an AI voice bot needing **bidirectional** audio
(RECV incoming user voice → STT/LLM → TTS → SEND). Terse.

---

## 1. Library Choice

| Option | Send | Recv | Notes |
|---|---|---|---|
| **discord.py** (Rapptz) | ✅ | ❌ native | Maintainer declared recv out-of-scope ([issue #1094](https://github.com/Rapptz/discord.py/issues/1094)). |
| **py-cord** | ✅ | ✅ via `discord.sinks` (WaveSink/MP3Sink/custom `Sink`) — file-oriented, `vc.start_recording(sink, cb, channel)` ([docs](https://docs.pycord.dev/en/master/api/sinks.html)). |
| **discord-ext-voice-recv** | ✅ (uses d.py) | ✅ streaming `AudioSink` with per-packet `write(VoiceData)` callback ([GitHub](https://github.com/imayhaveborkedit/discord-ext-voice-recv)). |
| disnake / nextcord | ✅ | ❌ native | d.py forks; same gap. |

### Pick: **discord.py + `discord-ext-voice-recv`**

1. Streaming sink API matches real-time AI pipeline (`write()` fires per 20 ms Opus/PCM packet, per-SSRC); py-cord sinks are recording-oriented (finalize at `stop_recording`).
2. `VoiceRecvClient` is a drop-in `VoiceProtocol`, no monkey-patching — coexists with stock d.py voice-send (`AudioSource`).
3. d.py is the most-maintained upstream (gateway, slash commands, rate limits). py-cord lags on some Discord API changes.
4. Author `imayhaveborkedit` is ex-d.py voice contributor; decodes Opus→PCM, handles jitter buffer, speaking state, SSRC→user mapping.
5. MIT-licensed, installable from PyPI (`discord-ext-voice-recv`) or git.

---

## 2. Install

```bash
python -m pip install -U "discord.py[voice]" discord-ext-voice-recv
# [voice] pulls PyNaCl; you still need native libs:
sudo apt install -y libopus0 libopus-dev libsodium23 ffmpeg
```

- **WSL2**: audio devices aren't exposed — fine for a server bot (no local mic/speaker needed, all I/O is Discord RTP). Just apt-install above. Source: [discord.py voice docs](https://discordpy.readthedocs.io/en/latest/api.html) note opus must be loadable.
- **libopus discovery**: d.py calls `ctypes.util.find_library('opus')`. On slim Alpine/Debian containers, add `libopus0` explicitly; on Ubuntu 22.04/24.04 the WSL default image usually has it missing — confirm with `python -c "import discord; discord.opus.load_opus('opus'); print(discord.opus.is_loaded())"`.
- **PyNaCl wheels**: on non-x86_64 or musl libc, PyNaCl may build from source → need `libsodium-dev` + `build-essential`. FFmpeg only required if you pipe through `FFmpegPCMAudio`; pure PCM playback via `discord.PCMAudio` does not need it.

---

## 3. Bot Setup

**Intents** (Discord dev portal + code):

- `voice_states` — required to track members joining/leaving VC (NOT privileged).
- `guilds` — default on; needed for guild/channel cache.
- `guild_messages` + `message_content` (privileged) — only if you also respond to text triggers; slash-command-only bots can skip `message_content`.
- `members` (privileged) — useful to resolve SSRC→`Member` reliably.

**Privileged-intent toggles** (portal → Bot → "Privileged Gateway Intents"):
Server Members Intent = ON; Message Content Intent = ON if used.

**OAuth2 scopes**: `bot` + `applications.commands`.
**Bot permissions** (integer 3148800 at minimum):
`Connect` (0x100000), `Speak` (0x200000), `Use Voice Activity` (0x2000000),
`Send Messages`, `Use Application Commands`.

---

## 4. Code Skeleton (~60 lines)

```python
# aria_voice_bot.py  — discord.py 2.x + discord-ext-voice-recv
import asyncio, os
import discord
from discord.ext import commands, voice_recv

INTENTS = discord.Intents.default()
INTENTS.voice_states = True
INTENTS.members = True
bot = commands.Bot(command_prefix="!", intents=INTENTS)

# --- Outgoing: pull 20ms PCM frames (48kHz, s16le, stereo = 3840 bytes) from queue
class QueuedPCM(discord.AudioSource):
    def __init__(self, q: asyncio.Queue[bytes]):
        self.q = q
    def is_opus(self) -> bool: return False
    def read(self) -> bytes:
        try:
            return self.q.get_nowait()
        except asyncio.QueueEmpty:
            return b"\x00" * 3840  # silence → keeps RTP flowing
    def cleanup(self): pass

# --- Incoming: callback-per-frame sink (decoded PCM by default)
class ARIASink(voice_recv.AudioSink):
    def __init__(self, on_frame):
        super().__init__()
        self.on_frame = on_frame  # async-safe callable(user_id:int, pcm:bytes)
    def wants_opus(self) -> bool: return False  # want decoded 48k s16le stereo
    def write(self, user: discord.Member | None, data: voice_recv.VoiceData):
        uid = user.id if user else 0
        # data.pcm is 20ms = 3840 bytes; schedule to loop
        asyncio.run_coroutine_threadsafe(
            self.on_frame(uid, data.pcm), bot.loop
        )
    def cleanup(self): pass

OUTGOING: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)  # ~1s buffer

async def handle_user_frame(uid: int, pcm: bytes):
    # TODO: VAD → STT(Whisper) → LLM → TTS → push PCM to OUTGOING
    pass

@bot.tree.command(description="Join your voice channel")
async def join(inter: discord.Interaction):
    if not inter.user.voice or not inter.user.voice.channel:
        return await inter.response.send_message("Join a VC first.", ephemeral=True)
    vc = await inter.user.voice.channel.connect(cls=voice_recv.VoiceRecvClient)
    vc.listen(ARIASink(handle_user_frame))
    vc.play(QueuedPCM(OUTGOING))
    await inter.response.send_message("🎙️ ARIA online.")

@bot.tree.command(description="Leave voice")
async def leave(inter: discord.Interaction):
    if inter.guild.voice_client:
        await inter.guild.voice_client.disconnect()
    await inter.response.send_message("👋")

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")

bot.run(os.environ["DISCORD_TOKEN"])
```

Notes:
- `voice_recv.VoiceData.pcm` is already-decoded 48 kHz s16le stereo, 20 ms/packet
  (sink API mirrors d.py `AudioSource`; `wants_opus=False` → lib runs decoder).
- `play()` and `listen()` run concurrently on the same `VoiceRecvClient`.
- Returning silence from `read()` on empty queue is standard — stopping the source
  triggers `speaking=False` and reconnect churn.

---

## 5. Latency / Jitter Budget

Round-trip (user's mouth → ARIA speaker) realistic on a same-region bot:

| Stage | Typical |
|---|---|
| Discord RTP ingress jitter | 20–60 ms (one 20 ms frame + buffer) |
| VAD + endpoint detect | 200–400 ms (waiting for pause) |
| Whisper/STT streaming | 150–500 ms (faster-whisper small, GPU) |
| LLM first token / full reply | 300–2000 ms (local 7B ≈ 300 ms TTFT) |
| TTS synth (streaming, e.g. Piper/XTTS) | 150–800 ms to first audio |
| Opus encode + RTP egress | 20–40 ms |
| **Total perceived** | **~1.0 – 3.5 s** to first response audio |

Jitter: Discord target 20 ms packet cadence; expect ±10 ms under normal load,
occasional 60–100 ms spikes. Keep a 200–400 ms outgoing PCM jitter buffer in `OUTGOING`
(≈ 10–20 queued frames) to absorb TTS stutter without starving `read()`.

Full-duplex "interrupt me" latency floor ≈ 400–700 ms (VAD + 1–2 LLM tokens
+ immediate TTS cut). Streaming TTS + speculative LLM decoding required to beat 1 s.

---

## 6. OSS Reference Projects

1. **imayhaveborkedit/discord-ext-voice-recv** — <https://github.com/imayhaveborkedit/discord-ext-voice-recv>
   Canonical voice-recv extension; `examples/recv.py` shows `BasicSink` callback + `VoiceRecvClient` wiring.
2. **MCKRUZ/openclaw-voice** — <https://github.com/MCKRUZ/openclaw-voice>
   AI voice bot with OpenAI-compatible STT/TTS + turn-detection; closest architectural match to ARIA.
3. **CobCob047/discord-voice-ai-bot** — <https://github.com/CobCob047/discord-voice-ai-bot>
   Real-time Whisper + Ollama over Discord voice; demonstrates per-user SSRC stream handling at scale.

---

## Sources

- discord-ext-voice-recv README & PyPI: <https://pypi.org/project/discord-ext-voice-recv/>
- discord.py RFC #1094 (voice recv scope): <https://github.com/Rapptz/discord.py/issues/1094>
- Py-cord sinks API: <https://docs.pycord.dev/en/master/api/sinks.html>
- discord.py voice API: <https://discordpy.readthedocs.io/en/latest/api.html>
- GitHub topic `voice-bot` (python): <https://github.com/topics/voice-bot?l=python>
