# Changelog

All notable changes to `hermes-s2s` are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this
project adheres to [Semantic Versioning](https://semver.org/).

## [0.5.2] — 2026-05-11

### Fixed (Codex review follow-ups for PR #1 + PR #2)

- **P1 — `S2SModeOverrideStore.patch_record` is now atomic across
  read-modify-write.** The 0.5.0 implementation released the
  in-process lock between reading the existing record and writing
  the merged result, so two near-simultaneous `patch_record` calls on
  different fields (mode + provider tap, common with the rich UI)
  could clobber each other's writes. The merge now happens inside the
  same `self._lock` critical section that calls `_set_locked` (which
  in turn does the flock-protected file write). New regression test
  spawns two threads doing 50 patches each on different fields and
  asserts both fields land. (Codex PR #1.)
- **P2 — `format_status` now shows the *effective* per-channel
  values, not the global config's.** Previously the rendered status
  block could read "Realtime provider: gpt-realtime-2 / Channel
  overrides set: realtime_provider" — a self-contradiction. The
  formatter folds the override record on top of the global values
  before rendering, and tags overridden lines with `(channel override)`
  so users can tell at a glance which lines came from `config.yaml`
  vs the per-channel store. (Codex PR #1.)
- **P1 — Deferred slash-install hook keeps retrying until install
  actually succeeds.** The 0.5.1 implementation set
  `_slash_install_done["discord"] = True` even when
  `install_s2s_command_on_adapter()` returned `False` (no live client
  yet), so a first-message-before-bot-login race permanently disabled
  the retry path. The hook now only marks itself done when the
  install fired OR the tree carries the install sentinel — meaning a
  later dispatch (when the tree is finally live) can still land the
  command. (Codex PR #2.)

### Tests
- 368 passed (was 365 → +3). New regression tests pin all three
  contracts above.

## [0.5.1] — 2026-05-11

### Fixed
- **`/s2s configure` (and the rest of the Group's subcommands) now
  actually appears in Discord's slash autocomplete.** The 0.5.0
  register-time install path raced against bot login — at
  `register(ctx)` time, `ctx.runner.adapters["discord"]._client.tree`
  was None (bot hadn't connected yet), so `install_s2s_command(ctx)`
  silently no-op'd. The Discord adapter then synced its own slash
  commands, computed a fingerprint over its own command set (which
  didn't include `/s2s`), and on subsequent runs refused to re-sync
  because the fingerprint matched. Result: `/s2s` was on the tree in
  Python but never published to Discord's UI.
- New `install_s2s_command_on_adapter(adapter)` helper takes a LIVE
  `DiscordAdapter` (with `_client.tree` populated) and forces a
  `tree.sync()` if the tree was already synced — bypassing the
  fingerprint-skip.
- The plugin's `register(ctx)` now wires a one-shot
  `pre_gateway_dispatch` hook that fires the deferred install on the
  first inbound message (when the gateway and Discord adapter are
  guaranteed live).
- The `_install_bridge_on_adapter` path also calls the new helper as
  belt-and-suspenders, so even users who don't trigger
  `pre_gateway_dispatch` get the slash on first `/voice join`.

### Tests
- 365 passed (was 363 → +2). New tests cover the live-tree adapter
  install path and the no-client/no-tree fallbacks.

## [0.5.0] — 2026-05-11

### Added
- **`/s2s configure` Discord rich panel** — ephemeral message with
  select menus (mode + realtime / STT / TTS providers) and action
  buttons (Test, Reset, Refresh). Selections persist immediately.
- **Discord subcommand surface** — `/s2s` is now an `app_commands.Group`
  with `mode`, `status`, `provider`, `test`, `doctor`, `reset`,
  `configure` leaves. Direct subcommands serve power-users; the rich
  panel serves discoverability.
- **Telegram `/s2s`** — posts the current status with an inline keyboard
  for switching mode / provider / running test or reset. Callback
  data namespaced `s2s:<verb>:<arg>` (within the 64-byte protocol
  limit).
- **CLI `/s2s` subcommand router** — pretty-printed status (no more raw
  JSON), full `mode / provider / test / doctor / reset / configure /
  help` parity with Discord. Provider sets in CLI print a
  guidance message pointing at `config.yaml` since CLI lacks
  per-channel context.
- **Per-channel provider overrides** — `realtime_provider`,
  `stt_provider`, `tts_provider` join `mode` in the override store
  and flow through `voice/factory.py:resolve_s2s_config_for_channel`
  into voice session construction.
- **`S2SModeOverrideStore.set_record / get_record / patch_record`** —
  dict-based public API alongside the legacy string `set / get`
  shims.
- **`voice/slash_format.py`** — pure-text formatters
  (`format_status`, `format_help`, `format_doctor_summary`) shared
  across Discord / Telegram / CLI presenters.
- **ADR-0015** documenting the design.
- **`docs/HOWTO-S2S-CONFIGURE.md`** — operator-facing user guide with
  per-platform smoke tests.

### Changed
- **Override store on-disk shape** from `{"<key>": "<mode>"}` to
  `{"<key>": {"mode": "...", "realtime_provider": "...", ...}}`.
  Legacy 0.4.x bare-string entries auto-lift on read; no manual
  migration required.
- **`/s2s` CLI status output** is now pretty-printed text via
  `format_status`, not raw JSON.

### Migration

After upgrading to 0.5.0:

1. Re-install plugin deps in the Hermes venv:
   `~/.hermes/hermes-agent/venv/bin/python3 -m pip install -e ~/.hermes/plugins/hermes-s2s'[all]'`
2. Restart the gateway: `hermes gateway restart`. Discord re-syncs
   the new command Group on the next bot connection (≤30s).
3. Run `/s2s configure` in a Discord channel to verify.

**Compatibility note:** do not run a 0.4.x and 0.5.0 instance against
the same `~/.hermes/`. The 0.4.x plugin will read the new dict-shaped
records as malformed.

### Tests
- Test suite grew from 320 → 363 tests (+43). All green.

[0.5.0]: https://github.com/baladithyab/hermes-s2s/releases/tag/v0.5.0
