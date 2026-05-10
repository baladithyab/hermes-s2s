# ADR-0010: Provider sub-block unwrap (gemini_live / openai)

**Status:** proposed
**Date:** 2026-05-10
**Driven by:** v0.3.9 Arabic-language regression; see
[research-10](../research/10-arabic-language-rootcause.md) for full evidence.

## Context

The v0.3.2 wizard (ADR-0009) writes realtime settings nested under a
provider-specific sub-block:

```yaml
s2s:
  realtime:
    provider: gemini-live
    gemini_live: { model, voice, language_code, system_prompt }   # NESTED
```

The 0.3.0-era runtime reads those keys from the **outer** level of
`cfg.realtime_options`:

- `_internal/discord_bridge.py` L337-346 calls
  `realtime_opts.get("system_prompt", ...)` / `...get("voice", ...)` on the
  top-level dict; wizard values live one level deeper under
  `realtime_opts["gemini_live"]`, so every `.get()` falls through to a default.
- `providers/realtime/gemini_live.py::make_gemini_live` (L424-432) has the
  same outer-level miss for `model`, `voice`, `language_code`, `url`.

Net effect in v0.3.8: the user's configured model
(`gemini-3.1-flash-live-preview`), voice, language anchor, and ARIA system
prompt are all **silently discarded** and `GeminiLiveBackend` runs on built-in
defaults. Without the English anchor, native-audio Gemini falls back to
input-audio language detection and replies in Arabic to English-accented
speakers (research-10 §Why Arabic).

The bug hid for a release because **doctor.py does the unwrap correctly**
(`_active_provider_block` L334-352 picks the `gemini_live` sub-dict), so
`hermes s2s doctor` reported green while the runtime ran on defaults. Two code
paths, two ideas of "where config lives" — that divergence shipped the regression.

## Decision

Codify a single **provider sub-block unwrap pattern** used by every code path
that reads `realtime_options`. Both shapes MUST resolve to the same backend:

```yaml
# Nested (wizard-produced, preferred going forward)
realtime: { provider: gemini-live, gemini_live: { voice: Puck, model: X } }

# Flat (pre-0.3.8 back-compat)
realtime: { provider: gemini-live, voice: Puck, model: X }
```

### The pattern

1. Resolve the sub-block key from the provider name:
   `gemini*` → `gemini_live`; `openai*` / `gpt-realtime*` → `openai`; else
   `provider.replace("-", "_")`.
2. Merge with **sub-block wins**:
   `merged = {**outer, **sub_block}`.
3. Read all settings (`model`, `voice`, `language_code`, `system_prompt`,
   `url`, `api_key_env`) off `merged`, never off the raw outer dict.

Applied in three seams (Fixes A + B from research-10):

- `discord_bridge._resolve_bridge_params(cfg)` — new helper, returns
  `(system_prompt, voice)` for `RealtimeAudioBridge`.
- `providers/realtime/gemini_live.make_gemini_live`.
- `providers/realtime/openai_realtime.make_openai_realtime`.

`doctor.py::_active_provider_block` already implements this shape; the runtime
now conforms to doctor, not the other way around.

## Consequences

**Positive**
- Wizard's nested YAML and hand-edited flat YAML both work; no breakage for
  0.3.0–0.3.7 users.
- Doctor and runtime see the same effective config; the class of "doctor says
  green, runtime runs on defaults" bug is eliminated at its source.
- Unwrap logic is localized to two factories + one bridge helper, all backed
  by `tests/test_config_unwrap.py`.

**Negative**
- Two valid shapes to support forever. Mitigation: wizard only emits nested;
  flat is acknowledged back-compat.
- Keys set at *both* levels resolve sub-block-wins — could surprise mixed
  configs. Mitigation: doctor may warn on dual-level collisions (future work).

## Alternatives considered

- **"Just write a flat config" — rejected.** Provider-specific keys collide:
  both `gemini_live` and `openai` have `voice` (and could diverge on `model`,
  `system_prompt`, future fields). Flat forces either ambiguous keys (`voice`
  means whichever provider is active — brittle on provider switch) or prefixed
  keys (`gemini_voice`, `openai_voice`) which re-invent the sub-block with
  worse ergonomics. Nested keeps provider configs independent and copy-pasteable.
- **Normalize to flat at load** — rejected. Loses the provider-grouping that
  makes `config.yaml` readable and makes switching providers a one-line change.
- **Fix doctor to match the buggy runtime** — rejected. Doctor matched user
  intent; runtime was wrong.

## Test discipline (regression fence)

`tests/test_config_unwrap.py` is the contract. It asserts both shapes resolve
correctly through all three seams:

- `test_discord_bridge_unwraps_gemini_live_subblock` — nested
  `{system_prompt: MARKER-EN, voice: Puck}` reaches the bridge.
- `test_discord_bridge_falls_back_to_outer_for_flat_config` — flat shape
  (back-compat).
- `test_discord_bridge_uses_default_when_nothing_set` — empty config falls
  through to a default that mentions "English" so Gemini's input-audio
  language-detect path can't drift to Arabic.
- `test_make_gemini_live_unwraps_subblock` / `_outer_level_back_compat` —
  factory resolves `model` + `language_code` from either shape.
- `test_make_openai_realtime_unwraps_subblock` — the pattern is
  provider-agnostic.

Any future realtime provider MUST add an analogous nested/flat pair here. A
new `realtime_options` consumer that reads keys directly off the outer dict
is a review-blocker.
