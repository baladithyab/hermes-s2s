# ADR-0015: `/s2s` rich configuration UI (Discord buttons + Telegram inline + CLI)

**Status:** Accepted (2026-05-11) — implementation in progress as wave-0.5.0.

## Context

The 0.4.x line of `hermes-s2s` ships a single Discord slash command —
`/s2s mode <choice>` — that toggles between the four canonical voice
modes (`cascaded` / `pipeline` / `realtime` / `s2s-server`) on a
per-(guild, channel) basis. Provider selection (which realtime backend,
which STT, which TTS) is `~/.hermes/config.yaml`-only. Telegram has no
slash at all. The CLI's in-session `/s2s` slash is a thin text router
limited to `status`, `mode`, and `test`.

User feedback (2026-05-11) asked for:

> "Can we have slash commands to better list or configure the s2s? Can
> we have an `s2s configure` command that uses Discord cards and buttons
> to configure and navigate the configuration options while also having
> direct command capability? This way from Discord/Telegram we can switch
> modes or providers or something."

The bones for "Discord-native slash + persistent config store" are
already wired (ADR-0011, `voice/slash.py`, `S2SModeOverrideStore`
JSON file with atomic flock writes). The gap is purely surface area:
no subcommands, no `discord.ui.View`, no Telegram presenter, no
CLI parity, no provider-key support in the override store schema.

## Decision

Land a multi-platform `/s2s` configuration UI in 0.5.0.

### 1. Extend the override store, don't replace it

The existing `S2SModeOverrideStore` JSON file (`~/.hermes/.s2s_mode_overrides.json`)
is the right place for per-channel state. We extend the value shape
from a bare string (`"cascaded"`) to a dict
(`{"mode": "cascaded", "realtime_provider": "gpt-realtime-2", ...}`).

**Migration is read-time and lossless.** A 0.4.x bare-string entry is
lifted to `{"mode": <str>}` on first read; subsequent writes flush the
upgraded shape. No ALTER TABLE, no migration tool, no breaking change
for users.

The legacy `set(g, c, mode)` and `get(g, c)` methods become thin shims
over `patch_record(g, c, mode=...)` and `get_record(g, c)["mode"]` so
existing call sites in `factory.py` keep working.

**Why not a parallel store?** Two stores doubles the surface area for
flock races, doubles the file-watching code, and forces every reader
to know about both files. One store with a richer value shape is
simpler.

### 2. Use `discord.app_commands.Group`, not flat commands

`/s2s` becomes a Discord *Group* with subcommands:

- `/s2s configure` — opens an ephemeral panel with `discord.ui.View`
  containing four `Select` menus (mode + 3 provider kinds) and three
  action buttons (Test, Reset, Refresh).
- `/s2s mode <choice>` — direct path, mirrors today's behavior.
- `/s2s status` — pretty-prints active mode, providers, and override state.
- `/s2s provider <kind> <name>` — direct provider override (kind ∈
  `realtime`, `stt`, `tts`; name validated against the live registry).
- `/s2s test [text]` — runs the existing TTS smoke test.
- `/s2s doctor` — runs the existing preflight, returns a compact
  summary.
- `/s2s reset` — clears all overrides for this channel.

**Why subcommands AND a View?** Subcommands serve power users and tab
completion. The View serves discoverability and mobile UX. The two
write to the same store, so they stay consistent.

### 3. Telegram parity via `InlineKeyboardMarkup`

A new `voice/slash_telegram.py` mirrors the Discord UX. Telegram has no
ephemeral messages, but inline keyboards on a regular message are a
close-enough analog. Callback data is namespaced `s2s:<verb>:<arg>`
(64-byte limit per Telegram protocol).

The Telegram adapter is reached via the same `ctx.runner.adapters[*]`
walk as the Discord installer — a parallel `_find_telegram_app` probe
looks for `_application` / `application` / `_app` attribute names.

### 4. CLI parity via the existing `register_command` slash hook

`/s2s` already pipes into the CLI in-session via `register_command`.
We upgrade the `handle_s2s_command` text router to match the Discord
subcommand surface — `mode`, `provider`, `status`, `test`, `doctor`,
`reset`, `configure`, `help` — using pure-text formatters from a new
`voice/slash_format.py` module. CLI doesn't have buttons, so
`/s2s configure` shows a `format_help` block; users use the direct
subcommands to actually flip settings.

### 5. Factory respects every override key, not just `mode`

`voice/factory.py` learns `resolve_s2s_config_for_channel(guild_id,
channel_id)` which:

1. Loads global `S2SConfig` via `load_config()`.
2. Reads `get_default_store().get_record(g, c)`.
3. Applies `mode`, `realtime_provider`, `stt_provider`, `tts_provider`
   override keys via `dataclasses.replace`-style helpers on `S2SConfig`.
4. Returns the patched config.

All voice-session construction sites switch to this resolver. Result:
per-channel provider overrides flow into bridge construction without
any per-call hand-plumbing.

## Consequences

### Positive

- **Discoverability.** New users get a UI, not a docs page.
- **Per-channel mix.** A user can run `realtime/gpt-realtime-2` in
  one Discord channel and `cascaded/moonshine+kokoro` in another
  without restarting the bot.
- **Live registry awareness.** Selects populate from
  `list_registered()`, so plugin-registered providers appear without
  hardcoding.
- **Backwards-compatible storage.** No migration tool needed.

### Negative

- **One-time Discord re-sync** required after upgrade — the Group
  shape change forces `tree.sync()` to run on next bot startup.
  Users see slash list update after `hermes gateway restart`.
- **Old plugin instances are forward-incompatible** with the new
  store shape. Two Hermes processes on the same `~/.hermes/` running
  different plugin versions will see each other's writes as malformed
  if the older one runs after the newer. Documented in CHANGELOG.
- **Discord 25-option Select cap.** With more than 25 registered STT
  providers, the panel truncates. Realistic count is <10 today;
  documented + log-warned when truncation kicks in.

### Neutral

- Test surface grows: 3 new test files + extensions to existing.
  Telegram tests gate on `pytest.importorskip("telegram")` so dev
  environments without `python-telegram-bot` skip cleanly.

## References

- ADR-0011 — original plugin-owned `/s2s` slash command (the pattern
  this builds on).
- `docs/plans/wave-0.5.0-s2s-configure.md` — task-by-task implementation plan.
- `docs/research/13-mode-ux-deep-dive.md` §4 — original mode-UX
  investigation that motivated the per-channel override store.
- `references/plugin-authoring.md` — Hermes plugin gotchas (sub-block
  unwrap, doctor-vs-runtime drift, log-level for catch-alls).
- `references/gateway-adapter-internals.md` — adapter object graph,
  voice receive path, `/v1/models`-vs-WS handshake mismatch.

## Open questions

- **Telegram chat-vs-thread keying.** Discord uses `(guild_id, channel_id)`.
  Telegram has `(chat_id, optional_message_thread_id)` — a topic in a
  group. For 0.5.0 we key on `chat_id` only (mapping `g_id == c_id ==
  chat_id` into the same store). Topic-aware keying is a 0.5.1
  follow-up.
- **CLI provider override scope.** Today `s2s_set_mode` stores per-session
  overrides in a process-local dict. Per-channel makes no sense in CLI
  (there's no channel). For 0.5.0 the CLI `provider` subcommand prints
  a guidance message pointing at `config.yaml`; richer CLI session-scoped
  provider overrides land in 0.5.1 if there's demand.
