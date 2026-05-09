# 05 — Realtime WebSocket Protocols (Implementation-Grade)

Exact wire protocol reference for **Gemini Live API** and **OpenAI Realtime API**.
Companion to `02-realtime-apis.md` (positioning/pricing). This doc is what an
implementer codes against.

Sources:
- https://ai.google.dev/api/live (BidiGenerateContent reference)
- https://ai.google.dev/gemini-api/docs/live-api
- https://ai.google.dev/gemini-api/docs/live-api/capabilities
- https://ai.google.dev/gemini-api/docs/live-session
- https://platform.openai.com/docs/guides/realtime
- https://platform.openai.com/docs/guides/realtime-conversations
- https://developers.openai.com/api/docs/guides/realtime-websocket
- https://openai.com/api/pricing/

---

## 1. Gemini Live API (BidiGenerateContent)

### 1.1 Connect URL
```
wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=<API_KEY>
```
- **Auth**: API key as `?key=` query param (AI Studio). For Vertex AI use
  `wss://{LOCATION}-aiplatform.googleapis.com/ws/google.cloud.aiplatform.v1beta1.LlmBidiService/BidiGenerateContent`
  with `Authorization: Bearer <gcloud-token>`.
- **Model** is NOT in the URL — it goes inside the first `setup` message.
- Ephemeral tokens (browser-safe): mint via REST `/v1alpha/auth_tokens`, pass as `key=`.

### 1.2 Initial setup message (first frame, exactly once)
```json
{
  "setup": {
    "model": "models/gemini-2.5-flash-native-audio-preview-09-2025",
    "generationConfig": {
      "responseModalities": ["AUDIO"],
      "speechConfig": {
        "voiceConfig": { "prebuiltVoiceConfig": { "voiceName": "Puck" } },
        "languageCode": "en-US"
      }
    },
    "systemInstruction": { "parts": [{ "text": "You are a helpful assistant." }] },
    "tools": [{
      "functionDeclarations": [{
        "name": "get_weather",
        "description": "Get current weather",
        "parameters": { "type": "OBJECT",
          "properties": { "city": { "type": "STRING" } },
          "required": ["city"] }
      }]
    }],
    "realtimeInputConfig": {
      "automaticActivityDetection": { "disabled": false },
      "activityHandling": "START_OF_ACTIVITY_INTERRUPTS"
    },
    "inputAudioTranscription": {},
    "outputAudioTranscription": {},
    "sessionResumption": { "handle": null },
    "contextWindowCompression": { "slidingWindow": {} }
  }
}
```
Server replies with `BidiGenerateContentSetupComplete` before you send audio.

### 1.3 Audio framing (client → server)
- Format: **raw 16-bit PCM, little-endian, mono**.
- Sample rate: **16 kHz input** (other rates resampled server-side if MIME declares it).
- Declared per-chunk via MIME type: `audio/pcm;rate=16000`.
- Transport: **JSON text frame** with **base64** payload — Gemini does NOT accept
  binary WS frames for audio.
- Recommended chunk: **~100 ms** (3200 bytes @ 16 kHz/16-bit mono).
```json
{
  "realtimeInput": {
    "audio": { "data": "<base64 pcm16>", "mimeType": "audio/pcm;rate=16000" }
  }
}
```
Output audio from server is **24 kHz, 16-bit PCM, little-endian, mono**.

### 1.4 Server-emitted events
Top-level envelope — exactly one of these keys per message:
- `setupComplete` — handshake done
- `serverContent` — contains `modelTurn.parts[]` (audio blob, text) and/or
  `inputTranscription`, `outputTranscription`, `interrupted`, `turnComplete`,
  `generationComplete`
- `toolCall` — `functionCalls[]` with `id`, `name`, `args`
- `toolCallCancellation` — `ids[]` to cancel
- `goAway` — `{ "timeLeft": "Ns" }` server disconnecting soon
- `sessionResumptionUpdate` — `{ "newHandle": "...", "resumable": true }`
- `usageMetadata` — token accounting

**Audio chunk (serverContent):**
```json
{ "serverContent": { "modelTurn": { "parts": [
  { "inlineData": { "mimeType": "audio/pcm;rate=24000", "data": "<base64>" } }
]}}}
```
**Output transcript (partial/final interleaved):**
```json
{ "serverContent": { "outputTranscription": { "text": "Hello there" } } }
```
**Barge-in / interruption notice:**
```json
{ "serverContent": { "interrupted": true } }
```
**Tool call:**
```json
{ "toolCall": { "functionCalls": [
  { "id": "fc_abc123", "name": "get_weather", "args": { "city": "Paris" } }
]}}
```
**Session resumption update:**
```json
{ "sessionResumptionUpdate": { "newHandle": "CiQx...", "resumable": true } }
```

### 1.5 Tool-call round-trip
Server → client: `toolCall.functionCalls[].id` (call it `fc_abc123`).
Client → server:
```json
{
  "toolResponse": {
    "functionResponses": [{
      "id": "fc_abc123",
      "name": "get_weather",
      "response": { "result": { "tempC": 14, "condition": "cloudy" } }
    }]
  }
}
```
Model resumes audio generation automatically after `toolResponse` is received.

### 1.6 Interrupt mechanism
Two paths:
1. **Automatic barge-in** (default): when server VAD hears user speech, it sends
   `serverContent.interrupted: true` and stops generating. Just start streaming
   new `realtimeInput.audio`.
2. **Manual** (when `automaticActivityDetection.disabled=true`): send
   `{"realtimeInput":{"activityStart":{}}}` to interrupt, stream audio, then
   `{"realtimeInput":{"activityEnd":{}}}`. Controlled by `activityHandling`
   (`START_OF_ACTIVITY_INTERRUPTS` vs `NO_INTERRUPTION`).

### 1.7 Session lifecycle
- **Max session**: ~15 min audio-only; ~2 min audio+video (native-audio models).
  Server sends `goAway` before closing.
- **Resumption**: enable `sessionResumption` in setup → receive
  `sessionResumptionUpdate.newHandle`; reconnect with `sessionResumption.handle=<newHandle>`.
  **Tokens valid 2 h** after last disconnect.
- **Context compression**: `contextWindowCompression.slidingWindow` extends
  effective session past native context.
- **Cleanup**: on WS close the server tears down session; no explicit "end" frame
  required, but a clean WS close (1000) is expected.

### 1.8 Pricing units (native-audio Live models, AI Studio, Nov 2026)
Billed in **tokens**, not minutes. Audio is tokenized at a special rate:
- Audio input: **~$3.00 / 1M input tokens** (≈ 32 tokens/sec of audio).
- Audio output: **~$12.00 / 1M output tokens**.
- Text in/out priced at standard Gemini-2.5-Flash token rates.
Confirm current numbers at https://ai.google.dev/gemini-api/docs/pricing.

---

## 2. OpenAI Realtime API

### 2.1 Connect URL
```
wss://api.openai.com/v1/realtime?model=gpt-realtime
```
Headers:
```
Authorization: Bearer $OPENAI_API_KEY
OpenAI-Beta: realtime=v1
```
- Model is in the **query string**. Ephemeral client tokens via
  `POST /v1/realtime/client_secrets` for browser use.
- Current production model id: `gpt-realtime` (aka `gpt-4o-realtime-preview` legacy).

### 2.2 Initial session configuration
Server sends `session.created` on connect with defaults. Override via `session.update`:
```json
{
  "type": "session.update",
  "session": {
    "type": "realtime",
    "model": "gpt-realtime",
    "instructions": "You are a concise voice assistant.",
    "voice": "alloy",
    "input_audio_format": "pcm16",
    "output_audio_format": "pcm16",
    "input_audio_transcription": { "model": "gpt-4o-mini-transcribe" },
    "turn_detection": {
      "type": "server_vad",
      "threshold": 0.5,
      "prefix_padding_ms": 300,
      "silence_duration_ms": 500,
      "create_response": true
    },
    "tools": [{
      "type": "function",
      "name": "get_weather",
      "description": "Get current weather",
      "parameters": { "type": "object",
        "properties": { "city": { "type": "string" } },
        "required": ["city"], "additionalProperties": false }
    }],
    "tool_choice": "auto",
    "temperature": 0.8,
    "max_response_output_tokens": "inf"
  }
}
```
NOTE: tool schema has NO outer `"function": {...}` wrapper and NO `strict` key —
Realtime rejects the Chat Completions shape.

### 2.3 Audio framing (client → server)
- Formats supported: **`pcm16`** (24 kHz, 16-bit LE, mono), **`g711_ulaw`**,
  **`g711_alaw`** (both 8 kHz, telephony).
- Transport: **JSON text frame** with **base64**-encoded bytes. No binary frames.
- Recommended chunk: **20–100 ms** (pcm16@24k → 960–4800 bytes per chunk).
```json
{ "type": "input_audio_buffer.append", "audio": "<base64 pcm16 chunk>" }
```
Without VAD you additionally send `input_audio_buffer.commit` then
`response.create`. With `server_vad` (default), commit+response are automatic.

### 2.4 Server-emitted events (full enumeration)
Lifecycle: `session.created`, `session.updated`, `error`.
Input buffer: `input_audio_buffer.speech_started`, `input_audio_buffer.speech_stopped`,
`input_audio_buffer.committed`, `input_audio_buffer.cleared`.
Conversation: `conversation.item.created`, `conversation.item.truncated`,
`conversation.item.deleted`,
`conversation.item.input_audio_transcription.delta`,
`conversation.item.input_audio_transcription.completed`,
`conversation.item.input_audio_transcription.failed`.
Response: `response.created`, `response.output_item.added`,
`response.content_part.added`, `response.audio.delta`, `response.audio.done`,
`response.audio_transcript.delta`, `response.audio_transcript.done`,
`response.text.delta`, `response.text.done`,
`response.function_call_arguments.delta`, `response.function_call_arguments.done`,
`response.output_item.done`, `response.done`, `rate_limits.updated`,
`output_audio_buffer.started`, `output_audio_buffer.stopped`, `output_audio_buffer.cleared`.

**Top 5 shapes:**

`session.created`:
```json
{ "type": "session.created", "event_id": "event_1",
  "session": { "id": "sess_abc", "model": "gpt-realtime",
    "voice": "alloy", "input_audio_format": "pcm16",
    "output_audio_format": "pcm16", "...": "..." } }
```
`response.audio.delta` (base64 PCM16 @ 24 kHz chunk):
```json
{ "type": "response.audio.delta", "event_id": "event_42",
  "response_id": "resp_1", "item_id": "item_1",
  "output_index": 0, "content_index": 0,
  "delta": "<base64 pcm16>" }
```
`response.audio_transcript.delta`:
```json
{ "type": "response.audio_transcript.delta",
  "response_id": "resp_1", "item_id": "item_1", "delta": "Hello" }
```
`response.function_call_arguments.done`:
```json
{ "type": "response.function_call_arguments.done",
  "response_id": "resp_1", "item_id": "item_2",
  "call_id": "call_xyz789", "name": "get_weather",
  "arguments": "{\"city\":\"Paris\"}" }
```
`response.done`:
```json
{ "type": "response.done",
  "response": { "id": "resp_1", "status": "completed",
    "output": [ /* items */ ],
    "usage": { "total_tokens": 512, "input_tokens": 120,
      "output_tokens": 392,
      "input_token_details": { "audio_tokens": 80, "text_tokens": 40 },
      "output_token_details": { "audio_tokens": 360, "text_tokens": 32 } }}}
```
`error`:
```json
{ "type": "error", "error": { "type": "invalid_request_error",
  "code": "invalid_value", "message": "...", "param": "session.voice",
  "event_id": "event_9" } }
```

### 2.5 Tool-call round-trip
Server streams `response.function_call_arguments.delta` then `.done` with
`call_id` (e.g. `call_xyz789`). Client posts TWO events:
```json
{ "type": "conversation.item.create",
  "item": { "type": "function_call_output",
            "call_id": "call_xyz789",
            "output": "{\"tempC\":14,\"condition\":\"cloudy\"}" } }
```
```json
{ "type": "response.create" }
```
The second event is **required** — submitting the output does not auto-trigger a
new response. `output` must be a JSON **string**.

### 2.6 Interrupt mechanism
- **With server_vad**: speaking over the model auto-fires
  `input_audio_buffer.speech_started` and the server cancels in-flight response.
- **Manual (push-to-talk / WebRTC)**: client sends
  ```json
  { "type": "response.cancel" }
  ```
  To drop queued server audio the client hasn't played yet:
  ```json
  { "type": "output_audio_buffer.clear" }
  ```
  To truncate a partially-played assistant item so the transcript matches what
  the user heard:
  ```json
  { "type": "conversation.item.truncate",
    "item_id": "item_1", "content_index": 0, "audio_end_ms": 1840 }
  ```

### 2.7 Session lifecycle
- **Hard cap**: **30 minutes** per WS connection. Server closes; no resumption
  handle — client must reconnect and re-send `session.update` + replay context
  via `conversation.item.create` items.
- **Idle**: sessions also close on extended inactivity.
- **Cleanup**: close WS (1000) on client side; server drops session state.
- **Rate limits**: streamed via `rate_limits.updated` after each response.

### 2.8 Pricing units (gpt-realtime, Nov 2026)
Billed as **tokens** (audio tokenized specially, ~ a few dozen tokens/sec):
- Text input: **$4 / 1M tok**, text output: **$16 / 1M tok**.
- Audio input: **$32 / 1M tok** (≈ $0.06/min), audio output: **$64 / 1M tok**
  (≈ $0.24/min).
- Cached input audio: **$0.40 / 1M tok**.
- `gpt-4o-mini-realtime-preview` is ~10× cheaper on audio.
Authoritative: https://openai.com/api/pricing/.

---

## 3. Cross-reference cheatsheet

| Concern              | Gemini Live                                   | OpenAI Realtime                                 |
| -------------------- | --------------------------------------------- | ----------------------------------------------- |
| Input audio          | PCM16 16k mono (MIME declared)                | PCM16 24k mono OR G.711 8k                      |
| Output audio         | PCM16 24k mono                                | PCM16 24k OR G.711 8k                           |
| Wire frame           | JSON + base64                                 | JSON + base64                                   |
| Tool-call id field   | `id`                                          | `call_id`                                       |
| Tool result envelope | `toolResponse.functionResponses[]`            | `conversation.item.create` (`function_call_output`) + `response.create` |
| Interrupt            | auto via VAD; manual `activityStart/End`      | auto via `server_vad`; manual `response.cancel` + `output_audio_buffer.clear` |
| Max session          | ~15 min, resumable via token (2 h TTL)        | 30 min hard cap, no resumption                  |
| Billing              | tokens (audio≈32 tok/s)                       | tokens (audio special multiplier)               |
