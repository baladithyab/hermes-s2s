# 14 — Thread Co-Management: deep dive (core hooks vs plugin)

Baseline: research‑11 covered the wiring. This doc designs the **upstream-PR‑able
hooks** so Hermes core and the s2s plugin *co‑own* voice-thread lifecycle. All
line numbers cite `/home/codeseys/.hermes/hermes-agent` + `/mnt/e/CS/github/hermes-s2s`.

## 0. Key discovery: reuse `HookRegistry`

Hermes core already ships a generic async hook bus at `gateway/hooks.py:35–210`:
- `emit(event_type, context)` — fire-and-forget, swallows exceptions, supports sync/async handlers.
- `emit_collect(event_type, context) -> List[Any]` — **returns handler results**, perfect for resolver-style hooks.
- Handler signature: `fn(event_type: str, context: dict) -> Any`.

**We do NOT need to add `register_voice_thread_resolver` / `register_voice_transcript_observer` as new public APIs.** We only need to (a) add a pair of `emit_collect`/`emit` call sites inside the voice handlers and (b) document two event-type strings. This is the minimal upstream change.

## 1. The two hook event‑types

### 1a. `voice:thread_resolve` — resolver (emit_collect)

```python
# Handler contract (what the plugin registers):
async def handler(event_type: str, ctx: dict) -> Optional[int]:
    # ctx = {
    #   "event": MessageEvent,           # the /voice join event
    #   "adapter": <DiscordAdapter>,
    #   "guild_id": int,
    #   "voice_channel": <discord.VoiceChannel>,
    #   "invoked_chat_id": int,          # event.source.chat_id
    #   "invoked_thread_id": Optional[int],
    # }
    # Return: thread_id:int to route voice through that thread,
    #         or None to keep default behavior.
    ...
```

**Landing point — `gateway/run.py:9161` (inside `_handle_voice_channel_join`)**, in the `if success:` branch, *before* the `_voice_text_channels` / `_voice_sources` writes:

```python
# around line 9161, after `if success:`
resolved = await self._hooks.emit_collect("voice:thread_resolve", {
    "event": event, "adapter": adapter, "guild_id": guild_id,
    "voice_channel": voice_channel,
    "invoked_chat_id": int(event.source.chat_id),
    "invoked_thread_id": getattr(event.source, "thread_id", None),
})
for tid in resolved:                       # first non-None wins
    if isinstance(tid, int) and tid > 0:
        event.source.thread_id = str(tid)
        event.source.chat_type = "thread"
        break
# existing lines 9162-9164 unchanged → now stash thread-scoped source
```

Edge cases:
- **Handler throws** — `emit_collect` already logs + swallows (`hooks.py:208-209`). Default behavior preserved.
- **Async vs sync** — `emit_collect` auto-awaits coroutines (`hooks.py:204-205`).
- **Multiple plugins register** — first non‑None win (documented). We iterate in registration order; plugin handlers typically register during gateway startup so order is stable.
- **No handlers** — `resolved == []`, loop no-ops, today's behavior unchanged. ✅ back-compat.

**Upstream PR diff size:** ~10 LOC in `run.py` + a 40-line ADR doc. Tests: +30 LOC (one already-hooked test in `tests/gateway/test_voice_command.py:772`).

### 1b. `voice:transcript` — observer (emit)

```python
async def handler(event_type: str, ctx: dict) -> None:
    # ctx = {
    #   "source": SessionSource,      # thread-scoped if resolver fired
    #   "role": "user" | "assistant",
    #   "text": str,
    #   "platform": "discord",
    #   "guild_id": int,
    #   "user_id": Optional[str],     # speaker's user id for role=user
    #   "turn_id": Optional[str],     # groups partials within a turn
    #   "final": bool,                # False for rolling partials, True once
    # }
    ...
```

**Landing points** — core emits **twice** from the existing seam at `_handle_voice_channel_input` (`run.py:9250–9316`):

1. **User STT mirror** — just before/after the existing `channel.send(f"**[Voice]** <@{uid}>: {text}")` at `run.py:9298-9301`, `await self._hooks.emit("voice:transcript", {..., "role":"user", "final": True})`. ~5 LOC.
2. **Assistant text reply** — where `handle_message` returns and the runner sends the reply text to the bound channel. Emit with `role="assistant"`. ~5 LOC.

This covers **cascaded mode** entirely from core. For **realtime mode** core never sees audio (STT/TTS bypassed), so the plugin emits `voice:transcript` itself from its realtime event pump — see §2.

**Upstream PR diff size:** ~15 LOC in `run.py`. Tests: +40 LOC.

Edge cases:
- `emit` already logs exceptions (`hooks.py:180-181`) — a crashing observer won't break voice replies.
- Multiple observers run in series. That's fine because each one just POSTs to Discord (I/O-bound) — serial execution lets us enforce rate-limit ordering (§4).

## 2. Realtime transcript plumb‑through

Core path already carries cascaded mode. Realtime goes plugin-only.

**Source of truth** — `hermes_s2s/providers/realtime/gemini_live.py`:
- **line 291–298** → `outputTranscription` ⇒ `RealtimeEvent(type="transcript_partial", payload={"text":…, "role":"assistant"})`
- **line 299–306** → `inputTranscription` ⇒ `RealtimeEvent(type="transcript_partial", payload={"text":…, "role":"user"})`
- **line 307–311** → `turnComplete` ⇒ `RealtimeEvent(type="transcript_final", …)`
- Parallel OpenAI source: `openai_realtime.py:204–256`.

**Consumer** — `hermes_s2s/_internal/audio_bridge.py:571 _dispatch_event` already receives every `RealtimeEvent`. At **line 616** there is a comment: *“transcript_partial / transcript_final / session_resumed: ignored for 0.3.1”*. **This is the exact edit.**

Proposed edit (audio_bridge.py, replace the comment at line 616):

```python
elif etype in ("transcript_partial", "transcript_final"):
    if self._transcript_sink is not None:
        role = payload.get("role", "assistant")
        txt  = payload.get("text", "") or ""
        final = etype == "transcript_final"
        try:
            await self._transcript_sink(role, txt, final)   # sync/async OK
        except Exception:
            logger.exception("transcript_sink raised; dropping")
```

`_transcript_sink` is a new constructor kwarg on `BridgeBuffer`/bridge class (`audio_bridge.py` ~line 300). The s2s adapter wiring layer (`discord_bridge.py:_install_bridge_on_adapter`) injects a sink that:
1. Looks up `adapter._voice_text_channels[guild_id]` → channel (thread) id.
2. Calls `adapter._hooks.emit("voice:transcript", {...})` if Hermes has the hook (probed via `hasattr(adapter._runner, "_hooks")`). If not, falls back to a direct `channel.send(...)` through the batcher.

## 3. Transcript format strategy (rate‑limit‑aware)

Discord rate limits (verified): **5 msgs / 5 s per channel** (soft), **global 50 req/s per bot**, threads share channel-level limits. Private threads have no boost-level gate (per discord.py maintainers; this contradicts older community claims and is authoritative). Thread creation is rate-limited per-route but not publicly enumerated; conservative target ≤ 2 creates/min/guild.

A raw 1-msg-per-utterance strategy at 24 msgs/min collides with the 5/5s burst limit on mixed traffic. **Recommendation: rolling-edit per turn + final coalesced line.**

| Strategy | Msgs/min | UX | Verdict |
|----------|---------:|----|---------|
| (a) 1 msg / utterance | 24+ | noisy, racy | ❌ |
| (b) edit rolling msg / turn | 2 | smooth | ✅ primary |
| (c) session-end summary only | 0 during call | no live visibility | ⚠ optional add-on |
| (d) edit rolling + final coalesced | 2 edits + 1 post / turn | best | ✅ **chosen** |

**Chosen scheme** (in plugin `transcript_mirror.py`):
1. Per (guild_id, turn_id) keep an *edit buffer*. First partial → `channel.send("🎤 @user: …")` and record `message_id`. Subsequent partials within the same turn → `msg.edit(content=rolling_text)` at most every 1.2 s (debounce).
2. On `final=True` for user turn: one last edit, unlock.
3. Assistant reply: same rolling-edit pattern against a second message (`🔊 ARIA: …`).
4. Add a **token-bucket rate limiter** in the plugin (5 ops / 5 s per channel, refill 1/s) shared between send & edit. Overflow is dropped to a *session transcript summary* posted on `/voice leave`.

Expected worst case: 12 turns/min × (1 send + ~3 edits + 1 send + ~3 edits) = 96 ops/min = 1.6 ops/s → safely under 5/5s headroom.

## 4. Thread auto-create policy

- **Type:** **public thread** off the invoking text channel (`discord.ChannelType.public_thread`, via `parent.create_thread(name=…, auto_archive_duration=60, type=...)`). Private threads bring no real benefit for voice transcripts and complicate permission checks.
- **Name template:** default `"🎤 {user} — {date:%Y-%m-%d %H:%M}"`, override via config key `s2s.voice.thread_name_template` (jinja2-ish tokens: `{user}`, `{guild}`, `{vc}`, `{date}`, `{date:%fmt}`).
- **auto_archive_duration:** **60** (1h). Accepted values per discord.py are 60/1440/4320/10080. 1 h is best: transcript thread goes quiet after the call and stops cluttering the parent channel. Users can continue text convo within that hour.
- **On invoked-in-thread:** resolver returns the existing thread id — **do not** create a new thread.
- **On forum parent:** fall back to no-thread (keep today's behavior), warn once per guild.

## 5. Session-key linkage (parent-linked vs fresh)

`session.build_session_key` keys on `…:chat_id:thread_id`. A new thread = a new session key = **clean slate**. The user's existing `#general` history will NOT bleed in.

**Recommendation: keep it fresh (separate).** Two reasons:
1. Voice context is usually a new task ("let's plan this"). Parent-channel history would poison the agent's working context with stale chatter.
2. Explicit opt-in for context carry-over is cheaper than opt-out: user can `/voice join` *from inside* a prior thread to reuse that context.

**But add a soft link for user-visibility:** the auto-created thread's starter message references the parent: `"Voice session started from #{parent_name}. Previous channel history is NOT included — reply here to continue."` This answers the "feature or bug?" question explicitly in the UI.

## 6. Multi-call concurrency (same guild)

Core constraint: `adapter._voice_text_channels[guild_id]` is a **single slot per guild** (`discord.py:1857-1889` + run.py wiring). Hermes today only supports one VC per guild anyway — `join_voice_channel` will replace the existing voice client. So the "two users, two VCs" scenario degrades to "second join kicks the first."

**Recommendation for 0.4.0:** preserve single-VC-per-guild. Thread co-management follows: one thread per active guild voice session. On a second `/voice join` in the same guild while a thread is active, reject with an error message pointing at the active thread (`"Already in VC; transcripts are posting to #thread-name."`). Cross-guild (two different guilds) is already isolated by the `guild_id` key.

Multi-VC-per-guild is a separate ADR.

## 7. `/voice leave` behavior

**Recommendation:**
1. Flush any pending transcript batches to the thread.
2. Post a final summary message: `"🛑 Voice session ended. Duration: {mm:ss}. Turns: {n}."` — optionally LLM-summarize the transcript if `s2s.voice.leave_summary=true`.
3. **Do not immediately archive.** Leave the thread open for 60 min auto-archive so users can follow up in text. Add reaction 🗄️ that the invoking user can click to archive immediately (bot handles `on_raw_reaction_add`).
4. If the thread was **auto-created by us** (track via `adapter._voice_autothreads: set[int]`), set `auto_archive_duration=60` on leave (idempotent if already 60). Never delete — destructive.

Expose `s2s.voice.archive_on_leave` (default `false`) to allow the "archive immediately" policy for high-volume servers.

## 8. Cross-platform parity

The hook contract is **platform-agnostic**: `voice:thread_resolve` returns an opaque integer id that the adapter interprets. Different adapters map it differently.

| Platform | Thread primitive | `thread_id` means | Notes |
|----------|------------------|-------------------|-------|
| Discord | Thread (channel.Thread) | Thread channel id | shipped in 0.4.0 |
| Telegram | Forum topic (message_thread_id) | Topic id within a forum chat | 0.4.x — already supported by Bot API |
| Slack | Thread on a message | `thread_ts` (string!) | adapter serializes str↔int, or we widen contract to `thread_key: str` |
| WhatsApp | none | — | resolver returns None ⇒ default path |
| Matrix | Thread (m.thread relation) | event_id root | adapter maps |

**Forward-compat recommendation:** keep the hook `ctx` keyed by strings (`"thread_id"` or future `"thread_key": str`) so Slack's `thread_ts` works without schema change. Internally `SessionSource.thread_id` is already `str | None` — ✅ consistent.

## 9. Plugin-side consumer sketch (~80 LOC)

```python
# hermes_s2s/_internal/thread_comanage.py  (new)
import asyncio, time, datetime as dt
from typing import Optional

class VoiceThreadCoManager:
    def __init__(self, adapter, cfg):
        self.adapter = adapter
        self.cfg = cfg
        self._autothreads: set[int] = set()
        self._rolling: dict[tuple[int,str,str], tuple[int,float,str]] = {}
        # key = (guild_id, turn_id, role) → (msg_id, last_edit_ts, content)
        self._bucket: dict[int, list[float]] = {}  # chan_id → send timestamps

    async def resolve(self, etype, ctx):
        ev = ctx["event"]; invoked_thread = ctx["invoked_thread_id"]
        if invoked_thread: return int(invoked_thread)             # reuse
        parent = self.adapter._client.get_channel(ctx["invoked_chat_id"])
        if parent is None or not hasattr(parent, "create_thread"):
            return None
        name = self._format_name(ctx)
        th = await parent.create_thread(
            name=name, auto_archive_duration=60,
            type=__import__("discord").ChannelType.public_thread,
        )
        self._autothreads.add(th.id)
        self.adapter._threads.mark(th.id)                         # mention-tracker
        await th.send(
            f"Voice session started from <#{parent.id}>. "
            "Previous channel history is NOT included — reply here to continue."
        )
        return th.id

    async def observe(self, etype, ctx):
        if not await self._allow(ctx):           # token-bucket
            return
        key = (ctx["guild_id"], ctx.get("turn_id") or "x", ctx["role"])
        chan = self.adapter._client.get_channel(int(ctx["source"].thread_id or ctx["source"].chat_id))
        icon = "🎤" if ctx["role"] == "user" else "🔊"
        prefix = f"{icon} <@{ctx.get('user_id')}>:" if ctx["role"] == "user" else f"{icon} ARIA:"
        text = f"{prefix} {ctx['text']}"[:1900]
        prior = self._rolling.get(key)
        now = time.time()
        if prior and (now - prior[1]) > 1.2 and not ctx["final"]:
            msg = await chan.fetch_message(prior[0])
            await msg.edit(content=text); self._rolling[key] = (prior[0], now, text)
        elif prior is None:
            msg = await chan.send(text); self._rolling[key] = (msg.id, now, text)
        if ctx["final"]:
            self._rolling.pop(key, None)

    # register: runner._hooks.register("voice:thread_resolve", self.resolve)
    #           runner._hooks.register("voice:transcript", self.observe)
```

## 10. Decision matrix — upstream now vs monkey-patch in 0.4.0

| Hook | Core diff | Monkey-patch feasibility | Ship in 0.4.0 | Upstream PR |
|------|-----------|--------------------------|---------------|-------------|
| `voice:thread_resolve` | ~10 LOC in `run.py:9161` | Medium — plugin can rewrite `_voice_sources[gid]` inside `join_voice_channel_wrapped` *after* `orig()` returns (we already do). Races possible if audio arrives in <5 ms. | **Monkey-patch** | **PR to upstream, target 0.4.1** — Option A in research-11. |
| `voice:transcript` (cascaded) | ~15 LOC in `run.py:9298, 9316` | Hard — we'd have to wrap `_handle_voice_channel_input` fully. High drift risk. | Emit plugin-local surrogate via `DiscordAdapter.send` wrap (intercept outgoing `[Voice]` frames). | **PR upstream, target 0.4.1** — clean. |
| `voice:transcript` (realtime) | 0 (plugin owns realtime) | N/A — plugin already owns the pump. | **Ship: audio_bridge.py `_dispatch_event` edit + sink wiring.** | N/A — stays in plugin. |

So: 0.4.0 ships all three behaviors, two via monkey-patch. 0.4.1 lands the upstream PR to move the cascaded-mode pieces into core hooks, and the plugin switches to `hasattr(runner, "_hooks")`-gated registration.

## 11. Rate‑limit & failure‑mode analysis

| Failure | Symptom | Mitigation |
|---------|---------|------------|
| 429 on thread create | join succeeds, transcripts go to channel | Fallback: resolver returns `None`, log warn, existing code posts to channel. Reset breaker after 60 s. |
| 429 on send/edit | silent drop | Token-bucket + exponential backoff (already provided by discord.py `HTTPException.retry_after`); overflow accumulates into final summary. |
| Thread archived mid-call | sends unarchive it (per Discord docs) | No action needed; Discord unarchive-on-send is automatic. |
| Gemini/OpenAI transcripts missing | realtime mode loses mirror | Cascaded fallback unaffected; emit 1-shot warning per session. |
| Observer raises | next observer still runs | `HookRegistry.emit` already isolates (`hooks.py:180`). |
| Two plugins resolve to different threads | nondeterministic | First non-None wins; document ordering; multiple s2s plugins in one install is an unsupported config. |
| Guild loses thread-perms during call | send fails | Catch `discord.Forbidden`, flip mirror to parent channel, warn once. |

## 12. Test strategy

**Unit (plugin, pytest‑asyncio):**
- `test_resolver_returns_existing_thread_when_invoked_in_thread`
- `test_resolver_creates_public_thread_off_text_channel`
- `test_resolver_none_on_forum_parent`
- `test_observer_rolling_edit_coalesces_partials`
- `test_observer_token_bucket_drops_overflow_to_summary`
- `test_transcript_sink_plumbs_gemini_input_transcription`
- `test_transcript_sink_plumbs_gemini_output_transcription`
- `test_transcript_sink_plumbs_openai_realtime_events`

**Unit (core, if PR merged):**
- `test_voice_thread_resolve_hook_first_non_none_wins`
- `test_voice_thread_resolve_hook_handler_throws_is_isolated`
- `test_voice_transcript_hook_emits_on_user_stt_and_assistant_reply`
- `test_voice_channel_join_without_handlers_behaves_as_before` ← back-compat gate

**Integration (fake Discord client, existing `tests/test_adapter_smoke.py` style):**
- `/voice join` from plain channel → thread created, name matches template, `_voice_sources[gid].thread_id == new_thread_id`, session key is thread-scoped.
- `/voice join` from existing thread → no new thread, same thread id carried.
- Cascaded utterance → mirror lands in thread with `🎤 @user:` prefix, not in parent.
- Realtime utterance (mocked Gemini ws) → mirror path identical.
- `/voice leave` → summary posted, thread not archived, `_voice_autothreads` cleaned.
- 6 utterances in 2 s → at most 5 Discord ops, rest batched.

**Manual:** boosted + non-boosted guild, forum-parent channel, 2 concurrent guilds.
