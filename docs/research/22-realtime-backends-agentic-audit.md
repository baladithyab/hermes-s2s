# Realtime backends audit: agentic readiness + context prefill

**Date:** 2026-05-10
**Scope:** hermes-s2s v0.4.4 — `hermes_s2s/providers/realtime/{gemini_live,openai_realtime}.py` + supporting glue (`history.py`, `audio_bridge.py`, `sessions_realtime.py`, `connect_options.py`).
**Goal:** confirm both realtime backends are correctly set up for **agentic operation** (multi-turn tool use, meta-commands) and **context prefill** (history injection from a Discord text thread). Identify gaps and prioritize fixes.

> **Author:** orchestrator (Opus). Three parallel deep-research subagents were dispatched but each stalled at the final `write_file` after spending 8+ minutes consuming docs (the documented "stalled write_file" pattern in `deep-work-loop`). Falling back to orchestrator-authored synthesis using the doc dumps already pulled into context (Gemini Live overview/guide/tools/session, OpenAI Realtime guide + conversations + client events).

## Summary verdict

The realtime stack is **structurally correct** — the wire formats match current specs, the connect-before-pumps fence works, history injection lands in the right place, the tool-call lifecycle ordering is well-engineered (sequence-number-based parallel-dispatch + ordered-inject). It's a real foundation, not vibes.

But it's **agent-incomplete**:

1. **No tools are exposed yet.** `_tools` is plumbed end-to-end through `ConnectOptions` but `discord_bridge._resolve_bridge_params` always passes `tools=[]`. The agent is technically a chatbot-with-voice, not a voice agent. v0.5.0 was supposed to add the tier system; that's the work.
2. **History injection is too literal.** We dump prior text turns verbatim and add a "you cannot call tools" disclaimer when history is present. But Gemini Live's native-audio model interprets long prior assistant turns as voice that already happened — it sometimes rambles a fragment of "what we were just talking about" instead of waiting for the user. OpenAI Realtime is better-behaved here but still benefits from clearer turn-role separation.
3. **The synthetic `(voice session starting)` closer is a hack** that works because Gemini stays silent on a model-role turn at the end of `clientContent.turns[]`. It's documented (research/19, UX critique §5) but it's not the canonical Gemini idiom; the canonical idiom is to call `clientContent` with `turnComplete=true` and **NOT add a synthetic closer**, then rely on AAD/manual-VAD to drive the first model turn.
4. **OpenAI tool-call output uses string-coerce instead of structured.** We `json.loads(result)` and re-`dumps` if structured, but OpenAI's `function_call_output.output` is *spec'd as a string* — so the round-trip is fine, but we throw away the structure that newer prompt-cache and reasoning paths can use.
5. **Tool-call cancellation isn't wired.** Gemini emits `toolCallCancellation` when it decides mid-turn to abandon a tool call; we silently ignore it (gemini_live.py:511 "no events emitted"). The tool keeps running, the result eventually injects, the model has moved on, and the result gets stuffed into a stale conversation slot. Low-impact today (we have no real tools) but a real bug once tools land.
6. **No support for built-in Gemini tools** (`googleSearch`, `urlContext`, `codeExecution`). Gemini's `tools[]` accepts a mix of `functionDeclarations` and built-ins; `_translate_tools` only emits `functionDeclarations`. Cheap to add — would let the voice agent actually search the web without us writing a custom tool wrapper.
7. **No ephemeral-token auth for browser-side use.** Both backends bake the long-lived API key into the WS URL/headers. Fine for server-side Hermes (where it is); nothing to do until/unless we ship a browser-side client.
8. **Reconnect path is half-implemented.** Gemini's `sessionResumption.handle` is captured and re-sent on reconnect — but we *also* set `_history_injected = True` on initial connect and then never re-inject on reconnect. Gemini retains server-side state across resumption, so this is correct in the happy path. But if Gemini drops `resumable: false` (which the docs say happens during in-flight tool calls and generation), we lose the handle silently and the next reconnect starts fresh with no history replay logic. OpenAI has no native resumption — we close and the caller decides — which is honest, but the caller (the audio bridge's `_pump_output`) doesn't currently reconnect.
9. **`turn_detection` is hardcoded.** OpenAI Realtime exposes `server_vad` and `semantic_vad` (newer, more accurate for VC barge-in); we never set `turn_detection` in `session.update` so we get the default `server_vad` with default thresholds. Tunable.
10. **No `input_audio_transcription` on OpenAI.** We rely on `response.audio_transcript.delta/done` for assistant transcripts but never enable `input_audio_transcription` (Whisper-1 by default) for user speech, so transcripts in the Discord thread mirror only the assistant side. Gemini already enables both via `inputAudioTranscription: {}` + `outputAudioTranscription: {}`. OpenAI is asymmetric.

The **rest is fine.** Tool-bridge sequence-number ordering is correct. ConnectOptions plumbing is forward-compatible. The history fallback (find_most_recent_thread_session_id) shipped in v0.4.4 is the right fix for joining VC from a non-thread channel. Session-cap detection on OpenAI fires correctly.

## Wire-format conformance check

### Gemini Live (gemini_live.py)

| Aspect | Spec (ai.google.dev/api/live + /docs/live*) | Our impl | Status |
|---|---|---|---|
| Connect URL | `wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=<key>` | matches | ✅ |
| First frame | `{"setup": BidiGenerateContentSetup}` | matches | ✅ |
| Wait for `setupComplete` | required before audio | yes, with 5s timeout fallback | ✅ |
| `responseModalities` | `["AUDIO"]` for native-audio models | `["AUDIO"]` | ✅ |
| `speechConfig.voiceConfig.prebuiltVoiceConfig.voiceName` | string | matches | ✅ |
| `speechConfig.languageCode` | BCP-47 | `language_code` | ✅ |
| `systemInstruction.parts[].text` | required | matches | ✅ |
| `realtimeInputConfig.automaticActivityDetection.disabled` | bool | `True` (we use manual VAD) | ✅ |
| `realtimeInputConfig.activityHandling` | `START_OF_ACTIVITY_INTERRUPTS` or `NO_INTERRUPTION` | `START_OF_ACTIVITY_INTERRUPTS` | ✅ |
| `inputAudioTranscription` / `outputAudioTranscription` | empty object enables | both enabled | ✅ |
| `contextWindowCompression.slidingWindow` | empty object enables | enabled | ✅ |
| Audio chunk frame | `{"realtimeInput":{"audio":{"data":<b64>,"mimeType":"audio/pcm;rate=16000"}}}` | matches | ✅ |
| `activityStart` / `activityEnd` | required when AAD disabled | `send_activity_start/end` emit `{realtimeInput:{activityStart:{}}}` | ✅ |
| Tool call event | `{"toolCall":{"functionCalls":[{id,name,args}]}}` | `_translate_server_msg` reads it | ✅ |
| Tool response | `{"toolResponse":{"functionResponses":[{id,response}]}}` | `inject_tool_result` matches | ✅ |
| Session resumption | `setup.sessionResumption.handle` (or `{}` to enable updates) | both branches | ✅ |
| `clientContent.turns[].role` | `"user"` or `"model"` | matches | ✅ |
| `clientContent.turnComplete` | bool | `True` for history | ✅ |
| **Tools mix** | `tools: [{functionDeclarations:[...]}, {googleSearch:{}}, {urlContext:{}}]` | only emits `functionDeclarations` | ⚠️ gap |
| **`toolCallCancellation`** | server emits when abandoning a call | silently ignored | ⚠️ gap |
| **`thinking_config` / `affective_dialog`** | new agentic fields | not set | ⚠️ gap |
| **Async function calling** (`behavior: "NON_BLOCKING"`) | per-tool flag | not set | ⚠️ gap |

### OpenAI Realtime (openai_realtime.py)

| Aspect | Spec (platform.openai.com/docs/guides/realtime + api-reference/realtime-*) | Our impl | Status |
|---|---|---|---|
| Connect URL | `wss://api.openai.com/v1/realtime?model=<model>` | matches | ✅ |
| Auth header | `Authorization: Bearer <key>` + `OpenAI-Beta: realtime=v1` | both | ✅ |
| `session.update` | first frame after connect | matches | ✅ |
| Voice / instructions / tools / tool_choice | `session.session.{...}` | matches | ✅ |
| `input_audio_format` / `output_audio_format` | `pcm16` | matches (24kHz mono LE assumed by spec) | ✅ |
| Audio append | `{"type":"input_audio_buffer.append","audio":<b64>}` | matches | ✅ |
| `conversation.item.create` for prefill | `item.type=message`, `role=user/assistant`, `content=[{type,text}]` | matches; user→`input_text`, assistant→`text` | ✅ |
| Function-call output | `conversation.item.create` with `item.type=function_call_output, call_id, output:string` + `response.create` | both sent in `inject_tool_result` | ✅ |
| Tool-call event | `response.function_call_arguments.delta` (streaming) + `response.function_call_arguments.done` | only `.done` consumed | ⚠️ minor |
| Session-cap | server closes WS at 30 min | detected; `session_cap` event emitted | ✅ |
| Out-of-band response | `response.create` with `response.conversation: "none"` | used in `send_filler_audio` | ✅ |
| Interrupt | `response.cancel` + `output_audio_buffer.clear` + `conversation.item.truncate` | all three emitted | ✅ |
| **`turn_detection`** | `server_vad` (default) or `semantic_vad` (newer) | not set, default applied | ⚠️ gap |
| **`input_audio_transcription`** | `{model: "whisper-1"}` enables user-side transcript events | not set | ⚠️ gap |
| **`response.audio_transcript.done`** vs `.audio.done` | distinct events | only handle `.delta`/`.done` for transcript | ✅ |
| **Streaming partial tool args** | `response.function_call_arguments.delta` lets agent display "thinking" | not surfaced | ⚠️ minor |
| **Prompt caching** | reusable system prompts marked via `prompt_cache_key` | not used | ⚠️ minor |
| **Ephemeral session tokens** (`POST /v1/realtime/sessions`) | for browser clients | not used (server-side only) | ✅ N/A |

## Context prefill correctness

### What we do today

`_internal/history.py` builds an OpenAI-format `[{role, content}, ...]` list filtered through:
1. Drop system/tool roles.
2. Drop `**[Voice]**` mirrored turns (rejoin dedup).
3. Coerce structured content to text.
4. Drop empty/whitespace.
5. Truncate to 8000 chars / 20 turns from oldest.

This payload feeds both backends. `RealtimeSession._on_start` constructs `ConnectOptions(history=...)` and the backends each translate it.

### Gemini path

```python
# gemini_live._send_history
gemini_turns = [{"role": "user"|"model", "parts": [{"text": ...}]} for t in history]
# If last turn is user, append synthetic model turn: {"text": "(voice session starting)"}
await ws.send({"clientContent": {"turns": gemini_turns, "turnComplete": True}})
```

Then `_build_setup` adds a tool-disclaimer to `systemInstruction` *only when history is non-empty*:

> "In this voice session you cannot call tools. References in the prior conversation to tool calls or actions you took describe completed work — treat them as known facts, not ongoing tasks. Keep replies short and conversational."

**What's right:**
- Sending one consolidated `clientContent` with `turnComplete=true` is the documented prefill idiom.
- Roles map correctly (`user`→`user`, `assistant`→`model`).
- The disclaimer is honest given that we expose zero tools today.

**What's hacky:**
- The `(voice session starting)` synthetic closer. Gemini's docs don't require it; the model is supposed to wait for the next `realtimeInput` audio frame after `turnComplete=true`. We added the closer to fix a behavior where Gemini sometimes spoke a "what's up?" greeting on session start when the last history turn was the user's question. The actual fix is to not include the trailing user turn at all (truncate history *to the last assistant turn*), or to use `clientContent` *without* `turnComplete` — Google's protocol allows turns to remain open. We should validate empirically; if the cleaner truncation works, drop the closer.
- The disclaimer is good defensive prompting but it bloats `systemInstruction` token-cost on every connect. v0.5.0 (when we expose `read` tier tools) flips this entirely — the disclaimer becomes wrong, and we need a positive enumeration of *which* tools are available in this session. Worth gating now behind a `tier` field.
- We don't tag prior turns as "happened in TEXT mode, this is now VOICE mode" — the model has no signal about which turns were typed vs spoken. Sometimes Gemini will respond as if the user just asked the *prior* question aloud. A short prefix on the synthetic closer ("The above was a typed conversation; voice begins now.") would be more honest.

### OpenAI path

```python
# openai_realtime._send_history
for t in history:
    role = t.role  # 'user' or 'assistant'
    content_type = 'input_text' if user else 'text'
    await send({"type": "conversation.item.create", "item": {"type":"message", "role":role, "content":[{"type":content_type, "text":...}]}})
# NO response.create after — server stays silent
```

**What's right:**
- One `conversation.item.create` per turn is the spec's prefill idiom.
- No trailing `response.create` — correctly avoids unprompted speech.
- Roles map correctly.

**What's missing:**
- We don't set `previous_item_id` to thread the items in order. OpenAI's docs say items default to appending, so this works, but explicit ordering would be more robust against race-y session.update timing.
- Same disclaimer addition as Gemini, with the same future-incompatibility issue.
- No transcript metadata: items are dropped raw without identifying them as "from earlier text conversation." OpenAI Realtime is more forgiving than Gemini here but it's still a missed opportunity to disambiguate.

## Agentic readiness gap analysis

The user's vision (from earlier context: voice agent that controls meta-commands like switching sessions, compressing context, starting fresh threads) requires:

1. **Tool exposure tier system** — the `read`/`act`/`meta`/`off` tier in ConnectOptions is reserved but not honored. v0.5.0 work.
2. **Tool catalogue translation** — `_translate_tools` (gemini_live.py:43) and OpenAI's `tools` field both accept JSON-schema-shaped tool defs. The Hermes core's existing tool registry uses the same schema. We need a function `build_voice_tools(tier)` that pulls from `meta_tools.py` (already exists, currently unused) and returns the right slice.
3. **Tool-call cancellation handling** — when the model decides not to use a result, we need to cancel any in-flight `_run_and_inject_tool` task and skip the inject. Today `audio_bridge._tool_seq_cond` will deadlock if a sequence number never gets injected.
4. **Voice-confirm flow for ASK-bucket tools** — tools that mutate state (memory writes, session restart, etc.) need a confirmation prompt. ADR-0014 designed this; not implemented.
5. **Multi-turn planning** — both APIs support this natively as long as tools resolve cleanly. Gemini's "Asynchronous function calling" (`behavior: "NON_BLOCKING"`) lets a long tool run in parallel with continued speech — relevant for "search the web while I keep talking" patterns. OpenAI doesn't have an equivalent; tool calls are inherently blocking.
6. **Session/context meta tools** — `hermes_meta_*` already exists in `meta_tools.py`. The work is registering them in the voice tool catalogue, mapping their results back through `inject_tool_result`, and gating them under the `meta` tier.

## Prioritized fix list

### P0 (correctness now, before tool tier system)

- **P0-1: Surface `toolCallCancellation` in Gemini.** The pump deadlocks if the model abandons a call mid-flight. *File: `gemini_live.py:511`. Effort: 30 min.* Translate to a `tool_cancelled` event; audio_bridge cancels the in-flight task and advances the sequence pointer.
- **P0-2: Asymmetric transcripts on OpenAI.** Add `input_audio_transcription: {model: "whisper-1"}` to `session.update` and route the resulting `conversation.item.input_audio_transcription.completed` event to `transcript_partial/final` with `role=user`. Without this the Discord thread mirror only shows ARIA, not the user. *File: `openai_realtime.py:163-174` + `recv_events`. Effort: 45 min.*
- **P0-3: Drop the synthetic `(voice session starting)` closer in Gemini history.** Or validate empirically that it's still needed. The cleaner fix is to truncate prefill history *after the last assistant turn* — never end on a user message. *File: `gemini_live.py:228-234`, `_internal/history.py`. Effort: 1 hr including A/B test in VC.*

### P1 (agent readiness — needed for v0.5.0)

- **P1-1: Honor `tier` in ConnectOptions.** When `tier in {"read","act","meta"}` is set, build the tool catalogue from `meta_tools.py` + tier-filtered Hermes core tools. Plumb through `_resolve_bridge_params` in `discord_bridge.py` (currently hardcodes `tools=[]`). *Effort: half day.*
- **P1-2: Handle Gemini built-in tools.** Add `googleSearch: {}`, `urlContext: {}` to the `tools[]` array when the session is configured to allow web search. Allows agent loops without writing custom search wrappers. *File: `gemini_live._translate_tools` and `_build_setup`. Effort: 1 hr.*
- **P1-3: Honest "this was text, voice begins now" prefix on history.** Replace the silent `(voice session starting)` with an explicit role=model turn that says: "Above was a typed conversation. Voice mode now active. Wait for the user to speak." Gemini and OpenAI both accept this naturally. *Effort: 1 hr including test for both backends.*
- **P1-4: Set explicit `turn_detection` on OpenAI.** Default `server_vad` thresholds are too eager for Discord (chatters on background noise). Set `server_vad: {threshold: 0.6, prefix_padding_ms: 300, silence_duration_ms: 500}` or experiment with `semantic_vad`. *Effort: 2 hr including A/B in VC.*
- **P1-5: Reconnect logic.** When Gemini's `goAway` arrives or the WS closes mid-call, attempt one reconnect with the captured handle. When OpenAI hits session-cap, the audio bridge should observe `session_cap` and reconnect with replay of recent history. *Effort: half day.*

### P2 (quality of life)

- **P2-1: Streaming partial tool arguments on OpenAI.** Surface `response.function_call_arguments.delta` as `tool_call_partial` events; useful for showing "ARIA is thinking…" in the Discord thread.
- **P2-2: Async function calling (`behavior: "NON_BLOCKING"`) on Gemini.** Per-tool flag in `_translate_tools` allows long-running tools (web search) to run while the model keeps talking.
- **P2-3: `affective_dialog` and `proactive_audio` on Gemini** for warmer, more agentic UX. Read ai.google.dev/gemini-api/docs/live-guide#affective-dialog before enabling.
- **P2-4: Prompt caching key on OpenAI.** `session.update.session.prompt_cache_key="aria-base"` cuts time-to-first-byte for the same persona across sessions.
- **P2-5: Mixin to share `_send_history` between backends.** `_BaseRealtimeBackend._render_history_for_provider(history, provider="gemini"|"openai")` would dedupe ~30 lines and make tier-aware filtering centralized.
- **P2-6: Honest session resumption test.** No test in `tests/test_gemini_live.py` exercises the reconnect-with-handle path with non-empty history (the no-re-injection branch).

## Top 5 actionable items (recommended order)

| # | Item | Files | Effort | Why now |
|---|---|---|---|---|
| 1 | P0-1: `toolCallCancellation` handling | `gemini_live.py`, `audio_bridge.py` | 30 min | Latent deadlock before tools land |
| 2 | P0-2: OpenAI user-side transcripts | `openai_realtime.py` | 45 min | User immediately benefits — thread shows both sides |
| 3 | P1-3: Honest history prefix (drop hacky closer) | `gemini_live.py`, `history.py` | 1 hr | Cleaner UX + correctness |
| 4 | P1-4: Explicit `turn_detection` for OpenAI | `openai_realtime.py` | 2 hr | Reduces VAD chatter in VC |
| 5 | P1-1 scaffold: `tier` plumbing | `discord_bridge.py`, `connect_options.py`, `factory.py` | half day | Prereq for v0.5.0 tool work |

Items 1-3 are mechanical and ship as **v0.4.5** (~3 hours total).
Item 4 needs A/B testing in a VC and ships as v0.4.6.
Item 5 is the gateway to v0.5.0 — design + scaffold + tests; actual tool registration follows in v0.5.0.

## Sources consulted

- ai.google.dev/gemini-api/docs/live (Live API overview + protocol table)
- ai.google.dev/gemini-api/docs/live-guide (manual VAD, activity signals)
- ai.google.dev/gemini-api/docs/live-tools (function calling, async behavior, googleSearch)
- ai.google.dev/gemini-api/docs/live-session (sessionResumption handle flow)
- ai.google.dev/api/live (BidiGenerateContent reference: ClientContent, RealtimeInput, ToolResponse, SessionResumptionConfig)
- platform.openai.com/docs/guides/realtime (session.update, audio formats, model IDs)
- platform.openai.com/docs/guides/realtime-conversations (conversation.item.create patterns)
- platform.openai.com/docs/api-reference/realtime-client-events (event schemas)

Internal:
- `hermes_s2s/providers/realtime/__init__.py` (Protocol + base + ConnectOptions shim)
- `hermes_s2s/providers/realtime/gemini_live.py` (full file read)
- `hermes_s2s/providers/realtime/openai_realtime.py` (full file read)
- `hermes_s2s/_internal/history.py` (build_history_payload, find_most_recent_thread_session_id)
- `hermes_s2s/_internal/audio_bridge.py` (RealtimeAudioBridge._pump_output, _run_and_inject_tool)
- `hermes_s2s/voice/sessions_realtime.py` (RealtimeSession lifecycle)
- `hermes_s2s/voice/connect_options.py` (ConnectOptions dataclass with reserved v0.5.0 fields)
- ADRs 0008, 0013, 0014; research/15, 19
