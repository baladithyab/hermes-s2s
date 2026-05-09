# 04 — Hermes Plugin Tool Interception: Mechanisms & Limits

**Status:** research note (input for ADR)
**Source:** deepwiki against NousResearch/hermes-agent (2 Q&A sessions)
**Goal:** determine how hermes-s2s can override `transcribe_audio` / `text_to_speech` without forking Hermes.

---

## 1. Findings (from deepwiki citations)

### Q1 — `ctx.register_tool(name="transcribe_audio", …)` vs built-in?
**Last-write-wins. Plugin registration OVERRIDES built-in silently.**
- `PluginContext.register_tool()` delegates to global `tools.registry.registry` (`hermes_cli/plugins.py:246-260`).
- Registry stores `ToolEntry` in a name-keyed dict; later `register()` overwrites the prior entry (`website/docs/developer-guide/tools-runtime.md:41-44`).
- Confirmed by docs: `website/docs/user-guide/features/plugins.md:126-127`.

### Q2 — `pre_tool_call` return value: observer or short-circuit?
**Can block/short-circuit, but cannot substitute a real result.**
- Return shape `{"action": "block", "message": "..."}` halts execution; `message` is returned to the model as an *error string* (`website/docs/user-guide/features/hooks.md:376, 412-417`).
- Dispatch: `model_tools.handle_function_call` at `model_tools.py:712-737` (checks block before running) and `run_agent.py:_invoke_tool` ~L9841-9861.
- There is **no** hook that returns a replacement tool result *as if the tool ran successfully*. Closest is `transform_tool_result` (`model_tools.py:788-811`), which rewrites the **string output after** the tool ran — wrong for bypassing a provider.
- Most other hooks are observer-only; only `pre_llm_call` (context injection), `pre_tool_call` (block), `transform_tool_result`, `transform_llm_output`, `pre_gateway_dispatch` affect flow.

### Q3 — Other interception points?
- **No** `tool_dispatch_middleware`, **no** `voice_pipeline_factory`, **no** public `STT_PROVIDER_REGISTRY` / `TTS_PROVIDER_REGISTRY`.
- Extension surface beyond tools/hooks is **config-driven**:
  - TTS: `tts.providers.<name>` with `type: command` in `config.yaml` — shells out to a CLI template. `tts.provider` selects active.
  - STT: `HERMES_LOCAL_STT_COMMAND` env var = shell template; `stt.provider` selects backend.
  - MemoryProvider / ModelProvider plugin systems exist (`AGENTS.md:491-539`) but are LLM/memory-specific, not STT/TTS.
- Built-in backends resolved by private `_get_provider()` inside each tool; priority order hardcoded: local faster-whisper → Groq → OpenAI → Mistral → xAI.

### Q4 — How does voice mode dispatch these tools? **CRITICAL FINDING**
**Voice mode BYPASSES the tool registry and ALL plugin hooks.**
- Inbound audio: `GatewayRunner._enrich_message_with_transcription` in `gateway/run.py` directly imports and calls `transcribe_audio(...)` as a Python function.
- Outbound audio: `GatewayRunner._send_voice_reply` in `gateway/run.py` directly calls `text_to_speech_tool(...)`.
- Neither path goes through `model_tools.handle_function_call`. **Plugin hooks (`pre_tool_call`, `transform_tool_result`, etc.) are NEVER invoked for voice I/O.**
- Tools themselves live in `tools/transcription_tools.py` (`transcribe_audio`) and `tools/tts_tool.py` (`text_to_speech_tool`), each with an internal `_get_provider()` + dispatcher over `_transcribe_local / _groq / _openai / _mistral / _xai` and `_generate_elevenlabs / _openai_tts / _kittentts / <command>`.

**Implication:** Registering a tool named `transcribe_audio` via `ctx.register_tool` will:
- ✅ Override the tool **when invoked by the LLM as a function call** (text chat paths).
- ❌ **NOT** override the voice path, because the gateway imports the built-in symbol directly, not via the registry.

---

## 2. Options to Intercept Voice I/O

### Option A — Tool override via `ctx.register_tool`
Register `transcribe_audio` and `text_to_speech` with same names → last-write-wins in the registry.
- **Pros:** clean, idiomatic plugin surface, no forking, covers LLM-invoked function-call path.
- **Cons:** **does not cover Discord voice mode** (voice gateway bypasses registry). For an S2S plugin, this is the single most important path, so Option A alone is insufficient.

### Option B — Custom command provider via `config.yaml`
Ship hermes-s2s as a CLI (or shim), wire it as `stt.providers.hermes_s2s: {type: command, ...}` and `tts.providers.hermes_s2s: {type: command, ...}`; set `stt.provider` / `tts.provider` to point at it.
- **Pros:** works for **both** voice mode and LLM-invoked calls (all paths funnel through `_get_provider`). No Hermes fork. No monkey-patching.
- **Cons:** subprocess / CLI shape imposes per-call cold start, stdin/stdout audio marshalling, no streaming duplex; loses in-process speed for low-latency real-time S2S (OpenAI Realtime, Gemini Live WS sessions). Config-driven, not plugin-driven — plugin install alone won't activate it without user editing `config.yaml`.

### Option C — Monkey-patch `_get_provider` / provider tables at plugin `register()` time
From plugin `register(ctx)`, import `hermes_agent.tools.transcription_tools` and `tools.tts_tool` and rebind `_get_provider` (or the dispatch dicts) to hermes-s2s providers.
- **Pros:** works for both voice and LLM paths (both call the same internals); keeps in-process latency; auto-activates on plugin load.
- **Cons:** relies on private symbols (`_get_provider`, `_transcribe_*`) — fragile across Hermes versions; violates plugin contract; upstream refactor will break us silently.

### Option D — Upstream contribution: add a public STT/TTS provider registry
Send a PR exposing `STT_PROVIDERS` / `TTS_PROVIDERS` registration + hook in `_get_provider`.
- **Pros:** permanent, idiomatic; benefits ecosystem; we get first-class support.
- **Cons:** blocks 0.2.0 on upstream merge; out-of-scope for this release timeline.

---

## 3. Recommended Path for hermes-s2s 0.2.0

**Ship a hybrid strategy: Option B (command provider) as the guaranteed-correct default, with Option A (`ctx.register_tool`) as a complementary override for the LLM function-call path.** On plugin install, generate (or patch) `config.yaml` fragments that register `stt.providers.hermes_s2s` and `tts.providers.hermes_s2s` as `type: command` pointing at a small `hermes-s2s stt` / `hermes-s2s tts` CLI — this is the only mechanism that cleanly intercepts `gateway/run.py`'s direct calls in Discord voice mode (confirmed bypass of `pre_tool_call`/registry per `_enrich_message_with_transcription` / `_send_voice_reply`). Additionally call `ctx.register_tool("transcribe_audio"/"text_to_speech", ...)` so LLM-initiated function calls also route through hermes-s2s. Defer low-latency streaming duplex (OpenAI Realtime, Gemini Live) to 0.3.0 behind an upstream PR that introduces a public provider registry (Option D), since the current command-provider contract is request/response only. Do **not** ship Option C (monkey-patch) in 0.2.0 — private-symbol dependency is a stability risk and contradicts the "no fork, no patch" goal stated in earlier research notes.

---

## 4. Citations quick-index

| Claim | File | ~Lines |
|---|---|---|
| register_tool last-write-wins | `hermes_cli/plugins.py` | 246-260, 16-18 |
| Registry dict overwrite | `website/docs/developer-guide/tools-runtime.md` | 41-44 |
| pre_tool_call block protocol | `website/docs/user-guide/features/hooks.md` | 376, 412-417 |
| pre_tool_call dispatch | `model_tools.py` | 712-737 |
| pre_tool_call dispatch (alt) | `run_agent.py` | ~9841-9861 |
| transform_tool_result | `model_tools.py` | 788-811 |
| Voice STT entry (bypass) | `gateway/run.py` | `_enrich_message_with_transcription` |
| Voice TTS entry (bypass) | `gateway/run.py` | `_send_voice_reply` |
| transcribe_audio impl | `tools/transcription_tools.py` | `_get_provider` + `_transcribe_*` |
| text_to_speech impl | `tools/tts_tool.py` | `_get_provider` + `_generate_*` |
| Command provider extension | `website/docs/user-guide/features/plugins.md` | command-provider section |
| Full hook list | `hermes_cli/plugins.py` | 78-118 |
