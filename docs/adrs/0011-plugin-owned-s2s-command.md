# ADR-0011: Plugin-owned `/s2s` slash command for mode dispatch

**Status:** accepted
**Date:** 2026-05-10
**Target release:** 0.4.0
**Driven by:** [research/13-mode-ux-deep-dive.md](../research/13-mode-ux-deep-dive.md)
**Supersedes:** research-12 §2 recommendation ("add a `mode:` option to core `/voice`")

## Context

Research-12 proposed extending the core Hermes `/voice` slash command with a second
`mode:` option carrying a 4-way `cascaded | pipeline | realtime | s2s-server` choice
dropdown. Research-13 validated the plan end-to-end and **found the slash-option
approach blocked**. We need a decision on the concrete command surface for 0.4.0.

## Decision

**The hermes-s2s plugin owns a dedicated `/s2s` slash command.** Core `/voice` stays
untouched. The `mode:` dropdown UX that research-12 wanted is preserved — just on a
plugin-namespaced command instead.

### Why the slash-option extension on `/voice` was rejected

Three concrete blockers from research-13 §2–3:

1. **Name collision on the `mode:` parameter.** `/voice` already declares a `mode:`
   option at `gateway/platforms/discord.py:L2962–L2978` that acts as the subcommand
   selector (`join | leave | on | off | tts | channel | status`). A second `mode:`
   option cannot coexist, and repurposing the first one breaks every existing branch.
   (research-13 §1.)

2. **discord.py has no `CommandTree.add_choice()` / `extend_options()`.** Per the
   Rapptz/discord.py wiki (2026-05), `@app_commands.choices` operates on the Command
   object at definition time; there is no public API for a second extension to attach
   choices to a Command registered by another extension. The only escape hatch —
   `tree.remove_command('voice')` + re-add — requires running between core's
   `_register_slash_commands` and `tree.sync()`, and **no such hook exists**. Plugin
   would also have to re-declare all 7 existing `/voice` choices, forking upstream UX.
   (research-13 §1.)

3. **`hermes_cli/plugins.py:416` `register_command(...)` takes no `choices=` /
   `options=` parameter.** The auto-register path (`discord.py:3098–3125`) mirrors
   plugin commands to Discord with a single free-text `args: str` option only. Until
   upstream grows structured options, a plugin cannot emit a native Discord
   autocomplete dropdown through the sanctioned surface. (research-13 §2.)

### Chosen command surface

Discord (plugin installs directly on `adapter.bot.tree` via the existing
`hermes_s2s._internal.discord_bridge` monkey-patch seam — same hook already used for
`join_voice_channel`):

```python
@tree.command(name="s2s", description="Pick a speech-to-speech voice mode")
@app_commands.describe(mode="Voice mode (omit to show status)")
@app_commands.choices(mode=[
    app_commands.Choice(name="cascaded — Whisper → Hermes → Edge TTS (default)", value="cascaded"),
    app_commands.Choice(name="pipeline — custom STT/TTS (Moonshine + Kokoro)",   value="pipeline"),
    app_commands.Choice(name="realtime — Gemini Live / gpt-realtime (low latency)", value="realtime"),
    app_commands.Choice(name="s2s-server — external duplex pipeline",            value="s2s-server"),
])
async def slash_s2s(interaction, mode: app_commands.Choice[str] | None = None): ...
```

Invariant: when `mode` is omitted the handler reports the currently resolved mode
for `(guild_id, channel_id)`; when supplied it either swaps in-place on the live
`VoiceClient` or persists the choice as a per-channel next-join override.

### Cross-platform parity

- **Telegram** — BotFather's `setMyCommands` is a flat list with no option metadata
  (Bot API limitation, research-13 §3). UX intent is preserved with a bare `/s2s`
  that replies with an `InlineKeyboardMarkup`:
  `[ 🎙️ cascaded ] [ 🔧 pipeline ] [ ⚡ realtime ] [ 🌐 s2s-server ]`. Callback data
  `s2s:mode:<value>` routes through the same `ModeRouter.resolve()` pathway. A
  free-text `/s2s realtime` form is kept as a power-user fallback.

- **CLI (`hermes chat`)** — CLI has no voice channel (no audio pipeline in the
  terminal), so `/s2s <mode>` **sets the next-join default** for the user's next
  Discord/Telegram VC join and echoes `✅ next voice session will use mode: realtime`.
  Tab-completion is wired via the existing `CommandDef.subcommands=(...)` pattern
  used by `/fast`, `/reasoning`, `/voice` in `hermes_cli/commands.py`.

### Future upstream PR (deferred to 0.4.1)

Extend `PluginContext.register_command(..., options: list[OptionSpec] = None)` where
`OptionSpec = (name, description, choices, required, type_hint)`. Teach
`_build_auto_slash_command` (discord.py) to synthesize `app_commands.Choice` per
tuple, and the Telegram adapter to auto-render an inline keyboard from the same
spec. ~80 LOC + one contract test in `tests/hermes_cli/test_plugins.py`. Once that
ships upstream, hermes-s2s deletes its monkey-patch seam and registers `/s2s` through
the sanctioned surface. Scoped for **hermes-s2s 0.4.1** (tracks an upstream Hermes
release); not a blocker for 0.4.0.

## Consequences

**Positive:**
- Zero upstream coordination cost for 0.4.0 — ships independently of any Hermes core
  change. Risk R1 (upstream changing `/voice` semantics in 0.11.x) is eliminated.
- Native Discord autocomplete dropdown preserved (the whole point of the
  slash-option ergonomic).
- `/s2s` lives in the plugin namespace — no collision, no fork of core UX.
- Monkey-patch seam is the same one already in use for voice-channel joins; no new
  integration surface to harden.

**Negative:**
- Users learn **one extra slash command** (`/s2s` alongside `/voice`). Mitigation:
  register an auto-typed alias `/voice-mode` that dispatches to the same handler so
  users who type-search `/voice*` still find it; `/help` cross-links both.
- Plugin still relies on the monkey-patch hook until the upstream PR lands — same
  brittleness profile as existing bridge code; covered by contract tests in
  `tests/s2s/test_discord_bridge.py`.
- Telegram and Discord paths diverge (inline-keyboard vs native dropdown). Mitigation:
  both reduce to the same `ModeRouter.resolve()` call; UX divergence is platform-
  native, not logic divergence.

## Alternatives considered

See research-13 §7 "Rejected alternatives" for the full table. Top rejections:
`tree.remove_command('voice')` + re-add (brittle timing, forks upstream UX);
free-text `/s2s <mode>` via `register_command(args_hint=...)` (no autocomplete —
loses the whole point); four separate `/s2s-<mode>` commands (pollutes the 100/guild
slash-command budget).
