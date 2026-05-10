# HOWTO: Hermes voice mode with hermes-s2s

This is the working reference for how the 0.2.0 integration actually flows end-to-end. The [README](../README.md) gives you the 3-step recipe; this doc explains what each step is doing and what to check when it doesn't.

Source: <https://github.com/baladithyab/hermes-s2s>. Rationale: [ADR-0004](adrs/0004-command-provider-interception.md).

## 0.4.0 — the four modes at a glance

As of 0.4.0, `s2s.mode` is one of exactly **four modes** (up from the
pre-0.4 two-mode world of `cascaded | duplex`). Pick one; every other
knob is a detail under it:

| Mode | What runs where | Latency | Best for |
|---|---|---|---|
| `cascaded` | STT → Hermes LLM → TTS, each stage a discrete provider | ~0.6–1.5 s | Mix-and-match, privacy on one stage, cheapest cloud |
| `pipeline` | Hermes's own tightly-coupled STT→LLM→TTS pipeline (no per-stage swap, but one process, no subprocess overhead) | ~0.4–1.0 s | Lowest-overhead cascaded when you don't need per-stage provider swaps |
| `realtime` | Full duplex WS to Gemini Live / GPT-4o Realtime, audio both ways, Hermes tools round-trip through the bridge | ~0.5–0.7 s | Interactive, barge-in, native voice quality |
| `s2s-server` | WS to your own `streaming-speech-to-speech` (or compatible) server; the whole turn lives there | ~0.25 s on RTX 5090 | Local v6 pipeline, strict privacy, lowest latency |

The legacy `s2s.mode: duplex` boolean is gone. The 0.4.0 config loader
auto-translates it to `realtime` on first load (sentinel guard prevents
re-translation). For a clean rewrite, run:

```bash
python -m hermes_s2s.migrate_0_4            # rewrite in place (creates .bak)
python -m hermes_s2s.migrate_0_4 --dry-run  # show diff, write nothing
python -m hermes_s2s.migrate_0_4 --rollback # restore from .bak
```

### The `/s2s` slash command (Discord, 0.4.0+)

0.4.0 ships a plugin-owned `/s2s` slash command that Hermes registers
when `HERMES_S2S_MONKEYPATCH_DISCORD=1` is set. It's the fastest way
to switch modes in a live Discord session without touching YAML:

```
/s2s                         # show current mode + backend readiness
/s2s mode realtime           # switch mode for this session only
/s2s mode cascaded           # ...and back
/s2s test                    # TTS smoke test — bot speaks a short phrase
/s2s test "hello there"      # ...with specific text
```

`/s2s mode <name>` is session-scoped — it does NOT write to
`config.yaml`. Edit YAML (or re-run `hermes s2s setup`) to make the
choice persistent across restarts.

### Thread mirroring (Discord, 0.4.0+)

When `/voice join` is invoked from inside a **thread**, the bot
reuses that thread for STT transcripts and assistant replies.
Invoked from a plain channel, the bot **auto-creates a public
thread** (auto-archive: 1 day) named from the configured template
and mirrors all transcripts into it. Forum parents are detected and
fall back to the parent channel (no thread auto-create). Both
templates are configurable:

```yaml
s2s:
  voice:
    thread_name_template: "Voice: {user_display_name} ({date})"
    thread_starter_message: "Voice transcripts for this session…"
```

See ADR-0012 and `docs/design-history/research/14-thread-resolution.md`
for the resolver decision table.

### Voice meta-commands (0.4.0+)

With voice mode active, you can control the session hands-free by
saying the wakeword followed by a verb. Defaults:

- **Wakeword:** `hermes` (configurable at `s2s.voice.wakeword`)
- **Verbs:** `new`, `compress`, `title`, `branch`, `stop`, `clear`
  (`resume` is planned for 0.4.1)

Examples (spoken aloud into the VC):

- "hermes new" — start a new conversation turn / reset context
- "hermes compress" — summarize the current session
- "hermes title" — ask Hermes to title the current thread
- "hermes branch" — branch this session into a new thread
- "hermes stop" — cancel the current assistant utterance / tool run
- "hermes clear" — clear the conversation history

Meta-commands are detected by the `MetaCommandSink` wakeword grammar
and dispatched through the gateway's `MetaDispatcher` BEFORE the LLM
sees the transcript, so they're deterministic and never get
misrouted to the model. See ADR-0011.

### Voice persona overlay + prompt-injection defense (0.4.0+)

Voice mode layers a short persona prompt over Hermes's base system
prompt (keeps replies short, natural, 1–3 sentences). The overlay
includes a hard-coded **prompt-injection-defense block** that refuses
overrides of the "ignore previous instructions" / "you are now a
different assistant" family when they appear inside a spoken
transcript.

**Security note:** this is a defense-in-depth layer, not a
replacement for auth. The usual Discord `DISCORD_ALLOWED_USERS`
allow-list still gates *who* can talk to the bot; the persona
overlay defends against a malicious payload *from within* an
allowed user's speech.

See ADR-0013 and `docs/design-history/research/13-persona-overlay.md`
for the prompt-design rationale and the attack taxonomy the
defense targets.

### 3-bucket tool-export policy (0.4.0+)

Voice-mode tools now fall into three explicit buckets:

- **Always-on** — `hermes_meta_*` tools exported to every voice
  session regardless of user toolset config (these back the
  meta-commands above).
- **Opt-in** — standard Hermes tools; exported only if they're in
  the user's configured toolset.
- **Deny-listed** — tools that are dangerous or nonsensical in a
  voice context (e.g. interactive TUI tools). Never exported,
  enforced by a CI fence in `tests/test_tool_export_policy.py`.

ADR-0014 has the bucket definitions; the policy module lives at
`hermes_s2s.voice.tool_bridge`.

## 1. How Hermes voice mode finds providers

When Hermes receives voice input (Discord VC audio frame, Telegram voice note, CLI mic capture), it runs a cascaded STT → LLM → TTS pipeline. For the STT and TTS stages it resolves a **provider** by name from your `~/.hermes/config.yaml` / environment and hands off the actual work.

The important property we rely on: Hermes's voice I/O path does **not** go through the LLM tool registry. It calls into the STT and TTS plumbing directly. That means the "register a tool named `transcribe_audio` and override it" approach — which is what the original Wave 1 plan sketched — would silently not fire in Discord / Telegram / CLI voice mode. See ADR-0004 for the full post-mortem; the short version is: we wire in through Hermes's **command-provider mechanism** instead, because that one *is* on the voice path.

In 0.2.0, `hermes-s2s` ships two console scripts (`hermes-s2s-tts`, `hermes-s2s-stt`) registered via `pyproject.toml` `[project.scripts]`, so they land on `PATH` on `pip install`. Hermes just shells out to them.

## 2. The `tts.providers.<name>: type: command` mechanism (TTS)

Hermes has a first-class "command" TTS provider type. You declare a provider entry with `type: command` and a `command:` template; Hermes writes the input text to a temp file, substitutes `{input_path}` and `{output_path}`, runs the command, then reads the audio file the command produced.

`hermes s2s setup --profile local-all` writes exactly this block:

```yaml
# ~/.hermes/config.yaml
tts:
  provider: hermes-s2s-kokoro
  providers:
    hermes-s2s-kokoro:
      type: command
      command: "hermes-s2s-tts --provider kokoro --voice af_heart --lang-code a --output {output_path} --text-file {input_path}"
      output_format: wav
```

You can verify the shim directly (no Hermes needed):

```bash
echo "hello from kokoro" > /tmp/in.txt
hermes-s2s-tts --provider kokoro --voice af_heart --text-file /tmp/in.txt --output /tmp/out.wav
ffplay /tmp/out.wav   # or: aplay, vlc, mpv
```

`hermes-s2s-tts` supports the following flags (run `hermes-s2s-tts --help` for the canonical list):
`--provider {kokoro|s2s-server}`, `--text` / `--text-file` (one required), `--output PATH` (required), `--voice`, `--lang-code`, `--speed`, `--endpoint` (s2s-server only).

Swapping voices / languages is a config edit — no code change. For other voices see the Kokoro docs; `af_heart` is the American-English default used by `local-all`.

## 3. The `HERMES_LOCAL_STT_COMMAND` env var (STT)

STT uses the symmetric pattern but lives in an environment variable rather than in `config.yaml`. Hermes reads `HERMES_LOCAL_STT_COMMAND` at startup, substitutes `{input_path}` (audio) and `{output_path}` (transcript target), runs the command, then reads the transcript back as UTF-8 text.

`hermes s2s setup` appends (idempotently) to `~/.hermes/.env`:

```bash
# ~/.hermes/.env
HERMES_LOCAL_STT_COMMAND='hermes-s2s-stt --provider moonshine --model tiny --input {input_path} --output {output_path}'
```

Verify the shim directly:

```bash
hermes-s2s-stt --provider moonshine --model tiny --input sample.wav --output /tmp/out.txt
cat /tmp/out.txt
```

`hermes-s2s-stt` flags: `--provider {moonshine|s2s-server}`, `--input PATH` (required), `--output PATH` (optional — defaults to `<input>.txt` next to the audio), `--model {tiny|base}`, `--device {cuda|cpu}`, `--endpoint` (s2s-server only).

`--device cuda` is advisory — Moonshine falls back to CPU on machines without a GPU. `tiny` is 27M params; `base` is 61M (better accuracy, ~2× slower).

## 4. Troubleshooting

**"Voice mode joins VC / starts CLI mic but doesn't transcribe anything."** First, confirm Hermes actually sees the env var: run `hermes` in the same shell after `source ~/.hermes/.env` (or let your shell auto-load it) and check the Hermes startup log for a line acknowledging `HERMES_LOCAL_STT_COMMAND`. Then try the shim directly on a known-good WAV (`hermes-s2s-stt --provider moonshine --input sample.wav --output /tmp/out.txt`). If that works standalone but not through Hermes, the most common cause is the env var being set in your terminal but not in the environment Hermes was launched from (systemd unit, tmux pane started before the `.env` change, etc.).

**"TTS produces a silent / zero-byte file."** Run the TTS shim directly and inspect the file. If it's empty, check stderr — `hermes-s2s-tts` prints `hermes-s2s-tts error: <reason>` on failure and exits non-zero. Common causes: missing `kokoro` install (`pip install "hermes-s2s[local-all]"`), missing `espeak-ng` on Linux (Kokoro's g2p backend), or an invalid `--voice` name.

**"The shim hangs."** In 0.2.0, every call loads the full model into a fresh Python subprocess, which can take several seconds on first run (ONNX weights download for Moonshine) and ~200–400ms of warm cold-start after. If a call seems hung beyond ~30s, it's probably first-run model download — check `~/.cache/` size and your network. The 0.2.1 daemon mode below fixes this permanently.

**"`s2s-server` provider rejects my `ws://` endpoint."** Correct and expected in 0.2.0 — the provider is HTTP-only (`/asr`, `/tts` REST). The WebSocket pipeline mode ships in 0.3.0. See [ADR-0004](adrs/0004-command-provider-interception.md) for the integration rationale and the roadmap for the WS plan.

**"I'm using a non-Hermes-compatible STT / TTS and want to plug it in."** Point `HERMES_LOCAL_STT_COMMAND` / `tts.providers.<name>.command` at your own script. The shims are not magic — any command that honors the `{input_path}` / `{output_path}` contract works. The `hermes-s2s-*` shims are just a pre-packaged, tested path for the local stack.

## 4.5. Diagnosing problems with `hermes s2s doctor`

Added in 0.3.2. A single command that walks every layer of the stack —
config file, Python deps, system deps, API keys, Hermes integration, and an
optional live WS probe to the configured realtime backend — and tells you
exactly what's missing, with a copy-pastable fix.

```
$ hermes s2s doctor
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
Overall: 1 warning, 1 error.
Required for realtime+Discord to work: install kokoro (warning above is optional).
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Reading each category

**Configuration.** The doctor loads your `~/.hermes/config.yaml` and checks
that `s2s.mode` is one of `cascaded | realtime | s2s-server`, and that the
matching provider block exists. If you see `✗ s2s.mode missing`, run
`hermes s2s setup --profile <something>` — the wizard writes a full block.

**Python dependencies.** Imports each required module. `✗ kokoro NOT
installed` means you picked a cascaded profile that uses Kokoro TTS but
didn't install the `[kokoro]` extra; follow the inline remediation.
Realtime-only setups don't need Moonshine or Kokoro — the doctor understands
your mode and only flags what your config actually needs.

**System dependencies.** Shells out to find `ffmpeg`, `libopus` (via
`ldconfig -p` on Linux / `dpkg`/`brew` where relevant), and `espeak-ng`.
See [INSTALL.md](https://github.com/baladithyab/hermes-s2s/blob/main/docs/INSTALL.md#system-dependencies)
for per-OS install commands.

**API keys.** Checks presence AND plausible length. An API key that's set
to the literal string `"YOUR_KEY_HERE"` fails the length heuristic with a
helpful warning. Keys aren't logged — only their length is reported.

**Hermes integration.** The realtime bridge is a monkey-patch over
discord.py's `AudioSink`, opt-in via `HERMES_S2S_MONKEYPATCH_DISCORD=1`.
The doctor checks that flag + the Discord bot credentials that Hermes
itself needs. If the flag is missing but you picked a realtime mode, this
fails loud — it's the #1 "installed everything but voice is silent" cause.

**Backend connectivity.** Opens a 5 s WebSocket probe to the configured
realtime backend (Gemini Live or OpenAI Realtime), waits for the first
server event, closes. Skipped with `--no-probe`. The probe costs ~$0.0001
per run (one billable session open). If this fails, the error message
contains the raw WebSocket response — usually a 401 (bad key) or a 403
(key lacks realtime scope).

### Remediation walkthrough

1. Run `hermes s2s doctor`. Every red `✗` is a blocker; yellow `⚠` is
   "this works but you should know".
2. Walk top to bottom — earlier checks block later ones (no point probing
   the backend if the API key is missing).
3. Apply the one-line remediation printed next to each check.
4. Re-run `hermes s2s doctor`. Iterate until all-green.
5. Restart `hermes gateway` (env vars are only re-read on process start)
   and `/voice join` in Discord.

### CI + LLM use

- `hermes s2s doctor --json` emits the same report as structured JSON for
  scripts and CI pipelines. Exit code is 0 on all-green, 1 otherwise.
- `hermes s2s doctor --no-probe` skips the WS probe. Use this in CI to
  avoid per-run charges, and on laptops offline.
- The LLM can also run the doctor directly via the `s2s_doctor` tool —
  just ask Hermes "is my voice setup working?" and it will invoke the
  tool, read the JSON, and explain the remediation in plain English.

## 5. Daemon mode (0.2.1 preview)

The per-call model-load cost is the only real wart of the 0.2.0 design. ADR-0004 already specifies the fix: a long-lived daemon that loads Moonshine and Kokoro once and serves subsequent calls over a Unix domain socket.

The planned shape (subject to change — this is a 0.2.1 preview, not shipping today):

```bash
# Start once at login / via systemd --user
hermes s2s serve --daemon    # listens on ~/.hermes/run/s2s.sock

# No config changes needed — the shims auto-detect the socket and use it.
# If the daemon isn't running, shims fall back to in-process model load,
# so the daemon is purely an optimization.
```

Expected impact: cold-start per call drops from ~300ms to ~5ms. Until 0.2.1 lands, the 0.2.0 command-provider path is functional but pays the subprocess cost every turn — see ADR-0004 for the full trade-off discussion.

---

See also: [ADR-0004](adrs/0004-command-provider-interception.md) (integration rationale), the hermes-s2s issue tracker at <https://github.com/baladithyab/hermes-s2s/issues>, and the wave 0.2.0 plan at [`docs/plans/wave-0.2.0-command-shims.md`](plans/wave-0.2.0-command-shims.md).
