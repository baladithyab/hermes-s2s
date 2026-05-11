# `/s2s` Configuration UI — User Guide

The hermes-s2s plugin v0.5.0+ exposes a multi-platform configuration
surface so you can switch S2S **modes** (cascaded / pipeline / realtime
/ s2s-server) and **providers** (which realtime backend, which STT,
which TTS) per-channel from Discord, Telegram, or the Hermes CLI —
without editing `~/.hermes/config.yaml`.

## TL;DR

| Platform | Open the panel | Direct command |
|---|---|---|
| Discord | `/s2s configure` (ephemeral panel with selects + buttons) | `/s2s mode realtime` |
| Telegram | `/s2s` (sends status + inline keyboard) | tap a button |
| Hermes CLI | `/s2s configure` (prints help) | `/s2s mode realtime` |

All three platforms write to the same per-channel store at
`~/.hermes/.s2s_mode_overrides.json`. Per-channel settings take
precedence over the global `s2s.*` block in `config.yaml`.

---

## Discord

### Rich panel — `/s2s configure`

Opens an ephemeral message visible only to you. Contains:

- **Mode select** — Cascaded / Pipeline / Realtime / External S2S server.
- **Realtime backend select** — populated dynamically from the live
  registry (`gpt-realtime-2`, `gpt-realtime-mini`, `gemini-live`, …).
- **STT select** — `moonshine`, `groq`, `openai`, … (whatever's
  registered).
- **TTS select** — `kokoro`, `elevenlabs`, `openai`, …
- **🧪 Test** — runs the TTS smoke test and replies with timing.
- **♻️ Reset** — clears every override for this channel.
- **🔄 Refresh** — re-renders the status block.

Selections persist immediately. The panel stays interactive for 5
minutes; after that the controls disable but the message remains as
a status snapshot.

> **First time?** Run `/s2s configure` after a `hermes gateway
> restart` so Discord re-syncs the new command tree shape.

### Direct subcommands

```
/s2s status                               # active mode + providers + override flag
/s2s mode <cascaded|pipeline|realtime|s2s-server>
/s2s provider <realtime|stt|tts> <name>   # set a single provider
/s2s test [text]                          # TTS smoke test
/s2s doctor                               # deps + keys + WS handshake probe
/s2s reset                                # clear all overrides for this channel
```

The `provider` subcommand validates the name against the live registry
and refuses unknown values with the available list — useful for
checking what's registered without scrolling the panel.

---

## Telegram

`/s2s` posts the status block plus an inline keyboard:

```
[Cascaded] [Realtime] [Pipeline] [Server]
[gpt-realtime-2] [gpt-realtime-mini] [gemini-live]
[moonshine] [groq] [openai]
[kokoro] [elevenlabs] [openai]
[🧪 Test] [♻️ Reset] [🔄 Refresh]
```

Tap any button — the message edits in place to acknowledge the change.
The action buttons (Test/Reset/Refresh) reply or edit as appropriate.

The override is keyed on the Telegram chat id, so per-chat
configuration is supported. Per-topic (forum thread) keying is on
the 0.5.1 roadmap.

> Telegram callback data is namespaced `s2s:<verb>:<arg>` (e.g.
> `s2s:rt:gpt-realtime-2`). Stays well within the 64-byte protocol
> limit.

---

## Hermes CLI

Inside an interactive `hermes` session, the in-session `/s2s` slash
mirrors the Discord subcommand surface:

```
/s2s                          # status (formatted, not JSON)
/s2s status                   # same
/s2s mode realtime            # session-scoped override
/s2s test                     # TTS smoke test
/s2s doctor                   # full preflight (sync mode)
/s2s reset                    # clear session override
/s2s help                     # full menu
/s2s configure                # alias for /s2s help (no buttons in CLI)
```

> The CLI doesn't have a (guild_id, channel_id) context, so
> `/s2s provider` prints a guidance message pointing at
> `~/.hermes/config.yaml` instead of writing to the per-channel store.
> Session-scoped provider overrides may land in 0.5.1.

---

## What's stored where

Per-channel overrides live in `~/.hermes/.s2s_mode_overrides.json`:

```json
{
  "1234567890:9876543210": {
    "mode": "realtime",
    "realtime_provider": "gpt-realtime-2"
  },
  "1234567890:1111222233": {
    "mode": "cascaded",
    "stt_provider": "groq",
    "tts_provider": "elevenlabs"
  }
}
```

Key shape: `"<guild_id>:<channel_id>"` (Discord) or
`"<chat_id>:<chat_id>"` (Telegram, both halves the same).

The file is written atomically with an `flock(LOCK_EX)` wrapper so
concurrent writes from multiple Hermes processes can't corrupt it.

**Migration from 0.4.x:** legacy bare-string entries (`"realtime"`)
are auto-lifted to dict form (`{"mode": "realtime"}`) on first read.
No manual migration needed.

---

## Operator smoke test

After installing 0.5.0, verify all three surfaces:

```bash
# 1. Plugin loads cleanly
~/.hermes/hermes-agent/venv/bin/python3 -c "
import hermes_s2s
class C:
    def __init__(self):
        self.tools=[]; self.cmds=[]; self.cli=[]; self.skills=[]; self.hooks=[]
    def register_tool(self, **kw): self.tools.append(kw['name'])
    def register_command(self, name, **kw): self.cmds.append(name)
    def register_cli_command(self, **kw): self.cli.append(kw['name'])
    def register_skill(self, n, p): self.skills.append(n)
    def register_hook(self, n, cb): self.hooks.append(n)
ctx = C()
hermes_s2s.register(ctx)
print('tools:', ctx.tools)
print('commands:', ctx.cmds)
print('cli:', ctx.cli)
"
# Expected: tools=[s2s_status,s2s_set_mode,s2s_test_pipeline,s2s_doctor]
#           commands=['s2s']  cli=['s2s']
```

```bash
# 2. Discord — restart the gateway so the new Group syncs:
hermes gateway restart

# In a Discord channel where the bot is connected:
#   /s2s configure        → ephemeral panel renders with 4 selects + 3 buttons
#   pick "Realtime" from the mode select
#   pick "gpt-realtime-2" from realtime backend select
#   click 🧪 Test         → ✅ TTS OK message
#   click ♻️ Reset         → "Cleared this channel's overrides"
```

```bash
# 3. Telegram — in a private chat with the bot:
#   /s2s                  → status reply + inline keyboard
#   tap "Realtime"        → message edits to "Mode → realtime"
#   tap 🧪 Test            → reply with synthesis result
```

```bash
# 4. CLI — in a fresh hermes session:
#   /s2s status           → pretty-printed text (NOT JSON)
#   /s2s mode cascaded    → ✅ Session mode → cascaded
#   /s2s help             → full menu
```

```bash
# 5. Cross-check the on-disk shape:
~/.hermes/hermes-agent/venv/bin/python3 -c "
import json
print(json.dumps(json.load(open('/home/codeseys/.hermes/.s2s_mode_overrides.json')), indent=2))
"
# Expected: dict-of-dicts shape, with 'mode' / 'realtime_provider' / etc keys.
```

---

## Troubleshooting

### Discord — `/s2s configure` doesn't appear

The Discord command tree only re-syncs at bot startup. Run
`hermes gateway restart` and wait ~30 seconds for Discord's slash
catalog to refresh.

### Discord — "❌ This must be used in a guild text channel"

`/s2s` subcommands need a (guild_id, channel_id) pair. DMs and
group DMs are not supported. Use them inside a server channel.

### Telegram — `/s2s` does nothing

- Confirm `python-telegram-bot` is installed in the Hermes venv:
  `~/.hermes/hermes-agent/venv/bin/python3 -c "import telegram; print(telegram.__version__)"`.
- Confirm the gateway started with the Telegram adapter active —
  `~/.hermes/logs/gateway.log` should show
  `hermes-s2s: /s2s installed on Telegram`.
- If Hermes started before 0.5.0 was installed, restart the gateway
  so the installer hook re-runs.

### CLI — `/s2s doctor` says "Doctor must be called from a sync context"

The async doctor handler conflicts with the CLI's running event
loop. Use `hermes s2s doctor` from a fresh shell instead — it has
its own async runner.

### Per-channel override references a missing provider

If you set `realtime_provider: gpt-realtime-mini` then later remove
that backend from `config.yaml`, the factory falls back to the
global default and logs a warning. To clear: `/s2s reset` in that
channel.

### Two Hermes instances on the same `~/.hermes/`

Don't run a 0.4.x gateway and a 0.5.0 gateway pointed at the same
home. The 0.4.x reader will see the new dict-shaped values as
malformed and crash on first use. Upgrade both, or run them in
separate `--profile` configs.

---

## Reference

- ADR-0015 — design rationale and migration strategy.
- `docs/plans/wave-0.5.0-s2s-configure.md` — task-by-task
  implementation plan.
- `hermes_s2s/voice/slash.py` — Discord installer + View.
- `hermes_s2s/voice/slash_telegram.py` — Telegram presenter.
- `hermes_s2s/voice/slash_format.py` — pure-text formatters
  shared across platforms.
- `hermes_s2s/tools.py:handle_s2s_command` — CLI subcommand
  router.
