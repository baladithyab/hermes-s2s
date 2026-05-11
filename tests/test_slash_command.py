"""Tests for hermes_s2s.voice.slash — /s2s slash + S2SModeOverrideStore.

Covers the M2.1 acceptance matrix:
- A1: the full file passes
- A3: cross-process persistence (fresh subprocess reads what parent wrote)
- A4: concurrent writes under flock don't corrupt the JSON
- plus idempotency + tree-already-synced warning + factory-uses-store.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from hermes_s2s.voice.slash import (
    S2SModeOverrideStore,
    _S2S_COMMAND_INSTALLED,
    get_default_store,
    install_s2s_command,
)


# --------------------------------------------------------------------------- #
# Store — basic round-trip                                                    #
# --------------------------------------------------------------------------- #


def test_store_get_set(tmp_path: Path) -> None:
    store_path = tmp_path / ".s2s_mode_overrides.json"
    store = S2SModeOverrideStore(path=store_path)

    assert store.get(111, 222) is None
    store.set(111, 222, "realtime")
    assert store.get(111, 222) == "realtime"

    # The file must exist on disk with the merged payload.
    # v0.5.0 (Wave 1): on-disk values are dict-shaped, not bare strings.
    assert store_path.exists()
    data = json.loads(store_path.read_text())
    assert data["111:222"] == {"mode": "realtime"}

    # clear() removes the entry.
    store.clear(111, 222)
    assert store.get(111, 222) is None


def test_store_normalizes_mode(tmp_path: Path) -> None:
    """Aliases like 's2s_server' must land on disk as canonical 's2s-server'."""
    store = S2SModeOverrideStore(path=tmp_path / "overrides.json")
    store.set(1, 2, "s2s_server")
    assert store.get(1, 2) == "s2s-server"

    store.set(1, 2, "  REALTIME ")
    assert store.get(1, 2) == "realtime"


# --------------------------------------------------------------------------- #
# A3 — cross-process persistence                                              #
# --------------------------------------------------------------------------- #


def test_persistence_survives_fresh_process(tmp_path: Path) -> None:
    """Phase-8 P0 acceptance: spawn a fresh Python process that re-imports
    the store and reads what the parent wrote. NOT mocked — real subprocess,
    real filesystem, real import.
    """
    # Use HERMES_HOME so the default-path resolver in the child process
    # lands on this tmp directory.
    hermes_home = tmp_path / "home"
    hermes_home.mkdir()

    # Parent-side write via the default-path constructor (which honors
    # HERMES_HOME through hermes_constants.get_hermes_home()).
    parent_env = dict(os.environ)
    parent_env["HERMES_HOME"] = str(hermes_home)

    # Do the parent write through a subprocess too so the parent process's
    # cached modules/singletons don't interfere with module-level state
    # elsewhere in the test suite.
    parent_script = (
        "import os;"
        "from hermes_s2s.voice.slash import S2SModeOverrideStore;"
        "s = S2SModeOverrideStore();"
        "s.set(123, 456, 'realtime');"
        "print('wrote:', s.get(123, 456));"
    )
    proj_root = str(Path(__file__).resolve().parents[1])
    parent_result = subprocess.run(
        [sys.executable, "-c", parent_script],
        env=parent_env,
        capture_output=True,
        text=True,
        cwd=proj_root,
        timeout=15,
    )
    assert parent_result.returncode == 0, (
        f"parent write failed: stdout={parent_result.stdout} "
        f"stderr={parent_result.stderr}"
    )
    assert "wrote: realtime" in parent_result.stdout

    # File must exist where hermes_constants.get_hermes_home() would
    # have put it — honoring HERMES_HOME.
    override_file = hermes_home / ".s2s_mode_overrides.json"
    assert override_file.exists(), (
        f"override file not created at {override_file}; "
        f"check that HERMES_HOME is honored by get_hermes_home()"
    )

    # Now spawn a COMPLETELY fresh child process and confirm it reads
    # the value back.
    child_script = (
        "import os;"
        "from hermes_s2s.voice.slash import S2SModeOverrideStore;"
        "s = S2SModeOverrideStore();"
        "print('read:', s.get(123, 456));"
    )
    child_result = subprocess.run(
        [sys.executable, "-c", child_script],
        env=parent_env,
        capture_output=True,
        text=True,
        cwd=proj_root,
        timeout=15,
    )
    assert child_result.returncode == 0, (
        f"child read failed: stdout={child_result.stdout} "
        f"stderr={child_result.stderr}"
    )
    assert "read: realtime" in child_result.stdout


# --------------------------------------------------------------------------- #
# A4 — concurrent writes don't corrupt the file                               #
# --------------------------------------------------------------------------- #


def test_concurrent_writes_dont_corrupt(tmp_path: Path) -> None:
    """10 threads each write a different (channel, mode) pair; all 10 entries
    must be present in the final JSON AND the file must still parse cleanly.

    Uses threads in one process (GIL-serialized but still exercises the
    in-process lock + flock path). The flock is the thing that matters
    for multi-process safety — we exercise the code path here, and the
    cross-process variant is covered by test_persistence_survives_fresh_process
    (sequential) since spinning up 10 subprocesses would be flaky in CI.
    """
    store_path = tmp_path / "overrides.json"
    store = S2SModeOverrideStore(path=store_path)

    # Distinct (channel, mode) per thread so we can verify every write
    # landed. Cycle through all 4 modes.
    modes = ["cascaded", "pipeline", "realtime", "s2s-server"]
    writes = [(1000 + i, modes[i % len(modes)]) for i in range(10)]
    errors: list[BaseException] = []

    def writer(channel_id: int, mode: str) -> None:
        try:
            store.set(42, channel_id, mode)
        except BaseException as exc:  # pragma: no cover - defensive
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=(cid, mode), daemon=True)
        for cid, mode in writes
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive(), "writer thread hung"

    assert not errors, f"threads raised: {errors}"

    # File must be valid JSON.
    raw = store_path.read_text()
    data = json.loads(raw)
    assert isinstance(data, dict)

    # All 10 writes present.
    # v0.5.0 (Wave 1): each on-disk value is a dict, not a bare string.
    for cid, mode in writes:
        key = f"42:{cid}"
        assert key in data, f"missing {key} in {list(data)}"
        assert data[key] == {"mode": mode}, (
            f"expected {{'mode': {mode!r}}} at {key}, got {data[key]}"
        )

    # Fresh store reads the same thing back.
    fresh = S2SModeOverrideStore(path=store_path)
    for cid, mode in writes:
        assert fresh.get(42, cid) == mode


# --------------------------------------------------------------------------- #
# Slash-command installer — idempotency + tree-synced warning                 #
# --------------------------------------------------------------------------- #


class _FakeTree:
    """Minimal stand-in for discord.app_commands.CommandTree."""

    def __init__(self, already_synced: bool = False) -> None:
        self.commands: list[Any] = []
        self._synced = already_synced

    def add_command(self, cmd: Any) -> None:
        self.commands.append(cmd)

    async def sync(self) -> list[Any]:  # pragma: no cover - not called
        return list(self.commands)


def _ctx_with_tree(tree: _FakeTree) -> Any:
    ctx = MagicMock(spec=[])
    ctx.tree = tree
    return ctx


@pytest.fixture(autouse=True)
def _reset_default_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate the process-wide singleton from other tests + point it at a tmp
    file so install_s2s_command doesn't touch the user's real
    ~/.hermes/.s2s_mode_overrides.json."""
    from hermes_s2s.voice import slash as slash_mod

    # Force-rebuild the singleton to a tmp-path-backed instance.
    monkeypatch.setattr(slash_mod, "_store_singleton", None, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield
    monkeypatch.setattr(slash_mod, "_store_singleton", None, raising=False)


def test_install_s2s_command_idempotent() -> None:
    """discord.py must be importable for this to be meaningful. If it's not,
    install_s2s_command() returns False and we skip cleanly."""
    pytest.importorskip("discord")

    tree = _FakeTree()
    ctx = _ctx_with_tree(tree)

    first = install_s2s_command(ctx)
    second = install_s2s_command(ctx)

    assert first is True
    assert second is False  # already installed → no-op
    assert len(tree.commands) == 1
    assert getattr(tree, _S2S_COMMAND_INSTALLED) is True


def test_install_s2s_command_logs_when_tree_already_synced(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pytest.importorskip("discord")

    tree = _FakeTree(already_synced=True)
    ctx = _ctx_with_tree(tree)

    with caplog.at_level(logging.WARNING, logger="hermes_s2s.voice.slash"):
        installed = install_s2s_command(ctx)

    assert installed is True
    joined = " ".join(rec.message for rec in caplog.records)
    assert "AFTER tree.sync" in joined or "will only appear" in joined


# --------------------------------------------------------------------------- #
# Factory uses the override store                                             #
# --------------------------------------------------------------------------- #


def test_factory_uses_override_store(tmp_path: Path) -> None:
    """ModeRouter precedence level 3 (channel_overrides) must be populated
    from the override store when resolving a mode for a (guild, channel)
    that has an entry in the store. The config default is 'cascaded'; the
    store says 'realtime'; the store wins.
    """
    from hermes_s2s.voice.modes import ModeRouter, VoiceMode

    store = S2SModeOverrideStore(path=tmp_path / "overrides.json")
    store.set(777, 999, "realtime")

    # Simulate what the bridge does in v0.4.0: fold the store's entry
    # for this (guild, channel) into router_cfg.s2s.voice.channel_overrides.
    override = store.get(777, 999)
    assert override == "realtime"

    router_cfg = {
        "s2s": {
            "voice": {
                "default_mode": "cascaded",
                "channel_overrides": {999: override},
            }
        }
    }
    router = ModeRouter(router_cfg)
    spec = router.resolve(guild_id=777, channel_id=999)
    assert spec.mode is VoiceMode.REALTIME

    # Absent any store entry for a different channel, the config default
    # wins (sanity check).
    spec2 = router.resolve(guild_id=777, channel_id=555)
    assert spec2.mode is VoiceMode.CASCADED


def test_bridge_folds_store_into_router_channel_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration: ``_attach_realtime_to_voice_client`` must consult the
    override store and fold the entry into ``router_cfg`` before building
    the ModeRouter. We run the bridge against mocks, assert the resolver
    saw the override value, and short-circuit before any real session
    construction by intercepting the ModeRouter.
    """
    from hermes_s2s._internal import discord_bridge
    from hermes_s2s.voice import slash as slash_mod
    from hermes_s2s.voice.modes import VoiceMode

    # Point the singleton store at a tmp path and pre-seed an override
    # for (guild=1001, channel=2002) → "realtime".
    seeded = S2SModeOverrideStore(path=tmp_path / "overrides.json")
    seeded.set(1001, 2002, "realtime")
    monkeypatch.setattr(slash_mod, "_store_singleton", seeded, raising=False)

    # Fake cfg object: mode defaults to cascaded.
    # v0.5.0 Wave 1: ``_attach_realtime_to_voice_client`` now routes through
    # ``resolve_s2s_config_for_channel`` which calls ``cfg.with_*`` helpers
    # when the per-channel record has overrides. The fake must expose those
    # so the bridge keeps progressing to the ModeRouter capture below.
    class FakeCfg:
        mode = "cascaded"
        voice = None
        realtime_provider = None

        def with_mode(self, mode: str) -> "FakeCfg":  # noqa: D401
            new = FakeCfg()
            new.mode = mode
            return new

        def with_realtime_provider(self, p: str) -> "FakeCfg":
            new = FakeCfg()
            new.mode = self.mode
            new.realtime_provider = p
            return new

        def with_stt_provider(self, p: str) -> "FakeCfg":  # pragma: no cover
            return self

        def with_tts_provider(self, p: str) -> "FakeCfg":  # pragma: no cover
            return self

    monkeypatch.setattr(
        discord_bridge, "load_config", lambda: FakeCfg(), raising=False
    )
    # Above only works if load_config is imported lazily in bridge, which
    # it is — but we need to also stub the ..config module path.
    fake_cfg_mod = MagicMock()
    fake_cfg_mod.load_config = lambda: FakeCfg()
    monkeypatch.setitem(
        __import__("sys").modules, "hermes_s2s.config", fake_cfg_mod
    )

    # Capture the router_cfg that ModeRouter sees.
    captured: dict[str, Any] = {}

    class _CaptureRouter:
        def __init__(self, cfg: Any) -> None:
            captured["cfg"] = cfg

        def resolve(self, *, guild_id: Any, channel_id: Any) -> Any:
            # Return a ModeSpec-like that's shaped enough for downstream
            # code to short-circuit cleanly. We raise after capturing so
            # the rest of the bridge path short-circuits on the exception
            # handler and we can assert without mocking the whole factory.
            captured["guild_id"] = guild_id
            captured["channel_id"] = channel_id
            raise RuntimeError("stop here, we captured what we need")

    monkeypatch.setattr(discord_bridge, "ModeRouter", _CaptureRouter)

    # Fake voice_client with guild.id / channel.id = 1001 / 2002.
    vc = MagicMock()
    vc.guild.id = 1001
    vc.channel.id = 2002

    adapter = MagicMock()

    # Call the bridge; it'll call our _CaptureRouter.resolve which raises
    # RuntimeError; the bridge logs and returns — we just assert the
    # captured cfg shows our override wired in.
    discord_bridge._attach_realtime_to_voice_client(adapter, vc, None, None)

    cfg = captured.get("cfg")
    assert cfg is not None, "ModeRouter was never instantiated"
    voice_cfg = cfg["s2s"]["voice"]
    channel_overrides = voice_cfg.get("channel_overrides", {})
    # Either int or str key — router accepts both. We stored with int.
    assert channel_overrides.get(2002) == "realtime" or channel_overrides.get(
        "2002"
    ) == "realtime", (
        f"expected channel_overrides[2002]='realtime', got {channel_overrides!r}"
    )


# --------------------------------------------------------------------------- #
# Wave 1 / 0.5.0 — dict-shaped record API                                     #
# --------------------------------------------------------------------------- #


def test_get_record_returns_dict_for_new_entries(tmp_path: Path) -> None:
    """0.5.0: set_record / get_record round-trip a dict-shaped record."""
    store = S2SModeOverrideStore(path=tmp_path / "ovr.json")
    store.set_record(123, 456, {"mode": "realtime", "realtime_provider": "gpt-realtime-2"})
    rec = store.get_record(123, 456)
    assert rec == {"mode": "realtime", "realtime_provider": "gpt-realtime-2"}


def test_get_record_lifts_legacy_string(tmp_path: Path) -> None:
    """Pre-0.5.0 entries on disk are bare strings; new readers must lift them
    losslessly into ``{"mode": <str>}``.
    """
    p = tmp_path / "ovr.json"
    p.write_text(json.dumps({"123:456": "cascaded"}), encoding="utf-8")
    store = S2SModeOverrideStore(path=p)
    rec = store.get_record(123, 456)
    assert rec == {"mode": "cascaded"}


def test_legacy_get_method_still_works_after_dict_upgrade(tmp_path: Path) -> None:
    """Existing factory.py call sites using ``.get()`` must continue to return
    the mode string unchanged after the schema migration.
    """
    store = S2SModeOverrideStore(path=tmp_path / "ovr.json")
    store.set_record(123, 456, {"mode": "s2s-server", "stt_provider": "groq"})
    assert store.get(123, 456) == "s2s-server"


# --------------------------------------------------------------------------- #
# Wave 2 / Task 2.1 — pure-text formatters (slash_format)                     #
# --------------------------------------------------------------------------- #


def test_status_formatter_renders_active_mode_and_providers() -> None:
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


def test_status_formatter_no_override_label() -> None:
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


def test_help_formatter_lists_all_subcommands() -> None:
    from hermes_s2s.voice.slash_format import format_help

    out = format_help()
    for sub in ("configure", "status", "mode", "provider", "test", "doctor", "reset"):
        assert f"/s2s {sub}" in out, f"missing /s2s {sub} in help text"


def test_doctor_summary_shows_counts_and_top_failures() -> None:
    from hermes_s2s.voice.slash_format import format_doctor_summary

    report = {
        "overall_status": "fail",
        "checks": [
            {"category": "configuration", "name": "mode", "status": "pass",
             "message": "mode=cascaded", "remediation": None},
            {"category": "python_deps", "name": "moonshine_onnx", "status": "pass",
             "message": "ok", "remediation": None},
            {"category": "api_keys", "name": "OPENAI_API_KEY", "status": "fail",
             "message": "env var not set",
             "remediation": "export OPENAI_API_KEY=sk-…"},
            {"category": "system_deps", "name": "ffmpeg", "status": "warn",
             "message": "old version",
             "remediation": "apt install ffmpeg"},
            {"category": "backend_connectivity", "name": "realtime_probe",
             "status": "fail",
             "message": "timed out",
             "remediation": "check network"},
            {"category": "api_keys", "name": "GEMINI_API_KEY", "status": "fail",
             "message": "env var not set",
             "remediation": "export GEMINI_API_KEY=..."},
        ],
    }
    summary = format_doctor_summary(report)
    # pass/warn/fail counts surfaced
    assert "2" in summary  # 2 pass
    assert "1" in summary  # 1 warn
    assert "3" in summary  # 3 fail
    # Overall status surfaced
    assert "fail" in summary.lower()
    # Top failures surfaced (first 3)
    assert "OPENAI_API_KEY" in summary
    assert "realtime_probe" in summary
    assert "GEMINI_API_KEY" in summary
    # Remediation text included for at least one failure
    assert "export" in summary.lower() or "check network" in summary.lower()


def test_doctor_summary_all_pass() -> None:
    from hermes_s2s.voice.slash_format import format_doctor_summary

    report = {
        "overall_status": "pass",
        "checks": [
            {"category": "configuration", "name": "mode", "status": "pass",
             "message": "ok", "remediation": None},
            {"category": "python_deps", "name": "websockets", "status": "pass",
             "message": "ok", "remediation": None},
        ],
    }
    summary = format_doctor_summary(report)
    assert "pass" in summary.lower()
    # No failure block when nothing failed
    assert "no failures" in summary.lower() or "all checks passed" in summary.lower()


# --------------------------------------------------------------------------- #
# Wave 2 / Task 2.2 — /s2s is now an app_commands.Group with subcommands      #
# --------------------------------------------------------------------------- #


def test_install_creates_group_with_subcommands() -> None:
    """The Discord tree should receive a Group named 's2s' with the expected
    leaf subcommands: mode, status, provider, test, doctor, reset.

    Uses a real ``discord.Client`` + ``app_commands.CommandTree`` so we
    verify against the real ``tree.get_command`` / ``Group.commands``
    public API, not a bespoke mock.
    """
    pytest.importorskip("discord")
    import discord
    from discord import app_commands

    intents = discord.Intents.none()
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    ctx = MagicMock(spec=[])
    ctx.tree = tree

    installed = install_s2s_command(ctx)
    assert installed is True

    cmd = tree.get_command("s2s")
    assert cmd is not None, "tree has no top-level 's2s' command after install"
    # It must be a Group, not a leaf Command — duck-type via ``.commands``.
    assert hasattr(cmd, "commands"), (
        "top-level 's2s' is not a Group; expected app_commands.Group with subcommands"
    )
    sub_names = {c.name for c in cmd.commands}
    assert {"mode", "status", "provider", "test", "doctor", "reset"} <= sub_names, (
        f"missing subcommands; got {sub_names}"
    )


def test_install_creates_group_is_idempotent() -> None:
    """Repeated install on the same tree still no-ops after the refactor."""
    pytest.importorskip("discord")
    import discord
    from discord import app_commands

    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    ctx = MagicMock(spec=[])
    ctx.tree = tree

    first = install_s2s_command(ctx)
    second = install_s2s_command(ctx)
    assert first is True
    assert second is False
    assert tree.get_command("s2s") is not None


def test_install_s2s_command_on_adapter_finds_live_tree() -> None:
    """The adapter-path installer should find ``adapter._client.tree``.

    Verifies the deferred-install seam used by the pre_gateway_dispatch
    hook + the join_voice_channel monkey-patch — both pass a live
    DiscordAdapter, NOT a register-time ctx that has no tree yet.
    """
    pytest.importorskip("discord")
    import discord
    from discord import app_commands
    from hermes_s2s.voice.slash import (
        install_s2s_command_on_adapter,
        _S2S_COMMAND_INSTALLED,
    )

    intents = discord.Intents.none()
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)
    client.tree = tree  # type: ignore[attr-defined]

    # Synthetic adapter shape mirroring DiscordAdapter._client
    adapter = type("FakeAdapter", (), {"_client": client})()

    installed = install_s2s_command_on_adapter(adapter)
    assert installed is True
    assert tree.get_command("s2s") is not None
    assert getattr(tree, _S2S_COMMAND_INSTALLED, False) is True

    # Idempotent — second call no-ops because the sentinel is set
    second = install_s2s_command_on_adapter(adapter)
    assert second is False


def test_install_s2s_command_on_adapter_no_client_returns_false() -> None:
    """Adapter without _client / client should be a clean no-op."""
    pytest.importorskip("discord")
    from hermes_s2s.voice.slash import install_s2s_command_on_adapter

    adapter_no_client = type("FA", (), {})()
    assert install_s2s_command_on_adapter(adapter_no_client) is False

    adapter_no_tree = type(
        "FA",
        (),
        {"_client": type("C", (), {})()},
    )()
    assert install_s2s_command_on_adapter(adapter_no_tree) is False


# --------------------------------------------------------------------------- #
# Codex PR #1 P1 — patch_record must be atomic (read-modify-write under lock) #
# --------------------------------------------------------------------------- #


def test_patch_record_concurrent_different_fields_dont_lose_writes(tmp_path) -> None:
    """Two threads patching different fields on the same channel must
    both land — ``patch_record`` releases the lock between read and
    write in the broken implementation, so the slower writer's snapshot
    of the record predates the fast writer's write and clobbers it.

    This test pins the contract by having two threads patch
    ``mode`` and ``realtime_provider`` in tight loops; the final record
    must contain BOTH keys regardless of interleaving.
    """
    import threading

    store = S2SModeOverrideStore(path=tmp_path / "ovr.json")
    g, c = 1, 2
    iterations = 50

    def _set_mode():
        for _ in range(iterations):
            store.patch_record(g, c, mode="realtime")

    def _set_provider():
        for _ in range(iterations):
            store.patch_record(g, c, realtime_provider="gpt-realtime-mini")

    t1 = threading.Thread(target=_set_mode)
    t2 = threading.Thread(target=_set_provider)
    t1.start(); t2.start()
    t1.join(); t2.join()

    final = store.get_record(g, c)
    assert final.get("mode") == "realtime", f"mode lost: {final!r}"
    assert final.get("realtime_provider") == "gpt-realtime-mini", \
        f"realtime_provider lost: {final!r}"


# --------------------------------------------------------------------------- #
# Codex PR #1 P2 — formatter must show effective per-channel values           #
# --------------------------------------------------------------------------- #


def test_format_status_shows_per_channel_provider_override() -> None:
    """When the override record sets ``realtime_provider``, the status
    line for "Realtime provider" must show the override, NOT the
    global config's value."""
    from hermes_s2s.voice.slash_format import format_status

    out = format_status(
        active_mode="realtime",
        config_mode="cascaded",
        realtime_provider="gpt-realtime-2",  # global
        stt_provider="moonshine",
        tts_provider="kokoro",
        guild_id=1,
        channel_id=2,
        per_channel_record={"realtime_provider": "gpt-realtime-mini"},  # override
    )
    assert "gpt-realtime-mini" in out, (
        f"effective realtime override not shown:\n{out}"
    )
    # The global value MUST NOT appear in the realtime line
    assert "Realtime provider: `gpt-realtime-2`" not in out, (
        f"status formatter still rendering global value over override:\n{out}"
    )
    # Must mark the line so users can tell it's overridden
    assert "(channel override)" in out


def test_format_status_uses_global_when_no_override() -> None:
    """No override → globals shown unmarked."""
    from hermes_s2s.voice.slash_format import format_status

    out = format_status(
        active_mode="cascaded",
        config_mode="cascaded",
        realtime_provider="gpt-realtime-2",
        stt_provider="moonshine",
        tts_provider="kokoro",
        guild_id=1,
        channel_id=2,
        per_channel_record={},
    )
    assert "gpt-realtime-2" in out
    assert "(channel override)" not in out


# --------------------------------------------------------------------------- #
# Wave 4 / Task 4.1 — CLI /s2s subcommand router                              #
# --------------------------------------------------------------------------- #


def test_handle_s2s_command_routes_subcommands(tmp_path, monkeypatch):
    from hermes_s2s.tools import handle_s2s_command

    # No-arg → status
    out = handle_s2s_command("")
    assert "active_mode" in out or "Active mode" in out  # JSON or formatted

    # mode set
    out = handle_s2s_command("mode realtime")
    assert "realtime" in out

    # provider set (in CLI we don't have guild_id/channel_id — should still work
    # via a session-scoped path or a clear guidance message)
    out = handle_s2s_command("provider realtime gpt-realtime-2")
    # Not great UX without channel ctx; should accept and warn
    assert (
        "gpt-realtime-2" in out
        or "warn" in out.lower()
        or "global" in out.lower()
        or "config.yaml" in out.lower()
    )

    # help
    out = handle_s2s_command("help")
    assert "configure" in out and "mode" in out and "provider" in out


def test_handle_s2s_command_unknown_subcommand_shows_help():
    from hermes_s2s.tools import handle_s2s_command

    out = handle_s2s_command("frobnicate")
    assert "Usage" in out or "configure" in out
