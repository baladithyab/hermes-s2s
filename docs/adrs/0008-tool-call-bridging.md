# ADR-0008: Tool-call bridging — soft/hard timeout + filler audio + parallel by call_id

**Status:** accepted
**Date:** 2026-05-09
**Driven by:** [research/08-tool-call-bridging-patterns.md](../design-history/research/08-tool-call-bridging-patterns.md)

## Context

Realtime backends emit `tool_call` events that we must (a) dispatch via Hermes's tool registry, (b) inject results back, (c) handle gracefully when tools take too long or fail. Research surfaced production patterns from Pipecat, LiveKit Agents, and the OpenAI/Gemini Live docs.

Key wire-format facts:
- **OpenAI Realtime**: requires the client to send TWO messages — `conversation.item.create` (with `function_call_output`) THEN `response.create` to make the model continue. Order matters; both required.
- **Gemini Live**: `send_tool_response` with one or more `functionResponses[]` keyed by `id`. Order-independent. Auto-resumes after.
- **Gemini async tools (unique)**: declaring a tool with `behavior: NON_BLOCKING` + scheduling `SILENT|WHEN_IDLE|INTERRUPT` lets the model continue talking while the tool runs. Eliminates the "model goes silent for 15s" UX problem.

## Decision

Implement `HermesToolBridge` with these conventions:

### 1. Timeout policy

- **Soft timeout (default 5s)**: trigger filler audio.
- **Hard timeout (default 30s)**: cancel the tool, inject `{"error": "tool timed out"}`.
- Both configurable via `s2s.realtime.tool_timeout_soft` / `tool_timeout_hard` config keys.
- Use `asyncio.wait_for(asyncio.shield(coro), hard)` so cancelling the wait doesn't cancel the underlying task in the soft case.

### 2. Filler audio (Pipecat pattern)

When `s2s.realtime.provider == "openai-realtime"`:
- On soft-timeout, send `response.create` with an `instructions` override telling the model to say "let me check on that" once. This is the Pipecat pattern — no pre-synthesis, no caching, model speaks naturally.
- Cancel that filler if the result arrives before the model finishes speaking it (no good clean way; accept the extra second of speech).

When `s2s.realtime.provider == "gemini-live"`:
- If the tool was declared with `behavior: NON_BLOCKING` (set via tool registration option `non_blocking: true`), no filler needed — model continues talking on its own.
- Otherwise: same approach as OpenAI but using Gemini's equivalent (a one-shot `clientContent` with text "let me check on that").

### 3. Parallel tool calls

If a single backend turn emits multiple `tool_call` events:
- Run all tools concurrently via `asyncio.gather(*[dispatch(c) for c in calls])`.
- Inject results in the order the model gave them, NOT the order they finished. The model already wired its response logic to specific call_ids.
- For OpenAI: send all `conversation.item.create` (function_call_output) first, then ONE `response.create`. For Gemini: a single `send_tool_response` batch.

### 4. Error injection format

```json
{"error": "<short human-readable reason>", "retryable": false}
```

Examples:
- `{"error": "tool timed out after 30s", "retryable": true}`
- `{"error": "tool not found: search_web", "retryable": false}`
- `{"error": "permission denied", "retryable": false}`

The model treats these as JSON strings — no special error flag in the API. Keep it short; the model reads it and recovers (apologizes, suggests alternatives, etc.).

### 5. Result truncation

Cap raw tool output at 4KB (configurable). For larger outputs (web scrapes, file dumps) the bridge truncates to 4KB and appends `\n\n[result truncated; original length: N bytes]`. Avoids blowing up the backend's context window mid-conversation.

### 6. Cancel-on-interruption flag

Mirror Pipecat's `cancel_on_interruption` — if the user interrupts the bot mid-tool-call, by default cancel the tool. Set per-tool to `false` for mutating operations (e.g., a `send_email` tool should complete even if the user changes the subject).

For 0.3.1 the default is `true` (cancel on interrupt). Per-tool overrides come in a follow-up wave when we add tool metadata.

## Implementation shape

```python
# hermes_s2s/_internal/tool_bridge.py

class HermesToolBridge:
    def __init__(self, dispatch_tool, soft_timeout=5.0, hard_timeout=30.0,
                 result_max_bytes=4096):
        self._dispatch = dispatch_tool       # Hermes's ctx.dispatch_tool
        self._soft = soft_timeout
        self._hard = hard_timeout
        self._max = result_max_bytes
        self._inflight: dict[str, asyncio.Task] = {}

    async def handle_tool_call(self, backend, call_id, name, args) -> str:
        """Returns the result string to inject back."""
        coro = self._run_with_timeouts(backend, call_id, name, args)
        task = asyncio.create_task(coro)
        self._inflight[call_id] = task
        try:
            return await task
        finally:
            self._inflight.pop(call_id, None)

    async def _run_with_timeouts(self, backend, call_id, name, args):
        try:
            tool_task = asyncio.create_task(self._invoke_tool(name, args))
            try:
                result = await asyncio.wait_for(asyncio.shield(tool_task), self._soft)
            except asyncio.TimeoutError:
                # Soft timeout — fire filler, keep waiting
                await backend.send_filler_audio("let me check on that")
                try:
                    result = await asyncio.wait_for(tool_task, self._hard - self._soft)
                except asyncio.TimeoutError:
                    tool_task.cancel()
                    return json.dumps({"error": f"tool timed out after {self._hard}s",
                                       "retryable": True})
            return self._truncate(self._serialize(result))
        except Exception as exc:
            return json.dumps({"error": str(exc), "retryable": False})

    async def cancel_all(self):
        """Called on bridge close or user interrupt."""
        for task in list(self._inflight.values()):
            task.cancel()
        self._inflight.clear()

    def _truncate(self, s: str) -> str:
        if len(s.encode("utf-8")) <= self._max:
            return s
        truncated = s.encode("utf-8")[:self._max].decode("utf-8", errors="ignore")
        return truncated + f"\n\n[result truncated; original length: {len(s)} bytes]"
```

The `backend.send_filler_audio` call is a new method we add to the `RealtimeBackend` Protocol — implementations differ per backend (see decision §2).

## Consequences

**Positive:**
- Predictable UX — user never waits >5s in silence.
- Clean API — bridge owns timeout policy, backend owns filler delivery, dispatcher owns Hermes integration.
- Errors recover gracefully without breaking the conversation.

**Negative:**
- Filler-audio adds 1-2s of "let me check" speech before the actual answer. For tools that finish in 5.5s, the user hears the filler unnecessarily. Mitigation: configurable `tool_timeout_soft`; users on a fast backend can lower to 3s or disable.
- 4KB truncation is arbitrary. May truncate a useful URL or table cell. Mitigation: log full output to Hermes's session log so it's recoverable.
- `cancel_on_interruption=true` default could cancel a `send_email` mid-flight if user changes the subject. Mitigation: per-tool override comes in 0.4.0; for 0.3.1 document the behavior loudly.

## Alternatives considered

- **No filler audio** — silence for up to 30s. Tested in early prototypes; users perceive it as "the bot froze." Rejected.
- **Pre-synthesized filler audio bytes** — saves a round-trip to TTS. But Pipecat showed dynamic TTS works fine and avoids speaker mismatch (filler in Edge voice but reply in OpenAI voice = jarring). Rejected.
- **Hard timeout only, no soft** — simpler but UX worse (wait 30s in silence). Rejected.
- **Per-tool timeouts as defaults** — over-engineered for 0.3.1. Defer.

## Open questions

1. How does `dispatch_tool` integrate with Hermes's plugin context? The plugin context (`ctx`) has `dispatch_tool(name, args)` per Hermes plugin docs — the bridge holds a reference to it from `register(ctx)` time.
2. Filler-audio cost — every interrupted tool burns ~$0.001 of synthesis. Acceptable. Document.
3. Per-tool `non_blocking` flag for Gemini — defer to 0.4.0 unless a real use case appears.
