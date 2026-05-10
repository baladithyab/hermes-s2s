# 0.4.0 Wave Plan — Phase 8 Pre-Execution Review (Scope + Completeness)

**Reviewer lens:** Does the plan deliver on the user's asks? Are there hidden P0s
in the deferred list? Is the Discord-only → cross-platform ramp actually viable?

**Inputs read:** `docs/plans/wave-0.4.0-rearchitecture.md` (642 lines, full),
`docs/research/13-mode-ux-deep-dive.md` §6–7, ADRs 0010–0014, and
`docs/research/12-voice-mode-rearchitecture.md` §5.

---

## Findings

### F1 [P0] — Plan is internally contradictory on migration scope (ASK1, W5a)

`## Scope refinements` (L27–31) says W5a is SIMPLIFIED: **"NO separate
`python -m hermes_s2s.migrate_0_4` script, NO `--dry-run` / `--rollback`
flags."** But Goal 8 (L70–72), the file-ownership table (L115: `migrate_0_4.py`
WAVE W5a), the wave spec itself (L526–535 lists `--dry-run`/`--rollback`), and
the rollback-plan section (L168: "Migration (W5) has explicit `--rollback`
callback") all describe the FULL script. Acceptance test A2/A3 still shells out
to `python -m hermes_s2s.migrate_0_4 --dry-run`. The subagent will not know
which spec to implement and Phase-7 reviewers will not know which to score against.

**Touches:** ASK1 (indirectly — migration touches the v0.3.9 shape) + plan integrity.
**Remediation:** Pick one. If deferring: strike L70–72, L115, L168, and L525–547.
If keeping: strike the Scope-refinements bullet. Do this BEFORE W5a dispatches.

---

### F2 [P0] — 2-bucket tool export contradicts ADR-0014 §3 security posture (ASK5)

Scope refinements (L22–26) reduce 3-bucket → 2-bucket (`default_exposed` + `deny`
only); W4b M4.4 (L493–500) then lists `read_file`, `search_files`, `memory`,
`session_search`, `browser_navigate`, `ha_state_read` as `default_exposed`. But
**ADR-0014 §3 explicitly classifies these as `ask`-bucket** ("advertised but
wrapped: the tool runs a confirmation turn before the underlying call… memory
reads, kanban read operations that cross user boundaries"). Research-12 §5 also
places `file_read`, `memory_read`, `session_search` in the permission-gated tier.

Collapsing `ask` into `default_exposed` turns previously-confirmed reads into
silent auto-grants. This is **not** "conservative default-exposed" as the scope
note claims — it is a security-posture regression vs. the accepted ADR. The
realtime model could call `read_file` / `search_files` / `memory` without any
user prompt the moment W4b lands.

**Touches:** ASK5. **Remediation:** Either (a) ship the `ask` bucket degraded as
"log + rate-limit" (no synchronous prompt UX needed — just one-shot-per-session
audible notice), OR (b) shrink `default_exposed` to the ADR-0014 §3 truly-safe
set (`web_search`, `get_current_time`, `calculator`, `hermes_meta_*`, HA read
sensors) and move `read_file`, `memory`, `session_search`, `browser_navigate`,
`ha_state_read` to `deny` for 0.4.0. Option (b) is smaller diff and fail-closed.

---

### F3 [P0] — W4a meta grammar drops three verbs that ADR-0014 §1 listed (ASK4)

Plan's MetaCommandSink (M4.1, L449–458) ships `new_session`, `resume`,
`compress`, `title`, `branch`. ADR-0014 §1 explicitly enumerates
**`<wake>, clear chat`**, **`<wake>, cancel that`**, **`<wake>, stop speaking`**
as the session-control primitives that must bypass the LLM to avoid "a full
turn of latency." Dropping `stop speaking` is the worst omission — it's the
*only* escape hatch when the realtime model starts a long monologue. `cancel`
and `clear` are similarly load-bearing.

`/resume` being included at all in the grammar is also inconsistent with
ADR-0014 §2 ("`/resume <name>` is … **not** in the MetaCommandSink voice
grammar") — the plan ships the hazardous verb the ADR rejected, while
dropping the three safe verbs the ADR required.

**Touches:** ASK4. **Remediation:** Reshuffle the 5-verb budget to
`new`, `clear`, `stop_speaking`, `cancel`, `title` (drop `resume` per ADR-0014
§2; drop `branch`/`compress` which can ride the tool-family path). Keep
`hermes_meta_compress` and `hermes_meta_branch` in W4b where LLM mediation
adds value.

---

### F4 [P1] — "Co-management" language overreaches what ships (ASK3)

ADR-0012 explicitly scopes 0.4.0 as "monkey-patch two core code paths" and
defers the `voice:thread_resolve` / `voice:transcript` hook-bus emit sites to
0.4.1 upstream PR. The plan ships zero upstream commits. That is honest
**plugin-owns-thread-management-and-core-is-unaware** — not "co-management."

The user's verbatim ask was "allow for voice/s2s to auto-comanage threads with
hermes gateway/core." A fair reading requires core to be AT LEAST AWARE of the
thread (hence "co"). Today's plan achieves the user-visible UX (thread gets
created, transcript appears) but if the user pushes back with "show me the core
PR," the answer is "there isn't one, it's deferred."

**Touches:** ASK3. **Remediation (pick one):**
(a) Add a W3c micro-wave: open draft upstream PR against Hermes core with the
   ~25-LOC `_hooks.emit("voice:thread_resolve", …)` / `"voice:transcript"` call
   sites (ADR-0012 already specs the exact lines — `run.py:9161, 9298, 9316`).
   This is ~1 hour and makes the "co-" in co-manage true.
(b) Reframe: rename in release notes to "plugin-side thread auto-management
   (0.4.1 will flip to core co-management via hook-bus)." Set expectations so
   the user isn't surprised.

---

### F5 [P1] — v0.3.9 Arabic-fix regression protection is unenforced in W1c (ASK1)

W1c M1.9 (L316–320) says "Preserve the v0.3.9 sub-block-unwrap behavior INSIDE
the factory's RealtimeSession path." But W1c's acceptance list (A1–A3) does
**not** cite `tests/test_config_unwrap.py` — the exact regression fence that
ADR-0010 §"Test discipline" requires. A2 vaguely references "existing 0.3.x
tests" but pytest collection doesn't prove those specific tests ran against
the factory path.

When the subagent replaces inline bridge construction at discord_bridge.py
L333–360 (where `_resolve_bridge_params` lives today) with a factory call,
it's easy to route through a code path that re-reads `realtime_options` at the
outer level — reintroducing exactly the 0.3.8 bug 0.3.9 fixed.

**Touches:** ASK1. **Remediation:** Add A4 to W1c:
`pytest tests/test_config_unwrap.py -q` exits 0 — and verify the factory's
RealtimeSession path *also* calls `_resolve_bridge_params` (grep check).
Add A5: `python -m hermes_s2s.doctor --probe-realtime-subblock` confirms
`{gemini_live: {system_prompt: MARKER}}` reaches the bridge.

---

### F6 [P1] — No in-place mode-swap wave; research-13 §6 specified it (ASK2)

Research-13 §6 UX table row 1: `/s2s mode:realtime` invoked while VC is already
connected should **switch in place** via `session_factory.swap(vc, spec)`
without leave+rejoin. ADR-0011 §"Invariant" (L65–67) re-affirms this
("either swaps in-place on the live VoiceClient or persists the choice").
Research-13 §9 calls it out as a contract that needs test coverage.

W2a M2.1 (L342–345) describes the PERSIST path only ("takes effect on the
NEXT `/voice join`"). No wave writes a `VoiceSessionFactory.swap()` method,
no tests assert in-place-swap behavior, no acceptance test covers the "VC
stays connected, bot replies `🔄 Switched`" UX. Users will `/s2s mode:realtime`
in a live cascaded VC, see "Next join here uses realtime" — and be confused.

**Touches:** ASK2. **Remediation:** Add M1.10 to W1c (or a new W2b):
`VoiceSessionFactory.swap(vc, new_spec)` that teardowns old session, constructs
new one against same `VoiceClient`, idempotency-tests per research-13 §9.
Update W2a M2.1 to call `factory.swap` when `vc.is_connected()`.

---

### F7 [P1] — Migration W5a breaks multi-machine shared-config users silently

W5a (simplified) auto-translates `s2s.mode` → `s2s.voice.default_mode` on
first config-load, writes backup, overwrites in-place. Scenario: user runs
hermes-s2s on both hostA (laptop) and hostB (server), sharing `~/.hermes/config.yaml`
via Syncthing / Dropbox / rsync. HostA updates to 0.4.0 first → auto-translates →
config now has ONLY `s2s.voice.default_mode`. Syncs to hostB (still 0.3.x) →
0.3.x reads `s2s.mode`, finds nothing, silently boots with cascaded default.
User: "why did my server bot lose its realtime mode?"

**Touches:** ASK2/migration. **Remediation:** Write BOTH keys for one release.
`s2s.mode` kept as alias (old value) + `s2s.voice.default_mode` (new value).
0.3.x reads old key; 0.4.0 reads new key; they stay in sync. Drop alias in 0.5.0.
~5 LOC add to the auto-translator. Document in release notes.

---

### F8 [P1] — No `/voice leave` or bot-disconnect-mid-call wave

Plan covers `/voice join` comprehensively. Zero coverage for:
- `/voice leave` / `/s2s leave`: does the session's `AsyncExitStack.aclose()`
  fire? Is the thread archived or left open? The thread was created in W3a
  but nothing archives it on leave.
- Bot-network-disconnect-mid-call: if Discord gateway drops, does
  `RealtimeSession` reconnect? Does the `AsyncExitStack` finalize cleanly?
  ADR-0013 §4 mentions state machine CLOSING → CLOSED but no wave tests the
  disconnect-initiated path.
- Second user joins VC mid-call: multi-user VC is common on Discord. Does
  transcript mirroring attribute both users correctly? `audio_bridge.py:616`
  emits `role="user"` but no `user_id`. ADR-0012 §"Hook 2" ctx includes
  `user_id: Optional[str]` — plan doesn't spec how it's populated.

**Touches:** completeness. **Remediation:** Add W3c "session teardown +
multi-speaker" covering (a) thread archive on leave, (b) AsyncExitStack
flush on gateway-disconnect (contract test), (c) speaker-attribution in
TranscriptMirror using Discord `member.id`. Can be ~2h sub-wave.

---

### F9 [P2] — 0.4.0 → 0.4.1 cross-platform ramp is not actually bootstrapped

Non-goals (L76–81) says "0.4.0 ships Discord only" and "upstream PRs deferred
to 0.4.1." But 0.4.1 needs: (a) upstream `register_command(options=…)` PR,
(b) `voice:thread_resolve` / `voice:transcript` hook emit sites, (c) Telegram
voice path. None of these are scoped — no branch, no stub PR. "0.4.1 upgrade
path" in the plan is aspirational text, not a plan.

**Touches:** cross-platform parity. **Remediation:** Append a 0.4.1
`docs/plans/wave-0.4.1-upstream-prs.md` stub during W6a docs wave. Doesn't
have to be executable — just explicit about what's needed, LOC estimates,
Hermes-core commit it targets.

---

### F10 [P2] — W6a docs don't cover the 4-mode UX end-to-end

W6a acceptance (L573–575) checks three greps: `/s2s`, "four modes", "0.4.0".
None verify: migration behavior, deny-list contents, in-place swap UX,
wakeword config, voice-persona override, thread auto-archive semantics.
SKILL.md gets one grep for "0.4.0". README gets "feature highlights only"
per scope-refinement L32 (badge deferred).

**Touches:** documentation. **Remediation:** Strengthen W6a accepts:
`grep -F 'default_mode' docs/HOWTO-VOICE-MODE.md`,
`grep -F 'wakeword' docs/HOWTO-VOICE-MODE.md`,
`grep -F 'deny' docs/HOWTO-REALTIME-DISCORD.md`,
`grep -F 'migrate' docs/HOWTO-VOICE-MODE.md`. ~10 min to strengthen.

---

## Deferred-items audit (6 items in "Scope refinements")

| Deferred | Secretly P0? | Why |
|---|---|---|
| W2a M2.2 Telegram fallback | No | User is Discord-primary per plan L16 |
| W2a M2.3 CLI `/s2s` | No | CLI has no VC; truly zero-urgency |
| W4b resume_session tool | **YES — but as a safety win** | ADR-0014 §2 explicitly rejects voice-resume as a data hazard. Keeping it deferred is *correct*; the plan text frames it as "lowest-value" which understates the security rationale. **Clarify in release notes.** |
| W4b 3-bucket → 2-bucket | **YES** — see F2 | Security regression |
| W5a migration script | Contradictory — see F1 | Plan-integrity P0 |
| W6a README badge | No | Cosmetic |

---

## Risk-register gap

Plan has a rollback-plan (L164–170) and Phase-7 review scatter (L598–623), but
no explicit "mid-wave failure recovery" story. Example: W1c fails after W1a/W1b
landed green. The `voice/` package now exists with no caller; `discord_bridge.py`
is mid-edit. Do we revert just W1c, leaving W1a/W1b? Revert all three?
Branch protection? **Remediation:** Add to "Rollback plan": a per-batch revert
script `git revert $(git log --oneline feat/0.4.0-rearchitecture ^main | grep '^wave-W<id>' | cut -f1)`.

---

## Verdict: **NEEDS-MINOR-PLAN-EDITS**

Three P0s block execution as-written: F1 (migration contradiction — subagent
will implement wrong spec), F2 (2-bucket ships security regression vs ADR-0014),
F3 (meta grammar drops `stop speaking` — the realtime escape hatch).

All three are ~15-minute plan edits. F4–F8 (P1) should be addressed but do not
block kick-off once the P0s land. F9–F10 (P2) go on post-0.4.0 backlog.

**Recommended action:** Edit the plan to resolve F1/F2/F3, then dispatch
B1 (W1a + W1b in parallel). F5 regression fence can be added as an acceptance
test to W1c without blocking W1a/W1b kick-off.
