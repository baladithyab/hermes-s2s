"""Plugin-owned Discord ``/s2s`` slash command + per-channel override store.

Implements WAVE 2a / M2.1 of the 0.4.0 re-architecture (Discord-only; Telegram
and CLI scopes deferred to 0.4.1). See:

- docs/adrs/0011-plugin-owned-s2s-command.md (full ADR)
- docs/research/13-mode-ux-deep-dive.md §4 (the exact command pattern)
- docs/plans/wave-0.4.0-rearchitecture.md WAVE 2a (A3 cross-process + A4 flock
  acceptance criteria)
- docs/adrs/0015-s2s-configure-rich-ui.md + docs/plans/wave-0.5.0-s2s-configure.md
  (Wave 1 — schema migration from bare-string mode to dict-shaped record).

The store persists to ``<HERMES_HOME>/.s2s_mode_overrides.json``. As of v0.5.0
(Wave 1) each entry is a JSON object — ``{"mode": "...", "realtime_provider":
"...", "stt_provider": "...", "tts_provider": "..."}`` — so per-channel overrides
can carry provider keys alongside the mode. Pre-0.5.0 bare-string values are
lifted on read into ``{"mode": <str>}`` losslessly; the legacy ``set``/``get``
methods are kept as thin shims so existing factory call sites keep working.

Writes go through a temp-file+atomic-rename path with an
``fcntl.flock(LOCK_EX)`` wrapper so concurrent writes from multiple processes
(or many threads in one process) can't corrupt the file — Phase-8 security
P1-F8.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from .modes import VoiceMode
from .slash_format import format_doctor_summary, format_status


logger = logging.getLogger(__name__)


# Sentinel attribute set on the Discord adapter (or its bot) after the
# slash command has been installed. Keeps install_s2s_command idempotent
# across repeated register() calls — which is expected in the Hermes
# plugin reload path.
_S2S_COMMAND_INSTALLED = "__hermes_s2s_command_installed__"

_OVERRIDES_FILENAME = ".s2s_mode_overrides.json"


# ---------------------------------------------------------------------------
# S2SModeOverrideStore
# ---------------------------------------------------------------------------


def _default_store_path() -> Path:
    """Resolve ``<HERMES_HOME>/.s2s_mode_overrides.json``.

    Falls back to ``~/.hermes/.s2s_mode_overrides.json`` when
    ``hermes_constants`` is unavailable (e.g. standalone plugin installs
    during local dev). Honors ``HERMES_HOME`` via ``hermes_constants``
    which reads the env var.
    """
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home()) / _OVERRIDES_FILENAME
    except Exception:  # pragma: no cover - defensive
        home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        return Path(home) / _OVERRIDES_FILENAME


def _coerce_value(v: Any) -> Dict[str, str]:
    """Lift any on-disk entry into the canonical dict-shape.

    - Pre-0.5.0 entries on disk are bare strings — lift to ``{"mode": <str>}``.
    - 0.5.0+ entries are dicts — coerce keys/values to strings, drop empties.
    - Anything else (None, lists, numbers) collapses to the empty dict so a
      corrupted file doesn't crash the loader.
    """
    if isinstance(v, str):
        return {"mode": v}
    if isinstance(v, dict):
        return {str(k): str(val) for k, val in v.items() if val is not None and str(val) != ""}
    return {}


class S2SModeOverrideStore:
    """Persistent per-(guild_id, channel_id) S2S override store.

    The store is deliberately simple: a JSON file on disk, lazy-loaded on
    first access, flushed atomically after every ``set``/``set_record``/
    ``patch_record``/``clear`` with an OS-level exclusive file lock. The
    in-memory cache is refreshed on demand via :meth:`reload` — callers
    that need cross-process consistency should call :meth:`reload` before
    :meth:`get` in hot paths (the voice-join flow does this once per join
    — cost is negligible).

    Schema (v0.5.0+)
    ----------------
    Each entry is a dict ``{"mode": "...", "realtime_provider": "...",
    "stt_provider": "...", "tts_provider": "..."}``. All keys are optional;
    missing keys mean "fall through to the global config". Pre-0.5.0
    bare-string entries are lifted on read into ``{"mode": <str>}``.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path is not None else _default_store_path()
        # In-process lock guards the cache dict + the file-writing section.
        # File-level locking (flock) is additive, for cross-process safety.
        self._lock = threading.Lock()
        # v0.5.0: cache value is now a dict-shaped record, not a bare string.
        self._cache: Dict[str, Dict[str, str]] = {}
        self._loaded = False

    # --- helpers --------------------------------------------------------

    @staticmethod
    def _key(guild_id: int, channel_id: int) -> str:
        return f"{int(guild_id)}:{int(channel_id)}"

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._load_locked()

    def _load_locked(self) -> None:
        """Populate ``self._cache`` from disk; tolerate missing/corrupt file.

        Each value is fed through :func:`_coerce_value` so legacy bare-string
        entries are lifted into the dict-shape on read.
        """
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                cache: Dict[str, Dict[str, str]] = {}
                for k, v in data.items():
                    rec = _coerce_value(v)
                    if rec:
                        cache[str(k)] = rec
                self._cache = cache
            else:
                logger.warning(
                    "hermes-s2s: override store at %s is not a dict; ignoring",
                    self._path,
                )
                self._cache = {}
        except FileNotFoundError:
            self._cache = {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "hermes-s2s: failed to load override store %s (%s); "
                "starting with empty map",
                self._path,
                exc,
            )
            self._cache = {}
        self._loaded = True

    def reload(self) -> None:
        """Force re-read from disk (useful across process boundaries)."""
        with self._lock:
            self._loaded = False
            self._load_locked()

    # --- public API: rich (v0.5.0+) ------------------------------------

    def get_record(self, guild_id: int, channel_id: int) -> Dict[str, str]:
        """Return the full per-channel record (or an empty dict if absent)."""
        with self._lock:
            self._ensure_loaded()
            return dict(self._cache.get(self._key(guild_id, channel_id), {}))

    def set_record(
        self, guild_id: int, channel_id: int, record: Dict[str, str]
    ) -> None:
        """Replace the entire record for ``(guild_id, channel_id)``.

        Empty/None record clears the entry. The ``mode`` key, if present,
        is normalized via :meth:`VoiceMode.normalize` so aliases land on
        disk as canonical values; an unparseable mode is dropped (other
        keys preserved) rather than failing the write.
        """
        cleaned = {
            str(k): str(v) for k, v in (record or {}).items() if v not in (None, "")
        }
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

    def patch_record(
        self, guild_id: int, channel_id: int, **fields: str
    ) -> Dict[str, str]:
        """Merge ``fields`` into the existing record; return the new record."""
        with self._lock:
            self._ensure_loaded()
            existing = dict(self._cache.get(self._key(guild_id, channel_id), {}))
        cleaned = {k: v for k, v in fields.items() if v not in (None, "")}
        if "mode" in cleaned:
            try:
                cleaned["mode"] = VoiceMode.normalize(cleaned["mode"]).value
            except ValueError:
                cleaned.pop("mode", None)
        existing.update({str(k): str(v) for k, v in cleaned.items()})
        self.set_record(guild_id, channel_id, existing)
        return existing

    # --- public API: back-compat shims (legacy mode-only callers) ------

    def get(self, guild_id: int, channel_id: int) -> str | None:
        """Return the stored mode string, or ``None`` if no override.

        Back-compat shim around :meth:`get_record` for pre-0.5.0 callers
        that only care about the mode field.
        """
        rec = self.get_record(guild_id, channel_id)
        return rec.get("mode") if rec else None

    def set(self, guild_id: int, channel_id: int, mode: str) -> None:
        """Persist ``mode`` for ``(guild_id, channel_id)``.

        Back-compat shim that patches only the ``mode`` field, preserving
        any provider keys an existing record may have. ``mode`` is
        normalized via :meth:`VoiceMode.normalize` so aliases like
        ``"s2s_server"`` land on disk as canonical ``"s2s-server"``.
        """
        self.patch_record(guild_id, channel_id, mode=mode)

    def clear(self, guild_id: int, channel_id: int) -> None:
        """Remove the entire per-channel record."""
        with self._lock:
            self._ensure_loaded()
            merged = dict(self._cache)
            merged.pop(self._key(guild_id, channel_id), None)
            self._write_atomic(merged)
            self._cache = merged

    # --- atomic, flock-protected write ---------------------------------

    def _write_atomic(self, payload: Dict[str, Dict[str, str]]) -> None:
        """Serialize ``payload`` → temp → fsync → os.replace, under flock.

        Strategy:

        1. ``mkdir -p`` the parent.
        2. Open (or create) a sibling ``.lock`` file and ``flock(LOCK_EX)``
           it — this blocks other processes doing the same.
        3. Re-load the file from disk INSIDE the lock and merge our
           payload on top, so we don't lose entries written by another
           process between our pre-flock read and the write.
        4. Write the merged dict to a NamedTemporaryFile in the same
           directory, ``flush()`` + ``fsync()``, then ``os.replace``
           over the real path (atomic on POSIX + modern NTFS).
        5. Release the flock.

        v0.5.0: payload values are dict-shaped. Legacy bare-string entries
        encountered on disk during the merge are lifted via
        :func:`_coerce_value` so a parallel writer running an older
        version doesn't get its data dropped — though once we write back,
        the file is in the new shape.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)

        lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        # Open with O_CREAT so the lock file always exists; keep the fd
        # open for the whole critical section.
        lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                # Merge with whatever is on disk right now (another process
                # may have written between our last cache load and this
                # write). We still keep our just-set entries on top.
                disk: Dict[str, Dict[str, str]] = {}
                try:
                    with self._path.open("r", encoding="utf-8") as fh:
                        loaded = json.load(fh)
                    if isinstance(loaded, dict):
                        for k, v in loaded.items():
                            rec = _coerce_value(v)
                            if rec:
                                disk[str(k)] = rec
                except (FileNotFoundError, OSError, json.JSONDecodeError):
                    disk = {}
                # Our in-memory payload wins per key (we want set/clear
                # to stick) but we preserve keys the other process added.
                final = dict(disk)
                final.update(payload)
                # For clear(): if a key is missing from ``payload`` AND
                # missing from our previous cache snapshot, respect the
                # other process's value (keep it). Since ``payload`` here
                # IS the post-op snapshot of OUR view, anything in
                # ``self._cache`` but not in ``payload`` was a clear().
                cleared_keys = set(self._cache.keys()) - set(payload.keys())
                for k in cleared_keys:
                    final.pop(k, None)

                tmp = tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=str(self._path.parent),
                    prefix=self._path.name + ".",
                    suffix=".tmp",
                    delete=False,
                )
                try:
                    json.dump(final, tmp, indent=2, sort_keys=True)
                    tmp.flush()
                    try:
                        os.fsync(tmp.fileno())
                    except OSError:  # pragma: no cover - e.g. tmpfs
                        pass
                    tmp.close()
                    os.replace(tmp.name, self._path)
                except Exception:
                    try:
                        os.unlink(tmp.name)
                    except OSError:
                        pass
                    raise
                # Mirror the merged result back into our cache so
                # subsequent get() calls see a consistent view.
                self._cache = final
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


# ---------------------------------------------------------------------------
# Discord /s2s slash command installer
# ---------------------------------------------------------------------------


# Process-wide singleton so the slash handler and the factory see the same
# in-memory cache. Constructed lazily to honor any late HERMES_HOME override.
_store_singleton: Optional[S2SModeOverrideStore] = None
_store_singleton_lock = threading.Lock()


def get_default_store() -> S2SModeOverrideStore:
    """Return the process-wide :class:`S2SModeOverrideStore`.

    Used by both the slash-command handler and the voice factory so they
    agree on the set of per-channel overrides without having to pass the
    store around through every call site.
    """
    global _store_singleton
    with _store_singleton_lock:
        if _store_singleton is None:
            _store_singleton = S2SModeOverrideStore()
        return _store_singleton


# ---------------------------------------------------------------------------
# S2SConfigureView — rich /s2s configure panel (Wave 2 / Task 2.3)
# ---------------------------------------------------------------------------
#
# The View subclasses ``discord.ui.View`` so discord.py MUST be importable
# at class-definition time. We do a top-level try-import and let
# ``S2SConfigureView`` remain undefined when discord.py is absent — tests
# ``pytest.importorskip("discord")`` before importing the symbol.
try:  # pragma: no cover - import-time guard
    import discord as _discord_mod  # type: ignore
    from discord import ui as _ui  # type: ignore
except ImportError:  # pragma: no cover - environment-dependent
    _discord_mod = None  # type: ignore[assignment]
    _ui = None  # type: ignore[assignment]


if _ui is not None:

    class S2SConfigureView(_ui.View):  # type: ignore[misc, valid-type]
        """Rich configuration panel for ``/s2s configure``.

        Lifetime: 5 minutes (default View timeout); on timeout the
        controls disable themselves but the message stays as a summary.

        Components
        ----------
        - Mode select (4 canonical modes).
        - Per-kind provider select for every kind with at least one
          registered provider. Options are ``[:25]`` sliced because
          Discord caps a Select at 25 options.
        - Three buttons: Test pipeline / Reset overrides / Refresh status.

        Every select and button patches the override store via the same
        ``S2SModeOverrideStore`` used by the direct ``/s2s mode`` and
        ``/s2s provider`` subcommands so the two surfaces can't drift.
        """

        def __init__(
            self,
            *,
            guild_id: int,
            channel_id: int,
            store: "S2SModeOverrideStore",
        ) -> None:
            super().__init__(timeout=300.0)
            self._g = int(guild_id)
            self._c = int(channel_id)
            self._store = store
            # Build options dynamically from the live registry so e.g. a
            # plugin-registered TTS provider shows up in the picker on
            # next /s2s configure.
            from ..registry import list_registered

            reg = list_registered()
            self._add_mode_select()
            self._add_provider_select("realtime", list(reg.get("realtime", [])))
            self._add_provider_select("stt", list(reg.get("stt", [])))
            self._add_provider_select("tts", list(reg.get("tts", [])))

        # ----- select builders -----------------------------------------

        def _add_mode_select(self) -> None:
            opts = [
                _discord_mod.SelectOption(label="Cascaded (default)", value="cascaded"),
                _discord_mod.SelectOption(label="Pipeline (custom)", value="pipeline"),
                _discord_mod.SelectOption(label="Realtime", value="realtime"),
                _discord_mod.SelectOption(label="External server", value="s2s-server"),
            ]
            sel = _ui.Select(
                placeholder="Pick a mode…",
                options=opts,
                min_values=1,
                max_values=1,
                custom_id="s2s_mode",
            )

            async def _cb(interaction: Any) -> None:
                values = getattr(sel, "values", None) or []
                if not values:
                    data = getattr(interaction, "data", {}) or {}
                    values = data.get("values") or []
                if not values:
                    return
                v = values[0]
                self._store.patch_record(self._g, self._c, mode=v)
                await interaction.response.send_message(
                    f"Mode → `{v}`", ephemeral=True
                )

            sel.callback = _cb  # type: ignore[assignment]
            self.add_item(sel)

        def _add_provider_select(self, kind: str, names: list[str]) -> None:
            if not names:
                return  # No registered providers of this kind — skip the row
            # Discord caps a Select at 25 options — respect it.
            opts = [
                _discord_mod.SelectOption(label=n, value=n) for n in names[:25]
            ]
            placeholder = {
                "realtime": "Pick realtime backend…",
                "stt": "Pick STT provider…",
                "tts": "Pick TTS provider…",
            }.get(kind, f"Pick {kind}…")
            field = f"{kind}_provider"
            sel = _ui.Select(
                placeholder=placeholder,
                options=opts,
                min_values=1,
                max_values=1,
                custom_id=f"s2s_{kind}",
            )

            async def _cb(interaction: Any) -> None:
                values = getattr(sel, "values", None) or []
                if not values:
                    data = getattr(interaction, "data", {}) or {}
                    values = data.get("values") or []
                if not values:
                    return
                v = values[0]
                self._store.patch_record(self._g, self._c, **{field: v})
                await interaction.response.send_message(
                    f"{kind.upper()} provider → `{v}`", ephemeral=True
                )

            sel.callback = _cb  # type: ignore[assignment]
            self.add_item(sel)

        # ----- buttons -------------------------------------------------

        @_ui.button(  # type: ignore[misc]
            label="Test pipeline",
            style=_discord_mod.ButtonStyle.primary,
            custom_id="s2s_test",
        )
        async def _test_btn(self, interaction: Any, _button: Any) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            from ..tools import s2s_test_pipeline

            r = json.loads(s2s_test_pipeline({}))
            if r.get("ok"):
                await interaction.followup.send(
                    f"✅ TTS OK — wrote {r.get('bytes')} bytes via "
                    f"`{r.get('tts_provider')}`",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"❌ Test failed at `{r.get('stage')}`: {r.get('error')}",
                    ephemeral=True,
                )

        @_ui.button(  # type: ignore[misc]
            label="Reset overrides",
            style=_discord_mod.ButtonStyle.danger,
            custom_id="s2s_reset",
        )
        async def _reset_btn(self, interaction: Any, _button: Any) -> None:
            self._store.set_record(self._g, self._c, {})
            await interaction.response.send_message(
                "✅ Cleared this channel's overrides.", ephemeral=True
            )

        @_ui.button(  # type: ignore[misc]
            label="Refresh status",
            style=_discord_mod.ButtonStyle.secondary,
            custom_id="s2s_refresh",
        )
        async def _refresh_btn(self, interaction: Any, _button: Any) -> None:
            from ..tools import s2s_status as _stat

            payload = json.loads(_stat({}))
            rec = self._store.get_record(self._g, self._c)
            text = format_status(
                active_mode=payload["active_mode"],
                config_mode=payload["config_mode"],
                realtime_provider=payload["realtime"]["provider"],
                stt_provider=payload["cascaded"]["stt_provider"],
                tts_provider=payload["cascaded"]["tts_provider"],
                guild_id=self._g,
                channel_id=self._c,
                per_channel_record=rec,
            )
            await interaction.response.edit_message(content=text, view=self)

        async def on_timeout(self) -> None:
            """Disable every control once the view expires (5 min)."""
            for child in self.children:
                try:
                    child.disabled = True  # type: ignore[attr-defined]
                except Exception:  # pragma: no cover - defensive
                    pass


async def _require_guild_channel(interaction: Any) -> Optional[tuple[int, int]]:
    """Return ``(guild_id, channel_id)`` or ``None`` after sending an error.

    Subcommand callbacks use this to short-circuit cleanly when ``/s2s`` is
    invoked from a DM or any non-guild-channel context. On failure it sends
    an ephemeral error directly, so callers can just do::

        pair = await _require_guild_channel(interaction)
        if pair is None:
            return
        g, c = pair
    """
    g = getattr(interaction, "guild", None)
    c = getattr(interaction, "channel", None)
    g_id = getattr(g, "id", None)
    c_id = getattr(c, "id", None)
    if g_id is None or c_id is None:
        try:
            await interaction.response.send_message(
                "❌ `/s2s` must be used inside a guild text channel.",
                ephemeral=True,
            )
        except Exception:  # pragma: no cover - defensive
            pass
        return None
    return int(g_id), int(c_id)


def _find_discord_tree(ctx: Any) -> Any | None:
    """Best-effort walk to a ``discord.app_commands.CommandTree`` instance.

    The Hermes adapter exposes its :class:`discord.Client` as ``self._client``
    and the slash tree as ``self._client.tree``. Plugin contexts don't hand
    us the adapter directly, so we probe a few common attribute paths.
    Returns ``None`` if no tree can be found — the caller then logs and
    no-ops (the slash won't appear until a restart on an uninstrumented
    gateway).
    """
    candidates = []
    # Direct ctx.tree (hypothetical future hook)
    candidates.append(getattr(ctx, "tree", None))
    # ctx.bot.tree (some plugin interfaces)
    bot = getattr(ctx, "bot", None)
    candidates.append(getattr(bot, "tree", None) if bot is not None else None)
    # ctx.adapter / ctx.discord_adapter
    for name in ("adapter", "discord_adapter", "platform"):
        adapter = getattr(ctx, name, None)
        if adapter is None:
            continue
        candidates.append(getattr(adapter, "tree", None))
        client = getattr(adapter, "_client", None) or getattr(adapter, "client", None)
        if client is not None:
            candidates.append(getattr(client, "tree", None))
    # ctx.runner.adapters[*]._client.tree (full gateway path)
    runner = getattr(ctx, "runner", None)
    adapters = getattr(runner, "adapters", None) if runner is not None else None
    if isinstance(adapters, dict):
        for ad in adapters.values():
            client = getattr(ad, "_client", None) or getattr(ad, "client", None)
            if client is not None:
                candidates.append(getattr(client, "tree", None))

    for cand in candidates:
        if cand is None:
            continue
        # Duck-typing check: a CommandTree exposes .add_command / .sync / .commands
        if callable(getattr(cand, "add_command", None)) and callable(
            getattr(cand, "sync", None)
        ):
            return cand
    return None


def _tree_already_synced(tree: Any) -> bool:
    """Heuristic: did the adapter already call ``tree.sync()``?

    The Hermes discord adapter sets a private marker
    ``self._slash_commands_synced`` on the client in its
    ``_register_slash_commands`` path. We also probe a generic
    ``_synced``/``__hermes_s2s_tree_synced__`` sentinel so tests can
    inject the state directly.
    """
    client = getattr(tree, "client", None)
    for obj in (tree, client):
        if obj is None:
            continue
        for attr in (
            "__hermes_s2s_tree_synced__",
            "_slash_commands_synced",
            "_synced",
        ):
            if getattr(obj, attr, False):
                return True
    return False


def install_s2s_command(ctx: Any) -> bool:
    """Register the plugin-owned ``/s2s`` slash command on the Discord tree.

    Idempotent: repeated calls on the same context are no-ops after the
    first successful install.

    Returns ``True`` if the command was newly installed, ``False`` otherwise
    (already installed, tree unavailable, or discord.py missing). Never
    raises — the caller wraps it in try/except regardless.
    """
    try:
        import discord  # type: ignore
        from discord import app_commands  # type: ignore
    except ImportError:
        logger.info(
            "hermes-s2s: discord.py not importable; skipping /s2s install"
        )
        return False

    tree = _find_discord_tree(ctx)
    if tree is None:
        logger.info(
            "hermes-s2s: no Discord CommandTree reachable from ctx; "
            "/s2s slash will not be registered"
        )
        return False

    # Idempotency sentinel on the TREE, not just ctx — ctx may be a
    # transient wrapper handed to register() on every reload.
    if getattr(tree, _S2S_COMMAND_INSTALLED, False):
        logger.debug("hermes-s2s: /s2s already installed on this tree")
        return False

    store = get_default_store()

    # discord.py's _extract_parameters_from_callback evaluates annotation
    # strings against ``callback.__globals__`` — the module-level globals
    # of this file, NOT the function's closure. Because we import
    # ``discord`` + ``app_commands`` lazily inside this function to stay
    # importable in test environments without discord.py, we temporarily
    # inject them into this module's globals so the @app_commands.command
    # decorator can resolve ``Interaction`` and ``Choice[str]`` from the
    # callback's annotations.
    _mod_globals = globals()
    _mod_globals.setdefault("discord", discord)
    _mod_globals.setdefault("app_commands", app_commands)
    Choice = app_commands.Choice  # noqa: N806 — for readability in annotation
    Interaction = discord.Interaction  # noqa: N806
    _mod_globals.setdefault("Choice", Choice)
    _mod_globals.setdefault("Interaction", Interaction)

    # ------------------------------------------------------------------
    # /s2s is a Group (Wave 2 / Task 2.2) with:
    #   mode, status, provider, test, doctor, reset   ← this task
    #   configure                                      ← Task 2.3
    # ------------------------------------------------------------------
    group = app_commands.Group(
        name="s2s",
        description="Configure speech-to-speech voice",
    )

    @group.command(name="mode", description="Set voice mode for this channel")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Cascaded (default)", value="cascaded"),
            app_commands.Choice(name="Pipeline (custom STT+TTS)", value="pipeline"),
            app_commands.Choice(name="Realtime", value="realtime"),
            app_commands.Choice(name="External S2S server", value="s2s-server"),
        ]
    )
    async def s2s_mode(  # type: ignore[no-untyped-def]
        interaction: Interaction,
        mode: Choice[str],
    ):
        pair = await _require_guild_channel(interaction)
        if pair is None:
            return
        g, c = pair
        try:
            canonical = VoiceMode.normalize(mode.value)
        except ValueError as exc:
            await interaction.response.send_message(
                f"❌ Unknown mode `{mode.value}`: {exc}", ephemeral=True
            )
            return
        try:
            store.patch_record(g, c, mode=canonical.value)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("hermes-s2s: /s2s mode patch_record failed: %s", exc)
            await interaction.response.send_message(
                "❌ Couldn't save the override (see gateway logs).",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"✅ This channel: mode → **{mode.name}**", ephemeral=True
        )

    @group.command(
        name="status",
        description="Show current S2S settings for this channel",
    )
    async def s2s_status_cmd(interaction: Interaction):  # type: ignore[no-untyped-def]
        pair = await _require_guild_channel(interaction)
        if pair is None:
            return
        g, c = pair
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
            guild_id=g,
            channel_id=c,
            per_channel_record=rec,
        )
        await interaction.response.send_message(text, ephemeral=True)

    @group.command(
        name="provider",
        description="Override a single provider for this channel",
    )
    @app_commands.choices(
        kind=[
            app_commands.Choice(name="Realtime backend", value="realtime"),
            app_commands.Choice(name="STT (cascaded)", value="stt"),
            app_commands.Choice(name="TTS (cascaded)", value="tts"),
        ]
    )
    @app_commands.describe(
        name="Provider name (see /s2s status for the available list)"
    )
    async def s2s_provider(  # type: ignore[no-untyped-def]
        interaction: Interaction,
        kind: Choice[str],
        name: str,
    ):
        pair = await _require_guild_channel(interaction)
        if pair is None:
            return
        g, c = pair
        from ..registry import list_registered

        field_map = {
            "realtime": "realtime_provider",
            "stt": "stt_provider",
            "tts": "tts_provider",
        }
        field = field_map[kind.value]
        available = list_registered().get(kind.value, [])
        if name not in available:
            listing = ", ".join(f"`{a}`" for a in available) or "_(none registered)_"
            await interaction.response.send_message(
                f"❌ Unknown {kind.value} provider `{name}`. Available: {listing}",
                ephemeral=True,
            )
            return
        try:
            store.patch_record(g, c, **{field: name})
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "hermes-s2s: /s2s provider patch_record failed: %s", exc
            )
            await interaction.response.send_message(
                "❌ Couldn't save the override (see gateway logs).",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"✅ This channel: {kind.value} provider → **{name}**",
            ephemeral=True,
        )

    @group.command(name="test", description="Run a TTS smoke test")
    @app_commands.describe(text="Optional text to synthesise")
    async def s2s_test(  # type: ignore[no-untyped-def]
        interaction: Interaction,
        text: str = "",
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        from ..tools import s2s_test_pipeline
        import json as _json

        result = _json.loads(s2s_test_pipeline({"text": text or None}))
        if result.get("ok"):
            await interaction.followup.send(
                f"✅ TTS OK — `{result.get('tts_provider')}` wrote "
                f"{result.get('bytes')} bytes to `{result.get('wrote')}`",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"❌ Smoke test failed at stage `{result.get('stage')}`: "
                f"{result.get('error')}",
                ephemeral=True,
            )

    @group.command(
        name="doctor",
        description="Run preflight checks (deps, keys, WS probe)",
    )
    async def s2s_doctor_cmd(interaction: Interaction):  # type: ignore[no-untyped-def]
        await interaction.response.defer(ephemeral=True, thinking=True)
        from ..tools import s2s_doctor as _doctor
        import json as _json

        report_str = await _doctor({})
        report = _json.loads(report_str)
        summary = format_doctor_summary(report)
        await interaction.followup.send(summary, ephemeral=True)

    @group.command(
        name="reset",
        description="Clear all S2S overrides for this channel",
    )
    async def s2s_reset(interaction: Interaction):  # type: ignore[no-untyped-def]
        pair = await _require_guild_channel(interaction)
        if pair is None:
            return
        g, c = pair
        store.set_record(g, c, {})
        await interaction.response.send_message(
            "✅ Cleared all S2S overrides for this channel — back to global config.",
            ephemeral=True,
        )

    @group.command(
        name="configure",
        description="Open interactive S2S configuration panel",
    )
    async def s2s_configure(interaction: Interaction):  # type: ignore[no-untyped-def]
        pair = await _require_guild_channel(interaction)
        if pair is None:
            return
        g, c = pair
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
            guild_id=g,
            channel_id=c,
            per_channel_record=rec,
        )
        view = S2SConfigureView(guild_id=g, channel_id=c, store=store)
        await interaction.response.send_message(
            text, view=view, ephemeral=True
        )

    try:
        tree.add_command(group)
    except Exception as exc:
        logger.warning("hermes-s2s: tree.add_command(/s2s group) failed: %s", exc)
        return False

    setattr(tree, _S2S_COMMAND_INSTALLED, True)

    if _tree_already_synced(tree):
        logger.warning(
            "hermes-s2s: /s2s registered AFTER tree.sync(); the command "
            "will only appear in Discord after the bot restarts. If you "
            "need it immediately, call `await tree.sync()` again."
        )
    else:
        logger.info(
            "hermes-s2s: /s2s slash command group registered on the Discord tree"
        )
    return True


def install_s2s_command_on_adapter(adapter: Any) -> bool:
    """Install ``/s2s`` against a LIVE :class:`DiscordAdapter` instance.

    Use this from inside a wrapped adapter method (e.g. the
    ``join_voice_channel`` monkey-patch) — at that point the adapter's
    ``_client.tree`` is fully constructed AND the bot is logged in,
    which is the seam ``register(ctx)`` cannot safely target (the
    register-time ``ctx.runner.adapters[...]._client.tree`` walk hits
    ``None`` because the bot hasn't connected yet).

    Idempotent — repeated calls on the same tree are no-ops.

    If the tree was already synced when we add the command, this
    function schedules an ``await tree.sync()`` on the bot's running
    loop so Discord's UI actually picks up the new command without
    requiring a bot restart. Without that re-sync, the existing
    fingerprint-skip path in ``gateway/platforms/discord.py`` blocks
    the implicit re-sync and ``/s2s`` never appears in the slash
    autocomplete (verified on hermes-s2s 0.5.0 against the live
    Hermes Discord adapter, 2026-05-11).

    Returns True if the command was newly installed, False otherwise.
    """
    try:
        import discord  # type: ignore
        import asyncio
    except ImportError:
        return False

    client = getattr(adapter, "_client", None) or getattr(adapter, "client", None)
    if client is None:
        logger.debug(
            "hermes-s2s: install_s2s_command_on_adapter: adapter has no client"
        )
        return False
    tree = getattr(client, "tree", None)
    if tree is None:
        logger.debug(
            "hermes-s2s: install_s2s_command_on_adapter: client has no tree"
        )
        return False

    # Reuse the existing register-time install path by feeding it a
    # synthetic ctx that exposes the live tree directly.
    class _LiveCtx:
        def __init__(self, t: Any) -> None:
            self.tree = t

    was_already_synced = _tree_already_synced(tree)
    installed = install_s2s_command(_LiveCtx(tree))
    if not installed:
        # Either already installed (idempotent no-op) or add_command
        # failed; either way nothing more to do.
        return False

    # If the adapter already synced its own slash commands before
    # we added /s2s, force a resync now so Discord's UI picks it up.
    if was_already_synced:
        loop = getattr(client, "loop", None)
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
        if loop is not None and not loop.is_closed():
            async def _resync():
                try:
                    await tree.sync()
                    logger.info(
                        "hermes-s2s: forced tree.sync() after late /s2s "
                        "install — Discord UI should refresh within ~30s"
                    )
                except Exception as exc:
                    logger.warning(
                        "hermes-s2s: post-install tree.sync() failed: %s", exc
                    )

            try:
                asyncio.run_coroutine_threadsafe(_resync(), loop)
            except Exception as exc:
                logger.warning(
                    "hermes-s2s: could not schedule post-install tree.sync: %s",
                    exc,
                )
        else:
            logger.warning(
                "hermes-s2s: /s2s installed but no live event loop "
                "to schedule tree.sync() on; restart the bot to surface "
                "the command in Discord"
            )

    return True


__all__ = [
    "S2SModeOverrideStore",
    "get_default_store",
    "install_s2s_command",
    "install_s2s_command_on_adapter",
]

if _ui is not None:
    __all__.append("S2SConfigureView")
