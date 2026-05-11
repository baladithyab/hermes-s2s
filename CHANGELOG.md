# Changelog

All notable changes to `hermes-s2s` are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this
project adheres to [Semantic Versioning](https://semver.org/).

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
