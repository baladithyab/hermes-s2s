# 0.4.0 Voice Mode Rearchitecture — Wave Plan

> Execution plan for the 0.4.0 four-mode rearchitecture, voice meta-commands,
> thread co-management, and realtime tool-export policy.
> 6 waves; 4 parallel batches + 2 sequential.

**Branch:** `feat/0.4.0-rearchitecture` off `main` (post-v0.3.9).

## Scope refinements (post-Phase-5 review)

The original plan was sized for ~5h wall-clock and ~$15 compute. Trimmed to
~$8-10 and ~3.5h wall-clock by deferring these items to 0.4.1 — none of
which the user asked for explicitly:

- **W2a M2.2** (Telegram inline-keyboard fallback) — DEFERRED. User is
  Discord-primary; Telegram users keep the 0.3.x experience until 0.4.1.
- **W2a M2.3** (CLI `/s2s` command setting next-join default) — DEFERRED.
  CLI has no VC; less than zero P0-urgency.
- **W4b M4.3** drops `hermes_meta_resume_session` from the 5-tool family —
  reduced to 4 tools. The user-pick disambiguation flow is complex and
  resume-by-voice-only is the lowest-value meta-command. Deferred to 0.4.1.
- **W4b M4.4** (3-bucket tool-export policy) — REDUCED to 2 buckets in
  0.4.0: `default_exposed` and `deny`. The `ask` bucket (confirmation-wrapped
  tool calls) needs a synchronous user-prompt-during-realtime UX that's not
  designed yet. 0.4.0 is fail-closed on the hard-deny list, conservative
  default-exposed list. Deferred 3rd-bucket to 0.4.1.
- **W5a** (migration) — SIMPLIFIED. Auto-translate `s2s.mode` →
  `s2s.voice.default_mode` runs on first config-load (in `config/__init__.py`)
  with one-time deprecation warning. NO separate `python -m hermes_s2s.migrate_0_4`
  script, NO `--dry-run` / `--rollback` flags. Backup file written next to
  config; user can manually `mv config.yaml.bak.0_4 config.yaml` if needed.
- **W6a M6.3** (README badge) — DEFERRED. Feature highlights only.

These changes are reflected in the wave specs below as `[REFINED]` notes.

**ADRs in scope:** 0010 (sub-block unwrap, already shipped in 0.3.9 — codified
post-hoc), 0011 (plugin-owned `/s2s` slash command), 0012 (voice thread
co-management hooks), 0013 (four-mode VoiceSession), 0014 (voice meta + tool
export).

**Research artifacts:** docs/research/10–15.md (six docs, ~1400 lines).

---

## Goals (what 0.4.0 ships)

1. **`/s2s` slash command on Discord** with native 4-choice dropdown picking
   the voice mode for the next `/voice join`. Telegram inline keyboard
   fallback. CLI sets next-join default.
2. **Per-VC mode selection** — slash override, channel override, guild
   override, config default, hard default. ModeRouter precedence per ADR-0013.
3. **Four `VoiceSession` classes** behind a `VoiceSessionFactory`, all using
   `AsyncExitStack`-based unified lifecycle (Cascaded / Pipeline / Realtime /
   S2SServer). Plugin-owned dispatch.
4. **Thread co-management** — invoking `/voice join` from a thread reuses it;
   from a plain channel auto-creates a public thread (60-min auto-archive,
   `🎤 {user} — {date}` template). User STT and ARIA TTS replies mirrored as
   text. Realtime mode emits transcripts via the new `_transcript_sink` at
   `audio_bridge.py:616`.
5. **MetaCommandSink** for M1/M2 — wakeword-anchored regex grammar matching
   "Hey ARIA, start a new session" → `/new`, etc., before STT-to-LLM.
6. **`hermes_meta_*` tool family** for M3/M4 — 5 JSON-Schema tools the
   realtime LLM can call (`new_session`, `title_session`, `compress_context`,
   `branch_session`, `resume_session_query`). `resume <name>` is gateway-direct
   only — LLM returns a list, user picks.
7. **Tool-export bucketing** — `default_exposed` / `ask` / `deny` for the
   realtime backend. Hard-deny: terminal, patch, write_file, computer_use,
   delegate_task, cronjob, ha_call_service, kanban writes, browser interactive
   ops.
8. **Migration** — `s2s.mode` → `s2s.voice.default_mode` auto-translated on
   first run with one-time deprecation warning. Wizard non-destructive.
   `python -m hermes_s2s.migrate_0_4` script with `--dry-run` and `--rollback`.

## Non-goals

- Upstream PRs to Hermes core (deferred to 0.4.1 — `voice:thread_resolve` and
  `voice:transcript` hook events, and `register_command(options=…)` extension).
  0.4.0 ships entirely as a plugin update with the existing monkey-patch seam.
- Multi-VC per guild concurrency (today's single-slot constraint preserved).
- 0.4.0 only ships Discord. Telegram fallback for `/s2s` is in scope but voice
  mode itself remains Discord-only as in 0.3.x.

---

## File ownership

| File                                                   | Wave   | Notes                                                                                      |
| ------------------------------------------------------ | ------ | ------------------------------------------------------------------------------------------ |
| `hermes_s2s/voice/__init__.py`                         | W1a    | NEW package — re-exports VoiceMode, ModeRouter, VoiceSessionFactory                        |
| `hermes_s2s/voice/modes.py`                            | W1a    | NEW — VoiceMode enum, ModeSpec dataclass, ModeRouter                                       |
| `hermes_s2s/voice/sessions.py`                         | W1a    | NEW — VoiceSession protocol, base class, AsyncExitStack-based lifecycle                    |
| `hermes_s2s/voice/sessions_cascaded.py`                | W1b    | NEW — CascadedSession (no-op shim, lets Hermes default loop run)                           |
| `hermes_s2s/voice/sessions_pipeline.py`                | W1b    | NEW — CustomPipelineSession (Moonshine + Kokoro via 0.2.0 command-providers)               |
| `hermes_s2s/voice/sessions_realtime.py`                | W1b    | NEW — RealtimeSession (wraps audio_bridge + tool_bridge)                                   |
| `hermes_s2s/voice/sessions_s2s_server.py`              | W1b    | NEW — S2SServerSession (existing pipeline backend)                                         |
| `hermes_s2s/voice/factory.py`                          | W1c    | NEW — VoiceSessionFactory.build(spec, vc, adapter, hermes_ctx)                             |
| `hermes_s2s/voice/capabilities.py`                     | W1c    | NEW — ModeRequirements + capability-gate logic                                             |
| `hermes_s2s/_internal/discord_bridge.py`               | W1c    | EDIT — install factory + delegate to it from monkey-patched join_voice_channel             |
| `tests/test_voice_modes.py`                            | W1a/b/c| NEW — unit + integration coverage for ModeRouter, factory, all 4 sessions                  |
| `hermes_s2s/voice/slash.py`                            | W2a    | NEW — Discord /s2s slash command + Telegram inline keyboard handler                        |
| `hermes_s2s/voice/cli_command.py`                      | W2a    | NEW — CLI /s2s command (sets next-join default)                                            |
| `hermes_s2s/_internal/discord_bridge.py`               | W2a    | EDIT — install_s2s_command(ctx) called from register()                                     |
| `tests/test_slash_command.py`                          | W2a    | NEW — unit tests for /s2s on all three platforms                                           |
| `hermes_s2s/voice/threads.py`                          | W3a    | NEW — ThreadResolver + thread auto-create + ThreadParticipationTracker integration         |
| `hermes_s2s/voice/transcript.py`                       | W3a    | NEW — TranscriptMirror with token-bucket rate limiter                                      |
| `hermes_s2s/_internal/audio_bridge.py`                 | W3b    | EDIT — line 616 transcript dropping → call into _transcript_sink                           |
| `hermes_s2s/_internal/discord_bridge.py`               | W3b    | EDIT — wire ThreadResolver into the join_voice_channel monkey-patch BEFORE source snapshot |
| `tests/test_threads.py`                                | W3a    | NEW — thread auto-create, reuse, mirroring, rate limit                                     |
| `hermes_s2s/voice/meta.py`                             | W4a    | NEW — MetaCommandSink with wakeword-anchored regex grammar                                 |
| `hermes_s2s/voice/meta_dispatcher.py`                  | W4a    | NEW — meta-command → gateway action dispatcher                                             |
| `hermes_s2s/voice/meta_tools.py`                       | W4b    | NEW — hermes_meta_* JSON-Schema tool definitions (5 tools)                                 |
| `hermes_s2s/_internal/tool_bridge.py`                  | W4b    | EDIT — extend with build_tool_manifest() bucketing default/ask/deny                        |
| `hermes_s2s/voice/persona.py`                          | W4b    | NEW — voice persona overlay (fenced, not merged into PERSONA.md)                           |
| `tests/test_meta.py`                                   | W4a/b  | NEW — wakeword grammar, false-positive guard, JSON schema validation                       |
| `hermes_s2s/migrate_0_4.py`                            | W5a    | NEW — `python -m hermes_s2s.migrate_0_4` with --dry-run / --rollback                       |
| `hermes_s2s/cli.py`                                    | W5a    | EDIT — wizard additive merge instead of overwrite; one-time deprecation warning            |
| `tests/test_migrate.py`                                | W5a    | NEW — migration script unit tests                                                          |
| `docs/HOWTO-VOICE-MODE.md`                             | W6a    | EDIT — rewrite with 0.4.0 four-mode UX                                                     |
| `docs/HOWTO-REALTIME-DISCORD.md`                       | W6a    | EDIT — update to /s2s + thread mirroring                                                   |
| `README.md`                                            | W6a    | EDIT — feature highlights + version badge                                                  |
| `hermes_s2s/skills/hermes-s2s/SKILL.md`                | W6a    | EDIT — embedded skill update for 0.4.0 commands                                            |
| `hermes_s2s/__init__.py` `pyproject.toml` `plugin.yaml`| W6b    | EDIT — version bump to 0.4.0, smoke test                                                   |

**Rule:** No two waves in the same parallel batch may write the same file.
W1c edits `discord_bridge.py`; W2a and W3b also edit it. They're in
**different sequential batches** to satisfy the rule. Same with `tool_bridge.py`
(only W4b touches it).

---

## Wave grouping

| Batch | Waves      | Parallel? | What                                                                        |
| ----- | ---------- | --------- | --------------------------------------------------------------------------- |
| B1    | W1a W1b W1c| W1a / W1b parallel; W1c sequential after both | Mode foundation: enum, sessions, factory                  |
| B2    | W2a        | solo      | Slash commands (Discord /s2s, Telegram inline kbd, CLI)                     |
| B3    | W3a W3b    | sequential (W3a first) | Thread auto-create + transcript mirror                          |
| B4    | W4a W4b    | parallel  | Meta-commands (sink + dispatcher + tools + persona)                         |
| B5    | W5a        | solo      | Migration                                                                   |
| B6    | W6a W6b    | sequential (W6a first) | Docs + version bump                                              |

---

## Acceptance test (run after every commit)

```bash
cd /mnt/e/CS/github/hermes-s2s
source .venv/bin/activate 2>/dev/null || source venv/bin/activate
python -m py_compile $(git diff --name-only HEAD~1 HEAD | grep '\.py$')
pytest tests/ -q  # must be all green
hermes_s2s_smoke=1 python -c "import hermes_s2s; print(hermes_s2s.__version__)"
```

**Hermes runtime smoke (W3 onward):**
```bash
# In one terminal:
hermes gateway run
# In another:
discord_e2e_smoke.sh  # joins VC, says "hello", expects English reply, leaves
```

---

## Rollback plan

Each wave produces 1-N commits. Wave-bad → `git revert <wave-merge-commit>`.
Migration (W5) has explicit `--rollback` callback that reverses the
`s2s.mode` ↔ `s2s.voice.default_mode` translation. The branch
`feat/0.4.0-rearchitecture` is squash-merged to `main` only after all 6
waves land green and Phase 8 reviewers sign off.

---

## Token / wall-clock budget

- Per-wave subagent: ~120k input + ~25k output (large because 0.4.0 spans many
  files with cross-references), ~3-6 minutes
- Per-wave Phase-7 reviewer: 3 reviewers × ~80k input + ~6k output = ~270k
  total, ~3-5 minutes
- Total estimate: 6 waves × (~280k input + ~31k output) for execution +
  6 waves × ~270k for review = **~1.65M input tokens, ~210k output tokens**
- Cost: ~$8-15 in OpenRouter + Bedrock (orchestrator stays cheap because most
  tokens are subagent-side)

---

## Commit discipline (enforced in every subagent prompt)

- One commit per logical task. Imperative-mood subjects.
- Always `git add <explicit paths>` — never `git add -A`.
- Pin authorship: `git -c user.email=baladithyab@users.noreply.github.com
  -c user.name=baladithyab commit ...`
- On `.git/index.lock` race in parallel batches: wait 1-2s and retry.
  If still failing AFTER `git status` confirms no in-flight op, then `rm
  .git/index.lock` and retry.

---

## WAVE 1a — VoiceMode enum + ModeRouter + VoiceSession protocol

**Subagent:** Claude Opus 4.7 (orchestrator-default; medium-complexity new code)
**File ownership:** `hermes_s2s/voice/__init__.py`, `hermes_s2s/voice/modes.py`,
`hermes_s2s/voice/sessions.py`, parts of `tests/test_voice_modes.py` (router
+ protocol tests only).

### Tasks

#### M1.1: VoiceMode + ModeSpec + ModeRouter

**Spec (research-15 §1, ADR-0013 §2):**
- New `VoiceMode(StrEnum)` with 4 variants: `CASCADED`, `PIPELINE`, `REALTIME`,
  `S2S_SERVER` (note underscore for S2S_SERVER to satisfy Python identifier
  rules; serialized value is `"s2s-server"` via `_value_`).
- `ModeSpec` frozen dataclass: `mode: VoiceMode`, `provider: str | None`,
  `options: dict`.
- `ModeRouter.resolve(...)` with 6-level precedence:
  1. explicit slash hint, 2. `HERMES_S2S_VOICE_MODE` env, 3. `s2s.voice.channel_overrides[chan_id]`,
  4. `s2s.voice.guild_overrides[guild_id]`, 5. `s2s.voice.default_mode`, 6. `"cascaded"`.
- Normalization: lowercase, strip whitespace, accept `s2s-server` AND
  `s2s_server` AND `s2s server`. Reject typos with explicit error
  ("unknown mode 'realitme', valid modes: ..."). NOT a quiet fall-through.
- Fail-closed-on-explicit-request: if slash mode is given but capability gate
  fails (W1c handles that piece), `resolve` returns the requested ModeSpec
  anyway; capability check happens in factory.

#### M1.2: VoiceSession protocol + AsyncExitStackBaseSession

**Spec (research-15 §2, ADR-0013 §4):**
- `VoiceSession(Protocol)` with `mode: VoiceMode`, async `start()`, async
  `stop()`, `meta_command_sink: MetaCommandSink | None`.
- `AsyncExitStackBaseSession` concrete base class managing an
  `_exit_stack: AsyncExitStack`. Subclasses register cleanup via
  `await self._stack.enter_async_context(...)`. `stop()` awaits
  `self._stack.aclose()` and is idempotent.
- State machine: `CREATED → STARTING → RUNNING → STOPPING → STOPPED`. Single
  `_state` attribute, transition checks raise on invalid transitions.

**Acceptance:**
- A1: `pytest tests/test_voice_modes.py::test_mode_router_precedence -q` exits 0
- A2: `pytest tests/test_voice_modes.py::test_mode_router_rejects_typo -q` exits 0
- A3: `pytest tests/test_voice_modes.py::test_mode_router_normalizes_aliases -q` exits 0
- A4: `pytest tests/test_voice_modes.py::test_session_stop_idempotent -q` exits 0
- A5: `python -c "from hermes_s2s.voice import VoiceMode, ModeRouter, VoiceSession; print('ok')"` outputs `ok`
- A6: `python -m py_compile hermes_s2s/voice/{__init__.py,modes.py,sessions.py}` exits 0

### Reference docs subagent reads
- `docs/research/15-modes-and-meta-deep-dive.md` §1, §2 — implementation spec
- `docs/adrs/0013-four-mode-voicesession.md` — full
- `docs/research/12-voice-mode-rearchitecture.md` §3 — pseudocode references

---

## WAVE 1b — Four VoiceSession concrete classes

**Subagent:** Claude Sonnet 4.6 (parallel with W1a; different file ownership)
**File ownership:** `hermes_s2s/voice/sessions_cascaded.py`,
`hermes_s2s/voice/sessions_pipeline.py`, `hermes_s2s/voice/sessions_realtime.py`,
`hermes_s2s/voice/sessions_s2s_server.py`, parts of `tests/test_voice_modes.py`
(per-session tests).

### Tasks

#### M1.3 CascadedSession
No-op session that lets Hermes core's native voice loop run unchanged.
`start()` records that we are in cascaded mode (for observability) and
returns. `stop()` is a no-op. Used as the safe default.

#### M1.4 CustomPipelineSession
Installs Moonshine STT + Kokoro TTS as command-providers per ADR-0004.
Already works in 0.3.x via env vars; W1b wraps it as a proper VoiceSession
with the AsyncExitStack pattern. `start()` exports
`HERMES_LOCAL_STT_COMMAND` and `HERMES_LOCAL_TTS_COMMAND` for the duration
of the session, restores prior values on `stop()`.

#### M1.5 RealtimeSession
Wraps the existing `RealtimeAudioBridge` + `HermesToolBridge` in a session
shape. `start()` constructs the bridge, awaits `backend.connect()` BEFORE
spawning input/output pump tasks (regression-fence per memory: silent-bot
P0 in 0.3.1). `stop()` cancels pumps via `_exit_stack.aclose()` then
disconnects backend.

#### M1.6 S2SServerSession
Wraps existing `s2s_server` pipeline backend in session shape.

**Acceptance:**
- A1: `pytest tests/test_voice_modes.py::test_realtime_session_calls_connect_before_pumps -q` exits 0
- A2: `pytest tests/test_voice_modes.py::test_pipeline_session_restores_env -q` exits 0
- A3: `pytest tests/test_voice_modes.py::test_cascaded_session_is_noop -q` exits 0
- A4: `pytest tests/test_voice_modes.py::test_session_failed_start_cleans_up -q` exits 0 (start() partway → stop() removes everything)

---

## WAVE 1c — VoiceSessionFactory + capability gate + bridge wiring

**Subagent:** Claude Opus 4.7 (sequential after W1a + W1b — needs both to land)
**File ownership:** `hermes_s2s/voice/factory.py`,
`hermes_s2s/voice/capabilities.py`,
`hermes_s2s/_internal/discord_bridge.py` (the join_voice_channel monkey-patch
EDIT — replace the inline RealtimeAudioBridge construction with a
`factory.build()` call).

### Tasks

#### M1.7 ModeRequirements + capability-gate
Per ADR-0013 §5: each mode declares its requirements. If gate fails:
- Slash-explicit mode: raise CapabilityError, refuse VC join, post
  user-friendly message ("realtime mode needs GEMINI_API_KEY; falling back
  not allowed because you explicitly asked for realtime").
- Config-default mode: warn + fall back to cascaded with spoken on-join
  notice.

#### M1.8 VoiceSessionFactory
Per ADR-0013 §3: resolves spec → checks capability → constructs session →
registers in `adapter._s2s_sessions[(guild_id, channel_id)]`.

#### M1.9 discord_bridge integration
Replace lines 333-360 of `_internal/discord_bridge.py` (the existing inline
bridge construction) with delegated factory call. Preserve the v0.3.9
sub-block-unwrap behavior INSIDE the factory's RealtimeSession path so
existing user configs still work.

**Acceptance:**
- A1: `pytest tests/test_voice_modes.py -q` exits 0 (full file green)
- A2: `pytest tests/test_realtime_session.py -q` (existing 0.3.x tests) exits 0
  — NO regression in realtime mode
- A3: Live smoke (manual): user joins VC with `s2s.voice.default_mode: realtime`, English reply received

---

## WAVE 2a — `/s2s` slash command + Telegram inline keyboard + CLI command

**Subagent:** Claude Opus 4.7
**File ownership:** `hermes_s2s/voice/slash.py`,
`hermes_s2s/voice/cli_command.py`,
`hermes_s2s/_internal/discord_bridge.py` (just an `install_s2s_command(ctx)`
call from the existing `register()`),
`tests/test_slash_command.py`.

### Tasks

#### M2.1 Discord /s2s with @app_commands.choices
Per research-13 §4 + ADR-0011: dedicated `/s2s` Discord slash with 4-choice
dropdown. Sets a per-(guild_id, channel_id) override that takes effect on
the NEXT `/voice join`. Persists to `~/.hermes/.s2s_mode_overrides.json`.

#### M2.2 Telegram inline keyboard fallback
Same intent for Telegram users. Sends `InlineKeyboardMarkup` with 4 buttons,
`callback_data="s2s:mode:<value>"`. Plus `/s2s <mode>` text-arg path for
power users.

#### M2.3 CLI /s2s command
In CLI mode there's no VC, but the user might still set the next-join
default. CLI `/s2s mode:realtime` writes the override and prints
"Next /voice join will use realtime mode."

**Acceptance:**
- A1: `pytest tests/test_slash_command.py -q` exits 0
- A2: `grep -F '@app_commands.choices' hermes_s2s/voice/slash.py` exits 0
- A3: User invokes `/s2s` on Discord → sees 4-option dropdown (manual smoke)

---

## WAVE 3a — ThreadResolver + TranscriptMirror

**Subagent:** Gemini 3.1 Pro (parallel-safe with W3b only AFTER W3a finishes —
W3b needs `_transcript_sink` from W3a)
**File ownership:** `hermes_s2s/voice/threads.py`,
`hermes_s2s/voice/transcript.py`, `tests/test_threads.py`.

### Tasks

#### M3.1 ThreadResolver
Per research-14 §5 + ADR-0012: resolves `(adapter, event)` → target thread:
- Invoked-in-thread → return existing thread_id
- Invoked-in-channel → call `parent.create_thread(name=template,
  type=public_thread, auto_archive_duration=60)`. Template:
  `🎤 {user.display_name} — {date:%Y-%m-%d %H:%M}` from
  `s2s.voice.thread_name_template` config.
- Mark on `adapter._threads.mark(new_thread.id)` so follow-ups don't
  need @mention.
- Fallback if parent is forum (can't create plain thread): defer to
  Hermes's existing `_send_to_forum` path, return None.

#### M3.2 TranscriptMirror with token-bucket
- Token bucket: 5 ops / 5s / channel (well under Discord 5/2s rate limit).
- Format: `**[Voice]** @{user}: {text}` for user STT;
  `**[Voice]** ARIA: {text}` for ARIA reply. Single message per utterance
  in 0.4.0 (rolling-edit deferred to 0.4.1).
- Async, fire-and-forget; failures logged not raised.

**Acceptance:**
- A1: `pytest tests/test_threads.py::test_resolver_reuses_existing_thread -q` exits 0
- A2: `pytest tests/test_threads.py::test_resolver_creates_thread_in_channel -q` exits 0
- A3: `pytest tests/test_threads.py::test_mirror_rate_limit -q` exits 0 (16 sends in 1s → only 5 land)
- A4: `pytest tests/test_threads.py::test_mirror_handles_send_failure -q` exits 0

---

## WAVE 3b — Realtime transcript plumbing + bridge wire-up

**Subagent:** Claude Opus 4.7 (sequential after W3a)
**File ownership:** `hermes_s2s/_internal/audio_bridge.py` (line ~616 EDIT),
`hermes_s2s/_internal/discord_bridge.py` (resolver invocation in
join_voice_channel monkey-patch BEFORE the source snapshot).

### Tasks

#### M3.3 audio_bridge.py:616 transcript plumb-through
Per research-14 §3: the comment "transcript_*: ignored for 0.3.1" replaced
by:
```python
if self._transcript_sink:
    role = "user" if event.type == "transcript_partial_user" else "assistant"
    self._transcript_sink(role=role, text=event.text, final=event.final)
```
`_transcript_sink` is set by `discord_bridge._install_bridge_on_adapter`
when it has access to the runner's hooks bus.

#### M3.4 Resolver invocation in monkey-patch
Inside the wrapped `join_voice_channel`:
1. Call `ThreadResolver.resolve(adapter, event, voice_channel)` — returns
   `target_thread_id`.
2. Mutate `event.source.thread_id = target_thread_id` and
   `event.source.chat_type = "thread"` BEFORE Hermes's runner snapshots it.
3. Hermes's existing transcript-mirror at `run.py:9298-9301` automatically
   sends to the thread (cascaded mode is FREE).
4. For realtime mode, plug `TranscriptMirror.send` into
   `RealtimeSession`'s `_transcript_sink`.

**Acceptance:**
- A1: `pytest tests/test_threads.py -q` exits 0 (full file)
- A2: `grep -F '_transcript_sink' hermes_s2s/_internal/audio_bridge.py` exits 0
- A3: Live smoke: user invokes `/voice join` from #general → new thread
  appears, says "hello" in VC → transcript appears in thread within 2s

---

## WAVE 4a — MetaCommandSink + meta_dispatcher

**Subagent:** DeepSeek V4 Pro (parallel with W4b)
**File ownership:** `hermes_s2s/voice/meta.py`,
`hermes_s2s/voice/meta_dispatcher.py`, parts of `tests/test_meta.py` (regex
+ false-positive tests).

### Tasks

#### M4.1 MetaCommandSink — wakeword-anchored regex grammar
Per research-15 §3 + ADR-0014:
- Wakeword (configurable): `s2s.voice.wakeword: "hey aria"`.
- Patterns (compiled, case-insensitive, anchored after wakeword):
  - `^(start|begin|open) (a |an )?new (session|chat|conversation)$` → `/new`
  - `^(continue|resume) (the |a )?(session|chat) (named |called |about )?(?P<query>.{1,80})$` → `/resume <query>`
  - `^(compress|condense|summarize) (the |my )?context$` → `/compress`
  - `^(title|name) (this|the) (session|chat) (as |to )?(?P<title>.{1,80})$` → `/title <title>`
  - `^(branch|fork) (off |from )?(here|now|this point)$` → `/branch`
- Filler-token tolerance: ≤3 fillers ("um", "uh", "like", "you know") tolerated between wakeword and verb.
- 80-char capture cap on user-supplied args.
- Confidence: stable rules, no LLM matcher.

#### M4.2 meta_dispatcher
Maps matched commands to gateway actions. Calls into Hermes runner's
existing `process_command()` via the adapter's runner reference. Earcon for
`/new`, `/title`, `/branch` (cheap verbs); spoken confirmation for `/compress`
("Got it, compressing context.") and `/resume` (lists matches, "I found 3
sessions, say one or two or three").

**Acceptance:**
- A1: `pytest tests/test_meta.py::test_grammar_matches_canonical_phrasings -q` exits 0
- A2: `pytest tests/test_meta.py::test_grammar_rejects_substring_false_positive -q` exits 0
- A3: `pytest tests/test_meta.py::test_grammar_requires_wakeword -q` exits 0
- A4: `pytest tests/test_meta.py::test_dispatcher_calls_process_command -q` exits 0

---

## WAVE 4b — hermes_meta_* tools + tool-export bucket + voice persona

**Subagent:** Claude Sonnet 4.6 (parallel with W4a)
**File ownership:** `hermes_s2s/voice/meta_tools.py`,
`hermes_s2s/_internal/tool_bridge.py` (extend with `build_tool_manifest`),
`hermes_s2s/voice/persona.py`, parts of `tests/test_meta.py` (tool-schema +
tool-export tests).

### Tasks

#### M4.3 5 hermes_meta_* JSON-Schema tool definitions
Per research-15 §4: paste the 5 tools verbatim. Includes
`hermes_meta_resume_session` returning `action_required: "user_pick"` to
mitigate the LLM-picks-wrong-session data hazard.

#### M4.4 build_tool_manifest in tool_bridge.py
Per ADR-0014 §3: `build_tool_manifest(enabled_toolsets, mode) -> list[ToolSchema]`
returning the merged manifest of:
- Hermes core tools FILTERED by 3-bucket policy:
  - `default_exposed`: web_search, vision_analyze, read_file, search_files,
    text_to_speech, memory, session_search, browser_navigate (RO),
    ha_state_read, etc.
  - `ask`: terminal (RO commands like ls/cat), browser_navigate (with prompt),
    ha_state_read (sensitive devices)
  - `deny`: terminal (write), patch, write_file, computer_use, delegate_task,
    cronjob, ha_call_service, kanban writes, browser interactive ops
- Plus the 5 hermes_meta_* tools (always exposed).

#### M4.5 Voice persona overlay
`<!-- VOICE_OVERLAY_BEGIN -->\n...\n<!-- VOICE_OVERLAY_END -->` appended to
system_prompt at session construction. Default text:
"You are speaking through a voice channel. Keep replies short — 1 to 3
sentences. Avoid markdown, bulleted lists, and code blocks." User overrides
via `s2s.voice.persona`.

**Acceptance:**
- A1: `pytest tests/test_meta.py::test_meta_tool_schemas_validate -q` exits 0
- A2: `pytest tests/test_meta.py::test_tool_manifest_excludes_denied -q` exits 0
- A3: `pytest tests/test_meta.py::test_voice_persona_overlay_appended -q` exits 0
- A4: `grep -F 'VOICE_OVERLAY_BEGIN' hermes_s2s/voice/persona.py` exits 0

---

## WAVE 5a — Migration script

**Subagent:** Claude Opus 4.7 (sequential after B4)
**File ownership:** `hermes_s2s/migrate_0_4.py`, `hermes_s2s/cli.py` (wizard
additive merge), `tests/test_migrate.py`.

### Tasks

#### M5.1 migrate_0_4.py
Per research-15 §8: `python -m hermes_s2s.migrate_0_4` reads
`~/.hermes/config.yaml`, translates `s2s.mode` → `s2s.voice.default_mode`
(if old-style config detected), preserves all other fields, writes to a
sibling backup first (`config.yaml.bak.0_4_<timestamp>`), then atomically
swaps.
Flags:
- `--dry-run`: print diff, don't write
- `--rollback`: restore most recent `.bak.0_4_*` and remove the migrated file
- (default = apply)

#### M5.2 Wizard non-destructive
Per research-15 §8: `hermes s2s setup` no longer overwrites; instead deep-merge
into existing config. Wizard flag `--reset` for the old destructive behavior.
Sentinel-gated one-time deprecation warning when `s2s.mode` is detected
(stored in `~/.hermes/.s2s_migrated`).

**Acceptance:**
- A1: `pytest tests/test_migrate.py -q` exits 0
- A2: `python -m hermes_s2s.migrate_0_4 --dry-run` on a 0.3.x config prints
  the new schema diff and exits 0
- A3: `python -m hermes_s2s.migrate_0_4 --rollback` restores prior config

---

## WAVE 6a — Documentation

**Subagent:** Claude Sonnet 4.6
**File ownership:** `docs/HOWTO-VOICE-MODE.md`,
`docs/HOWTO-REALTIME-DISCORD.md`, `README.md`,
`hermes_s2s/skills/hermes-s2s/SKILL.md`.

### Tasks

#### M6.1 HOWTO-VOICE-MODE.md rewrite
4-mode UX, `/s2s` command, thread mirroring, voice persona.

#### M6.2 HOWTO-REALTIME-DISCORD.md update
`/s2s mode:realtime` flow, hermes_meta_* tools, deny list.

#### M6.3 README.md feature highlights + 0.4.0 badge

#### M6.4 hermes-s2s SKILL.md update
Per skill_manage discipline: update the embedded skill that loads when
hermes-s2s is active.

**Acceptance:**
- A1: `grep -F '/s2s' README.md` exits 0
- A2: `grep -F 'four modes' docs/HOWTO-VOICE-MODE.md` exits 0
- A3: `grep -F '0.4.0' hermes_s2s/skills/hermes-s2s/SKILL.md` exits 0

---

## WAVE 6b — Version bump + smoke + tag

**Subagent:** Orchestrator-direct (no delegation; trivial).

### Tasks

- Bump `hermes_s2s/__init__.py.__version__`, `pyproject.toml`, `plugin.yaml` → `0.4.0`
- Update `tests/test_smoke.py::test_version_is_0_3_9` → `test_version_is_0_4_0`
- Run full pytest suite → must be green
- Tag `v0.4.0`, push to origin
- Optional: run a CLI smoke against a fake Discord harness

**Acceptance:**
- A1: `pytest tests/ -q` exits 0
- A2: `git tag` shows v0.4.0
- A3: `python -c "import hermes_s2s; assert hermes_s2s.__version__ == '0.4.0'"` exits 0

---

## Phase 7 (concurrent review) — per wave

For waves W1c, W3b, W4b (the high-risk integration waves), dispatch a
3-reviewer scatter (different model families) in parallel with the
execution subagent. Reviewer prompt template:

```
Review the diff produced by the W<id> execution subagent on branch
feat/0.4.0-rearchitecture. The wave aimed to <one-sentence goal from spec>.

Read independently. Did the diff:
- Introduce new bugs (especially asyncio lifecycle, monkey-patch
  idempotency, file-handle leaks)?
- Miss the bug or feature it claims to address?
- Skip acceptance tests A1..AN?
- Break back-compat with 0.3.x users?

Report CONFIRMED / ISSUES (P0|P1|P2 + line refs) / QUESTIONS / READY-TO-MERGE.

Do NOT include reasoning from the executor's commit message — read the
diff cold.
```

Intersection of P0 across reviewers = must-fix before commit lands. Union of
P1 = follow-up backlog or fold into next wave.

---

## Phase 8 (cross-family final review) — pre-merge to main

After W6b lands on `feat/0.4.0-rearchitecture`, dispatch a 3-reviewer scatter
(model families: claude-opus, gemini-3.1-pro, kimi-k2-thinking) reviewing
the WHOLE branch diff against `main`. Same `parallel-critique` skill rules:
intersection-P0 must-fix before merge to main; union-P1 = post-merge
backlog. Tag v0.4.0 only after merge to main lands clean.

---

## Reflexion (post-batch)

After every batch:
1. Run full acceptance test suite + integration smoke.
2. Append 5 lessons-bullets to `AGENTS.md` under `## 0.4.0 lessons`.
3. Update todo entries to `completed`.
4. Commit batch result with structured message listing each task ID + outcome.
