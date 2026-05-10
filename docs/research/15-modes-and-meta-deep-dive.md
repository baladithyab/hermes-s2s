# Research-15: Mode Dispatch + Meta-Commands + Realtime Tool Access — Deep Dive

**Status:** draft / Phase 3 research (concretization of research-12)
**Date:** 2026-05-10 · **Target:** 0.4.0 · **Relates to:** research-12 §3–7, ADR-0008, ADR candidates 10–14

## 1. ModeRouter — Precedence and Capability Gating

### 1.1 Precedence (highest wins)
```
1. explicit slash option   /voice join mode:<m>
2. env override            HERMES_S2S_FORCE_MODE=<m>            (dev/CI)
3. channel override        s2s.voice.channel_overrides[<cid>]
4. guild   override        s2s.voice.guild_overrides[<gid>]
5. config default          s2s.voice.default_mode
6. hard default            "cascaded"
```
Env sits at #2 (not #1) so operators can pin CI behaviour while a voice user can
still observe the non-env path by passing the slash option. Logs record which
source won so surprises are traceable.

### 1.2 Pseudocode
```python
def resolve(self, *, mode_hint, guild_id, channel_id) -> ModeSpec:
    cfg = self._cfg["s2s"]["voice"]
    raw = (_force_env()
        or _normalize(mode_hint)
        or cfg.get("channel_overrides", {}).get(channel_id)
        or cfg.get("guild_overrides", {}).get(guild_id)
        or cfg.get("default_mode")
        or "cascaded")
    mode = _coerce_enum(raw)                 # VoiceMode | raise
    spec = self._build_spec(mode, cfg)       # provider + options
    self._check_capabilities(spec)           # may raise or downgrade (§1.5)
    return spec
```

### 1.3 Normalization + "auto"
- `None`, `""`, `"default"`, `"auto"` → treated as "no hint", fall through.
  `"auto"` is **not** a real mode — reserved for a future smart-router (probe
  latency / cost). Document as "not yet implemented; falls back to default."
- Typos (`"realitme"`, `"casscaded"`) → **reject early** with a structured error
  returned to the slash handler: `"Unknown mode 'realitme'. Valid: cascaded, pipeline, realtime, s2s-server."` No Levenshtein auto-correct — silent correction is worse than a loud error for programmatic callers, and voice users just re-click the autocomplete.
- Case-fold + strip; accept hyphen or underscore (`s2s-server` ↔ `s2s_server`).

### 1.4 Capability matrix
| Mode | Always avail. | Requires | Check |
|---|---|---|---|
| M1 cascaded  | ✅ | Hermes core voice loop | none |
| M2 pipeline  | ❌ | `hermes-s2s[pipeline]` → `moonshine-onnx`, `kokoro` | `importlib.util.find_spec` |
| M3 realtime  | ❌ | provider API key (`GEMINI_API_KEY`/`OPENAI_API_KEY`) + `websockets` | env probe at resolve |
| M4 s2s-server | ❌ | `register_pipeline()` call + health probe on endpoint | `asyncio.wait_for(probe, 2.0)` |

### 1.5 Missing requirements — **fall back with a loud warning; explicit requests fail closed**
Voice is conversational: "can't join" is worse than "joined in cascaded, here's why". Exception: when the user *explicitly* chose a mode via slash option, honor the explicit intent and fail closed (ephemeral error message) — silent downgrade violates user choice.
```
if mode came from slash-option and capability fails:
    refuse_join(reason)                       # fail closed
else:
    log.warning("downgrading %s → cascaded: %s", mode, reason)
    speak_on_join(f"Using cascaded voice; {reason}")
    return ModeSpec(CASCADED, …)              # fall back
```
`doctor.py` surfaces the same matrix ahead of time.

## 2. VoiceSession Lifecycle
Common protocol per research-12 §3. Each concrete session owns a
`_cleanup_stack: contextlib.AsyncExitStack`; `start()` pushes each acquired
resource as a callback, `stop()` just awaits `stack.aclose()`. This resolves the
"half-started" cleanup discipline — no bespoke teardown per class.

### 2.1 Per-class state + threading
| Class | State | start() | stop() | Thread |
|---|---|---|---|---|
| `CascadedSession` | `vc`, sink ref | attach MetaCommandSink to Hermes voice-adapter callback chain | detach sink | loop (sink); Hermes voice worker unchanged |
| `CustomPipelineSession` | STT/TTS provs, frame-cb | `register_stt`/`register_tts` shims + install frame-cb | unregister provs; drop frame-cb | init on loop; audio on Hermes voice thread |
| `RealtimeSession` | ws client, audio/tool bridges, interim-transcript task | open ws → `session.update(tools, instructions)` → spawn send/recv tasks | cancel tasks → `tool_bridge.cancel_all()` → close ws | loop only; discord.py voice thread writes PCM via `call_soon_threadsafe` |
| `S2SServerSession` | subproc, pipeline backend, supervisor task | spawn/adopt backend → wait for health → wire pipes | terminate supervisor → `backend.shutdown()` → reap subproc | supervisor on loop; subproc OS-level |

### 2.2 RealtimeSession — fully specified
```python
class RealtimeSession:
    def __init__(self, spec, tool_bridge, meta_dispatcher, vc, adapter, loop):
        self.spec, self._vc, self._adapter, self._loop = spec, vc, adapter, loop
        self._tool_bridge, self._meta = tool_bridge, meta_dispatcher
        self._stack = contextlib.AsyncExitStack()
        self._backend: RealtimeBackend | None = None
        self._send_task = self._recv_task = self._transcript_task = None
        self._pcm_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)

    async def start(self):
        try:
            self._backend = await self._stack.enter_async_context(build_backend(self.spec))
            tools = self._tool_bridge.build_tool_manifest(self._adapter.hermes_ctx, self.spec.policy)
            await self._backend.session_update(
                tools=tools,
                instructions=self._compose_voice_persona(),
                voice=self.spec.options.get("voice", "alloy"))
            self._stack.callback(self._detach_sink)
            self._attach_sink()                                  # discord PCM → queue
            self._send_task = self._spawn(self._pump_mic())
            self._recv_task = self._spawn(self._pump_backend())
            self._transcript_task = self._spawn(self._pump_transcripts())
            for t in (self._send_task, self._recv_task, self._transcript_task):
                self._stack.push_async_callback(_cancel_task, t)
        except BaseException:
            await self._stack.aclose()                           # clean half-start
            raise

    async def stop(self):
        await self._tool_bridge.cancel_all()
        await self._stack.aclose()
```
Threading invariant: discord.py's voice receiver thread pushes PCM via `loop.call_soon_threadsafe(self._pcm_q.put_nowait, chunk)` — the queue is the one-and-only handoff. Playback back to discord uses `vc.play(...)` from the loop.

## 3. MetaCommandSink — Grammar (No ML)

### 3.1 Wakeword gate
Mandatory. Default `"hey aria"` (case-insensitive, whitespace-collapsed), configurable as `s2s.voice.wakeword`. Matching proceeds **only** after the wakeword is detected at the start of the **final** STT hypothesis (interim transcripts ignored — R3 in research-12), optionally followed by a comma/pause.

### 3.2 Verb table (anchored `re` regex)
```python
WAKE = r"(?i)^\s*hey\s+aria[,\s]+(?:please\s+|let'?s\s+|can\s+you\s+)?"
PATTERNS = [
    (r"(start|begin|open)\s+(a\s+)?new\s+(session|chat|conversation)\b"
     r"(?:\s+(?:about|called|named)\s+(?P<title>.{1,80}))?",           "new"),
    (r"(continue|resume|load)(?:\s+the)?\s+(session|chat|one)\s+"
     r"(?:called|named|about)\s+(?P<query>.{1,80})",                   "resume"),
    (r"(compress|condense|summariz(?:e|e))\s+(the\s+)?"
     r"(context|history|conversation)\b",                              "compress"),
    (r"(title|name|call)\s+this\s+(?:as\s+)?(?P<title>.{1,80})",       "title"),
    (r"(branch|fork)(?:\s+(?:off|here|this))?\b"
     r"(?:\s+(?:as|called)\s+(?P<name>.{1,80}))?",                     "branch"),
    (r"(enable|turn\s+on)\s+topic(?:\s+mode)?\b",                      "topic_on"),
]
# Match: re.search(WAKE + pattern, text) — substring match elsewhere is disallowed.
```
Rules: (1) no match without wakeword — prevents "I think we should start a new feature" from firing; (2) imperative anchor — verb must come immediately after wakeword + ≤3 filler tokens; (3) captured string >80 chars rejected (likely swallowed a run-on); (4) on ambiguity, prefer longer match and log both.

### 3.3 No fuzzy/LLM matcher
In M1/M2 the sink runs **before** the LLM; in M3/M4 on the backend's interim-transcript feed. An LLM here adds a round-trip we don't own. Levenshtein at ≤2 edits per verb is feasible but deferred — 0.4.1 adds `s2s.voice.meta_aliases` for operator-defined extensions.

### 3.4 Confirmation policy
**Speak a short confirmation before executing** for destructive/long verbs (`compress`, `branch`, `resume`). **Silent** for idempotent cheap verbs (`new`, `title`, `topic_on`) — just play a 200 ms earcon (single sine beep) so the user knows the command landed. Balances latency against undo-ability.

### 3.5 `/resume` disambiguation flow
1. Match captures `query`. 2. `SessionDB.search_fts(query, limit=5)`. 3. 0 hits → "no session found matching X". 4. 1 hit → confirm + resume ("resuming deployment session, last updated Tuesday"). 5. ≥2 hits → speak top 3 with ordinals, wait ≤8 s for "first" / "second" / "number two" / "cancel" via same grammar. **Never let the LLM pick.**

## 4. `hermes_meta_*` Tool Family (M3/M4)
JSON-schema is OpenAI Realtime / Gemini Live compatible (both accept the same JSON Schema subset for function params).

```json
{"type":"function","name":"hermes_meta_new_session",
 "description":"Start a fresh Hermes session. Use when user asks to start/begin a new chat, session, or conversation.",
 "parameters":{"type":"object","additionalProperties":false,
   "properties":{"title":{"type":"string","description":"Optional initial title. Omit if user didn't provide one."}}}}
```
```json
{"type":"function","name":"hermes_meta_title_session",
 "description":"Set the current session's title.",
 "parameters":{"type":"object","required":["title"],"additionalProperties":false,
   "properties":{"title":{"type":"string","maxLength":120}}}}
```
```json
{"type":"function","name":"hermes_meta_compress_context",
 "description":"Compress (summarize) the current session's history to free context. Long-running (≤30s).",
 "parameters":{"type":"object","properties":{},"additionalProperties":false}}
```
```json
{"type":"function","name":"hermes_meta_branch_session",
 "description":"Fork the current session at the current turn into a new branch.",
 "parameters":{"type":"object","additionalProperties":false,
   "properties":{"name":{"type":"string","description":"Optional branch name."}}}}
```
```json
{"type":"function","name":"hermes_meta_resume_session",
 "description":"Search sessions by query and RETURN A LIST. The model MUST NOT pick one; return the list verbatim and let the user choose by ordinal.",
 "parameters":{"type":"object","required":["query"],"additionalProperties":false,
   "properties":{"query":{"type":"string"},"limit":{"type":"integer","default":5,"maximum":10}}}}
```
Data hazard mitigation: `hermes_meta_resume_session` returns a **list** `{candidates:[…], action_required:"user_pick"}`, not a resume action. Prompt text + response envelope force the realtime model to read the list aloud; actual resume is executed by the `MetaCommandSink` watching the *next* user utterance for an ordinal.

### 4.1 Dispatcher location
New module `hermes_s2s/voice/meta_dispatcher.py` owns `MetaCommandDispatcher`. Lazy-imports `GatewayClient` and calls the same `/new`, `/title`, … entrypoints the CLI/TUI already use. Rationale: keep gateway-direct side effects out of `tool_bridge.py` (pure transport) **and** out of upstream `run.py` (not ours to edit). Bridge dispatch: `if name.startswith("hermes_meta_"): meta_dispatcher.invoke(name, args)` else fall through to existing tool dispatcher.

## 5. Tool-Export Bucketing (audit of `_HERMES_CORE_TOOLS`)
| Tool | Bucket | Rationale |
|---|---|---|
| `web_search`, `web_extract` | default | RO, idempotent, voice-friendly |
| `vision_analyze` | default | voice user describes attached image |
| `text_to_speech` | default | synthesis only |
| `read_file` | ask | RO but privacy-sensitive; per-path confirmation |
| `search_files` | ask | filename leakage risk; RO |
| `skills_list`, `skill_view` | default | RO |
| `skill_manage` | deny | writes skills |
| `memory` (read) | ask | private data |
| `session_search` | ask | exposes past conversations |
| `todo` | default | self-scoped, low risk |
| `browser_navigate` | ask | no auto-click when bucket=ask |
| `browser_snapshot`, `browser_vision`, `browser_console`, `browser_get_images` | ask | RO-ish; page content may be private |
| `browser_click`/`type`/`press`/`scroll`/`back`/`cdp`/`dialog` | deny | destructive / form-fill risk |
| `terminal`, `process` | deny | arbitrary shell / kills |
| `write_file`, `patch` | deny | destructive |
| `execute_code` | deny | arbitrary code |
| `delegate_task` | deny | spawns subagent with full surface |
| `image_generate` | ask | cost + content moderation |
| `clarify` | default | no-op meta |
| `cronjob` | deny | persistent side effect |
| `send_message` | deny | cross-platform impersonation |
| `ha_list_entities`/`get_state`/`list_services` | ask | physical-world read |
| `ha_call_service` | deny | physical-world write |
| `kanban_show` | ask | read |
| `kanban_complete`/`block`/`heartbeat`/`comment`/`create`/`link` | deny | writes |
| `computer_use` | deny | screen / keyboard control |
| `hermes_meta_new_session`/`title_session`/`branch_session`/`topic` | default | scoped to current session |
| `hermes_meta_compress_context` | ask | long + summary replaces detail |
| `hermes_meta_resume_session` | default | list-only, non-acting |

Default YAML ships this as `s2s.voice.tool_policy`; operators can promote `ask`→`default` or demote to `deny` per deployment.

## 6. Realtime Tool-Call Latency Budget
Backend behaviour (May 2026): **OpenAI Realtime** emits tool_calls in `response.function_call_arguments.done`; parallel supported via `parallel_tool_calls` (default true) — recommend **false** for voice (simpler filler-audio state machine). **Gemini Live 2.5/3.1 Flash**: `BidiGenerateContentToolCall` can carry multiple calls; sync by default, per-function `behavior: NON_BLOCKING` possible — we don't use NON_BLOCKING for meta commands (state ordering matters).

### 6.1 Timeouts
| Budget | Value | Action |
|---|---|---|
| Soft | **3 s** per call | `backend.send_filler_audio("one moment")`; keep waiting |
| Hard | **15 s** per call | cancel tool, return `{"error":"timeout","retryable":true}`; LLM apologizes |
| Meta min gap | 3 s | reject second `hermes_meta_*` within 3 s |
| `/new` re-issue | 10 s | R6 hardening |

`tool_bridge.py` currently soft=5/hard=30 — **reduce** for voice via policy keys `s2s.voice.tool_policy.soft_timeout`/`hard_timeout`.

### 6.2 Concurrency
**Serial by default.** Set `parallel_tool_calls=false` on OpenAI; for Gemini Live process the `functionCalls[]` array one-at-a-time on our side even if protocol allows parallel returns. Rationale: (1) meta-commands mutate session state — ordering matters; (2) sequencing keeps the filler-audio state machine single-threaded; (3) most voice tool calls are single — parallelism rarely pays. Escape hatch: per-tool `parallel_safe: true` metadata (web_search, get_current_time); when **all** pending calls are parallel-safe, gather them; otherwise serialize.

## 7. Voice Persona Overlay
### 7.1 Config
```yaml
s2s:
  voice:
    persona: |
      You are speaking through a voice channel.
      Keep replies SHORT — 1 to 3 sentences. Avoid markdown, lists, code blocks, and URLs.
      If asked to show something visual, say "I'll post it in text chat" and call the text-post tool.
    persona_mode: overlay    # overlay | replace | append
```

### 7.2 Recommendation: **overlay, not merged into `PERSONA.md`**
Compose at session start as a separate system-prompt layer appended after Hermes core persona, clearly fenced:
```
<voice_mode_overlay>
{voice.persona}
</voice_mode_overlay>
```
Why not merge: (1) PERSONA.md is user-owned and platform-agnostic; voice overlay is platform-scoped. (2) Mode-conditional — M1 text pathways (if the same agent handles a DM) must not inherit "keep replies SHORT". (3) Overlay-text upgrades shouldn't dirty the user's PERSONA.md. For M3/M4, overlay goes into `instructions` on `session.update`; Hermes core never sees it because those modes bypass core.

## 8. Migration 0.3.x → 0.4.0

### 8.1 Config auto-translation (on first 0.4.0 startup)
```python
# hermes_s2s/config/loader.py
def _migrate_v0_3_to_v0_4(cfg):
    if cfg["s2s"].get("mode") and not cfg["s2s"].get("voice", {}).get("default_mode"):
        cfg.setdefault("s2s", {}).setdefault("voice", {})["default_mode"] = cfg["s2s"]["mode"]
        warn("s2s.mode is deprecated; migrated to s2s.voice.default_mode")
        cfg["s2s"]["_migrated_from"] = "0.3"
        write_config(cfg, backup=".pre-0.4.yaml")
```
Warning fires **once per machine** (sentinel at `~/.hermes/.s2s-migration-warned`) to avoid log spam. `s2s.mode` stays honored for two minor versions.

### 8.2 Wizard policy — **non-destructive additive merge**
`hermes s2s setup` must: (1) load existing config; (2) prompt only for **absent** keys; (3) for new 0.4 keys (`voice.default_mode`, `voice.wakeword`, `voice.tool_policy`, `voice.persona`) prompt "keep existing? (y) / reconfigure (n)"; (4) never delete user customizations; never re-order YAML (preserve via ruamel). Destructive regen belongs to `hermes s2s setup --reset` (explicit opt-in). ADR-0009 plug-and-play demands re-running the wizard is safe.

### 8.3 Migration script `python -m hermes_s2s.migrate_0_4`
```
usage: migrate_0_4 [--dry-run] [--rollback] [--config PATH]
  1. Load config.
  2. If already v0.4 (has voice.default_mode): exit 0 "nothing to migrate".
  3. Backup → <path>.pre-0.4-<ISO_TS>.yaml
  4. Translate:
       s2s.mode         → s2s.voice.default_mode
       s2s.realtime.*   → s2s.voice.realtime.*    (if present)
       s2s.pipeline.*   → s2s.voice.pipeline.*    (if present)
  5. Insert defaults for new keys (wakeword, tool_policy) from shipped template.
  6. Validate via pydantic schema; on failure, restore backup.
  7. Emit summary diff.
  --dry-run:  steps 4–6 in memory, print diff, don't write.
  --rollback: find most recent .pre-0.4-*.yaml next to config, restore it.
```

### 8.4 Manual rollback
1. `python -m hermes_s2s.migrate_0_4 --rollback`, OR
2. `cp ~/.hermes/config.pre-0.4-*.yaml ~/.hermes/config.yaml`
3. `pip install 'hermes-s2s<0.4'` to downgrade the package.
4. Restart gateway.

Risk: if a 0.4-only plugin has written new keys after migration, rollback loses them. Document that the backup is a safety net for the migration itself, not a general checkpoint.

## 9. Deferred to 0.4.1
User-extensible MetaCommandSink grammar via YAML (non-English); `voice.persona` per-guild override; `parallel_safe` tool metadata honored by bridge; smart `auto` mode (probe latency, pick M3 if key else M1); `CustomPipelineSession` exposing `hermes_meta_*` for symmetry with M3/M4.
