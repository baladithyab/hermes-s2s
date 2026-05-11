# Research 18 — Realtime history injection for voice sessions

Status: **Design** — not yet implemented
Target version: **0.4.2**
Authors: hermes-s2s core
Related: ADR-0012 (voice thread co-management), research-05 (realtime WS protocols)

## 1. Problem

When a user runs `/voice join` after chatting with ARIA in a Discord thread,
the Gemini Live realtime session is born with **only the system prompt**.
The model has no idea what was just discussed in text and starts every voice
call from zero. Users perceive this as ARIA having "amnesia" at the moment
they switch to voice.

Root cause: `_build_setup()` in `providers/realtime/gemini_live.py:192`
only emits `systemInstruction`. Nothing ever sends the text-chat history
down the wire.

## 2. Protocol mechanism (Gemini Live)

Gemini Live's `BidiGenerateContentClientContent` frame accepts a `turns[]`
list of prior `{role, parts}` dicts and a `turnComplete` boolean. When sent
**after `setupComplete` and before the first `realtimeInput`** with
`turnComplete:true`, the server absorbs the turns as context and **does not
produce an audio response** — it simply updates its conversation state.

```json
{"clientContent": {
  "turns": [
    {"role": "user",  "parts": [{"text": "What's the weather?"}]},
    {"role": "model", "parts": [{"text": "Seventy and sunny."}]}
  ],
  "turnComplete": true
}}
```

This is exactly the same primitive already used by `send_filler_audio()`
(line 417) — the only differences are multiple turns and the `"model"` role
for assistant entries.

### Why `turnComplete:true` doesn't trigger audio

Gemini emits audio only when the **last** turn in the session is a `user`
turn that has been finalised. If we close the injected batch with a `model`
turn, the model sees "I already spoke, now I'm waiting" and stays silent.
If the last injected turn is `user` we flip the final role to `model` or
append an empty-content model closer — see §6.

## 3. Where to fetch history

**Decision: use Hermes core's `SessionDB.get_messages_as_conversation()`**
(hermes_state.py:1683). It returns the canonical, sanitised OpenAI-format
`[{"role","content"}, ...]` list the text agent already uses, so we get
exact parity with what the text conversation looked like.

### Resolving the `session_id` from a VC join

The plugin sits downstream of the Discord gateway adapter. The adapter
owns a `SessionStore` that maps a `source` → `session_key` via
`session_store._generate_session_key(source)` (gateway/run.py:1659).

Inside `_attach_realtime_to_voice_client` (discord_bridge.py:717) we have:

- `adapter` — the gateway DiscordAdapter
- `voice_client` — has `.guild`, `.channel`
- `ctx` — Hermes `PluginContext`, does **not** expose `session_db` today

Resolution algorithm (new helper `_resolve_session_id_for_thread`):

1. Prefer the thread we just resolved in step (a) of the bridge —
   `thread_id` is already in `adapter._s2s_transcript_mirrors[guild_id]`.
2. Build a synthetic `source` namespace with `chat_id=thread_id,
   chat_type="thread", platform="discord"` matching the shape
   `_generate_session_key()` expects.
3. Call `adapter.session_store._generate_session_key(source)` →
   `session_key`. The adapter's `SessionStore` in-memory map
   (`_entries`) gives us the `session_id` persisted in SQLite.
4. Fall back to `None` if any step fails — history injection is
   best-effort, voice still works without it.

SessionDB itself is a thin wrapper over SQLite — reopening it with
`hermes_state.SessionDB()` from the plugin is safe (the class manages
its own file lock and reader connection). We do **not** need to bolt
a new method onto `PluginContext`.

### Why not ask Discord directly?

We considered fetching `thread.history(limit=N)` from the Discord API:

- **Pro:** zero coupling to Hermes internals.
- **Con:** returns user-visible text only — tool calls, assistant's
  reasoning chunks, and system-side summaries are missing. Also racy
  against in-flight Hermes writes and costs a rate-limited REST round
  trip on every VC join.

Reject. Keep Discord history as a last-resort fallback for when the
session DB path fails (forum threads, fresh bot process).

## 4. Token budget & formatting

### 4.1 Gemini Live context window

- Gemini 2.5 Flash native-audio: **1M-token context window**, but audio
  frames consume that budget voraciously (~32 tokens per second).
- The plugin already sets `contextWindowCompression: {slidingWindow: {}}`
  (line 208) so audio older than the slide gets dropped automatically.
  Injected text turns ride on top of that same budget.

**Budget for injected history: 8k tokens by default (~32 KB of text),
hard cap 32k.** This leaves ~15 minutes of realtime audio headroom
before sliding-window compression kicks in. Exposed as
`s2s.voice.realtime.history.max_tokens` (default `8000`).

### 4.2 Turn selection

- **Last-N raw turns, default N=20** (`history.max_turns`).
- Filter:
  - Keep `role in {"user","assistant"}` only.
    Drop `"system"` (we already have `systemInstruction`) and `"tool"`
    / `"function"` (noisy; Gemini can't re-execute them anyway).
  - Drop empty / whitespace-only content.
  - Coerce structured `content` lists (OpenAI multimodal format) to
    plain text by concatenating any `{"type":"text","text":...}`
    entries; drop image/audio parts.
- Truncate from the **oldest** end until the rendered token count is
  ≤ `max_tokens`. Use a cheap heuristic (`len(text)//4`) — we don't
  want a tokenizer dependency.
- Never split a turn — if the single newest turn exceeds the budget,
  truncate its text to budget with an ellipsis suffix
  `"...[truncated]"`.

### 4.3 Summarisation

Out of scope for 0.4.2. Raw-last-N is dramatically simpler and covers
~95% of real Discord thread lengths. We can add summarisation later
(call core's title/summary helper) behind a config flag if users report
cliff-edge truncation in long threads.

### 4.4 Deduplication

The text thread and the voice session write to the **same** SessionDB
via the transcript mirror. A user who says "hi" in voice and the mirror
writes it to the thread is fine on first join; on **reconnect** within
the same session we'd re-inject turns that the model already produced.

Mitigation: Gemini's `sessionResumption` handle path (already wired,
line 213) reuses server-side state and skips the initial
`clientContent`. So history injection runs **only on the first
connect, not on resumption reconnects**. Tracked by
`self._history_injected = False` on `__init__`, flipped to `True`
after first successful send.

## 5. Code change (gemini_live.py)

### 5.1 Accept history on `connect()`

Extend the backend protocol. `_BaseRealtimeBackend.connect` signature
becomes:

```python
async def connect(
    self,
    system_prompt: str,
    voice: str,
    tools: List[Dict[str, Any]],
    history: Optional[List[Dict[str, Any]]] = None,
) -> None: ...
```

Keyword-only default of `None` preserves backward compatibility for
existing tests. `RealtimeSession._on_start()` (sessions_realtime.py:161)
forwards `self._history` which the factory sets from spec options.

### 5.2 Wedge point in `GeminiLiveBackend.connect`

Insert after the `setupComplete` wait (line 149), before returning:

```python
# --- NEW: history injection (research-18) ---
if history:
    await self._send_history(history)
    self._history_injected = True
```

And add:

```python
async def _send_history(self, turns: List[Dict[str, Any]]) -> None:
    """Inject prior text-conversation turns as context.

    Sends a single BidiGenerateContentClientContent with role-mapped
    turns and turnComplete:true. Must be called AFTER setupComplete
    and BEFORE the first realtimeInput.audio frame.
    """
    gemini_turns: List[Dict[str, Any]] = []
    for t in turns:
        role = "user" if t.get("role") == "user" else "model"
        text = _extract_text(t.get("content"))
        if not text.strip():
            continue
        gemini_turns.append({"role": role, "parts": [{"text": text}]})
    if not gemini_turns:
        return
    # Ensure final turn is role="model" so Gemini doesn't speak.
    if gemini_turns[-1]["role"] == "user":
        gemini_turns.append(
            {"role": "model", "parts": [{"text": "(ready)"}]}
        )
    msg = {"clientContent": {"turns": gemini_turns, "turnComplete": True}}
    await self._ws.send(json.dumps(msg))
```

`_extract_text` handles str / list-of-parts content shape.

### 5.3 Where the history list is assembled

New helper in `hermes_s2s/voice/history.py`:

```python
def build_history_payload(
    session_db: Any,
    session_id: str,
    *,
    max_turns: int = 20,
    max_tokens: int = 8000,
) -> list[dict]: ...
```

Called from `_attach_realtime_to_voice_client` just before
`factory.build(...)` and stashed in `spec.options["history"]`. The
session forwards it to `backend.connect(..., history=...)`.

## 6. OpenAI Realtime parity

OpenAI Realtime uses a **different primitive** —
`conversation.item.create` events, one per prior turn, sent after
`session.update` and before the first `input_audio_buffer.append`:

```json
{"type": "conversation.item.create",
 "item": {"type": "message",
          "role": "user" | "assistant",
          "content": [{"type": "input_text" | "text", "text": "..."}]}}
```

Notes:
- `input_text` for user turns; `text` for assistant turns (the docs are
  inconsistent but both work).
- No explicit "commit" — each item is appended to the server-side
  conversation state as it arrives.
- Do **not** emit `response.create` after the batch, or the model
  will speak unprompted.

Implementation mirrors Gemini: extend `connect()` signature, loop over
`history` and `_send_json` one `conversation.item.create` per turn.
Same selection / budget helper is shared (`build_history_payload`).

## 7. Test strategy

New file `tests/test_realtime_history_injection.py`:

1. **Gemini — order:** mock `websockets.connect` with a recording WS.
   Drive `backend.connect(system, voice, tools, history=[...])`.
   Assert the sent frames sequence is exactly:
   `setup` → (server `setupComplete`) → `clientContent` → nothing else.
2. **Gemini — payload shape:** assert the `clientContent.turns` list
   has correct role mapping (`user`→`user`, `assistant`→`model`) and
   `turnComplete == True`.
3. **Gemini — final role:** given a history ending in `user`, assert
   the backend appended a synthetic `model` closer.
4. **Gemini — empty history:** `history=None` and `history=[]` must
   not emit any `clientContent` frame.
5. **OpenAI — order:** assert one `conversation.item.create` per turn,
   no `response.create` tail.
6. **Budget truncation:** pass a 100-turn history; assert ≤
   `max_turns` turns reach the wire and total rendered chars ≤
   `max_tokens*4`.
7. **Session resumption:** connect with `sessionResumption.handle`
   set and history present → assert **no** `clientContent` emitted
   (second-connect path).
8. **Threads integration (small):** monkey-patch `SessionDB` with an
   in-memory stub, drive `build_history_payload` and assert it filters
   out system/tool roles and coerces multimodal content to text.

## 8. Config surface

```yaml
s2s:
  voice:
    realtime:
      history:
        enabled: true          # default true
        max_turns: 20          # last-N raw turns
        max_tokens: 8000       # soft budget for rendered text
        include_system: false  # never inject system (already in prompt)
```

Stored in `spec.options["history_config"]`; plumbed through
`VoiceSessionFactory` to `RealtimeSession` which calls the builder.

## 9. Rollout

- 0.4.2: Gemini + OpenAI implementations behind
  `history.enabled=true` (default on), raw-last-20 default.
- 0.4.3: observability — emit a
  `realtime.history.injected{turns,tokens}` metric.
- 0.5.x: optional summarisation path for threads > 50 turns.

## 10. Risks

- **Latency on join:** extra WS round trip after `setupComplete`.
  Measured via the mock test — budget < 50 ms for typical 20-turn
  payloads.
- **Privacy:** injected text reaches Google/OpenAI servers. Same
  surface as the existing text-chat path — no new boundary crossed.
- **Prompt injection amplification:** a malicious user could plant
  "ignore previous instructions" in text then escalate via voice.
  Mitigated by the `language anchor` + `systemInstruction` still
  leading every turn, plus existing Hermes content filters on
  persisted messages.
