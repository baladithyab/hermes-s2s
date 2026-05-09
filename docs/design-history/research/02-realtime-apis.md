# 02 — Realtime Speech-to-Speech APIs (late 2026)

Scope: Gemini Live + OpenAI Realtime only. Pricing pulled Nov 2026.

---

## 1. Gemini Live API (Google)

- **Status:** Live API itself is GA on Vertex AI; flagship speech model `gemini-2.5-flash-native-audio` is GA. `gemini-3.1-flash-live-preview` is preview-only.
- **WS endpoint:**
  `wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=$API_KEY`
  (Vertex: `wss://{region}-aiplatform.googleapis.com/ws/google.cloud.aiplatform.v1beta1.LlmBidiService/BidiGenerateContent`)
  Ref: https://ai.google.dev/api/live , https://ai.google.dev/gemini-api/docs/live-api/get-started-websocket
- **Audio in:** 16-bit PCM, **16 kHz**, mono, little-endian, base64 in `realtimeInput.mediaChunks[].data` (mime `audio/pcm;rate=16000`).
- **Audio out:** 16-bit PCM, **24 kHz**, mono. Native-audio models stream raw PCM; half-cascade also 24 kHz.
- **Tool calling:** Yes — function calling + Google Search + code execution. Server emits `toolCall`, client replies `BidiGenerateContentToolResponse`.
- **Voices:** 30+ prebuilt voices (Puck, Charon, Kore, Fenrir, Aoede, Leda, Orus, Zephyr, …) for half-cascade; native-audio adds expressive/affective voices. 24 languages.
- **Max session length:** Audio-only ~15 min, audio+video ~2 min per WS. Extend via **session resumption** tokens (`sessionResumption`) + **context window compression** → effectively unbounded. "Go-away" warning sent before forced disconnect.
- **Pricing (native audio, `gemini-2.5-flash-native-audio`):**
  - Audio in: **$3.00 / 1M tokens** (~32 tok/s → ≈ **$0.0058/min**)
  - Audio out: **$12.00 / 1M tokens** (~25 tok/s → ≈ **$0.018/min**)
  - Text in/out: $0.50 / $2.00 per 1M
  - Cite: https://ai.google.dev/gemini-api/docs/pricing
- **Pricing (half-cascade, `gemini-live-2.5-flash`):** audio in $0.50/1M, audio out $2.00/1M (≈ $0.00096/min in, $0.003/min out). Much cheaper, slightly lower voice quality.
- **Free tier:** Yes on AI Studio for dev (rate-limited).

---

## 2. OpenAI Realtime API

- **Status:** **GA since Aug 28 2025**. Flagship model: `gpt-realtime` (replaces `gpt-4o-realtime-preview`). Supports WebSocket, WebRTC, SIP, MCP, image input.
  Ref: https://openai.com/index/introducing-gpt-realtime/
- **WS endpoint:**
  `wss://api.openai.com/v1/realtime?model=gpt-realtime`
  Headers: `Authorization: Bearer $OPENAI_KEY`, `OpenAI-Beta: realtime=v1`
- **Audio in:** PCM16 **24 kHz** mono (also g711_ulaw/alaw 8 kHz for SIP); base64 in `input_audio_buffer.append`.
- **Audio out:** PCM16 **24 kHz** mono default (or g711 for phone).
- **Tool calling:** Yes — function tools, **remote MCP servers** (GA), parallel tool calls, image attachment mid-session.
- **Voices:** 10+ (Alloy, Ash, Ballad, Coral, Echo, Sage, Shimmer, Verse, + new Cedar & Marin exclusive to gpt-realtime).
- **Max session length:** **30 min per WS** (hard cap). Reconnect + replay summarized context to continue.
- **Pricing `gpt-realtime` (flagship, GA 2025-08-28):**
  - Audio in: **$32.00 / 1M tokens** (≈ **$0.032/min** — OpenAI quotes ~$0.06/min at preview; GA repriced down)
  - Audio out: **$64.00 / 1M tokens** (≈ **$0.064/min**, OpenAI says ~$0.24/min including overhead of interleaved text)
  - Cached audio in: $0.40/1M
  - Text in/out: $4 / $16 per 1M
  - Cite: https://cloudprice.net/models/openai/gpt-realtime , https://developers.openai.com/api/docs/pricing
- **Pricing `gpt-4o-mini-realtime-preview`:**
  - Audio in: **$10.00 / 1M** (≈ $0.01/min)
  - Audio out: **$20.00 / 1M** (≈ $0.02/min)
  - Text in/out: $0.60 / $2.40 per 1M
  - Cite: https://www.eesel.ai/blog/gpt-realtime-mini-pricing , reddit confirm https://www.reddit.com/r/OpenAI/comments/1hgxz8e/
- **New `gpt-realtime-mini` (Oct 2025):** ~70% cheaper than flagship; audio in ~$10/1M, out ~$20/1M (same tier as 4o-mini-realtime). Preview.

---

## 3. 30-min call cost (50/50 split → 15 min user audio in, 15 min agent audio out)

Using rough token rates: input ≈ 32 tok/s (Gemini) / OpenAI bills ~300 tok/min audio in, 600 tok/min out at 24 kHz; cross-checked with per-minute estimates from vendor pages.

| Model | Audio-in cost | Audio-out cost | **30-min total** |
|---|---|---|---|
| Gemini 2.5 Flash native-audio | 15 × $0.0058 = $0.087 | 15 × $0.018 = $0.27 | **~$0.36** |
| Gemini Live 2.5 Flash (half-cascade) | 15 × $0.00096 = $0.014 | 15 × $0.003 = $0.045 | **~$0.06** |
| OpenAI `gpt-realtime` (GA) | 15 × $0.032 = $0.48 | 15 × $0.064 = $0.96 | **~$1.44** |
| OpenAI `gpt-realtime-mini` / `gpt-4o-mini-realtime` | 15 × $0.010 = $0.15 | 15 × $0.020 = $0.30 | **~$0.45** |

Excludes text/system-prompt tokens, tool-call overhead, and interleaved transcript billing (adds 10-30%).

---

## 4. WebSocket message flow

### 4a. Gemini Live (pseudocode)

```python
import json, base64, websockets
URL = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=KEY"
async with websockets.connect(URL) as ws:
    # 1. setup
    await ws.send(json.dumps({"setup": {
        "model": "models/gemini-2.5-flash-native-audio",
        "generationConfig": {"responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Aoede"}}}},
        "tools": [{"functionDeclarations": [{"name": "get_weather", "parameters": {...}}]}]}}))
    # 2. stream mic audio (PCM16 16kHz, base64 chunks ~20ms)
    await ws.send(json.dumps({"realtimeInput": {"mediaChunks": [
        {"mimeType": "audio/pcm;rate=16000", "data": base64.b64encode(pcm).decode()}]}}))
    async for raw in ws:
        msg = json.loads(raw)
        if "serverContent" in msg:      # 3. audio out (inlineData.data b64 PCM 24kHz)
            play(base64.b64decode(msg["serverContent"]["modelTurn"]["parts"][0]["inlineData"]["data"]))
        if msg.get("serverContent", {}).get("interrupted"):  # 4. barge-in → stop local playback
            stop_playback()
        if "toolCall" in msg:           # 5. tool call
            result = run_tool(msg["toolCall"]["functionCalls"][0])
            await ws.send(json.dumps({"toolResponse": {"functionResponses": [result]}}))
```

### 4b. OpenAI Realtime (pseudocode)

```python
import json, base64, websockets
URL = "wss://api.openai.com/v1/realtime?model=gpt-realtime"
HDR = {"Authorization": f"Bearer {KEY}", "OpenAI-Beta": "realtime=v1"}
async with websockets.connect(URL, additional_headers=HDR) as ws:
    # 1. configure
    await ws.send(json.dumps({"type": "session.update", "session": {
        "voice": "cedar", "modalities": ["audio", "text"],
        "input_audio_format": "pcm16", "output_audio_format": "pcm16",
        "turn_detection": {"type": "server_vad"},
        "tools": [{"type": "function", "name": "get_weather", "parameters": {...}}]}}))
    # 2. append mic audio (PCM16 24kHz base64)
    await ws.send(json.dumps({"type": "input_audio_buffer.append",
                              "audio": base64.b64encode(pcm).decode()}))
    async for raw in ws:
        ev = json.loads(raw)
        if ev["type"] == "response.audio.delta":              # 3. audio out b64
            play(base64.b64decode(ev["delta"]))
        if ev["type"] == "input_audio_buffer.speech_started": # 4. barge-in detected
            await ws.send(json.dumps({"type": "response.cancel"}))
        if ev["type"] == "response.function_call_arguments.done":  # 5. tool call
            result = run_tool(ev["name"], json.loads(ev["arguments"]))
            await ws.send(json.dumps({"type": "conversation.item.create",
                "item": {"type": "function_call_output", "call_id": ev["call_id"],
                         "output": json.dumps(result)}}))
            await ws.send(json.dumps({"type": "response.create"}))
```

---

## 5. Verdict table

| Backend | $/30min | Latency (p50 TTFB) | Voice quality | Tool calling | Pick when |
|---|---|---|---|---|---|
| **Gemini 2.5 Flash native-audio** | ~$0.36 | ~600 ms | Expressive, affective, 24+ langs | Fn-calling, Google Search, code exec | Multilingual, long sessions, cheap native-audio expressivity |
| **Gemini Live 2.5 Flash (half-cascade)** | ~$0.06 | ~500 ms | Good, less emotion | Same as above | Rock-bottom cost, GCP stack, high-volume telephony |
| **OpenAI `gpt-realtime`** (GA) | ~$1.44 | ~300 ms | Best-in-class, most natural | Fn + remote MCP + SIP + image | Premium UX, complex agents, enterprise MCP integration |
| **OpenAI `gpt-realtime-mini` / 4o-mini-realtime** | ~$0.45 | ~350 ms | Very good, slight regression | Fn + MCP | Mid-volume consumer voice, cost-sensitive but OpenAI ecosystem |

### Recommendation for ARIA
- **Default:** `gpt-realtime-mini` — best quality/$ balance, 30-min cap handled by reconnect.
- **Budget tier / long sessions:** Gemini Live 2.5 Flash half-cascade (10× cheaper, built-in session resumption).
- **Premium tier:** `gpt-realtime` when UX and MCP tool ecosystem justify 3× cost.

---

## Sources
- https://ai.google.dev/api/live
- https://ai.google.dev/gemini-api/docs/live-api/get-started-websocket
- https://ai.google.dev/gemini-api/docs/pricing
- https://openai.com/index/introducing-gpt-realtime/
- https://openai.com/index/introducing-the-realtime-api/
- https://developers.openai.com/api/docs/pricing
- https://cloudprice.net/models/openai/gpt-realtime
- https://www.eesel.ai/blog/gpt-realtime-mini-pricing
- https://tokenmix.ai/blog/gpt-4o-realtime-audio-api-guide-2026
- https://blog.laozhang.ai/en/posts/gemini-3-1-flash-live-api
