# ADR-0012: Voice thread co-management via core hook events

**Status:** accepted
**Date:** 2026-05-10
**Driven by:** research-14 ([`docs/research/14-thread-comanagement-deep-dive.md`](../research/14-thread-comanagement-deep-dive.md))

## Context

Research-14 asked how the s2s plugin and Hermes core should *co-own* the
lifecycle of a Discord thread auto-created on `/voice join`. Two superficially
reasonable designs exist:

1. Add new dedicated APIs on the runner — `register_voice_thread_resolver(fn)`,
   `register_voice_transcript_observer(fn)` — symmetric to `register_command_provider`.
2. Reuse Hermes core's already-shipping async hook bus at
   `gateway/hooks.py:35–210` (`HookRegistry.emit` / `emit_collect`) and add only
   two new event-type strings.

Option 1 grows the public surface of core once per voice feature. Option 2 is
one generic extensibility point that we (and anyone else) can reuse forever.

## Decision

**Adopt Option 2.** The plugin registers ordinary hook handlers against two new
event-type strings on the existing `HookRegistry`. No new `register_voice_*`
methods are introduced on `HermesRunner`.

### Hook 1 — `voice:thread_resolve` (resolver, `emit_collect`)

```python
async def handler(event_type: str, ctx: dict) -> Optional[int]:
    # ctx = { "event": MessageEvent, "adapter": DiscordAdapter,
    #         "guild_id": int, "voice_channel": discord.VoiceChannel,
    #         "invoked_chat_id": int, "invoked_thread_id": Optional[int] }
    # Return: thread_id:int → route voice through that thread; None → default.
```

First non-`None` return wins; exceptions are swallowed by `emit_collect`
(`hooks.py:208`). Core emit site lives in `gateway/run.py:9161` inside
`_handle_voice_channel_join`, in the `if success:` branch before the
`_voice_text_channels` / `_voice_sources` writes.

### Hook 2 — `voice:transcript` (observer, `emit`)

```python
async def handler(event_type: str, ctx: dict) -> None:
    # ctx = { "source": SessionSource, "role": "user"|"assistant",
    #         "text": str, "platform": "discord", "guild_id": int,
    #         "user_id": Optional[str], "turn_id": Optional[str],
    #         "final": bool }
```

Core emits twice from `_handle_voice_channel_input` (`run.py:9298`, `9316`) —
once for user STT, once for assistant reply. Observer exceptions are isolated
by `HookRegistry.emit` (`hooks.py:180`).

## Plugin-vs-core split

| Behavior | 0.4.0 ships | 0.4.1 upstream-PR target |
|---|---|---|
| `voice:thread_resolve` emit site in `run.py:9161` | Monkey-patch `_handle_voice_channel_join` from the plugin; rewrite `_voice_sources[gid].thread_id` after `orig()` returns. | ~10 LOC diff in core `run.py`; plugin switches to `hasattr(runner, "_hooks")`-gated `register()`. |
| `voice:transcript` (cascaded STT/TTS) | Plugin intercepts outgoing `[Voice]` frames via `DiscordAdapter.send` wrap — surrogate emit. | ~15 LOC diff in core `run.py` at 9298/9316; clean replacement. |
| `voice:transcript` (realtime) | **Plugin-owned forever.** Core never sees realtime audio. Edit at [`audio_bridge.py:616`](../../hermes_s2s/_internal/audio_bridge.py) replaces the `# transcript_*: ignored for 0.3.1` comment with a `_transcript_sink(role, txt, final)` dispatch; the sink (injected by `discord_bridge._install_bridge_on_adapter`) probes `hasattr(runner, "_hooks")` and either `emit("voice:transcript", …)` or falls back to a direct `channel.send(...)`. | N/A — stays in plugin. |

The plugin thus works against both un-patched 0.4.0 core (via monkey-patch) and
patched 0.4.1 core (via native `_hooks.register`), with a single code path.

## Default thread-auto-create policy

- **Type:** `discord.ChannelType.public_thread` off the invoking text channel
  (`parent.create_thread(type=…, auto_archive_duration=60, name=…)`).
- **Auto-archive:** **60 minutes.** The transcript thread goes quiet after the
  call and stops cluttering the parent; users still have a 1-hour follow-up
  window for text.
- **If invoked from inside a thread:** resolver returns the existing thread id;
  **no** new thread is created.
- **On forum parent or permission failure:** resolver returns `None` → default
  (today's) behavior is preserved. ✅ back-compat.

Private threads were rejected: they add no value for transcript mirroring and
complicate permission handling.

## Session-key linkage — fresh slate, not parent-linked

`session.build_session_key` already keys on `…:chat_id:thread_id`. A new thread
= a new session key = **clean slate**. We deliberately do **not** pre-seed it
with parent-channel history.

**Rationale:**

1. A voice session is a sub-conversation — typically a *new* task
   ("let's plan X"). Inheriting `#general`'s history would poison working
   context with unrelated chatter.
2. Opt-in context carry-over is cheaper than opt-out: a user who wants prior
   context can `/voice join` from *inside* that prior thread, and the resolver
   reuses it (bullet above).

To make the break visible, the auto-created thread's starter message explicitly
states: *"Voice session started from <#parent>. Previous channel history is
NOT included — reply here to continue."* This disambiguates "feature vs bug"
in the UI itself.

## Consequences

- ✅ Zero new public APIs on `HermesRunner` for this feature.
- ✅ Upstream PR diff for 0.4.1 is ~25 LOC in `run.py` + tests — trivially
  reviewable.
- ✅ Back-compat: no registered handlers ⇒ `emit_collect` returns `[]` ⇒
  existing behavior unchanged.
- ✅ Platform-agnostic: Telegram forum topics, Matrix threads, Slack
  `thread_ts` all fit the same `ctx` schema (see research-14 §8).
- ⚠ 0.4.0 monkey-patches two core code paths; drift risk is bounded because
  the seams (`run.py:9161`, `9298`, `9316`) are exactly the ones the upstream
  PR will touch.
- ⚠ First-non-`None` ordering is defined but only observable when multiple
  s2s-style plugins are installed in one deployment — an unsupported config.

## References

- research-14: two-hook design, emit-site analysis, rate-limit strategy,
  cross-platform parity matrix.
- ADR-0006 (Discord voice bridge), ADR-0007 (audio bridge frame callback),
  ADR-0009 (plug-and-play UX) — upstream ADRs this builds on.
