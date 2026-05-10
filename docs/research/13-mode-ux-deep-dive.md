# Research-13: Mode-UX Deep Dive — Validating the Slash-Option Recommendation

**Status:** draft / Phase 3 validation
**Date:** 2026-05-10
**Target release:** 0.4.0
**Follows:** research-12 §2 ("slash-option on `/voice join`")
**TL;DR:** research-12 recommended adding a `mode:` option to the core `/voice` command. **That approach has a hard blocker in discord.py and the Hermes plugin surface.** Pivot: plugin owns a dedicated `/s2s` slash command that carries the `mode:` choice dropdown; `/voice join` stays untouched. The slash-option *ergonomic* is preserved — just on a plugin-owned command.

---

## 1. Discord interaction-option mechanics — BLOCKER

The existing `/voice` registration lives in `gateway/platforms/discord.py` **L2962–L2978**:

```python
@tree.command(name="voice", description="Toggle voice reply mode")
@discord.app_commands.describe(mode="Voice mode: join, channel, leave, on, tts, off, or status")
@discord.app_commands.choices(mode=[
    discord.app_commands.Choice(name="join — join your voice channel", value="join"),
    ...  # 7 subcommand-like choices
])
async def slash_voice(interaction, mode: str = ""):
    await self._run_simple_slash(interaction, f"/voice {mode}".strip())
```

The `mode:` option is **already consumed** as the subcommand selector (join / leave / on / off / …). We cannot add a *second* `mode:` option because the name is taken, and we cannot repurpose the existing one without breaking every other branch.

**discord.py library constraint** (confirmed via Rapptz/discord.py wiki, 2026-05):
> "A third-party extension cannot add `app_commands.Choice` options to a slash command registered by another extension without redefining the command. `@app_commands.choices` operates on the Command object at definition time; `CommandTree` exposes no `add_choice()` / `extend_options()` method."

**Escape hatch**: `tree.remove_command('voice')` + `tree.add_command(new_voice)` *before* `tree.sync()` works, but:
- requires the plugin to execute **after** core's `_register_slash_commands` but **before** `sync()` — no hook exists for that
- plugin would have to re-declare all 7 existing choices plus the new `voice_mode:` option, forking upstream UX
- upstream core could change `/voice` semantics in 0.11.x → plugin silently breaks (risk R1)

**Verdict: blocker.** research-12's literal recommendation ("add an option to `/voice join`") is not implementable cleanly.

## 2. Plugin extension surface — no `choices` support

`hermes_cli/plugins.py:416` exposes exactly one slash-command hook:

```python
def register_command(self, name, handler, description="", args_hint=""):
```

`args_hint` becomes a free-text `args: str` Discord option (see `_build_auto_slash_command` in `discord.py:3028–3058`). **There is no `choices=` / `option_spec=` parameter.** The auto-register path (discord.py:3098–3125) mirrors plugin commands into Discord with only a single free-text `args` field.

**Upstream PR proposal** (candidate for a later push, not a blocker for 0.4.0):
Extend `PluginContext.register_command` with an optional `options: list[OptionSpec]` argument where `OptionSpec` is a lightweight dataclass `(name, description, choices: list[tuple[str,str]] | None, required: bool, type: str)`. `_build_auto_slash_command` grows a branch that constructs one `discord.app_commands.Choice` per tuple when `choices` is set; on Telegram it falls back to an inline-keyboard reply. ~80 LOC change; ships with a new test in `tests/hermes_cli/test_plugins.py`. File under `hermes-agent/` issue as **"feat(plugins): structured slash-command options (choices + typed params)"**.

Until that PR lands, the plugin must bypass `register_command` and **directly register on the adapter's `tree`** via a monkey-patch hook installed in `register(ctx)`. Pattern: subscribe to a future `post_slash_tree_built` event or, today, hook `DiscordAdapter._register_slash_commands` via the same monkey-patch seam `hermes_s2s._internal.discord_bridge` already uses for `join_voice_channel`.

## 3. Cross-platform — Telegram + CLI

| Platform | Surface | Mode-selection UX |
|---|---|---|
| Discord | `/s2s mode:<choice>` native slash picker | dropdown via `app_commands.Choice` |
| Telegram | `/s2s` (BotFather-registered) + inline keyboard reply | bot answers with 4-button `InlineKeyboardMarkup` (callback_data=`s2s:mode:realtime`). Telegram commands **cannot carry typed arguments with autocomplete** — setMyCommands is a flat list capped at 100 entries (per Bot API); no option metadata exists. Inline keyboards are the canonical workaround. Also accept `/s2s realtime` free-text args as a power-user path. |
| CLI (`hermes chat`) | `/s2s` in-session slash | Hermes CLI has no voice VC (no audio pipeline in terminal); `/s2s mode <m>` instead sets the **next-join default** and echoes `✅ next voice session will use mode: realtime`. When a Discord/Telegram VC join follows, that default wins unless per-join override is supplied. |

**Decision on Telegram**: option (c) from the task prompt — inline keyboard. Option (b) (free-text args) is kept as a fallback because it survives rate-limited BotFather command menus.

## 4. Mode discovery

- **Discord**: native — user types `/s2s`, Discord shows 4-item autocomplete dropdown with descriptions ("realtime — Gemini Live / gpt-realtime, lowest latency, requires API key").
- **Telegram**: bot replies with inline keyboard after bare `/s2s`:
  `[ 🎙️ cascaded ] [ 🔧 pipeline ] [ ⚡ realtime ] [ 🌐 s2s-server ]` — callback sets the mode and bot acks.
- **CLI**: tab-completion hooked into prompt_toolkit via `subcommands=("cascaded","pipeline","realtime","s2s-server")` on the `CommandDef` (same pattern `/fast`, `/reasoning`, `/voice` use — L133/L146 of `hermes_cli/commands.py`).
- **Graceful `/s2s` with no args**: prints a 4-line table showing mode, availability (✅/⚠), and requirement hint.

## 5. Per-channel override config

```yaml
s2s:
  voice:
    default_mode: cascaded
    channel_overrides:
      "1234567890":    realtime        # raw Discord channel ID
      "#voice-debug":  pipeline        # resolved via channel_directory
      "telegram:4242": s2s-server      # platform-prefixed for non-numeric conflicts
```

Resolution uses the existing `gateway.channel_directory.resolve_channel_name(platform, name)` (L267 of `gateway/channel_directory.py`) — it already handles `"#bot-home"`, `"GuildName/bot-home"`, and raw IDs. `ModeRouter.resolve()` walks: explicit slash arg → `channel_overrides[raw_id]` → `channel_overrides[resolved_name]` → `default_mode` → `cascaded`. No new resolver needed; just pipe through the existing one with `platform_name="discord"` / `"telegram"`.

## 6. UX edge cases

| Scenario | Behavior |
|---|---|
| User is in cascaded VC, types `/s2s mode:realtime` in same channel | **Switch in place**: tear down CascadedSession, build RealtimeSession against the same `VoiceClient`. VC stays connected (no `leave+rejoin`). Bot replies `🔄 Switched cascaded → realtime`. |
| User types `/s2s mode:realtime` while not in VC | Bot persists the choice as per-channel override; reply `✅ Next join in this channel will use realtime`. |
| Mode unavailable (realtime needs `GEMINI_API_KEY`, pipeline needs Kokoro installed) | `ModeRouter` checks availability via `VoiceSessionFactory.probe(mode)`; if missing, degrades to next-available mode and replies `⚠️ realtime unavailable (GEMINI_API_KEY not set) — falling back to cascaded`. Exit code matches R7 in research-12. |
| Rapid re-invoke (`/s2s mode:realtime` then `/s2s mode:cascaded` within 2s) | Debounce: second call waits for first teardown. Use `asyncio.Lock` keyed by `(guild_id, channel_id)`. |
| Non-admin invokes on a deployment with `DISCORD_HIDE_SLASH_COMMANDS=true` | Blocked at Discord-server level (zero-permissions default); server-side `_check_slash_authorization` (discord.py L3143-3178) confirms. Plugin inherits the same gate. |

## 7. Decision — **plugin-owned `/s2s`, not `/voice` extension**

| Criterion | `/voice` extension | **`/s2s` plugin-owned** |
|---|---|---|
| discord.py feasible | ❌ (no `add_choice`) | ✅ (plugin owns the Command) |
| Upstream dependency | ❌ (tree-mutate seam) | ✅ (uses existing `register_command`) |
| UX quality | best in theory | equal — native dropdown via a direct `tree.command` call (bypasses `register_command`'s text-only path) |
| Conflict with core | high (upstream may change `/voice`) | none — `/s2s` is plugin namespace |
| Discoverability | familiar verb | slightly less (2 commands to learn) — mitigated by `/help` cross-link |

**Final pick: `/s2s` plugin-owned slash command.** Plus an auto-typed alias `/voice-mode` that calls the same dispatcher, for users who look for it under `/voice*`.

### Implementation snippet (~30 LOC)

```python
# hermes_s2s/gateway/discord_commands.py
import discord
from discord import app_commands

MODE_CHOICES = [
    app_commands.Choice(name="cascaded — Whisper → Hermes → Edge TTS (default)", value="cascaded"),
    app_commands.Choice(name="pipeline — custom STT/TTS (Moonshine + Kokoro)",   value="pipeline"),
    app_commands.Choice(name="realtime — Gemini Live / gpt-realtime (low latency)", value="realtime"),
    app_commands.Choice(name="s2s-server — external duplex pipeline",            value="s2s-server"),
]

def install_s2s_command(adapter, mode_router, session_factory):
    tree = adapter.bot.tree

    @tree.command(name="s2s", description="Pick a speech-to-speech voice mode")
    @app_commands.describe(mode="Voice mode (omit to show status)")
    @app_commands.choices(mode=MODE_CHOICES)
    async def slash_s2s(interaction, mode: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id, channel_id = interaction.guild_id, interaction.channel_id
        if mode is None:
            spec = mode_router.resolve(mode_hint=None, guild_id=guild_id, channel_id=channel_id)
            return await interaction.followup.send(f"🎙️ Current mode: **{spec.mode.value}**", ephemeral=True)
        spec = mode_router.resolve(mode_hint=mode.value, guild_id=guild_id, channel_id=channel_id)
        vc = adapter._voice_clients.get(guild_id)
        if vc and vc.is_connected():
            await session_factory.swap(vc, spec)       # in-place teardown + rebuild
            await interaction.followup.send(f"🔄 Switched to **{spec.mode.value}**", ephemeral=True)
        else:
            mode_router.persist_channel_override(channel_id, spec.mode)
            await interaction.followup.send(f"✅ Next join here uses **{spec.mode.value}**", ephemeral=True)
```

Installed from the existing monkey-patch seam in `hermes_s2s._internal.discord_bridge` right after `adapter._register_slash_commands` finishes and before `tree.sync()` (hook into `DiscordAdapter.start` override, same pattern as the current `join_voice_channel` wrap).

### Rejected alternatives

| Alternative | Why rejected (one line) |
|---|---|
| Extend core `/voice` with a second `mode:` option | Name collision with existing subcommand selector; discord.py has no choice-extension API. |
| `tree.remove_command('voice')` + re-add from plugin | Brittle timing (must run between core register and sync); plugin forks upstream UX. |
| Free-text `/s2s <mode>` via `register_command(args_hint=…)` | No native autocomplete — loses the whole point of slash UX. |
| Env var `HERMES_S2S_MODE` only | No per-join switching; violates research-12 §2. Kept as `HERMES_S2S_FORCE_MODE` dev override. |
| Separate `/s2s-cascaded`, `/s2s-realtime`, … | Four commands for one concept; pollutes slash picker (count toward 100/guild limit). |
| Text-command only (`!s2s realtime`) | No autocomplete on Discord; inconsistent with rest of Hermes slash UX. |
| Config-file-only (no runtime command) | Forces restart to switch; explicitly rejected in research-12 Decision Matrix. |

## 8. Upstream PR shape (deferred to 0.4.1)

File: `hermes_cli/plugins.py` — extend `PluginContext.register_command(..., options: list[OptionSpec] = None)`. New `OptionSpec(name, description, choices=None, required=False, type_hint="str")`. Update `gateway/platforms/discord.py:_build_auto_slash_command` to synthesize `app_commands.Choice` from `options[*].choices` and use `@app_commands.describe` for descriptions. Telegram adapter gets a parallel `_register_inline_keyboard_for_choices` path that responds to bare commands with a choice keyboard. Ship with a `tests/hermes_cli/test_plugins.py::test_register_command_with_choices` contract test. Motivation paragraph emphasizes: **plugins cannot currently give Discord users autocomplete on command arguments, which breaks UX parity with core commands**.

## 9. Open questions

- Should `/s2s` also accept `channel:` option to let admins set overrides from anywhere? Deferred — admin-only surface, lower traffic.
- In-place mode swap (§6) requires `VoiceSession.stop()` to be idempotent and leave `VoiceClient` alive — contract-test this in `tests/s2s/test_session_swap.py`.
- Telegram inline-keyboard callbacks need a 1-minute TTL; stale clicks should reply `❌ choice expired, resend /s2s`.
