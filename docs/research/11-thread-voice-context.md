# 11 — Thread ↔ Voice-Channel Session Context (Hermes core + s2s plugin)

Scope: trace how `/voice join` wires a Discord VC to a text chat/thread today,
and design the minimum extension so the plugin can route voice turns through a
thread (existing or auto-created) and mirror every utterance as text.

Hermes commit surveyed: `/home/codeseys/.hermes/hermes-agent` as of the current
checkout. All line numbers cite that tree.

## Findings (current state)

1. **Slash entry-point — `discord.py:2962–2978`.** A single `/voice` command
   with a `mode` choice (`join|channel|leave|on|tts|off|status`) dispatches
   into the shared text runner via `self._run_simple_slash(interaction,
   f"/voice {mode}")`. The interaction's `channel` (thread OR text channel OR
   DM) is captured by `_build_slash_event` (`discord.py:3375–3418`), which sets
   `chat_type = "thread"` and `thread_id = str(interaction.channel_id)` **only
   when `interaction.channel` is already a `discord.Thread`**. Plain channels
   yield `chat_type="group"`, `thread_id=None`.

2. **Runner dispatch — `run.py:9083 → 9125`.** `/voice join` routes to
   `_handle_voice_channel_join(event)`. This handler has full access to
   `event.source` (the channel/thread the command was invoked from), the
   `guild_id`, and the adapter. It:
   - Wires `adapter._voice_input_callback = self._handle_voice_channel_input`
     (`run.py:9144`) **before** connecting.
   - Calls `await adapter.join_voice_channel(voice_channel)` (`run.py:9149`).
   - On success, binds the invoking *text* channel to the guild via
     `adapter._voice_text_channels[guild_id] = int(event.source.chat_id)`
     (`run.py:9162`) and snapshots the full `SessionSource` into
     `adapter._voice_sources[guild_id] = event.source.to_dict()`
     (`run.py:9164`). **This snapshot is the only plumbing linking VC audio to
     a text session** — keep it in mind, the extension hinges on it.

3. **Adapter-side join — `discord.py:1857–1889`.** `join_voice_channel(channel)`
   takes **only a VC channel** — no thread, no guild context beyond
   `channel.guild.id`, no source. It starts a `VoiceReceiver` + background
   `_voice_listen_loop` (`discord.py:2081`) and stores nothing about the
   invoking text context. Context is injected *by the runner* via
   `_voice_text_channels` / `_voice_sources` *after* the call returns. The s2s
   plugin's monkey-patch wraps this method (`discord_bridge.py:165-176`), so
   the same injection pattern is available to the plugin post-join.

4. **VC → text session replay — `run.py:9250–9316`.** When the receiver emits
   a transcript, `_handle_voice_channel_input` rebuilds a `SessionSource` by
   deserialising `adapter._voice_sources[guild_id]` (`run.py:9269–9272`). It
   then **posts the transcript back to the bound text channel** with
   `channel.send(f"**[Voice]** <@{user_id}>: {safe_text}")`
   (`run.py:9298–9301`) and synthesises a `MessageType.VOICE` `MessageEvent`
   that is fed through the normal pipeline (`adapter.handle_message(event)`).
   **Key insight:** this is already the transcript-mirror seam. Whatever
   `source.thread_id` / `chat_id` was captured at join time determines where
   the mirror lands.

5. **Session key — `session.py:594–659` (`build_session_key`).** For non-DM
   chats the key is `agent:main:{platform}:{chat_type}:{chat_id}[:{thread_id}][:user]`.
   `thread_id` is appended as a dedicated segment (`session.py:646`) and
   thread sessions default to *shared across users* (not per-user, see
   `session.py:649–654`). So: **if the `SessionSource` snapshot the runner
   stashes at VC-join has `thread_id` set, the voice session auto-routes
   through the thread's key**. If it doesn't, voice and thread text diverge.

6. **Existing thread auto-create paths.** Two uses exist today — neither
   triggered by `/voice join`:
   - `discord.py:1467` — `forum_channel.create_thread()` inside
     `_send_to_forum`, used when an agent *send* targets a forum channel.
   - `discord.py:3576-3648` — `_create_thread(parent_channel, name, ...)`
     called from `/thread` slash and as a fallback in `_auto_create_thread`
     (`discord.py:3651-3681`), which wraps `message.create_thread(...)`.
     These are the building blocks to reuse for the on-join thread creation —
     `message.create_thread()` or `parent.create_thread(type=PUBLIC_THREAD)`
     are the discord.py surfaces we want.

7. **`ThreadParticipationTracker` — `platforms/helpers.py:201–262`,
   instantiated at `discord.py:555` as `self._threads`.** Persists a set of
   thread IDs the bot has spoken in to `~/.hermes/discord_threads.json`
   (bounded to 500 entries). API: `self._threads.mark(thread_id)`,
   `thread_id in self._threads`. Used to decide whether follow-up replies in
   a thread need an @mention. A new voice-thread created by our extension
   **must call `adapter._threads.mark(new_thread.id)`** so subsequent text
   replies don't expect mentions.

8. **`_enrich_message_with_transcription` — `run.py:12506–12580`, called at
   `run.py:6305`.** This path is for *attached audio files on a normal
   message event* (e.g. a Discord voice-message attachment) — it runs
   `transcribe_audio` and returns a prepended `[The user sent a voice
   message~ "…"]` caption. **It does NOT post the transcript back to the
   channel**; the string is only injected into the agent's user-turn text.
   Therefore, for our requirement, the `_handle_voice_channel_input` path
   (finding 4, with its explicit `channel.send(...)`) is the correct model to
   copy — not `_enrich_message_with_transcription`.

## Recommended architecture

```
                        ┌────────────────────────────────────────┐
 /voice join  ──►  Hermes runner: _handle_voice_channel_join    │
 (interaction      │ 1. resolve target thread via resolver hook ◄─── NEW
  from #chan       │     • invoked-in-thread? → reuse            │
  or thread)       │     • invoked-in-channel? → create thread   │
                   │ 2. mutate event.source: chat_type="thread", │
                   │    thread_id=<target>, chat_id=<parent>     │
                   │ 3. existing code path continues:            │
                   │    adapter._voice_text_channels[gid]=thread │
                   │    adapter._voice_sources[gid]=source.dict  │
                   └──────────────┬─────────────────────────────┘
                                  ▼
        (VC audio) ──► VoiceReceiver ──► _voice_listen_loop
                                  │
                                  ▼
                   _handle_voice_channel_input  (run.py:9250)
                   - source rebuilt from _voice_sources (now thread-scoped)
                   - session_key = build_session_key(source) → thread key ✓
                   - channel.send("[Voice] @user: …")          ← USER STT mirror
                   - handle_message(event) → agent reply
                                  │
                                  ▼
                   agent reply → _send_voice_reply (TTS plays in VC)
                               + base adapter.send(thread, text)  ← ARIA reply
                                                                     lands in
                                                                     thread
                                                                     already (5)
```

Entry-point summary (where the plugin plugs in):

| Concern                    | Seam                                                         |
|----------------------------|--------------------------------------------------------------|
| Pick/create thread at join | new hook: `voice_thread_resolver(event) -> thread_id | None` |
| Mutate `source`            | runner calls resolver **before** writing `_voice_sources`    |
| User STT mirror            | already posts to `_voice_text_channels[guild_id]` (→ thread) |
| ARIA text reply mirror     | `adapter.send(chat_id=thread_id, …)` — already correct once  |
|                            | `_voice_text_channels` points at the thread                  |
| Realtime input transcripts | `gemini_live.py:299-306` (`inputTranscription`) — emit       |
|                            | `transcript_partial {role:user}` → plugin consumer posts to  |
|                            | `_voice_text_channels[gid]` via `adapter._client.get_channel`|
| Realtime output transcripts| `gemini_live.py:291-298` (`outputTranscription`) — same      |

## Option comparison

| Option | Where it sits | LOC | Upstream-break risk | Notes |
|--------|--------------|-----|---------------------|-------|
| **A — native hook in Hermes core** (`register_voice_thread_resolver`) | New 10-line extension point in `run.py` between `_handle_voice_channel_join:9138` (channel check) and `:9162` (bind text channel). Plugin registers via `register()` ctx. | core ~30; plugin ~120 | Low — single documented seam. | Preferred. Matches the existing `ctx.register_voice_pipeline_factory` pattern the plugin already probes for (`discord_bridge.py:92`). |
| **B — monkey-patch `_handle_voice_channel_join`** on `GatewayRunner` | Plugin replaces the bound method. | 0 core; plugin ~150 | High — method body changes often (9125-9174 touches auth, timeouts, TTS state). | Brittle. Avoid. |
| **C — monkey-patch `DiscordAdapter.join_voice_channel`** (current wrap) extended to accept/create a thread | Plugin-only, sits *after* runner's `_voice_sources` write → too late to change `source.thread_id` unless we also mutate `adapter._voice_sources[gid]` post-hoc. | 0 core; plugin ~80 | Medium — works today via `_voice_sources` rewrite, but races with `_handle_voice_channel_input` if audio arrives before we patch it. | Works as **fallback** when core lacks Option A. |

Recommended path: **ship Option C now (zero core changes), upstream Option A**
as an ADR (`ADR-0008: voice_thread_resolver`). The existing
`_install_bridge_on_adapter(adapter, channel, ctx)` is already called inside
the `join_voice_channel_wrapped` coroutine after `orig(self, channel)` — so
it can, in the same critical section, (a) resolve/create the thread and
(b) rewrite `adapter._voice_sources[gid]["thread_id"]` +
`adapter._voice_text_channels[gid]` before the listen loop publishes the
first packet.

## Diff scope estimate

**Hermes core (if Option A upstreamed):**
- `gateway/run.py`: +~20 LOC. New `self._voice_thread_resolvers: dict`,
  `register_voice_thread_resolver()`, invoke resolver at top of
  `_handle_voice_channel_join` before line 9162.
- `gateway/plugin_context.py` (or wherever `ctx` is assembled): expose
  `register_voice_thread_resolver`. ~5 LOC.

**hermes-s2s plugin:**
- `hermes_s2s/_internal/discord_bridge.py`: +~80 LOC — add
  `_resolve_or_create_thread(adapter, channel, source)` helper, call it
  from `_install_bridge_on_adapter`, rewrite `_voice_sources`/
  `_voice_text_channels`, call `adapter._threads.mark(thread_id)`.
- `hermes_s2s/_internal/transcript_mirror.py`: new ~100 LOC — consumes
  `RealtimeEvent(type="transcript_partial", role={user,assistant})` from
  `providers/realtime/gemini_live.py:291–306` (and OpenAI at
  `openai_realtime.py:209,215`), batches by turn, and calls
  `adapter._client.get_channel(thread_id).send(...)`. For cascaded mode no
  extra work needed — `run.py:9298–9301` already mirrors user STT, and the
  agent's text reply is sent to the (now-thread) `chat_id` by the standard
  send path.
- `hermes_s2s/__init__.py`: register both via `ctx` hooks.
- Tests: `tests/test_thread_voice_context.py` ~150 LOC covering
  (thread-invoked / channel-invoked / realtime) × (transcript mirrored).

**Total:** core ~25 LOC, plugin ~330 LOC (incl. tests). Fallback-only path
(Option C without core changes) is ~250 LOC plugin-side.

## Risk & mitigation

- **`_handle_voice_channel_join` body drifts upstream.** Mitigation: we only
  mutate `adapter._voice_sources` / `_voice_text_channels` (public-in-
  practice, used since ≥0.3.0). Add `SUPPORTED_HERMES_RANGE` gating in
  `discord_bridge.py` (already present — `_hermes_version_supported`,
  line 186-214). Extend the version probe to assert `_voice_sources` /
  `_voice_text_channels` attribute presence at wrap time and degrade to a
  warn-and-skip with no monkey-patch (matches existing failure modes from
  tests `test_install_bails_cleanly_when_gateway_module_missing`).
- **`build_session_key` signature changes.** Session key is constructed
  *inside* the runner — the plugin never calls it directly. Mutating
  `source.thread_id` is sufficient and stable across the documented
  `SessionSource` contract (`session.py:581-591`).
- **Thread auto-archive (1440 min default) closes the voice thread mid-call.**
  Mitigation: set `auto_archive_duration=10080` when we create the voice
  thread (matches Discord API max for non-boosted guilds; `_create_thread`
  already accepts this kwarg — `discord.py:3613`).
- **Forum channels have no plain `create_thread(message=…)` starter model.**
  Voice-join from a forum root channel is an edge case; detect
  `isinstance(channel, discord.ForumChannel)` and fall back to `/voice` in
  the channel (no thread) with a warning — matches today's behaviour.
- **Pre-existing duplicate-suppressor.** `_is_duplicate_voice_transcript`
  (`run.py:9209-9248`) dedups user STT over a 12s window — our thread mirror
  runs *after* that check so we won't double-post.

## Open questions for follow-up

- Should `/voice leave` auto-archive the created thread? Probably yes when
  the thread was *auto*-created (track with an `adapter._voice_autothreads:
  set[int]` on create, close on leave).
- Transcript format in the thread: emoji prefix (`🎤 user:`, `🔊 ARIA:`)
  vs. Hermes' existing `**[Voice]** <@id>:` — recommend extending the
  existing format for consistency.
