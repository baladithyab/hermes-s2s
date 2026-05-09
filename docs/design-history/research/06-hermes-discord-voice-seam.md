# 06 — Hermes Discord Voice Integration Seam

**Status:** Research • **Audience:** hermes-s2s maintainers
**Source:** DeepWiki (NousResearch/hermes-agent), 3 queries

## TL;DR

Hermes has **no swap-point** for the Discord voice audio pipeline today. The
`VoiceReceiver` (sink) and `discord.FFmpegPCMAudio` (source) are constructed
inline inside `DiscordAdapter.join_voice_channel()` and `play_in_voice_channel()`
respectively. The plugin system supports platform adapters and lifecycle hooks
but **does not** expose the voice pipeline. Recommendation at bottom: **Strategy A**
(upstream factory hook) as the primary path, with **Strategy C** (monkey-patch)
as a short-term bridge behind a feature flag until the PR lands.

---

## 1. Current Wiring (as of the queried revision)

### Files / classes
- `gateway/platforms/discord.py`
  - `DiscordAdapter` — the platform adapter
  - `VoiceReceiver` — custom AudioSink-equivalent (not a `discord.AudioSink` subclass)
- `gateway/run.py`
  - `GatewayRunner._handle_voice_command` → `_handle_voice_channel_join`
  - `_handle_voice_channel_input` — post-STT callback wired to adapter

### Library
- `discord.py[voice]` (Rapptz), **not** `discord.py-voice-recv`, py-cord, or nextcord.
- `VoiceReceiver` bypasses discord.py's (non-existent) receive API and hooks the
  raw UDP socket on the `discord.VoiceClient`:
  - `_install_speaking_hook()` wraps the voice WS to map SSRC→user_id on
    opcode-5 SPEAKING events
  - `_on_packet()` is registered as a socket listener, decrypts RTP via
    `nacl.secret.Aead` with the session secret key, decodes Opus → PCM
  - Per-user PCM buffers + 1.5s silence detection → utterance completion
  - Utterances <0.5s are dropped
- Outgoing: `play_in_voice_channel(path)` builds
  `discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(path))` and calls
  `voice_client.play(...)`. `VoiceReceiver.pause()` is called first to kill echo.

### Packet flow (today)
```
Discord VC ──► VoiceReceiver (RTP/Opus→PCM, per-user buffer, VAD)
                   │  completed utterance (PCM)
                   ▼
          DiscordAdapter._process_voice_input
          (PCM→WAV → transcribe_audio → _voice_input_callback)
                   │  transcript (str)
                   ▼
   GatewayRunner._handle_voice_channel_input  (synthetic MessageEvent)
                   │  agent response (str) → TTS provider → wav/mp3 path
                   ▼
    DiscordAdapter.play_tts → play_in_voice_channel (FFmpegPCMAudio)
                   ▼
              Discord VC
```

### `/voice join` call path
1. Slash command `slash_voice` in `DiscordAdapter` → `_run_simple_slash("/voice join")`
2. `GatewayRunner._handle_voice_command` → `_handle_voice_channel_join`
3. Wires `_handle_voice_channel_input` as `adapter._voice_input_callback`
   and `_handle_voice_timeout_cleanup` as `adapter._on_voice_disconnect`
4. Calls `adapter.join_voice_channel(voice_channel)` — this is where
   `VoiceReceiver(voice_client, ...)` is instantiated and started.

### Existing extension surface
The plugin ctx exposes `register_platform`, tool registration, and lifecycle
hooks — **none** touch the voice sink/source. The documented seam is
"register a new platform adapter," which is a full adapter swap, not what we want.

---

## 2. Where to Add a Swap-Point (minimal upstream PR)

Two callables are what hermes-s2s actually needs to replace end-to-end speech:

```python
# In gateway/platforms/discord.py (or a new gateway/platforms/voice_pipeline.py)

VoicePipelineFactory = Callable[
    ["DiscordAdapter", "discord.VoiceClient", VoicePipelineConfig],
    "VoicePipeline",
]

class VoicePipeline(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_user_text(self, user_id: int, text: str) -> None: ...
    async def play_tts(self, path: str) -> None: ...   # or send_bot_text
    def pause(self) -> None: ...
    def resume(self) -> None: ...
```

Minimal diff (est. ~60–90 LOC):
1. `DiscordAdapter.__init__` gains `self._voice_pipeline_factory: Optional[VoicePipelineFactory] = None`
2. `BasePlatformAdapter.set_voice_pipeline_factory(factory)` setter
3. Plugin `ctx` gets `ctx.register_voice_pipeline_factory("discord", factory)`
4. `DiscordAdapter.join_voice_channel` pseudocode:
   ```python
   if self._voice_pipeline_factory:
       self._voice_pipeline = self._voice_pipeline_factory(self, vc, cfg)
       await self._voice_pipeline.start()
   else:
       self._voice_receiver = VoiceReceiver(vc, ...)   # default path
       self._voice_receiver.start()
   ```
5. `play_tts` / `play_in_voice_channel` branches to `self._voice_pipeline.play_tts`
   when the pipeline is set; bypass transcribe/TTS in the default STT→text→TTS path.

This is roughly a one-file, one-new-protocol PR. Backward compatible (factory
defaults to None → existing behaviour).

---

## 3. Strategy Comparison

### A. Upstream voice-pipeline-factory hook
- **Pros:** Clean separation; single connection; zero monkey-patching; future-proof;
  hermes-s2s stays small (~120 LOC for the factory + pipeline impl).
- **Cons:** Blocked on upstream review/merge + release; coordination cost.
- **Complexity:** ~60–90 LOC upstream + ~120 LOC in plugin.
- **Version-pin risk:** Low once merged — pin `hermes-agent>=X.Y`.
- **Maintenance:** Low. Protocol is the contract; refactors inside `VoiceReceiver`
  don't break us.

### B. Parallel Discord voice client in the plugin
- **Pros:** Zero upstream changes; fully independent; we control the whole stack.
- **Cons:** **Two bot voice connections in one VC is not actually supported** —
  Discord permits one voice state per user/guild. We'd need a second bot
  identity (second token, second invite, second presence) which is UX-hostile.
  Double bandwidth/CPU if it did work; duplicated SSRC mapping, echo handling;
  Hermes's `VoiceReceiver` would still run and still transcribe, racing us.
- **Complexity:** ~600–1000 LOC (full receive stack re-implementation or a
  dep on `discord-ext-voice-recv`) + bot identity plumbing.
- **Version-pin risk:** Low for Hermes, **high for discord.py** (we own the voice stack).
- **Maintenance:** High — we now own RTP/Opus/nacl code paths.

### C. Hot-patch (monkey-patch) at plugin load
- **Pros:** Ships today, no upstream PR, single voice connection.
- **Cons:** Fragile — binds to private names (`DiscordAdapter.join_voice_channel`,
  `VoiceReceiver._on_packet`, `play_in_voice_channel`). Any refactor in
  hermes-agent (which is pre-1.0 and actively changing i18n/voice internals per
  the wiki) silently breaks us or, worse, produces echo/double-STT. Hard to test.
- **Complexity:** ~150–250 LOC (patch shims + guards + version sniff).
- **Version-pin risk:** **High.** Must pin `hermes-agent==X.Y.Z` exactly
  (or a very narrow range) and gate by `importlib.metadata.version` at load.
- **Maintenance:** Medium-to-high; each Hermes release requires a smoke-test
  matrix and likely a patch update.

---

## 4. Recommendation

**Go with Strategy A, with Strategy C as a temporary, feature-flagged bridge.**
Open a small upstream PR that adds `VoicePipelineFactory` + `ctx.register_voice_pipeline_factory("discord", ...)` and branches inside `DiscordAdapter.join_voice_channel` / `play_tts` — this is ~60–90 LOC, backward compatible, and gives every future speech-to-speech plugin (not just ours) a first-class seam. While that PR is in review, ship hermes-s2s 0.3.0 with an **opt-in** (`HERMES_S2S_MONKEYPATCH=1`) monkey-patch path pinned to the current Hermes release so we can dogfood end-to-end now; delete it the release after the hook lands. Do **not** pursue Strategy B — two concurrent Discord voice connections require a second bot identity and re-implementing the RTP/Opus/nacl stack that Hermes already has working, which is a net loss.
