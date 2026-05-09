# ADR-0006: Discord voice integration — upstream PR for `voice_pipeline_factory` + monkeypatch bridge

**Status:** accepted
**Date:** 2026-05-09
**Deciders:** Codeseys, ARIA-orchestrator
**Driven by:** [research/06-hermes-discord-voice-seam.md](../research/06-hermes-discord-voice-seam.md)

## Context

For 0.3.0 realtime mode (Gemini Live, OpenAI Realtime, eventually s2s-server full-duplex), we need to inject a custom audio sink/source into Hermes's Discord voice handler. Phase 3 research confirmed:

- Hermes uses `discord.py[voice]` (Rapptz) with a **custom `VoiceReceiver`** class in `gateway/platforms/discord.py` that hooks `discord.VoiceClient`'s raw UDP socket, decrypts RTP via `nacl.secret.Aead`, and Opus-decodes inline.
- It's **not** a `discord.AudioSink` subclass — it's a low-level integration that wraps the voice client's network plumbing.
- Outgoing TTS uses `discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(path))` — file-based, single-shot.
- **No swap-point exists** in plugin context (`ctx`) for voice pipelines. There's `register_platform` and lifecycle hooks, but nothing for sink/source factories.

Three integration strategies were considered:

1. **A. Upstream PR** adding `ctx.register_voice_pipeline_factory("discord", ...)` and a branch in `join_voice_channel` / `play_tts` that calls the factory if registered.
2. **B. Parallel Discord voice client** — hermes-s2s connects its own bot session to the same VC. Rejected: Discord requires a separate bot identity for a second VC connection (one bot, one VC at a time per guild). Also doubles bandwidth and duplicates RTP/Opus stack.
3. **C. Monkey-patch** `gateway/platforms/discord.py` at plugin load time. Fragile; breaks on Hermes refactors.

## Decision

**Combine A + C, gated by Hermes version detection.**

- **Strategy A (primary, long-term):** Author and submit an upstream PR to NousResearch/hermes-agent adding:
  - `ctx.register_voice_pipeline_factory(platform: str, factory: Callable[[VoiceContext], VoicePipeline])` to plugin context.
  - A `VoicePipeline` Protocol with `audio_sink_for(user_id) -> AudioSink` and `audio_source() -> AudioSource` methods.
  - Branch in `gateway/platforms/discord.py::join_voice_channel`: if a factory is registered, use its sink/source; else use the existing `VoiceReceiver` / `FFmpegPCMAudio` defaults.
  - Estimated PR size: ~60-90 LOC + tests.

- **Strategy C (bridge, short-term):** Until the upstream PR lands and rolls out to user installs, ship a monkey-patch in hermes-s2s that activates when:
  - User has set `HERMES_S2S_MONKEYPATCH_DISCORD=1` in their env (explicit opt-in), OR
  - We detect a Hermes version that doesn't have the new hook AND the user has `s2s.mode: realtime` in config.
  - The patch wraps `DiscordAdapter.join_voice_channel` to install our sink/source after the discord.py voice client connects.
  - Versioning: monkey-patch checks `hermes_agent.__version__`; refuses to load on versions outside a known-tested range and logs an error pointing the user at the upstream PR status.

Once the PR merges and a Hermes release ships, the monkeypatch self-disables when it detects the new hook is available, and 0.4.0 deprecates it entirely.

## Consequences

**Positive:**
- 0.3.0 realtime mode works for users TODAY without waiting for upstream release cycle.
- Clean long-term path via upstream PR — eventual zero-monkeypatch state.
- Two-strategy approach lets us prove demand before asking Hermes maintainers to merge.

**Negative:**
- Monkey-patch maintenance burden — every Hermes minor release needs a quick verify pass.
- Two integration paths in the same codebase (hook vs patch) — extra complexity in `_internal/discord_bridge.py`.
- Users on the patch path are fragile to Hermes refactors. Mitigation: pin to Hermes minor version, error loudly on mismatch, document.

## Implementation shape

### `hermes_s2s/_internal/discord_bridge.py`

```python
def install_discord_voice_bridge(ctx) -> None:
    """Install the Discord voice bridge using whichever strategy is available."""
    if _has_native_voice_pipeline_hook(ctx):
        _install_via_factory_hook(ctx)
        logger.info("hermes-s2s using native voice_pipeline_factory hook")
        return

    if _monkeypatch_enabled():
        if not _hermes_version_supported_for_patch():
            logger.error(
                "hermes-s2s monkey-patch path requires Hermes Agent in range "
                "[X, Y), got %s. Set HERMES_S2S_MONKEYPATCH=0 to disable, or "
                "wait for upstream PR #<PR>.",
                _hermes_version(),
            )
            return
        _install_via_monkey_patch()
        logger.warning("hermes-s2s using monkey-patch bridge — temporary; "
                       "track upstream PR for native hook")
        return

    logger.info(
        "hermes-s2s realtime mode disabled in Discord: no native hook AND "
        "HERMES_S2S_MONKEYPATCH not set. Use cascaded mode or set the env var."
    )
```

### Upstream PR template

A `docs/upstream-pr-draft.md` in this repo with:
- The exact file diff for `gateway/platforms/discord.py`.
- The `VoicePipeline` Protocol definition.
- Tests covering the new hook (no-op factory, custom sink, custom source).
- Migration note for existing voice users (zero impact — defaults preserved).

We submit this PR after 0.3.0 ships and we have working monkeypatch users to demonstrate need.

## Alternatives considered (and rejected)

- **B. Parallel Discord voice client** — Discord doesn't allow two VC connections from the same bot. Would require a second bot account, doubling user setup complexity.
- **Pure monkey-patch (no upstream PR effort)** — long-term unmaintainable.
- **Pure upstream PR (no monkey-patch bridge)** — blocks 0.3.0 on upstream review and release cycle.
- **Build hermes-s2s as a Hermes fork** — explicitly rejected by user ("no need for the upstream pr lets do just the standalone plugin"). The monkey-patch bridge is the standalone-friendly compromise; the upstream PR is the long-term cleanup, not the v1 dependency.

## Open questions

1. The `VoicePipeline` Protocol shape — what exactly does Hermes need to call to flow audio through? Need to read `gateway/platforms/discord.py` deeper before drafting the PR. Phase 5 plan task: "spike — read discord.py voice handler in detail and draft VoicePipeline interface."
2. How does the bridge interact with cascaded mode? When `s2s.mode: cascaded`, hermes-s2s should NOT install the bridge — Hermes's normal voice handler runs unchanged. Implementation detail: bridge install is gated on `s2s.mode in {realtime, s2s-server}`.
3. Telegram voice notes — they don't use the same `VoiceReceiver` (Telegram is non-VC voice). 0.3.0 scope is Discord-only realtime; Telegram realtime is 0.4.0+.
