# ADR-0009: Plug-and-play UX — wizard auto-config + doctor pre-flight

**Status:** accepted
**Date:** 2026-05-09
**Driven by:** user requirement "make sure that this is a seamless install and usage"

## Context

After 0.3.1 the bridge architecture is sound but install/configure is still hands-on:
- User must edit `~/.hermes/.env` to set `HERMES_S2S_MONKEYPATCH_DISCORD=1`
- User must edit `~/.hermes/config.yaml` to set `s2s.mode: realtime`
- User must know which API keys to set
- Failure modes are scattered (missing dep, missing env, missing system lib, wrong API key, Hermes Discord misconfig) with no single "is this working" check

For the "from `pip install` to AI voice in a VC" path to be 30 seconds, we need:
1. Wizard profiles for realtime modes that write the FULL config block (mode + provider + monkey-patch flag)
2. Pre-flight check (`hermes s2s doctor`) that diagnoses every failure mode in one command
3. `s2s_doctor` LLM-callable tool so the user can just ask "is my voice working"

## Decision

Add three things in 0.3.2:

### 1. Wizard realtime profiles

`hermes s2s setup --profile realtime-gemini` produces:

```yaml
# ~/.hermes/config.yaml
s2s:
  mode: realtime
  realtime:
    provider: gemini-live
    gemini_live:
      model: gemini-live-2.5-flash
      voice: Aoede
      system_prompt: "You are a helpful voice assistant. Respond briefly."
```

```bash
# ~/.hermes/.env (idempotent append)
HERMES_S2S_MONKEYPATCH_DISCORD=1  # hermes-s2s realtime bridge
```

The wizard shows:
```
Profile: realtime-gemini

  Will write to ~/.hermes/config.yaml:
    s2s.mode: realtime
    s2s.realtime.provider: gemini-live
    ...

  Will append to ~/.hermes/.env:
    HERMES_S2S_MONKEYPATCH_DISCORD=1

  Realtime mode requires:
    [ ] GEMINI_API_KEY  — get one at https://aistudio.google.com/apikey
    [✓] discord.py     — installed
    [✗] DISCORD_BOT_TOKEN — set this in ~/.hermes/.env
    [✓] DISCORD_ALLOWED_USERS — found

Apply? [Y/n]
```

Two more profiles: `realtime-openai` (gpt-realtime, premium), `realtime-openai-mini` (gpt-realtime-mini, mid-tier).

### 2. `hermes s2s doctor` pre-flight check

Comprehensive readiness check. Each row is a (label, status, remediation) tuple:

```
hermes s2s doctor
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
hermes-s2s 0.3.2 — readiness check
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Configuration:
  ✓ s2s.mode = realtime
  ✓ s2s.realtime.provider = gemini-live

Python dependencies:
  ✓ websockets        installed
  ✓ scipy             installed
  ✗ kokoro            NOT installed   →  pip install hermes-s2s[kokoro]

System dependencies:
  ✓ ffmpeg            in PATH
  ✓ libopus           found
  ⚠ espeak-ng         not found       →  needed for kokoro; sudo apt install espeak-ng

API keys:
  ✓ GEMINI_API_KEY    set (39 chars)
  ⚠ OPENAI_API_KEY    not set         →  optional; needed if you switch to openai-realtime

Hermes integration:
  ✓ HERMES_S2S_MONKEYPATCH_DISCORD = 1
  ✓ DISCORD_BOT_TOKEN              set
  ✓ DISCORD_ALLOWED_USERS          set (1 user)

Backend connectivity:
  ⏳ Probing Gemini Live ws://...     ✓ connected and responsive (412ms)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Overall: 3 warnings, 1 error.
Required for realtime+Discord to work: install kokoro (warning above is optional).
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

`--json` flag emits the same data as a structured JSON object the LLM can consume.

`--no-probe` skips the backend WS probe (saves ~$0.0001 per run, useful for CI).

### 3. `s2s_doctor` LLM tool

The LLM sees:

```python
S2S_DOCTOR = {
  "name": "s2s_doctor",
  "description": (
    "Run a comprehensive pre-flight check on the hermes-s2s voice setup. "
    "Use when the user asks 'is my voice setup working', 'why isn't voice "
    "responding', 'check my speech-to-speech config'. Returns structured "
    "JSON with passed/warning/error checks and remediation steps."
  ),
  "parameters": {
    "type": "object",
    "properties": {
      "probe": {"type": "boolean", "default": True,
                "description": "Open a 5s WS probe to the configured realtime backend"},
    },
    "required": []
  }
}
```

Handler delegates to the same code as `hermes s2s doctor --json`.

## Consequences

**Positive:**
- 30-second happy path: `pip install hermes-s2s[all] && hermes plugins enable hermes-s2s && hermes s2s setup` then answer prompts.
- Single-command diagnosis when something breaks.
- LLM can self-diagnose voice issues without the user having to know what to check.
- All install/config knobs live in one wizard, one doctor check.

**Negative:**
- More CLI surface to maintain. Mitigation: factor doctor checks into a single registry of `(name, check_fn, remediation_fn)` tuples so adding a new check is one entry.
- WS probe costs money. ~$0.0001 per probe; document.
- Wizard now mutates `.env` AND `config.yaml`. Both edits must be idempotent + reversible. Mitigation: marker-line pattern + a `--dry-run` mode shows the diff before applying.

## Implementation shape

```
hermes_s2s/
  cli.py                    # extend with realtime profiles + doctor subcommand
  doctor.py                 # NEW — check registry + runner
  schemas.py                # add S2S_DOCTOR
  tools.py                  # add s2s_doctor handler
```

## Alternatives considered

- **Auto-set `HERMES_S2S_MONKEYPATCH_DISCORD` without asking** — risky. Monkey-patches should be opt-in even if the wizard makes opt-in trivial. Rejected.
- **Make doctor run automatically on `hermes s2s setup` finish** — yes; setup wizard's "Next steps" block now ends with `Run hermes s2s doctor to verify everything's wired correctly`.
- **Bundle a pre-recorded sample audio for live test** — defer to 0.3.3; doctor probing the backend WS is enough signal for 0.3.2.
