# 10 — Arabic-language regression in hermes-s2s v0.3.8 (realtime / gemini-live)

Status: **root cause identified — config-layer bug, not Gemini behavior**. Scope: realtime voice, Discord, `provider=gemini-live`.

## TL;DR

Wizard writes the English-anchored `system_prompt` + `language_code` to
`s2s.realtime.gemini_live.*` (nested), but `discord_bridge.py` reads them
from `cfg.realtime_options` at the **outer** level. Result: **both the
system_prompt and the entire `gemini_live` sub-block (model, voice,
language_code) never reach `GeminiLiveBackend`.** Backend runs with
built-in defaults (`"You are a helpful voice assistant. Respond briefly."`
+ `gemini-2.5-flash-native-audio-latest` + `Aoede` + `en-US`). With no
English anchor, native-audio Gemini falls back to input-audio language
detection → Arabic.

## Root cause — code evidence

### Write side (wizard)
`hermes_s2s/cli.py` L64-76 (`_realtime_s2s_block`) produces:

```yaml
s2s.realtime:
  provider: gemini-live
  gemini_live:            # nested sub-block
    model, voice, language_code, system_prompt
```

User's live `~/.hermes/config.yaml` matches (verified on disk: model is
`gemini-3.1-flash-live-preview`, system_prompt starts "You are ARIA...").

### Read side (runtime)
`hermes_s2s/config/__init__.py` L56-69 stores the **entire** `realtime`
dict as `S2SConfig.realtime_options` — so `realtime_options` =
`{"provider":..., "gemini_live": {...}}`, NOT the inner block.

`hermes_s2s/_internal/discord_bridge.py` L337-346:

```python
realtime_opts = getattr(cfg, "realtime_options", {}) or {}
bridge = RealtimeAudioBridge(
    ...
    system_prompt=realtime_opts.get(
        "system_prompt", "You are a helpful voice assistant. Respond briefly."
    ),  # MISS: key is under realtime_opts["gemini_live"]["system_prompt"]
    voice=realtime_opts.get("voice", None),  # MISS
    tools=[],
)
```

L315: `backend = resolve_realtime(cfg.realtime_provider, cfg.realtime_options)`
→ `make_gemini_live(cfg.realtime_options)` in `providers/realtime/gemini_live.py`
L424-432:

```python
return GeminiLiveBackend(
    model=cfg.get("model", "gemini-2.5-flash-native-audio-latest"),  # MISS
    voice=cfg.get("voice", "Aoede"),                                 # MISS
    language_code=cfg.get("language_code", "en-US"),                 # MISS
    ...
)
```

All three `.get()`s look at the **outer** level and fall through to defaults.
User's `gemini-3.1-flash-live-preview` model is silently ignored.

Corroborating: `hermes_s2s/doctor.py` L334-352 (`_active_provider_block`)
**does** unwrap the `gemini_live` sub-key correctly — which is why
`hermes s2s doctor` appears fine while the runtime misbehaves.

### Why Arabic specifically
Google AI Dev forum confirms native-audio Live models detect language
primarily from input audio, and the official remediation is baking the
language into `systemInstruction` ([arabic thread][1], [si thread][2]).
With the English-anchored prompt stripped by the bug, Gemini follows the
user's accent/breath cues → Arabic.

[1]: https://discuss.ai.google.dev/t/new-gemini-native-audio-model-failing-for-some-languages/110433
[2]: https://discuss.ai.google.dev/t/new-gemini-live-api-native-audio-output-models-not-supporting-system-instructions/86513

## Alternative causes (ranked)

**#2 — Gemini 3.1 Flash Live Preview behavioral differences (LOW once #1 fixed).**
`gemini-3.1-flash-live-preview` is real (launched 2026-03-26, per
[ai.google.dev model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview)
and vercel/ai PR #13904). It changes `thinkingLevel`, requires
`send_realtime_input` for mid-turn text, drops async function calling. None
alter `systemInstruction`/`languageCode` semantics — but because of bug #1,
the user's 3.1 config never runs anyway. After the fix, residual risk is
mitigated by Fix C below.

**#3 — Hermes core competing system prompt (RULED OUT).** `gateway/run.py`
L1175-1176, L2189-2204, L14310-14485 inject `_ephemeral_system_prompt` via
`AIAgent(..., ephemeral_system_prompt=...)` — text-agent path only. The
realtime bridge bypasses the text agent entirely: `discord_bridge.py`
L398-412 silences `_voice_input_callback` and L418-439 disables cascaded
auto-TTS when `mode=='realtime'`. The gate is unconditional and active for
this user. No Hermes-core prompt reaches the Gemini WS.

**#4 — Cached v0.3.7 Arabic turns replayed from session DB (RULED OUT).**
`~/.hermes/sessions/*.db` is the text-agent SQLite store. Gemini Live WS
sessions are not persisted across process restarts: `gemini_live.py` L97
(`self._session_handle = None`) and L176-180 send empty `sessionResumption: {}`
on each fresh connect. No on-disk Arabic turn can prefill a new WS session.

**#5 — `responseModalities: ['AUDIO']`-only path (UNLIKELY).** L157 only
disables the text channel; it doesn't change language selection. Adding
`outputAudioTranscription: {}` (already present at L169) helps *diagnose*
language drift but isn't the cure.

## Concrete fixes

### Fix A (REQUIRED) — unwrap sub-block in bridge
`hermes_s2s/_internal/discord_bridge.py` L337-346. Mirror `doctor.py`
L334-352 to pick the `gemini_live` / `openai` sub-dict:

```python
realtime_opts = getattr(cfg, "realtime_options", {}) or {}
provider = (getattr(cfg, "realtime_provider", "") or "").lower()
key = ("gemini_live" if "gemini" in provider
       else "openai" if ("openai" in provider or "gpt-realtime" in provider)
       else provider.replace("-", "_"))
provider_block = realtime_opts.get(key, {}) or {}

bridge = RealtimeAudioBridge(
    backend=backend, tool_bridge=tool_bridge,
    system_prompt=provider_block.get(
        "system_prompt",
        realtime_opts.get("system_prompt", _DEFAULT_SYSTEM_PROMPT)),  # back-compat
    voice=provider_block.get("voice", realtime_opts.get("voice")),
    tools=[],
)
```

### Fix B (REQUIRED) — unwrap sub-block in factory
`hermes_s2s/providers/realtime/gemini_live.py` L424-432:

```python
def make_gemini_live(config):
    cfg = dict(config or {})
    sub = cfg.get("gemini_live") if isinstance(cfg.get("gemini_live"), dict) else {}
    merged = {**cfg, **sub}   # sub-block wins
    return GeminiLiveBackend(
        api_key_env=merged.get("api_key_env", "GEMINI_API_KEY"),
        model=merged.get("model", "gemini-2.5-flash-native-audio-latest"),
        voice=merged.get("voice", "Aoede"),
        language_code=merged.get("language_code", "en-US"),
        url=merged.get("url"),
    )
```

Also fixes the latent bug where `gemini-3.1-flash-live-preview` never
actually gets used.

### Fix C — defense-in-depth language anchor
`gemini_live.py` L151-180, in `_build_setup`, prepend a hard language
directive so even a weak/overridden system_prompt stays English:

```python
lang = (self.language_code or "en-US").split("-")[0].upper()
anchored = f"Respond exclusively in {lang}.\n\n{system_prompt}"
setup["systemInstruction"] = {"parts": [{"text": anchored}]}
```

Keep `outputAudioTranscription: {}` (L169) — it's the observability hook for
catching language drift.

### Fix D (minor) — wizard warn on 3.1 feature mismatches
`cli.py` L70 default is still 2.5; if user picks 3.1 preview, `doctor.py`
should warn about sync-only function calling (3.1 drops async per
ai.google.dev). Unrelated to Arabic but prevents silent tool-call hangs.

## Verification plan

1. **Unit (new)** — `tests/test_discord_bridge.py`: with config
   `realtime.gemini_live.system_prompt="MARKER"`, assert the constructed
   `RealtimeAudioBridge._system_prompt == "MARKER"`.
2. **Unit (new)** — `tests/test_gemini_live.py`: given
   `{"provider":"gemini-live","gemini_live":{"model":"X","language_code":"fr-FR"}}`,
   factory returns backend with `model=="X"`, `language_code=="fr-FR"`.
3. **Integration** — `hermes s2s doctor --probe` logs the resolved
   `system_prompt` length; after fix must exceed generic-fallback length.
4. **Manual** — user joins voice with Arabic accent; bot replies in English
   for 3+ consecutive turns. Check `outputAudioTranscription` logs are
   English. Flip `language_code` to `fr-FR` — bot switches to French.
5. **Regression** — `scripts/smoke_realtime.py` still passes (uses test URL
   override, unaffected by Fix B).

## Files to edit

| File | Lines | Fix |
|------|-------|-----|
| `hermes_s2s/_internal/discord_bridge.py` | 337-346 | A — unwrap in bridge |
| `hermes_s2s/providers/realtime/gemini_live.py` | 424-432 | B — unwrap in factory |
| `hermes_s2s/providers/realtime/gemini_live.py` | 151-180 | C — language anchor |
| `hermes_s2s/doctor.py` | ~200-250 | D — warn on 3.1 feature gaps |

## Confidence

**High.** Config key-path mismatch is observable in source. Forum evidence explains the Arabic flip. No code was modified during this research.
