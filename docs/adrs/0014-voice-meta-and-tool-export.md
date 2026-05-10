# ADR-0014: Voice meta-commands and realtime tool-export policy

**Status:** accepted
**Date:** 2026-05-10
**Driven by:** [research/15-voice-meta-commands-and-tool-export.md](../design-history/research/15-voice-meta-commands-and-tool-export.md) §3–§7

## Context

Hermes text clients support meta-commands (`/resume`, `/new`, `/clear`, etc.) parsed by the gateway before routing. Voice/realtime has no slash syntax and a different trust model: the STT transcript is noisy, the LLM may "pick" the wrong session on its own, and dangerous tools (shell, filesystem writes, HA actuators) must never be callable from a voice-driven agent without a human in the loop. Research-15 §3–7 designed two parallel interception mechanisms and a tool-export bucketing scheme.

## Decision

### 1. Two parallel meta mechanisms (not one)

**MetaCommandSink (M1/M2 — pre-LLM, wakeword-anchored regex):**
- Runs on the STT transcript **before** it is forwarded to the realtime LLM.
- Recognizes a small, fixed grammar anchored to the wakeword: `"<wake>, new chat"`, `"<wake>, clear chat"`, `"<wake>, cancel that"`, `"<wake>, stop speaking"`, etc.
- Matched utterance is consumed (not sent to LLM) and dispatched directly to the gateway command layer.
- Rationale: these are session-control primitives — letting the LLM paraphrase them introduces ambiguity and a full turn of latency.

**`hermes_meta_*` tool family (M3/M4 — LLM-invoked JSON-schema tools):**
- Exposed to the realtime model as first-class tools with strict JSON schemas: `hermes_meta_list_sessions`, `hermes_meta_summarize_session`, `hermes_meta_set_persona_overlay`, `hermes_meta_search_memory`, etc.
- Used for meta-operations the user can reasonably phrase in natural language ("what were we talking about yesterday?", "summarize my last session") where LLM mediation **adds** value.
- These are safe because they are read-only or additive; no destructive side effects.

### 2. `/resume <name>` is gateway-direct only

`/resume` is **not** exposed as a realtime tool and **not** in the MetaCommandSink voice grammar. It is reachable only from text clients through the gateway.

Rationale: LLM-picking-wrong-session is a **data hazard** — a fuzzy-matched resume silently redirects the entire subsequent conversation into a stranger's context. STT ambiguity (e.g. "resume Alice" vs "resume Alex") compounds the risk. The correct voice flow is: user invokes `hermes_meta_list_sessions` (read-only), then starts a fresh session; explicit resume requires the typed, exact-match path.

### 3. Three-bucket tool-export policy

Every tool in the Hermes registry is tagged with one of:

- **`default_exposed`** — auto-advertised to the realtime LLM. Read-only or low-blast-radius: search, weather, list_sessions, get_time, HA **read** sensors, memory reads, etc.
- **`ask`** — advertised but wrapped: the tool runs a confirmation turn ("I'm about to do X, confirm?") before the underlying call. Examples: sending a message, creating a calendar event, kanban **read** operations that cross user boundaries.
- **`deny`** — **never** advertised to the realtime session. Explicit deny list:
  - `terminal` (shell execution)
  - `patch`, `write_file` (filesystem mutation)
  - `computer_use` (GUI control)
  - `delegate_task` (spawn subagents — compounds blast radius)
  - `cronjob` (scheduled execution)
  - `ha_call_service` (Home Assistant actuators — lights/locks/etc.)
  - kanban **writes** (create/update/delete cards)
  - browser **interactive** ops (click, type, submit — read-only fetch/extract is `ask`)

Bucket assignment lives in the tool's registration metadata; `deny` is the default for any tool lacking an explicit tag (fail-closed).

### 4. Latency budget

- **Soft deadline: 3 s** — if a tool call has not returned, trigger filler audio (see ADR-0008 pattern; shorter than text-flow budget because voice silence feels worse).
- **Hard deadline: 15 s** — cancel the tool, inject `{"error": "tool timed out"}`, let the model recover verbally.
- Deadlines are per-call, not per-turn.

### 5. Serial-by-default tool calls

Unlike text-flow (ADR-0008 runs parallel tool calls via `asyncio.gather`), the realtime path dispatches tools **serially by default**. Rationale: voice turns are short, users expect linear narration ("checking your calendar… now checking weather…"), and serial execution lets filler audio reference the in-flight call by name. Parallel execution is opt-in per tool via a `voice_parallel_safe: true` registration flag.

### 6. Voice persona as fenced overlay

The realtime system prompt is built as:

```
<PERSONA.md verbatim>

<!-- VOICE_OVERLAY_BEGIN -->
<voice-specific instructions: brevity, no markdown, speak numbers as words, …>
<!-- VOICE_OVERLAY_END -->
```

The overlay is **not** merged into `PERSONA.md` and **not** edited by the LLM. It is appended at session construction time and stripped from any persona-export path. This keeps the canonical persona text-first and prevents voice-specific instructions ("don't use bullet points") from leaking into text chats.

### 7. Module location: `meta_dispatcher.py`

The MetaCommandSink grammar, the `hermes_meta_*` tool implementations, and the export-bucket filter live in a **new** module: `hermes_s2s/meta_dispatcher.py`.

Explicitly **not** placed in:
- `tool_bridge.py` — that module's responsibility is wire-format translation (OpenAI/Gemini tool-call events ↔ Hermes registry). Adding meta-dispatch would conflate provider protocol with session-control policy.
- Upstream `hermes/run.py` — this is plugin-local concern; upstream gateway stays voice-agnostic. The plugin registers a pre-STT hook that delegates to `meta_dispatcher.MetaCommandSink`.

## Consequences

- Two grammars to maintain (regex + JSON schema), but each is simple and covers a distinct UX surface.
- Deny list must be audited on every new tool addition; CI check enforces `deny` default.
- `/resume` gap in voice is deliberate UX — documented in user-facing help as "use the text client to resume a specific session by name."
- Serial-by-default costs some latency on multi-tool turns; reconsider if Gemini `NON_BLOCKING` adoption (ADR-0008 §2) makes parallel narration natural.

## References

- research/15-voice-meta-commands-and-tool-export.md §3 (MetaCommandSink grammar), §4 (tool family), §5 (export buckets), §6 (latency), §7 (module boundaries)
- ADR-0004 (command provider interception — text analogue)
- ADR-0008 (tool-call bridging — timeout/filler primitives reused)
