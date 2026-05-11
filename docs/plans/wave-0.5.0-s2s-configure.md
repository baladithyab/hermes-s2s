# `/s2s` v2 — Rich Configuration UI Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Build a multi-platform `/s2s configure` experience — Discord buttons + select menus, Telegram inline keyboards, CLI subcommands — that lets users switch S2S mode AND providers (realtime / STT / TTS) per-channel without editing `config.yaml`. Direct command paths preserved alongside the rich UI.

**Architecture:**

1. **One source of truth — `S2SConfigStore`.** Extends the existing `S2SModeOverrideStore` JSON file. Per-(guild, channel) record evolves from a flat string into a dict: `{"mode": "...", "realtime_provider": "...", "stt_provider": "...", "tts_provider": "..."}`. Migration is read-time and write-once (legacy string → wrapped dict). Reads honour back-compat.
2. **One presentation layer per platform.** `voice/slash.py` grows a `discord.app_commands.Group` with subcommands + `discord.ui.View` for the rich panel. New `voice/slash_telegram.py` mirrors via `python-telegram-bot`'s `InlineKeyboardMarkup`. CLI registration flows through Hermes' `COMMAND_REGISTRY` so `/s2s` works in the TUI too.
3. **Factory respects overrides for ALL keys, not just mode.** `voice/factory.py` reads each provider key from the per-channel record and falls back to `S2SConfig` when missing.

**Tech Stack:**
- discord.py 2.4+ (`app_commands.Group`, `ui.View`, `ui.Select`, `ui.Button`)
- python-telegram-bot 21.x (`InlineKeyboardMarkup`, `CallbackQueryHandler`)
- Existing plugin: hermes-s2s 0.4.6 → 0.5.0
- Tests: pytest + pytest-asyncio (already in `[dev]` extras)

---

## Wave 0 — Spec & Branch Setup

### Task 0.1: Create feature branch + ADR

**Objective:** Land a `0015-s2s-configure-rich-ui.md` ADR + `wave-0.5.0-s2s-configure.md` plan in repo so reviewers see the design intent before code.

**Files:**
- Create: `/mnt/e/CS/github/hermes-s2s/docs/adrs/0015-s2s-configure-rich-ui.md`
- Create: `/mnt/e/CS/github/hermes-s2s/docs/plans/wave-0.5.0-s2s-configure.md` (this file, copied in)

**Step 1: Branch**

```bash
cd /mnt/e/CS/github/hermes-s2s && git checkout -b feat/s2s-configure-v2
```

**Step 2: ADR content** — short. Records the decision to extend the override store schema (vs. a parallel store), why a `Group` not a flat command, why we keep direct subcommands alongside the View, and the migration path for legacy string entries.

**Step 3: Commit**

```bash
git add docs/adrs/0015-s2s-configure-rich-ui.md docs/plans/wave-0.5.0-s2s-configure.md
git commit -m "docs: ADR-0015 + wave-0.5.0 plan for /s2s rich UI"
```

---

## Wave 1 — Schema migration: extend the override store

### Task 1.1: Add failing test for dict-shaped record reads

**Objective:** Pin the contract: `S2SModeOverrideStore.get_record(g, c)` returns `{"mode": "...", "realtime_provider": ..., ...}` for new entries and lifts legacy string entries into `{"mode": <str>}`.

**Files:**
- Modify: `tests/test_slash_command.py` (add new test class)

**Step 1: Write failing test**

```python
# tests/test_slash_command.py — append:

def test_get_record_returns_dict_for_new_entries(tmp_path):
    from hermes_s2s.voice.slash import S2SModeOverrideStore
    store = S2SModeOverrideStore(path=tmp_path / "ovr.json")
    store.set_record(123, 456, {"mode": "realtime", "realtime_provider": "gpt-realtime-2"})
    rec = store.get_record(123, 456)
    assert rec == {"mode": "realtime", "realtime_provider": "gpt-realtime-2"}


def test_get_record_lifts_legacy_string(tmp_path):
    """Pre-0.5.0 entries on disk are bare strings; new readers must lift them."""
    import json
    p = tmp_path / "ovr.json"
    p.write_text(json.dumps({"123:456": "cascaded"}), encoding="utf-8")
    from hermes_s2s.voice.slash import S2SModeOverrideStore
    store = S2SModeOverrideStore(path=p)
    rec = store.get_record(123, 456)
    assert rec == {"mode": "cascaded"}


def test_legacy_get_method_still_works_after_dict_upgrade(tmp_path):
    """Existing factory.py call sites using .get() must continue to return the mode string."""
    from hermes_s2s.voice.slash import S2SModeOverrideStore
    store = S2SModeOverrideStore(path=tmp_path / "ovr.json")
    store.set_record(123, 456, {"mode": "s2s-server", "stt_provider": "groq"})
    assert store.get(123, 456) == "s2s-server"
```

**Step 2: Run to verify failure**

```bash
~/.hermes/hermes-agent/venv/bin/python3 -m pytest /mnt/e/CS/github/hermes-s2s/tests/test_slash_command.py -k "record" -v
```

Expected: 3 failures (`set_record` / `get_record` not defined).

### Task 1.2: Implement `set_record` / `get_record`, keep `set`/`get` shims

**Files:**
- Modify: `hermes_s2s/voice/slash.py` (around lines 131-160 — public API section of `S2SModeOverrideStore`)

**Step 1: Refactor cache type to `dict[str, dict[str, str]]`** internally; update `_load_locked` to coerce legacy string values via:

```python
def _coerce_value(v: Any) -> dict[str, str]:
    if isinstance(v, str):
        return {"mode": v}
    if isinstance(v, dict):
        return {str(k): str(val) for k, val in v.items() if val is not None}
    return {}
```

**Step 2: Add the new methods, keep old ones as thin shims:**

```python
# Public: rich
def get_record(self, guild_id: int, channel_id: int) -> dict[str, str]:
    with self._lock:
        self._ensure_loaded()
        return dict(self._cache.get(self._key(guild_id, channel_id), {}))

def set_record(self, guild_id: int, channel_id: int, record: dict[str, str]) -> None:
    """Replace the entire record for (guild, channel). Empty dict clears it."""
    cleaned = {str(k): str(v) for k, v in (record or {}).items() if v}
    if "mode" in cleaned:
        try:
            cleaned["mode"] = VoiceMode.normalize(cleaned["mode"]).value
        except ValueError:
            cleaned.pop("mode", None)
    with self._lock:
        self._ensure_loaded()
        merged = dict(self._cache)
        if cleaned:
            merged[self._key(guild_id, channel_id)] = cleaned
        else:
            merged.pop(self._key(guild_id, channel_id), None)
        self._write_atomic(merged)
        self._cache = merged

def patch_record(self, guild_id: int, channel_id: int, **fields: str) -> dict[str, str]:
    """Merge ``fields`` into the existing record; return the new record."""
    with self._lock:
        self._ensure_loaded()
        existing = dict(self._cache.get(self._key(guild_id, channel_id), {}))
    cleaned = {k: v for k, v in fields.items() if v}
    if "mode" in cleaned:
        try:
            cleaned["mode"] = VoiceMode.normalize(cleaned["mode"]).value
        except ValueError:
            cleaned.pop("mode", None)
    existing.update(cleaned)
    self.set_record(guild_id, channel_id, existing)
    return existing

# Back-compat: legacy callers
def set(self, guild_id: int, channel_id: int, mode: str) -> None:
    self.patch_record(guild_id, channel_id, mode=mode)

def get(self, guild_id: int, channel_id: int) -> str | None:
    rec = self.get_record(guild_id, channel_id)
    return rec.get("mode") if rec else None
```

**Step 3: Update `_write_atomic`** so the on-disk shape is `{"<key>": {"mode": "...", ...}}`, not `{"<key>": "..."}`. Same flock-protected merge logic — only the value-shape changes.

**Step 4: Run tests to verify pass**

```bash
~/.hermes/hermes-agent/venv/bin/python3 -m pytest /mnt/e/CS/github/hermes-s2s/tests/test_slash_command.py -v
```

Expected: all pre-existing tests + the 3 new ones pass.

**Step 5: Commit**

```bash
git add hermes_s2s/voice/slash.py tests/test_slash_command.py
git commit -m "feat(slash): extend override store to dict-shaped records (mode + provider keys)"
```

---

### Task 1.3: Factory consumes provider overrides

**Objective:** When a channel has e.g. `realtime_provider: gpt-realtime-2` in the override store, `voice/factory.py` must construct the realtime backend with that provider — not the global config's provider.

**Files:**
- Read: `hermes_s2s/voice/factory.py` (404 lines — find the override-resolution section that currently calls `store.get(guild_id, channel_id)`)
- Modify: same file — switch to `store.get_record(...)` and apply provider overrides on top of `cfg.realtime_provider` / `cfg.stt.provider` / `cfg.tts.provider`.
- Modify: `tests/test_connect_options_and_config.py` (add 3 tests for override resolution)

**Step 1: Failing tests** — assert that with the override store containing `{"realtime_provider": "gpt-realtime-mini"}`, the factory's resolved realtime backend reflects that, not the config default.

```python
# tests/test_connect_options_and_config.py — append:

def test_factory_uses_per_channel_realtime_provider_override(tmp_path, monkeypatch):
    from hermes_s2s.voice.slash import S2SModeOverrideStore
    from hermes_s2s.voice import factory as fac
    # Wire a temp store
    store = S2SModeOverrideStore(path=tmp_path / "ovr.json")
    store.set_record(123, 456, {"mode": "realtime", "realtime_provider": "gpt-realtime-mini"})
    monkeypatch.setattr(fac, "get_default_store", lambda: store)
    # Resolve the per-channel S2SConfig
    cfg = fac.resolve_s2s_config_for_channel(guild_id=123, channel_id=456)
    assert cfg.mode == "realtime"
    assert cfg.realtime_provider == "gpt-realtime-mini"


def test_factory_falls_back_to_global_when_no_override(tmp_path, monkeypatch):
    from hermes_s2s.voice.slash import S2SModeOverrideStore
    from hermes_s2s.voice import factory as fac
    store = S2SModeOverrideStore(path=tmp_path / "ovr.json")
    monkeypatch.setattr(fac, "get_default_store", lambda: store)
    cfg = fac.resolve_s2s_config_for_channel(guild_id=999, channel_id=888)
    # No override → global config wins; just assert no crash + valid mode
    assert cfg.mode in {"cascaded", "realtime", "s2s-server", "pipeline"}


def test_factory_partial_override_keeps_other_keys_global(tmp_path, monkeypatch):
    from hermes_s2s.voice.slash import S2SModeOverrideStore
    from hermes_s2s.voice import factory as fac
    store = S2SModeOverrideStore(path=tmp_path / "ovr.json")
    # Only TTS overridden; mode + STT come from global config
    store.set_record(123, 456, {"tts_provider": "elevenlabs"})
    monkeypatch.setattr(fac, "get_default_store", lambda: store)
    cfg = fac.resolve_s2s_config_for_channel(guild_id=123, channel_id=456)
    assert cfg.tts.provider == "elevenlabs"
```

**Step 2: Run, expect failures** (`resolve_s2s_config_for_channel` may not exist yet, or it currently only handles `mode`).

**Step 3: Implement.** Find the existing override-aware path in `factory.py`. Generalize it:

```python
def resolve_s2s_config_for_channel(*, guild_id: int | None, channel_id: int | None) -> S2SConfig:
    cfg = load_config()
    if guild_id is None or channel_id is None:
        return cfg
    rec = get_default_store().get_record(int(guild_id), int(channel_id))
    if not rec:
        return cfg
    # Mutate a shallow copy — never the cached singleton
    cfg = dataclasses.replace(cfg)  # if S2SConfig is a frozen dataclass; otherwise copy.copy
    if "mode" in rec:
        cfg = cfg.with_mode(rec["mode"])  # add helper if missing
    if "realtime_provider" in rec:
        cfg = cfg.with_realtime_provider(rec["realtime_provider"])
    if "stt_provider" in rec:
        cfg = cfg.with_stt_provider(rec["stt_provider"])
    if "tts_provider" in rec:
        cfg = cfg.with_tts_provider(rec["tts_provider"])
    return cfg
```

`S2SConfig.with_*` helpers are 1-line wrappers around dataclasses.replace; check current shape and add as needed.

**Step 4: Wire it.** Find the call sites that build a voice session and switch them to `resolve_s2s_config_for_channel(guild_id=..., channel_id=...)`. There should be ≤3 sites — `_internal/discord_bridge.py` and `voice/sessions*.py`.

**Step 5: Verify**

```bash
~/.hermes/hermes-agent/venv/bin/python3 -m pytest /mnt/e/CS/github/hermes-s2s/tests/ -v -x
```

Expected: full suite green.

**Step 6: Commit**

```bash
git add hermes_s2s/voice/factory.py hermes_s2s/voice/sessions*.py hermes_s2s/_internal/ tests/test_connect_options_and_config.py
git commit -m "feat(factory): per-channel provider overrides flow into voice session config"
```

---

## Wave 2 — Discord rich UI

### Task 2.1: Failing test for `/s2s status` subcommand text

**Objective:** Pin the format of the status reply for the new subcommand. We can test the formatter independently of discord.py.

**Files:**
- Create: `hermes_s2s/voice/slash_format.py` (new — pure functions, easy to test)
- Modify: `tests/test_slash_command.py` (add subcommand-formatter tests)

**Step 1: Test**

```python
# tests/test_slash_command.py — append:

def test_status_formatter_renders_active_mode_and_providers():
    from hermes_s2s.voice.slash_format import format_status
    out = format_status(
        active_mode="realtime",
        config_mode="cascaded",
        realtime_provider="gpt-realtime-2",
        stt_provider="moonshine",
        tts_provider="kokoro",
        guild_id=123,
        channel_id=456,
        per_channel_record={"mode": "realtime", "realtime_provider": "gpt-realtime-2"},
    )
    assert "realtime" in out and "gpt-realtime-2" in out
    assert "this channel overrides" in out.lower() or "override" in out.lower()


def test_status_formatter_no_override_label():
    from hermes_s2s.voice.slash_format import format_status
    out = format_status(
        active_mode="cascaded",
        config_mode="cascaded",
        realtime_provider="gemini-live",
        stt_provider="moonshine",
        tts_provider="kokoro",
        guild_id=123,
        channel_id=456,
        per_channel_record={},
    )
    assert "global default" in out.lower() or "no channel override" in out.lower()
```

**Step 2: Verify failure** — module doesn't exist.

**Step 3: Implement**

```python
# hermes_s2s/voice/slash_format.py
"""Pure-text formatters for the /s2s slash command.

Decoupled from discord.py / python-telegram-bot so unit tests don't need
either dep installed. Discord and Telegram presenters import from here.
"""
from __future__ import annotations
from typing import Mapping


def format_status(
    *,
    active_mode: str,
    config_mode: str,
    realtime_provider: str,
    stt_provider: str,
    tts_provider: str,
    guild_id: int,
    channel_id: int,
    per_channel_record: Mapping[str, str],
) -> str:
    has_override = bool(per_channel_record)
    lines = [
        f"**S2S status — guild `{guild_id}` channel `{channel_id}`**",
        f"  • Active mode: `{active_mode}`"
        + (f" (this channel overrides global `{config_mode}`)" if has_override else " (global default)"),
        f"  • Realtime provider: `{realtime_provider}`",
        f"  • Cascaded STT: `{stt_provider}`",
        f"  • Cascaded TTS: `{tts_provider}`",
    ]
    if not has_override:
        lines.append("  • _no channel override — using global config_")
    else:
        keys = ", ".join(f"`{k}`" for k in sorted(per_channel_record))
        lines.append(f"  • Channel overrides set: {keys}")
    return "\n".join(lines)


def format_help() -> str:
    return (
        "**`/s2s` subcommands**\n"
        "  `/s2s configure` — open interactive panel\n"
        "  `/s2s status` — show active mode + providers\n"
        "  `/s2s mode <choice>` — switch mode for this channel\n"
        "  `/s2s provider <kind> <name>` — set a provider override\n"
        "  `/s2s test [text]` — TTS smoke test\n"
        "  `/s2s doctor` — preflight checks (deps, keys, WS handshake)\n"
        "  `/s2s reset` — clear this channel's overrides"
    )
```

**Step 4: Verify**

```bash
~/.hermes/hermes-agent/venv/bin/python3 -m pytest /mnt/e/CS/github/hermes-s2s/tests/test_slash_command.py -v
```

**Step 5: Commit**

```bash
git add hermes_s2s/voice/slash_format.py tests/test_slash_command.py
git commit -m "feat(slash): pure-text formatters for /s2s status & help"
```

### Task 2.2: Refactor `/s2s` from single command to `app_commands.Group`

**Objective:** Replace the existing single `@app_commands.command` decorator with a `Group`, then add subcommands to it: `mode`, `status`, `provider`, `test`, `doctor`, `reset`. `configure` is added in 2.3.

**Files:**
- Modify: `hermes_s2s/voice/slash.py` (the entire `install_s2s_command` function and the existing `s2s_command` definition)
- Modify: `tests/test_slash_command.py` (update existing tests if any reference the single-command shape)

**Step 1: Failing test**

```python
# tests/test_slash_command.py — append:

def test_install_creates_group_with_subcommands():
    """The Discord tree should receive a Group named 's2s' with the expected leaf subcommands."""
    pytest.importorskip("discord")
    import discord
    from discord import app_commands
    from hermes_s2s.voice.slash import install_s2s_command

    # Build a minimal fake tree
    class FakeClient:
        pass

    intents = discord.Intents.none()
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)
    ctx = type("Ctx", (), {})()
    ctx.adapter = type("A", (), {"_client": client})()
    client.tree = tree  # type: ignore[attr-defined]

    installed = install_s2s_command(ctx)
    assert installed is True
    cmd = tree.get_command("s2s")
    assert cmd is not None
    # Group must expose these subcommands
    sub_names = {c.name for c in cmd.commands}
    assert {"mode", "status", "provider", "test", "doctor", "reset"} <= sub_names
```

**Step 2: Verify failure**.

**Step 3: Implement.** Inside `install_s2s_command`, replace the single-command block with:

```python
# After the discord/app_commands lazy-import + globals injection:
group = app_commands.Group(name="s2s", description="Configure speech-to-speech voice")

@group.command(name="mode", description="Set voice mode for this channel")
@app_commands.choices(mode=[
    app_commands.Choice(name="Cascaded (default)", value="cascaded"),
    app_commands.Choice(name="Pipeline (custom STT+TTS)", value="pipeline"),
    app_commands.Choice(name="Realtime", value="realtime"),
    app_commands.Choice(name="External S2S server", value="s2s-server"),
])
async def s2s_mode(interaction: Interaction, mode: Choice[str]):
    g, c = _require_guild_channel(interaction)
    if g is None: return
    store.patch_record(g, c, mode=mode.value)
    await interaction.response.send_message(
        f"✅ This channel: mode → **{mode.name}**", ephemeral=True
    )

@group.command(name="status", description="Show current S2S settings for this channel")
async def s2s_status_cmd(interaction: Interaction):
    g, c = _require_guild_channel(interaction)
    if g is None: return
    from ..tools import s2s_status as _status_tool
    import json as _json
    payload = _json.loads(_status_tool({}))
    rec = store.get_record(g, c)
    text = format_status(
        active_mode=payload["active_mode"],
        config_mode=payload["config_mode"],
        realtime_provider=payload["realtime"]["provider"],
        stt_provider=payload["cascaded"]["stt_provider"],
        tts_provider=payload["cascaded"]["tts_provider"],
        guild_id=g, channel_id=c, per_channel_record=rec,
    )
    await interaction.response.send_message(text, ephemeral=True)

@group.command(name="provider", description="Override a single provider for this channel")
@app_commands.choices(kind=[
    app_commands.Choice(name="Realtime backend", value="realtime"),
    app_commands.Choice(name="STT (cascaded)", value="stt"),
    app_commands.Choice(name="TTS (cascaded)", value="tts"),
])
@app_commands.describe(name="Provider name (see `/s2s status` for available)")
async def s2s_provider(interaction: Interaction, kind: Choice[str], name: str):
    g, c = _require_guild_channel(interaction)
    if g is None: return
    field_map = {
        "realtime": "realtime_provider",
        "stt": "stt_provider",
        "tts": "tts_provider",
    }
    field = field_map[kind.value]
    # Validate against the registry — refuse unknown names with the available list
    from ..registry import list_registered
    available = list_registered().get(kind.value, [])
    if name not in available:
        await interaction.response.send_message(
            f"❌ Unknown {kind.value} provider `{name}`. Available: "
            + ", ".join(f"`{a}`" for a in available),
            ephemeral=True,
        )
        return
    store.patch_record(g, c, **{field: name})
    await interaction.response.send_message(
        f"✅ This channel: {kind.value} provider → **{name}**", ephemeral=True
    )

@group.command(name="test", description="Run a TTS smoke test")
@app_commands.describe(text="Optional text to synthesise")
async def s2s_test(interaction: Interaction, text: str = ""):
    await interaction.response.defer(ephemeral=True, thinking=True)
    from ..tools import s2s_test_pipeline
    import json as _json
    result = _json.loads(s2s_test_pipeline({"text": text or None}))
    if result.get("ok"):
        await interaction.followup.send(
            f"✅ TTS OK — `{result.get('tts_provider')}` wrote {result.get('bytes')} bytes to "
            f"`{result.get('wrote')}`",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            f"❌ Smoke test failed at stage `{result.get('stage')}`: {result.get('error')}",
            ephemeral=True,
        )

@group.command(name="doctor", description="Run preflight checks (deps, keys, WS probe)")
async def s2s_doctor_cmd(interaction: Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    from ..tools import s2s_doctor as _doctor
    import json as _json
    report_str = await _doctor({})
    report = _json.loads(report_str)
    # Compact summary; full json in followup if user clicks "More"
    summary = format_doctor_summary(report)  # add to slash_format.py
    await interaction.followup.send(summary, ephemeral=True)

@group.command(name="reset", description="Clear all S2S overrides for this channel")
async def s2s_reset(interaction: Interaction):
    g, c = _require_guild_channel(interaction)
    if g is None: return
    store.set_record(g, c, {})
    await interaction.response.send_message(
        "✅ Cleared all S2S overrides for this channel — back to global config.",
        ephemeral=True,
    )

tree.add_command(group)
```

Helpers to add at module top (just under imports):

```python
def _require_guild_channel(interaction):
    """Return (guild_id, channel_id) or (None, None) after sending an error."""
    g = getattr(interaction, "guild", None)
    c = getattr(interaction, "channel", None)
    g_id = getattr(g, "id", None)
    c_id = getattr(c, "id", None)
    if g_id is None or c_id is None:
        # Caller's responsibility to await the send; we can't here without making this async
        return None, None
    return int(g_id), int(c_id)
```

Note: `_require_guild_channel` needs to send an error itself; refactor to async returning `(g, c) | None` and have callers `if not pair: return`. The plan above shows the contract — implementer adapts the exact shape.

**Step 4: Verify** — full pytest, plus a real Discord smoke test deferred to Wave 5.

**Step 5: Commit**

```bash
git add hermes_s2s/voice/slash.py hermes_s2s/voice/slash_format.py tests/test_slash_command.py
git commit -m "feat(slash): /s2s as Group with mode/status/provider/test/doctor/reset"
```

### Task 2.3: Add `/s2s configure` View — buttons + select menus

**Objective:** Implement the rich panel the user wants. Persistent ephemeral message with selects for each provider kind + buttons for Test, Reset, Refresh.

**Files:**
- Modify: `hermes_s2s/voice/slash.py` (add `S2SConfigureView` class + `configure` subcommand)
- Create: `tests/test_configure_view.py` (View construction + select-callback unit tests via mocks)

**Step 1: Failing test**

```python
# tests/test_configure_view.py
import pytest
pytest.importorskip("discord")

import discord
from discord import ui


def test_configure_view_has_expected_components():
    """View should contain 4 selects (mode + 3 providers) + 3 buttons."""
    from hermes_s2s.voice.slash import S2SConfigureView
    view = S2SConfigureView(guild_id=1, channel_id=2, store=MockStore())
    selects = [c for c in view.children if isinstance(c, ui.Select)]
    buttons = [c for c in view.children if isinstance(c, ui.Button)]
    assert len(selects) == 4   # mode, realtime, stt, tts
    assert len(buttons) >= 3   # test, reset, refresh
    # Mode select must include all four canonical modes
    mode_select = next(s for s in selects if s.placeholder and "mode" in s.placeholder.lower())
    values = {opt.value for opt in mode_select.options}
    assert {"cascaded", "realtime", "pipeline", "s2s-server"} <= values


class MockStore:
    def get_record(self, *_): return {}
    def set_record(self, *args, **kwargs): pass
    def patch_record(self, *args, **kwargs): return {}
```

**Step 2: Verify failure** (`S2SConfigureView` undefined).

**Step 3: Implement** — full View class:

```python
# hermes_s2s/voice/slash.py — append below the install function

class S2SConfigureView(ui.View if 'ui' in globals() else object):
    """Rich configuration panel for /s2s configure.

    Lifetime: 5 minutes (default View timeout); on timeout the controls
    disable themselves but the message stays as a summary.
    """

    def __init__(self, guild_id: int, channel_id: int, store: S2SModeOverrideStore):
        super().__init__(timeout=300.0)
        self._g = guild_id
        self._c = channel_id
        self._store = store
        # Build options dynamically from the live registry
        from ..registry import list_registered
        reg = list_registered()
        self._add_mode_select()
        self._add_provider_select("realtime", reg.get("realtime", []))
        self._add_provider_select("stt", reg.get("stt", []))
        self._add_provider_select("tts", reg.get("tts", []))

    def _add_mode_select(self):
        opts = [
            discord.SelectOption(label="Cascaded (default)", value="cascaded"),
            discord.SelectOption(label="Pipeline (custom)", value="pipeline"),
            discord.SelectOption(label="Realtime", value="realtime"),
            discord.SelectOption(label="External server", value="s2s-server"),
        ]
        async def _cb(interaction):
            v = interaction.data["values"][0]
            self._store.patch_record(self._g, self._c, mode=v)
            await interaction.response.send_message(f"Mode → `{v}`", ephemeral=True)

        sel = ui.Select(placeholder="Pick a mode…", options=opts, min_values=1, max_values=1, custom_id="s2s_mode")
        sel.callback = _cb
        self.add_item(sel)

    def _add_provider_select(self, kind: str, names: list[str]):
        if not names:
            return  # No registered providers of this kind — skip the row
        opts = [discord.SelectOption(label=n, value=n) for n in names[:25]]  # Discord cap
        field = f"{kind}_provider" if kind in {"stt", "tts", "realtime"} else f"{kind}_provider"
        placeholder = {
            "realtime": "Pick realtime backend…",
            "stt": "Pick STT provider…",
            "tts": "Pick TTS provider…",
        }[kind]

        async def _cb(interaction):
            v = interaction.data["values"][0]
            self._store.patch_record(self._g, self._c, **{field: v})
            await interaction.response.send_message(f"{kind.upper()} provider → `{v}`", ephemeral=True)

        sel = ui.Select(placeholder=placeholder, options=opts, min_values=1, max_values=1,
                        custom_id=f"s2s_{kind}")
        sel.callback = _cb
        self.add_item(sel)

    @ui.button(label="Test pipeline", style=discord.ButtonStyle.primary, custom_id="s2s_test")
    async def _test_btn(self, interaction, _button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        from ..tools import s2s_test_pipeline
        import json
        r = json.loads(s2s_test_pipeline({}))
        if r.get("ok"):
            await interaction.followup.send(
                f"✅ TTS OK — wrote {r.get('bytes')} bytes via `{r.get('tts_provider')}`",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"❌ Test failed at `{r.get('stage')}`: {r.get('error')}", ephemeral=True
            )

    @ui.button(label="Reset overrides", style=discord.ButtonStyle.danger, custom_id="s2s_reset")
    async def _reset_btn(self, interaction, _button):
        self._store.set_record(self._g, self._c, {})
        await interaction.response.send_message("✅ Cleared this channel's overrides.", ephemeral=True)

    @ui.button(label="Refresh status", style=discord.ButtonStyle.secondary, custom_id="s2s_refresh")
    async def _refresh_btn(self, interaction, _button):
        from ..tools import s2s_status as _stat
        import json
        payload = json.loads(_stat({}))
        rec = self._store.get_record(self._g, self._c)
        text = format_status(
            active_mode=payload["active_mode"],
            config_mode=payload["config_mode"],
            realtime_provider=payload["realtime"]["provider"],
            stt_provider=payload["cascaded"]["stt_provider"],
            tts_provider=payload["cascaded"]["tts_provider"],
            guild_id=self._g, channel_id=self._c, per_channel_record=rec,
        )
        await interaction.response.edit_message(content=text, view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
```

Then add the `/s2s configure` subcommand to the group (in Task 2.2's block):

```python
@group.command(name="configure", description="Open interactive S2S configuration panel")
async def s2s_configure(interaction: Interaction):
    g, c = _require_guild_channel(interaction)
    if g is None:
        await interaction.response.send_message("`/s2s configure` requires a guild channel.", ephemeral=True)
        return
    from ..tools import s2s_status as _stat
    import json as _json
    payload = _json.loads(_stat({}))
    rec = store.get_record(g, c)
    text = format_status(
        active_mode=payload["active_mode"],
        config_mode=payload["config_mode"],
        realtime_provider=payload["realtime"]["provider"],
        stt_provider=payload["cascaded"]["stt_provider"],
        tts_provider=payload["cascaded"]["tts_provider"],
        guild_id=g, channel_id=c, per_channel_record=rec,
    )
    view = S2SConfigureView(guild_id=g, channel_id=c, store=store)
    await interaction.response.send_message(text, view=view, ephemeral=True)
```

**Pitfalls watch:**
- Discord caps a single Select at 25 options. We `[:25]` slice. Realistic count is <10 per kind for now.
- `discord.ui.button` is a method-decorator that captures `self`; the auto-generated `custom_id` must be unique per View instance — discord.py handles this via the `custom_id` kwarg we pass.
- View timeout = 300s. Discord's interaction-token expiry is 15 min; defer for any callback that hits the network (`_test_btn`, `_refresh_btn` if it loads from disk a lot).

**Step 4: Verify**

```bash
~/.hermes/hermes-agent/venv/bin/python3 -m pytest /mnt/e/CS/github/hermes-s2s/tests/ -v -x
```

**Step 5: Commit**

```bash
git add hermes_s2s/voice/slash.py tests/test_configure_view.py
git commit -m "feat(slash): /s2s configure — rich Discord View with select menus + buttons"
```

---

## Wave 3 — Telegram inline keyboard

### Task 3.1: Telegram presenter scaffold + tests

**Objective:** Stand up `voice/slash_telegram.py` so `/s2s` works on Telegram with inline-keyboard buttons. Telegram doesn't have ephemeral messages — we use private chat replies and `delete_after` patterns.

**Files:**
- Create: `hermes_s2s/voice/slash_telegram.py`
- Create: `tests/test_slash_telegram.py`

**Step 1: Failing test**

```python
# tests/test_slash_telegram.py
import pytest
pytest.importorskip("telegram")  # python-telegram-bot

def test_build_status_keyboard_has_mode_and_provider_rows():
    from hermes_s2s.voice.slash_telegram import build_configure_keyboard
    kb = build_configure_keyboard(realtime_providers=["gpt-realtime-2", "gemini-live"],
                                  stt_providers=["moonshine"],
                                  tts_providers=["kokoro"])
    # kb is InlineKeyboardMarkup with rows for each provider kind
    assert kb is not None
    rows = kb.inline_keyboard
    # at least 1 row of mode buttons + 3 provider rows + 1 actions row
    assert len(rows) >= 4
    # All buttons have callback_data starting with "s2s:"
    for row in rows:
        for btn in row:
            assert btn.callback_data.startswith("s2s:")
```

**Step 2: Verify failure.**

**Step 3: Implement**

```python
# hermes_s2s/voice/slash_telegram.py
"""Telegram inline-keyboard presenter for /s2s.

Mirrors the Discord rich UI on Telegram. Telegram callback_data has a
64-byte limit, so we use short tokens: ``s2s:<verb>:<arg>`` (e.g.
``s2s:mode:realtime``, ``s2s:rt:gpt-realtime-2``).

Registration entry point: ``install_s2s_telegram_handlers(application)``
called from the Hermes telegram adapter via the plugin's
``on_session_start`` hook (or a dedicated installer triggered when the
adapter is first reachable from ``ctx``).
"""
from __future__ import annotations

import logging
from typing import Any, Sequence

logger = logging.getLogger(__name__)


def build_configure_keyboard(
    *,
    realtime_providers: Sequence[str],
    stt_providers: Sequence[str],
    tts_providers: Sequence[str],
) -> Any:
    """Construct an InlineKeyboardMarkup with mode + provider buttons + actions."""
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    except ImportError:
        return None

    # Row 1: mode picker
    mode_row = [
        InlineKeyboardButton("Cascaded", callback_data="s2s:mode:cascaded"),
        InlineKeyboardButton("Realtime", callback_data="s2s:mode:realtime"),
        InlineKeyboardButton("Pipeline", callback_data="s2s:mode:pipeline"),
        InlineKeyboardButton("Server", callback_data="s2s:mode:s2s-server"),
    ]
    # Provider rows — at most 3 per row to keep mobile readable
    def _provider_row(kind_short: str, names: Sequence[str]) -> list:
        return [InlineKeyboardButton(n, callback_data=f"s2s:{kind_short}:{n}")
                for n in list(names)[:3]]

    rows = [mode_row]
    if realtime_providers:
        rows.append(_provider_row("rt", realtime_providers))
    if stt_providers:
        rows.append(_provider_row("stt", stt_providers))
    if tts_providers:
        rows.append(_provider_row("tts", tts_providers))
    rows.append([
        InlineKeyboardButton("🧪 Test", callback_data="s2s:test"),
        InlineKeyboardButton("♻️ Reset", callback_data="s2s:reset"),
        InlineKeyboardButton("🔄 Refresh", callback_data="s2s:refresh"),
    ])
    return InlineKeyboardMarkup(rows)


async def handle_s2s_command_telegram(update, context):
    """Implements `/s2s` on Telegram. Replies with status + inline keyboard."""
    from ..registry import list_registered
    from ..tools import s2s_status
    from .slash import get_default_store
    from .slash_format import format_status
    import json

    chat = update.effective_chat
    chat_id = int(chat.id)
    # Telegram has no "guild_id"; use chat_id for both — store key remains stable.
    g_id, c_id = chat_id, chat_id
    payload = json.loads(s2s_status({}))
    rec = get_default_store().get_record(g_id, c_id)
    text = format_status(
        active_mode=payload["active_mode"],
        config_mode=payload["config_mode"],
        realtime_provider=payload["realtime"]["provider"],
        stt_provider=payload["cascaded"]["stt_provider"],
        tts_provider=payload["cascaded"]["tts_provider"],
        guild_id=g_id, channel_id=c_id, per_channel_record=rec,
    )
    reg = list_registered()
    kb = build_configure_keyboard(
        realtime_providers=reg.get("realtime", []),
        stt_providers=reg.get("stt", []),
        tts_providers=reg.get("tts", []),
    )
    await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="Markdown")


async def handle_s2s_callback(update, context):
    """Process inline-keyboard taps."""
    from .slash import get_default_store
    from ..tools import s2s_test_pipeline
    import json

    query = update.callback_query
    await query.answer()  # ack within 3s
    parts = (query.data or "").split(":", 2)
    if len(parts) < 2 or parts[0] != "s2s":
        return
    chat_id = int(query.message.chat.id)
    g_id, c_id = chat_id, chat_id
    store = get_default_store()
    verb = parts[1]
    arg = parts[2] if len(parts) >= 3 else None

    if verb == "mode" and arg:
        store.patch_record(g_id, c_id, mode=arg)
        await query.edit_message_text(f"Mode → `{arg}`", parse_mode="Markdown")
    elif verb in {"rt", "stt", "tts"} and arg:
        field_map = {"rt": "realtime_provider", "stt": "stt_provider", "tts": "tts_provider"}
        store.patch_record(g_id, c_id, **{field_map[verb]: arg})
        await query.edit_message_text(
            f"{verb.upper()} provider → `{arg}`", parse_mode="Markdown"
        )
    elif verb == "test":
        r = json.loads(s2s_test_pipeline({}))
        msg = (f"✅ TTS OK — {r.get('bytes')} bytes via `{r.get('tts_provider')}`"
               if r.get("ok") else
               f"❌ failed at `{r.get('stage')}`: {r.get('error')}")
        await query.message.reply_text(msg, parse_mode="Markdown")
    elif verb == "reset":
        store.set_record(g_id, c_id, {})
        await query.edit_message_text("✅ Cleared overrides for this chat.")
    elif verb == "refresh":
        await handle_s2s_command_telegram(update, context)


def install_s2s_telegram_handlers(application: Any) -> bool:
    """Register `/s2s` + callback handler on a python-telegram-bot Application.

    Idempotent — repeated calls are no-ops. Returns True if handlers were
    newly added, False otherwise.
    """
    try:
        from telegram.ext import CommandHandler, CallbackQueryHandler
    except ImportError:
        return False
    sentinel = "__hermes_s2s_telegram_installed__"
    if getattr(application, sentinel, False):
        return False
    application.add_handler(CommandHandler("s2s", handle_s2s_command_telegram))
    application.add_handler(CallbackQueryHandler(handle_s2s_callback, pattern=r"^s2s:"))
    setattr(application, sentinel, True)
    return True
```

**Step 4: Wire it in `__init__.py:register(ctx)`**:

```python
# After install_s2s_command(ctx)... add a Telegram walker:
try:
    from .voice.slash_telegram import install_s2s_telegram_handlers
    # Mirror _find_discord_tree's walking pattern
    runner = getattr(ctx, "runner", None)
    adapters = getattr(runner, "adapters", {}) if runner else {}
    for ad in (adapters.values() if isinstance(adapters, dict) else []):
        app = (getattr(ad, "_application", None)
               or getattr(ad, "application", None)
               or getattr(ad, "_app", None))
        if app is not None and install_s2s_telegram_handlers(app):
            logger.info("hermes-s2s: /s2s installed on Telegram")
            break
except Exception as exc:
    logger.debug("Telegram /s2s install skipped: %s", exc)
```

(The exact attribute name on the Telegram adapter — `_application` vs `application` — needs grep verification. Look at `gateway/platforms/telegram.py` to confirm.)

**Step 5: Verify**

```bash
~/.hermes/hermes-agent/venv/bin/python3 -m pytest /mnt/e/CS/github/hermes-s2s/tests/test_slash_telegram.py -v
```

**Step 6: Commit**

```bash
git add hermes_s2s/voice/slash_telegram.py hermes_s2s/__init__.py tests/test_slash_telegram.py
git commit -m "feat(telegram): /s2s inline-keyboard configure UI for Telegram"
```

---

## Wave 4 — CLI parity

### Task 4.1: Wire `/s2s` slash into Hermes CLI command registry

**Objective:** When typing `/s2s configure` in `hermes` TUI, get a curses-style picker (or numbered prompts) — no buttons, but the same configuration flow. Direct subcommands (`/s2s mode realtime`, `/s2s status`) must work as plain text replies.

**Files:**
- Modify: `hermes_s2s/tools.py` (extend `handle_s2s_command` to support subcommand routing matching the Discord Group)
- The existing `ctx.register_command("s2s", ...)` already pipes the slash into the CLI; we just upgrade the handler.

**Step 1: Failing test**

```python
# tests/test_slash_command.py — append:

def test_handle_s2s_command_routes_subcommands(tmp_path, monkeypatch):
    from hermes_s2s.tools import handle_s2s_command
    # No-arg → status
    out = handle_s2s_command("")
    assert "active_mode" in out or "Active mode" in out  # JSON or formatted

    # mode set
    out = handle_s2s_command("mode realtime")
    assert "realtime" in out

    # provider set (in CLI we don't have guild_id/channel_id — should still work via session-id key)
    out = handle_s2s_command("provider realtime gpt-realtime-2")
    # Not great UX without channel ctx; should accept and warn
    assert "gpt-realtime-2" in out or "warn" in out.lower() or "global" in out.lower()

    # help
    out = handle_s2s_command("help")
    assert "configure" in out and "mode" in out and "provider" in out


def test_handle_s2s_command_unknown_subcommand_shows_help():
    from hermes_s2s.tools import handle_s2s_command
    out = handle_s2s_command("frobnicate")
    assert "Usage" in out or "configure" in out
```

**Step 2: Verify failure** — current handler doesn't route `provider`, `doctor`, `reset`, `configure`, or `help`.

**Step 3: Implement** — replace `handle_s2s_command` body with a real subcommand router:

```python
def handle_s2s_command(raw_args: str) -> str:
    parts = (raw_args or "").strip().split()
    if not parts or parts[0] in {"status", "show", "info"}:
        # Pretty-print, not JSON, in CLI
        from .voice.slash_format import format_status
        import json
        payload = json.loads(s2s_status({}))
        return format_status(
            active_mode=payload["active_mode"],
            config_mode=payload["config_mode"],
            realtime_provider=payload["realtime"]["provider"],
            stt_provider=payload["cascaded"]["stt_provider"],
            tts_provider=payload["cascaded"]["tts_provider"],
            guild_id=0, channel_id=0, per_channel_record={},
        )
    sub = parts[0]
    if sub == "mode" and len(parts) >= 2:
        result = json.loads(s2s_set_mode({"mode": parts[1]}))
        if "error" in result:
            return f"❌ {result['error']}"
        return f"✅ Session mode → {result['session_mode']}"
    if sub == "provider" and len(parts) >= 3:
        kind, name = parts[1], parts[2]
        if kind not in {"realtime", "stt", "tts"}:
            return "Usage: /s2s provider <realtime|stt|tts> <name>"
        from .registry import list_registered
        avail = list_registered().get(kind, [])
        if name not in avail:
            return f"❌ Unknown {kind} provider '{name}'. Available: {', '.join(avail)}"
        # Per-session override (CLI lacks guild/channel — store under session id)
        # TODO: route through a sessions-aware override store; for now, edit config.yaml suggestion
        return (f"⚠️  CLI provider override not yet plumbed (no per-channel context). "
                f"Edit ~/.hermes/config.yaml: s2s.{kind}.provider: {name}")
    if sub == "test":
        text = " ".join(parts[1:]) or None
        result = json.loads(s2s_test_pipeline({"text": text}))
        if result.get("ok"):
            return f"✅ TTS OK — wrote {result['bytes']} bytes to {result['wrote']}"
        return f"❌ failed at {result.get('stage')}: {result.get('error')}"
    if sub == "doctor":
        # Note: handler is async; CLI sync path needs asyncio.run() if it's a sync slash
        import asyncio
        try:
            report_str = asyncio.run(s2s_doctor({}))
        except RuntimeError:
            # Already inside an event loop — fall back to non-probing version
            return "Doctor must be called from a sync context. Use `hermes s2s doctor` instead."
        return report_str  # JSON for now; consider format_doctor_summary later
    if sub == "reset":
        # CLI reset = clear session override
        sid = "default"
        _SESSION_MODE_OVERRIDE.pop(sid, None)
        return "✅ Cleared session override."
    if sub in {"configure", "help"}:
        from .voice.slash_format import format_help
        return format_help()
    # Unknown
    from .voice.slash_format import format_help
    return f"Unknown subcommand `{sub}`.\n\n{format_help()}"
```

**Step 4: Verify**

```bash
~/.hermes/hermes-agent/venv/bin/python3 -m pytest /mnt/e/CS/github/hermes-s2s/tests/test_slash_command.py -v
```

**Step 5: Commit**

```bash
git add hermes_s2s/tools.py tests/test_slash_command.py
git commit -m "feat(cli): /s2s subcommand router (mode/provider/test/doctor/reset/configure)"
```

---

## Wave 5 — End-to-end smoke + docs

### Task 5.1: Manual smoke test script

**Objective:** A repeatable manual script that an operator (or you) can run after `hermes gateway restart` to verify Discord + Telegram + CLI work.

**Files:**
- Create: `docs/HOWTO-S2S-CONFIGURE.md`

**Content sections:**
- Discord: open a channel where the bot is, type `/s2s configure`, verify ephemeral panel renders with the four selects + three buttons. Pick `Realtime` from mode select, pick `gpt-realtime-2` from realtime provider select, click Test → confirm green ✅. Click Reset → confirm overrides cleared.
- Telegram: in a private chat with the bot, type `/s2s` → verify status reply with inline keyboard. Tap a mode button → confirm message edits to ack. Tap Test → confirm reply with timing.
- CLI: `hermes` → `/s2s status` → text output. `/s2s mode cascaded` → confirm. `/s2s help` → menu.
- Cross-check: run `~/.hermes/hermes-agent/venv/bin/python3 -c "import json; print(json.load(open('/home/codeseys/.hermes/.s2s_mode_overrides.json')))"` → confirm dict-shaped records on disk.

### Task 5.2: Update README + bump version + CHANGELOG entry

**Files:**
- Modify: `README.md` (section "Configuration UI")
- Modify: `pyproject.toml` (`version = "0.5.0"`)
- Modify: `hermes_s2s/__init__.py` (`__version__ = "0.5.0"`)
- Modify: `plugin.yaml` (`version: "0.5.0"`)
- Create: `CHANGELOG.md` if missing, else append `## 0.5.0` section

**Step 1: README block**

```markdown
## /s2s configuration UI

The plugin now exposes a multi-platform configuration surface:

**Discord** — `/s2s configure` opens an ephemeral panel with select menus
for mode + each provider kind, plus Test / Reset / Refresh buttons.
Direct subcommands (`/s2s mode <choice>`, `/s2s provider <kind> <name>`,
`/s2s status`, `/s2s test`, `/s2s doctor`, `/s2s reset`) are also available.

**Telegram** — `/s2s` posts the current status with an inline keyboard;
tap any button to switch mode or provider. Selections persist
per-(chat).

**CLI** — `/s2s` inside `hermes` works as a text router; subcommands match
Discord. Provider overrides via CLI are session-scoped and don't yet
flow into per-channel config.

Per-channel overrides are persisted to `~/.hermes/.s2s_mode_overrides.json`
as `{"<guild_id>:<channel_id>": {"mode": "...", "realtime_provider": "...", ...}}`.
Legacy 0.4.x bare-string entries are auto-lifted on first read; no manual
migration needed.

After upgrading: `hermes gateway restart` so the new Discord slash group
re-syncs.
```

**Step 2: Verify CHANGELOG**

```markdown
## 0.5.0 — /s2s rich UI

### Added
- `/s2s configure` Discord slash command opens an interactive panel with
  select menus (mode / realtime / STT / TTS providers) and action
  buttons (Test, Reset, Refresh).
- `/s2s` on Telegram now responds with status + inline-keyboard.
- `/s2s` subcommand router for CLI (mode, provider, test, doctor,
  reset, configure, help, status).
- Per-channel provider overrides — `realtime_provider`, `stt_provider`,
  `tts_provider` join `mode` in the override store and flow through
  factory.py into voice session construction.
- `S2SModeOverrideStore.set_record` / `get_record` / `patch_record`.
- Pure-text formatters in `voice/slash_format.py` (`format_status`,
  `format_help`, `format_doctor_summary`).

### Changed
- Override store on-disk shape from `{"<key>": "<mode>"}` to
  `{"<key>": {"mode": "...", ...}}`. Legacy entries auto-lift on read.
- `/s2s` on Discord is now an `app_commands.Group` with subcommands
  rather than a single command with a `mode` choice arg.

### Migration
- `hermes gateway restart` after upgrade so Discord re-syncs the slash
  group. The bare-string fallback means no config edits are required.
```

**Step 3: Bump version + commit**

```bash
git add README.md CHANGELOG.md pyproject.toml hermes_s2s/__init__.py plugin.yaml docs/HOWTO-S2S-CONFIGURE.md
git commit -m "docs: 0.5.0 release notes + HOWTO-S2S-CONFIGURE"
```

### Task 5.3: Final test sweep + open PR

**Step 1: Full test suite**

```bash
cd /mnt/e/CS/github/hermes-s2s
~/.hermes/hermes-agent/venv/bin/python3 -m pytest tests/ -v -x --tb=short
```

Expected: all green. Watch for `pytest.importorskip` skips on machines without discord.py / python-telegram-bot — those are expected.

**Step 2: Live install probe** (smoke that the plugin still loads cleanly):

```bash
~/.hermes/hermes-agent/venv/bin/python3 -c "
import hermes_s2s
class Ctx:
    def __init__(self):
        self.tools = []; self.commands = []; self.cli = []; self.skills = []; self.hooks = []
    def register_tool(self, **kw): self.tools.append(kw['name'])
    def register_command(self, name, **kw): self.commands.append(name)
    def register_cli_command(self, **kw): self.cli.append(kw['name'])
    def register_skill(self, n, p): self.skills.append(n)
    def register_hook(self, n, cb): self.hooks.append(n)
ctx = Ctx()
hermes_s2s.register(ctx)
print('tools:', ctx.tools)
print('commands:', ctx.commands)
print('cli:', ctx.cli)
print('skills:', ctx.skills)
"
```

Expected: `tools` includes the 4 s2s_* tools; `commands` includes `s2s`; `cli` includes `s2s`.

**Step 3: Push + open PR**

```bash
git push -u origin feat/s2s-configure-v2
gh pr create --title "feat: /s2s rich configuration UI (Discord + Telegram + CLI)" \
  --body "$(cat docs/plans/wave-0.5.0-s2s-configure.md | head -60)"
```

**Step 4: After PR merge — restart instructions for users**

Document at the top of the release notes / README:

> Discord users must run `hermes gateway restart` after upgrading to 0.5.0 — the Discord command tree only re-syncs on bot startup, so the new `/s2s` Group won't appear until the bot reconnects.

---

## Risks & Pitfalls

1. **Discord tree.sync()** — slash.py already warns when re-installation lands after `tree.sync()`. The Group refactor *changes the command shape*, so a sync will run once on next bot restart. Document this in the migration note.

2. **Telegram adapter attribute name** — `gateway/platforms/telegram.py` may expose the `Application` as `_application`, `application`, or `_app`. The plan's installer probes all three. Verify against the live source before merging.

3. **CLI `/s2s doctor` and event loops** — `s2s_doctor` is async. CLI slash handlers are sync. Wrapping with `asyncio.run` will fail if the CLI itself is already inside a loop. Plan punts to `hermes s2s doctor` (the CLI subcommand, which has its own async runner) — confirm that path works during Wave 4 testing.

4. **Per-channel provider overrides + global config drift** — if a user sets `realtime_provider: gpt-realtime-mini` for a channel, then later edits `config.yaml` to remove that backend, the channel's override now references a missing provider. The factory must catch this and fall back gracefully (log + use global default). Add a test for this.

5. **Override file format** — atomic-write via flock is already in place. The dict-shape change means readers running 0.4.x will see something they don't expect — they read `cache[key]` as a string. Old plugin instances will crash on the new format. Document: "do not run 0.4.x and 0.5.0 instances pointed at the same `~/.hermes/`."

6. **Discord cap of 25 options per Select** — slash.py truncates with `[:25]`. If users register more than 25 STT/TTS providers via the public `register_*` API, only the first 25 appear. Add a log warning when truncation happens.

7. **View timeout = 5 min, interaction token = 15 min** — `_test_btn` defers, that's fine. `_refresh_btn` calls `edit_message`, which uses the original interaction token from the `/s2s configure` command — that token expires after 15 min regardless of View timeout. Document this; the user just re-runs `/s2s configure`.

8. **Cross-process consistency** — the override store has flock; the in-memory cache reload-on-demand pattern is already proven in slash.py. New `patch_record` correctly does load+merge+write under the lock; confirm in tests with two `S2SModeOverrideStore` instances against the same file.

9. **Tests that import discord.py** — gate on `pytest.importorskip("discord")`. The plugin's CI should have discord.py installed; local dev without it will skip those tests (acceptable).

---

## Rollout

1. Merge to `main` once tests + manual smoke pass.
2. Tag `v0.5.0`, push tag.
3. Bump the install command in the README to point at `@v0.5.0`.
4. Tell users in the release announcement to:
   - `~/.hermes/hermes-agent/venv/bin/python3 -m pip install -e ~/.hermes/plugins/hermes-s2s'[all]'`
   - `hermes gateway restart`
   - `/s2s configure` in Discord to verify.
