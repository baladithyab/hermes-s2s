# Research 08 — Tool-Call Bridging Patterns for Realtime Voice Agents

**Date:** 2026-05-09
**Feeds:** ADR-0008 (Tool-call bridging in hermes-s2s 0.3.1)
**Sources:** OpenAI Realtime docs, Gemini Live docs, Pipecat docs/reference, LiveKit Agents docs.

---

## 1. Wire protocols — how each backend expects results

### OpenAI Realtime (WebSocket)
Two-step flow is the only reliable pattern (per community confirmation — embedding `function_call_output` in `response.create.input` produces "Tool call ID not found" errors):

```jsonc
// 1. tool result as a conversation item
{"type":"conversation.item.create",
 "item":{"type":"function_call_output","call_id":"call_...","output":"<string>"}}
// 2. then ask for the next turn
{"type":"response.create"}
```
`output` is a **string** (typically `JSON.stringify(result)`). Multiple tool calls in one turn → send one `conversation.item.create` per `call_id`, then a single `response.create`.
Sources: https://developers.openai.com/api/docs/guides/realtime-conversations ; https://community.openai.com/t/realtime-api-tool-call-id-not-found-in-conversation-after-sending-function-call-output-via-response-create/1302944

### Gemini Live (BidiGenerateContent)
Single message carries **all** responses as an array — order-independent, matched by `id`:

```python
await session.send_tool_response(function_responses=[
    types.FunctionResponse(id=fc.id, name=fc.name, response={"result": ...}),
    ...
])
```
`response` is a **dict** (not a string). Gemini docs explicitly state: "you don't need to return the `function_result` objects in the same order that the `function_call` objects were received. The Gemini API maps each result back to its corresponding call using the `id`."
Sources: https://ai.google.dev/gemini-api/docs/live-api/tools ; https://ai.google.dev/gemini-api/docs/function-calling

### Gemini Live async mode
Gemini Live supports **non-blocking** tool calls via a `behavior: NON_BLOCKING` flag on the function declaration, plus a scheduling policy (`SILENT | WHEN_IDLE | INTERRUPT`) for when the late result arrives. The model keeps talking while the backend runs the tool — unique among realtime APIs.
Source: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api/asynchronous-function-calling

---

## 2. Filler audio ("thinking" cue) patterns

| Framework | Pattern |
|---|---|
| **Pipecat** | Ship an `on_function_calls_started` event handler; example from docs literally does `await tts.queue_frame(TTSSpeakFrame("Let me check on that."))`. No pre-synthesis — the TTS engine handles it on first call. |
| **LiveKit** | Docs recommend letting the LLM speak a preamble before invoking the tool (prompt-level instruction: "acknowledge the user before calling a tool"). No built-in filler frame. |
| **OpenAI Realtime** | No framework primitive — the model itself will often say "one moment" before `response.function_call_arguments.done` if prompted to. |
| **Gemini Live** | Same as OpenAI — rely on the model, or use async mode so silence doesn't happen. |

**Practical takeaway:** pure TTS inject ("Let me check on that.") is the dominant production pattern. Pre-synthesizing a single WAV per session to avoid TTS latency on first call is a reasonable optimization but not standard; Pipecat queues a fresh TTS frame each time.
Sources: https://docs.pipecat.ai/pipecat/learn/llm ; https://docs.livekit.io/agents/logic/tools/definition/

---

## 3. Concurrent tool calls

Both APIs allow N concurrent `call_id`s in one turn. Both accept results **out of order**.

- **OpenAI Realtime**: N `conversation.item.create` events (one per call_id) then one `response.create`. Model waits for all.
- **Gemini Live**: one `tool_response` with a list of `FunctionResponse` (matched by `id`).

**Pipecat** runs them through a `FunctionCallRunnerItem` queue — executes in registration order but the runner is async, so effectively concurrent. **LiveKit** executes tools as asyncio tasks with `FunctionToolsExecutedEvent` fired after all complete.

**Recommendation:** `asyncio.gather()` all calls, collect results by `call_id`, inject in a single batch. Ordering back to the model is arbitrary for both APIs.

---

## 4. Timeout policy — what production does

- **Pipecat**: per-tool `timeout_secs` kwarg on `register_function` (no documented default — effectively unbounded unless set). Global `function_call_timeout_secs` on the LLM service. On timeout, fires `on_completion_timeout`. Also exposes `cancel_on_interruption` flag — if the user talks over a read-only call, drop it; for mutating calls, disable this.
  Source: https://reference-server.pipecat.ai/en/stable/api/pipecat.services.llm_service.html
- **LiveKit**: no framework-level tool timeout — tools raise `ToolError(message)` to return a graceful error string to the LLM. Docs explicitly say "Set explicit timeouts on your HTTP requests to avoid blocking the agent indefinitely." For MCP tools, LiveKit uses `timeout=30s` and `client_session_timeout_seconds=30` as community-reported safe defaults (5s default is too aggressive).
  Sources: https://docs.livekit.io/agents/logic/tools/definition/ ; https://community.livekit.io/t/mcp-tool-timeout-error-even-though-tool-calls-succeed/129
- **Conventions:** 5s is a common "soft" user-perceived latency threshold; 30s is the de-facto hard cap across MCP/LiveKit/LLM request defaults.

---

## 5. Result format — raw vs summarized

Both APIs accept arbitrary strings/dicts. Production frameworks **send the raw tool output**, not a pre-summarized version:
- Pipecat: whatever the handler returns is serialized into the `FunctionCallResultFrame` verbatim.
- LiveKit: tool return value is passed through as the `role: tool` message content.
- OpenAI docs example: `content: JSON.stringify({ horoscope })` — raw JSON.
- Gemini docs example: `response={"result": "ok"}` — raw dict.

For **realtime voice** the model has to synthesize spoken prose from this, and token-heavy JSON hurts TTFB. Community guidance (LiveKit MCP thread): keep tool outputs small, or pre-filter at dispatcher level. Neither framework auto-summarizes.

**Pragmatic rule:** truncate at ~4 KB before injection; let the model decide what to speak. If an output is structurally huge (table, large list), the tool itself should return a model-friendly summary — that's the tool author's job, not the bridge's.

---

## 6. Error injection shape

- **LiveKit** (`ToolError`): just a string. "Raise `ToolError` on failure. This returns the error message to the LLM so it can inform the user rather than crashing the tool." Content example: `"The weather service is unavailable. Try again later."`
- **Pipecat**: same pattern — return an error string from the handler; no special error channel.
- **OpenAI/Gemini**: both accept whatever string/dict you send. There is no "error" flag — the model reads English.

**Minimal viable error:** one sentence, states *what failed* and optionally *what to try*. No stack traces. No "I'm sorry" — that makes the model over-apologize. Format:

```json
{"error": "lookup_order timed out after 30s", "retryable": true}
```
The model reads the JSON and says something like "I couldn't reach the order system — want me to try again?" — exactly the recovery behaviour we want.

---

## 7. Recommended pattern for hermes-s2s

### 7.1 Dispatcher API shape

```python
# hermes_s2s/tools/bridge.py

from dataclasses import dataclass
from typing import Any

@dataclass
class ToolCall:
    call_id: str           # backend-assigned id
    name: str
    arguments: dict[str, Any]

@dataclass
class ToolResult:
    call_id: str
    output: str            # always string; JSON.dumps(dict) if structured
    is_error: bool = False

class ToolBridge:
    async def dispatch(self, call: ToolCall) -> ToolResult: ...
    async def dispatch_batch(self, calls: list[ToolCall]) -> list[ToolResult]: ...
```

### 7.2 Defaults
- **Soft timeout: 5s** — emit a filler-audio event (`"Let me check on that."` via TTS queue).
- **Hard timeout: 30s** — abort, inject `ToolResult(is_error=True, output='{"error":"<name> timed out","retryable":true}')`.
- **Filler strategy:** TTS a fixed phrase on first tool call per turn (not per session — resets each turn). Skip if the model has already spoken in the last 1.5s. Do not pre-synthesize a WAV in 0.3.1 — premature optimization; revisit if TTS TTFB becomes an issue.
- **Parallel-call ordering:** `asyncio.gather(*[dispatch(c) for c in calls], return_exceptions=True)` → collect → inject in one batch. Order is arbitrary, `call_id` is the join key. This matches both OpenAI (N items then one `response.create`) and Gemini (one `tool_response` with list).
- **Result truncation:** 4 KB cap on `output` string. Beyond that, replace tail with `"…[truncated; N more bytes]"`.
- **Error injection format:** `{"error": "<short reason>", "retryable": <bool>}` serialized as JSON string. Keep under 200 chars.
- **Cancellation:** per-tool `cancel_on_interruption: bool` (default True for read-only, False for mutating). Mirror Pipecat's flag.

### 7.3 Code skeleton

```python
import asyncio, json, logging
from hermes.tools import Dispatcher   # existing Hermes dispatcher

log = logging.getLogger(__name__)
SOFT_TIMEOUT_S = 5.0
HARD_TIMEOUT_S = 30.0
MAX_OUTPUT_BYTES = 4096

class HermesToolBridge(ToolBridge):
    def __init__(self, dispatcher: Dispatcher, on_soft_timeout=None):
        self.dispatcher = dispatcher
        self.on_soft_timeout = on_soft_timeout  # async callable → emit filler TTS

    async def dispatch(self, call: ToolCall) -> ToolResult:
        task = asyncio.create_task(self.dispatcher.run(call.name, call.arguments))
        try:
            return await asyncio.wait_for(asyncio.shield(task), SOFT_TIMEOUT_S)
        except asyncio.TimeoutError:
            if self.on_soft_timeout:
                asyncio.create_task(self.on_soft_timeout(call))
        try:
            raw = await asyncio.wait_for(task, HARD_TIMEOUT_S - SOFT_TIMEOUT_S)
            return self._ok(call.call_id, raw)
        except asyncio.TimeoutError:
            task.cancel()
            return self._err(call.call_id, f"{call.name} timed out", retryable=True)
        except Exception as e:
            log.exception("tool %s failed", call.name)
            return self._err(call.call_id, f"{call.name}: {e.__class__.__name__}", retryable=False)

    async def dispatch_batch(self, calls):
        return await asyncio.gather(*(self.dispatch(c) for c in calls))

    def _ok(self, call_id: str, raw) -> ToolResult:
        s = raw if isinstance(raw, str) else json.dumps(raw, default=str)
        if len(s) > MAX_OUTPUT_BYTES:
            s = s[:MAX_OUTPUT_BYTES] + f'…[truncated; {len(s)-MAX_OUTPUT_BYTES} more bytes]'
        return ToolResult(call_id=call_id, output=s, is_error=False)

    def _err(self, call_id: str, msg: str, *, retryable: bool) -> ToolResult:
        return ToolResult(call_id=call_id,
                          output=json.dumps({"error": msg, "retryable": retryable}),
                          is_error=True)
```

### 7.4 Backend adapters (thin)

```python
# OpenAI Realtime
async def inject_openai(ws, results: list[ToolResult]):
    for r in results:
        await ws.send_json({"type":"conversation.item.create",
                            "item":{"type":"function_call_output",
                                    "call_id":r.call_id,"output":r.output}})
    await ws.send_json({"type":"response.create"})

# Gemini Live
async def inject_gemini(session, results: list[ToolResult]):
    await session.send_tool_response(function_responses=[
        types.FunctionResponse(id=r.call_id, name="", response=json.loads(r.output)
                               if not r.is_error else {"error": r.output})
        for r in results
    ])
```

---

## 8. Open questions (not blocking 0.3.1)

1. Should we adopt Gemini's async mode (`behavior: NON_BLOCKING`) for Gemini backend only? Eliminates filler audio need but requires policy plumbing (`SILENT/WHEN_IDLE/INTERRUPT`). Defer to 0.4.
2. Pre-synthesized filler WAV cache — worthwhile if p99 TTS TTFB > 500ms. Benchmark first.
3. Streaming tool outputs (OpenAI community thread discusses partial `function_call_output`) — not standardized; skip.
