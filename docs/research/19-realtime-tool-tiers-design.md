# Research-19: v0.5.0 realtime tool exposure tier system

**Status:** design proposal
**Date:** 2026-05-10
**Builds on:** [ADR-0014](../adrs/0014-voice-meta-and-tool-export.md), [research-15](./15-modes-and-meta-deep-dive.md)
**Target release:** 0.5.0

## 1. Current state (trace)

**The tier system is designed; it is not wired.** Audit of 0.4.1:

1. `voice/factory.py:345` — `tools = list(options.get("tools") or [])`. Nobody populates `spec.options["tools"]`; `ModeRouter` doesn't set it, and `discord_bridge._attach_realtime_to_voice_client` doesn't either. **The realtime backend ships today with `tools=[]`.**
2. `factory._build_realtime` → `RealtimeAudioBridge(... tools=list(tools))` → `gemini_live._build_setup` → `_translate_tools([])` → Gemini session has zero tool declarations.
3. `_internal/tool_bridge.py:120` exposes `build_tool_manifest(enabled_tools, mode)` which filters by `DEFAULT_EXPOSED`/`ASK`/`DENY` and appends `get_meta_tools()`. **No production code imports it.** Only tests do.
4. `hermes_meta_*` schemas exist in `voice/meta_tools.py` but are never advertised to the realtime model. The LLM literally cannot call `hermes_meta_new_session`.
5. `MetaDispatcher` (`voice/meta_dispatcher.py`) dispatches `MetaMatch` objects — but those come from the **pre-LLM wakeword regex** (`voice/meta.py`), not from tool calls. The tool-call side of meta has no handler.
6. `HermesToolBridge.handle_tool_call` dispatches by name to `ctx.dispatch_tool`, which is the Hermes core dispatcher. It can reach any core tool the gateway registered; the bridge has no name-filtering of its own (deny happens at manifest build time, not at dispatch time).

**Net: v0.4.1 realtime has no tools at all.** The scaffolding is wired for a gated mute state. v0.5.0's job is to open the gate without undoing the fail-closed posture.

## 2. Tool-bridge API review

`HermesToolBridge(dispatch_tool)` is a thin wrapper around a callable `(name, args) → result | awaitable`. It owns:

- soft/hard timeouts (5s / 30s) with filler audio
- serialization + 4 KB truncation
- in-flight task tracking + `cancel_all()`

It is **provider-agnostic** and **name-agnostic** — it will call anything `dispatch_tool` will call. That means the tier filter *must* run at manifest build time (what the LLM sees) AND at dispatch time (defense in depth, because a hallucinated tool name from a noisy audio session is a real failure mode). Today the dispatch-side check is missing.

The bridge can pass through to any Hermes core tool because `ctx.dispatch_tool` *is* Hermes's registry. Plugin-local tools (if any) would need to be merged into the same dispatcher — not a concern for 0.5.0.

## 3. Tier model — recommendation

The proposed 3-tier model collides with ADR-0014's already-accepted 3-bucket model (`default_exposed` / `ask` / `deny`). **Do not introduce a parallel tier taxonomy.** Instead, layer a user-facing *tier* on top of the existing per-tool classification:

```
tier = "read"   →  DEFAULT_EXPOSED                 (plus meta: always)
tier = "act"    →  DEFAULT_EXPOSED + ASK           (plus meta: always)
tier = "meta"   →  DEFAULT_EXPOSED + ASK + session-mutating hermes_meta_*
tier = "off"    →  meta only, no core tools         (kiosk / demo mode)
```

Per-tool bucket stays the atomic unit (tagged in Hermes core registration, not per-plugin). Tier is a *presentation layer* that decides which buckets get emitted this session. This preserves the ADR-0014 CI fence (`test_every_core_tool_classified`) and keeps `deny` immutable — no tier can expose a deny-listed tool, full stop.

### Why not a tier 4 (explicit risky)?

The task brief asks whether to map ADR-0014's ASK bucket into its own tier. Answer: **no.** ASK is about **per-call** voice confirm (synchronous yes/no mid-turn); tier is about **per-session** exposure. These are orthogonal axes. A user on `tier=act` still gets mid-turn confirm prompts for every ASK-bucket call; they don't get a separate tier for it.

### Destructive tools stay deny, period

`terminal`, `patch`, `write_file`, `computer_use`, `delegate_task`, `cronjob`, `ha_call_service`, kanban writes, browser interactive: DENY in ADR-0014, DENY in every tier including `meta`. The voice trust model does not admit shell execution. If a user *really* wants to schedule a cron from voice, the path is "ask the agent to draft it → agent uses `send_message` (ASK) to propose text → user types `/cronjob` in the text client." The extra hop is the feature, not the bug.

### `send_message` re-classification

ADR-0014 puts `send_message` in DENY. For 0.5.0 I'd move it to ASK (gated by the voice-confirm flow that ships alongside tier support). Sending a Discord message *from within* a voice session, where the user explicitly says "tell Alice I'll be late," is the canonical ASK case. Keep it DENY if the voice-confirm flow slips.

## 4. Configuration UX

Both config-level default and per-join override. Precedence:

1. **Per-join slash arg** (highest): `/voice join tier:act` on the join command. Ephemeral; lasts for the session.
2. **Per-guild/channel config**: `s2s.realtime.tool_tier: read|act|meta|off` in the router config (same shape as mode routing). Honored when no slash override.
3. **Global default**: `read` (conservative — matches the 0.4.1 fail-closed instinct; meta tools are always on top).

The `/voice tier <level>` command while already joined hot-swaps the tier by calling `backend.update_tools(build_tool_manifest(...))` if the provider supports mid-session tool updates (Gemini Live does not — falls back to requiring reconnect with a spoken "restarting session to enable actions"). OpenAI Realtime's `session.update` does support this; design the backend interface to expose `supports_hot_tool_swap: bool`.

Store the **resolved** tier on the session so observability shows it. Surface it in the `/voice status` slash reply: `mode=realtime, tier=act, tools=12/47 exposed`.

## 5. Voice-confirm UX (the ASK-bucket mid-turn flow)

This is the piece ADR-0014 deferred from 0.4.0 and is the prerequisite for `tier=act`. Flow:

1. LLM emits `tool_call(name="send_message", args={...})`.
2. Before `tool_bridge.handle_tool_call` dispatches, check `name in ASK`.
3. If yes: `backend.inject_tool_result(call_id, {"status": "pending_confirm"})` — the model gets a synthetic "I need to confirm" result, keeping its state machine happy — AND the bridge speaks a fixed short confirm via a sidechannel TTS: `"About to send message to Alice — say yes or no."` Keep the template parameterized, terse, and include the key arg (recipient, filename, amount) not the whole blob.
4. Open a 5-second listen window with a narrow regex matcher (`^(yes|yeah|yep|go ahead|do it|confirm)\b` positive; anything else or timeout → deny).
5. On `yes`: dispatch for real, inject actual result. On `no`/timeout: inject `{"error":"user declined","retryable":false}`.

Implementation location: extend `MetaDispatcher` into `ConfirmDispatcher` living in the same module, or introduce `voice/confirm_sink.py`. The latter is cleaner; `MetaDispatcher` is about verbs, `ConfirmSink` is about call-gating.

**Critical**: the confirm window uses the **STT stream directly**, not another LLM hop. Running a confirm through Gemini costs ~1.5 s of latency and risks the model re-generating the original request. Match on the raw transcript, anchored by a short cooldown where any non-matching speech also counts as "no" (prevents accidental yes from background chatter).

Cooldown: after a confirm event (yes or no), suppress the same tool name for 2 seconds to avoid double-fires from repeated hallucinations.

## 6. Meta-command voice ergonomics

The `hermes_meta_*` tools already have good descriptions (`meta_tools.py`) anchored to user phrases ("new chat," "start over"). Three refinements for 0.5.0:

1. **Include anti-examples in descriptions.** Gemini over-triggers on "chat" in sentences like "the chat is slow." Add: *"Do NOT call when the user says 'this chat', 'chat history', or is describing something about chat functionality. Only call when the user is issuing a command to end the conversation."*

2. **Dual-path for high-risk verbs.** `new_session` and `compress_context` should *also* remain in the wakeword regex (`MetaCommandSink`) because pre-LLM match is both lower-latency AND doesn't depend on the model's tool-selection quality. The tool is the fallback for users who don't know wakeword-anchored grammar. De-dupe on the downstream side: if `MetaCommandSink` already fired `/new` in the last 2 seconds, drop a subsequent `hermes_meta_new_session` call.

3. **No `resume_session` tool.** ADR-0014 is correct; keep it out. "Switch to my last session" is a text-client command, full stop. The data-hazard argument hasn't changed.

Add for 0.5.0: `hermes_meta_switch_model(model_name: enum)` — only if the gateway exposes the model registry safely; guarded to a small allowlist (`fast`, `smart`, `voice-optimized` aliases — not raw model IDs). Moderate value, low-to-moderate risk.

## 7. Failure modes

| Failure | Current behavior | v0.5.0 target |
|---|---|---|
| Tool raises | `handle_tool_call` catches, returns `{"error": ..., "retryable": false}` JSON. Model narrates the error. | Keep. Add: structured error codes (`permission_denied`, `timeout`, `invalid_args`) so persona overlay can suggest specific recovery speech. |
| Hard timeout (30 s) | Returns `tool timed out` error. | Lower realtime hard timeout to **15 s** (voice silence budget << text budget). Matches ADR-0014 §4. |
| Soft timeout (5 s) | Filler audio "let me check on that." | Keep, but parameterize per-tool in registration (`voice_filler: "checking the calendar"`) for less robotic UX. |
| LLM hallucinates non-existent tool | Bridge dispatches to `ctx.dispatch_tool` which raises `ToolNotFound`. Falls through to generic error. | Add dispatch-time allowlist check: `if name not in _exposed_names: return {"error":"unknown tool","retryable":false}`. Defense in depth against manifest/dispatch drift. |
| Backend WS drops mid-tool | `cancel_all()` already exists. | Ensure `_run_and_inject_tool` in audio_bridge.py propagates cancellation cleanly — it already does as of line 794. Add an integration test. |
| Confirm window expires during backend reconnect | Race condition — undefined today. | Hold confirm state in the session object, not the bridge. On reconnect, if a confirm is pending, inject `{"error":"confirm expired during reconnect"}` into the fresh session and clear state. |
| User interrupts during filler audio | Barge-in already cancels TTS in gemini_live; tool keeps running. | Keep. Filler is non-blocking; treat it as speculative narration. |
| Tool succeeds but result > 4 KB | Truncated with marker. Model sees partial result. | Keep, but add `memory(action=add)` as the recommended follow-up tool in the persona overlay ("if result is truncated, stash full via memory and continue"). |

## 8. Testing strategy

**Unit (pure):**

- `test_tool_export.py::test_tier_resolution_read|act|meta` — for each tier, assert the exposed set against a fixture registry. Existing `test_every_core_tool_classified` stays as the CI fence.
- `test_meta_tools_always_present_across_tiers` — meta appears in every tier including `off`.
- `test_deny_bucket_never_exposed` — property test: for every tier × every DENY tool, tool is absent.

**Unit (ConfirmSink):**

- `test_confirm_yes_dispatches` / `test_confirm_no_blocks` / `test_confirm_timeout_blocks`.
- `test_confirm_regex_rejects_ambiguous` — "yes, but first..." → no-dispatch (policy choice: strict match only).
- `test_cooldown_suppresses_repeat` — double-fire protection.

**Integration (fake backend):**

- `test_realtime_session_manifest_matches_tier` — start RealtimeSession with tier=act, assert the backend's `setup["tools"]` contains the expected union. Uses the existing `_backend` injection hook in `spec.options`.
- `test_hot_tier_swap_openai` vs `test_hot_tier_swap_gemini_reconnects` — capability-gated; OpenAI swaps in place, Gemini full reconnect.
- `test_hallucinated_tool_name_returns_error` — bridge receives `tool_call(name="rm_rf")`, returns structured error, does not reach dispatcher.

**E2E (opt-in, real keys):**

- Scripted Gemini Live session: speak "what's the weather in Denver" → asserts `web_search` tool was invoked. Gated behind `HERMES_S2S_E2E=1`.
- Confirm-flow script: speak "send a message to Alice saying hi" → expect spoken confirm → speak "yes" → assert `send_message` dispatched.

**Smoke / regression fences:**

- Snapshot of the 0.5.0 tier tables committed; a PR that alters a tool's bucket without updating the snapshot fails CI. Forces bucket changes to be reviewer-visible.

## 9. Migration path & out-of-scope

**In scope for 0.5.0:**

- Wire `build_tool_manifest` into `factory._build_realtime` (source: `ctx.list_tools()` or equivalent — confirm the gateway exposes this; if not, carry a small helper).
- Land the `ConfirmSink` and flip `ASK` back to the 15-entry set from ADR-0014's deferred list.
- Add tier config knob + slash override.
- Dispatch-time allowlist check in `HermesToolBridge`.

**Out of scope (0.5.x / 0.6):**

- Hot tier swap for Gemini (needs backend protocol work; reconnect fallback is fine for 0.5.0).
- `/resume` by voice (stays denied per ADR-0014 — reconsider only with user-pick UI).
- Plugin-contributed tools beyond core (would require registration API; not blocking).
- Multimodal tools (`image_generate`, `vision_analyze` with attached images from voice) — keep `vision_analyze` in DEFAULT_EXPOSED as text-only for now.

## 10. Decisions to ratify before coding

1. Tier names: `read` / `act` / `meta` / `off` — or `safe` / `normal` / `power` / `mute`? Prefer the action-verb set (`read`/`act`/`meta`) — self-documenting, maps to user intent.
2. Default tier: `read` (proposed) vs `off` (stricter). Proposal: `read`. The 0.4.1 posture was effectively `off` and the feedback was "why does voice feel dumb?"
3. `send_message` promotion: DENY → ASK. Proposed yes; easy to revert if confirm flow has bugs.
4. `switch_model` meta tool: ship in 0.5.0 or defer? Defer unless a concrete user need surfaces.

---

*If these four decisions land, the implementation is ~400 LOC plus tests: `build_tool_manifest` call site in factory, `ConfirmSink` module, slash-command wiring, dispatch-time check, docs. The hard work was done in 0.4.0 — 0.5.0 is connecting the wires.*
