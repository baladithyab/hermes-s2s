# Research-12: Voice Mode Rearchitecture — Four-Mode Selector

**Status:** draft / Phase 3 research
**Date:** 2026-05-10
**Target release:** 0.4.0
**Relates to:** ADR-0004 (command-provider), ADR-0006 (discord bridge), ADR-0007 (frame-cb), ADR-0008 (tool bridge), ADR-0009 (plug-and-play UX)

## 1. Problem

hermes-s2s 0.3.x conflates *voice mode* with *config at startup*. `s2s.mode` is read once (`cascaded` | `realtime` | `s2s-server`) and the monkey-patch behaves differently depending on it. Users want:

- Per-VC-join switching without a process restart.
- Four cleanly-separated modes (M1 cascaded-default, M2 custom-pipeline, M3 realtime-duplex, M4 external-s2s-server).
- Voice-triggered Hermes meta-commands (`/new`, `/resume`, `/compress`, `/title`, `/branch`, `/topic`) — impossible today because voice users cannot type.
- Realtime backends (which bypass Hermes core) must still be able to call Hermes tools.

## 2. Mode Selection UX

**Recommendation: slash-command option with a config default.** Add a single Discord interaction option `mode:` to the existing `/voice join` command. Users get a native autocompleted dropdown; the option is optional and falls back to `s2s.voice.default_mode` from config, which itself defaults to `cascaded`. No env toggles except for dev overrides (`HERMES_S2S_FORCE_MODE`). Rationale: the slash option gives per-join control with zero extra surface area, config gives deploy-level defaults, env gives CI escape hatch.

### Decision Matrix

| Option | Per-VC switch | Discoverability | Implementation cost | UX |
|---|---|---|---|---|
| Slash option on `/voice join` (**chosen**) | ✅ | native autocomplete | medium — upstream `/voice` lives in Hermes core, plugin adds an option via gateway hook | best |
| Config default only | ❌ restart | low | trivial | poor |
| Env toggle | ❌ restart | zero | trivial | dev only |
| New `/s2s mode <m>` command | ✅ | separate surface | medium — plugin owns the command | ok, but two commands to remember |
| Per-channel config override | ✅ on rejoin | medium | medium — YAML channel map | good deploy story |

**Chosen composite:** `/voice join mode:<m>` (primary) → `s2s.voice.channel_overrides[<chan_id>]` (persistent override) → `s2s.voice.default_mode` (base default) → `cascaded` (hard default).

## 3. Mode Dispatch — Where the Routing Lives

**Recommendation:** a new plugin-owned abstraction, `VoiceSessionFactory`, installed by the existing monkey-patch. Do **not** push routing into `gateway/run.py` yet — upstream is not ready and we would block on a PR. Do **not** fork `join_voice_channel`'s body; instead, wrap it so that after the native `VoiceReceiver` is attached we hand control to the factory, which returns a `VoiceSession` object whose lifetime matches the VC connection.

```
Discord /voice join (mode=?)
        │
        ▼
gateway.platforms.discord.DiscordAdapter.join_voice_channel     [upstream]
        │   (monkey-patched by hermes_s2s._internal.discord_bridge)
        ▼
ModeRouter.resolve(mode_hint, guild_id, channel_id, config)  ──► ModeSpec
        │
        ▼
VoiceSessionFactory.build(mode_spec, vc, adapter, hermes_ctx) ──► VoiceSession
        │
        ▼
           M1 CascadedSession     ← no-op: lets Hermes's native loop run unchanged
           M2 CustomPipelineSession ← installs custom STT/TTS command-providers per ADR-0004
           M3 RealtimeSession     ← audio_bridge.RealtimeAudioBridge + tool_bridge
           M4 S2SServerSession    ← pipeline backend from registry._PIPELINE_REGISTRY
```

### Class/function shapes

```python
# hermes_s2s/voice/modes.py   (new)
class VoiceMode(str, Enum):
    CASCADED   = "cascaded"      # M1 — Whisper → Hermes core → Edge TTS
    PIPELINE   = "pipeline"      # M2 — Moonshine → Hermes core → Kokoro
    REALTIME   = "realtime"      # M3 — Gemini Live / gpt-realtime
    S2S_SERVER = "s2s-server"    # M4 — external duplex pipeline

@dataclass(frozen=True)
class ModeSpec:
    mode: VoiceMode
    provider: str | None        # "gemini-live", "kokoro+moonshine", "v6", ...
    options: dict                # mode-specific kwargs

class ModeRouter:
    def __init__(self, config: dict): ...
    def resolve(self, *, mode_hint: str | None,
                guild_id: int, channel_id: int) -> ModeSpec:
        # precedence: explicit hint > channel_override > guild_override
        #             > config default > "cascaded"
        ...

class VoiceSession(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    mode: VoiceMode
    # Optional — surfaced to the meta-command dispatcher
    meta_command_sink: "MetaCommandSink | None"

class VoiceSessionFactory:
    def __init__(self, registry, tool_bridge, meta_dispatcher): ...
    def build(self, spec: ModeSpec, vc, adapter, hermes_ctx) -> VoiceSession:
        match spec.mode:
            case VoiceMode.CASCADED:   return CascadedSession(...)
            case VoiceMode.PIPELINE:   return CustomPipelineSession(spec, ...)
            case VoiceMode.REALTIME:   return RealtimeSession(spec, self._tool_bridge, ...)
            case VoiceMode.S2S_SERVER: return S2SServerSession(spec, ...)
```

Lifecycle: the monkey-patched `join_voice_channel` stores the built `VoiceSession` on `adapter._s2s_sessions[channel_id]`; the corresponding `leave_voice_channel` wrap calls `session.stop()` and pops it. Per-session is keyed by channel, not guild.

## 4. Voice-Controlled Meta-Commands

Hermes has a central `COMMAND_REGISTRY` (`hermes_cli/commands.py`) with the session verbs `/new`, `/resume`, `/compress`, `/title`, `/branch`, `/topic`. All four voice modes need access to these. The mechanism differs between M1/M2 and M3/M4.

### 4.1 Classification

| Command | Kind | LLM-tool safe? | Requires gateway-direct? | Notes |
|---|---|---|---|---|
| `/new`      | session-control | ✅ | no | idempotent, no args |
| `/title`    | session-control | ✅ | no | args = string |
| `/branch`   | session-control | ✅ | no | args = branch name |
| `/compress` | session-control | ✅ | no | long-running — fits tool-bridge soft/hard timeout |
| `/topic`    | session-control | ✅ | no | Telegram-specific but tool-callable |
| `/resume <name>` | **gateway-direct** | ⚠ | **yes** | fuzzy session search + UI-style picker — LLM picking the "wrong" session is a data-hazard; keep out of tool surface and only expose through a voice-direct matcher |

### 4.2 M1 / M2 — intercept before STT-to-LLM

Hermes's cascaded voice loop hands the STT string straight to the agent as a user message. Insert a **MetaCommandSink** between STT and the LLM via a new callback the plugin registers on the adapter:

```
STT text  ──►  MetaCommandSink.match(text)  ──►  if match: dispatch to gateway
                                             └─►  else:     continue to Hermes LLM
```

Match strategy (ordered):
1. **Explicit prefix:** text starts with `"slash "` (STT-friendly), `"hermes "`, or `"command "`. E.g. `"slash new"` → `/new`.
2. **Wakeword + verb:** `"hey aria, start a new session"` → regex/intent table maps "start a new session" → `/new`.
3. **Fallback:** pass through to LLM unchanged.

Dispatch calls the existing `GatewayClient` path the TUI uses (same one the `/resume` picker uses today), so session IDs, event hooks, and UI messages all fire correctly. The MetaCommandSink is reused by M2 because M2 still routes through Hermes's text loop.

### 4.3 M3 / M4 — expose as LLM tools on the realtime backend

Realtime backends never see STT text in Python space; audio goes direct to Gemini Live / OpenAI Realtime. Extend `tool_bridge.py` to export a set of `hermes_meta_*` tools as part of the initial tool list the realtime backend is handed at session open.

```python
# Tool schemas registered with the realtime backend:
hermes_meta_new_session(title: str | None) -> {ok: bool, session_id: str}
hermes_meta_title(title: str)             -> {ok: bool}
hermes_meta_branch(name: str)             -> {ok: bool, branch_id: str}
hermes_meta_compress()                    -> {ok: bool, summary: str}
hermes_meta_topic(action: "enable"|"show") -> {ok: bool}
# /resume is intentionally NOT exposed as an LLM tool.
```

Each tool dispatches through `HermesToolBridge.handle_tool_call` → a new `MetaCommandDispatcher.invoke(name, args)` → same gateway path as 4.2. Soft-timeout filler audio handles `/compress`'s long tail.

Example flow: `"Hey ARIA, start a new session about deployment"`
```
mic → Gemini Live → tool_call("hermes_meta_new_session", {"title": "deployment"})
     → HermesToolBridge → MetaCommandDispatcher.invoke
     → GatewayClient.send_command("/new") + follow-up "/title deployment"
     → tool_result({ok: true, session_id: "…"}) back to Gemini
     → Gemini speaks "started a new session called deployment"
```

`/resume` for M3/M4: the voice user says "resume the one about deployment"; the realtime model can't safely pick it. Route via a direct voice-intent matcher that runs on **Gemini's text transcript feed** (both Gemini Live and gpt-realtime emit interim user transcripts alongside audio). The matcher uses the same `MetaCommandSink` as M1/M2, finds the prefix `resume …`, fuzzy-matches against `SessionDB`, and either resumes directly or emits a disambiguation audio cue via `backend.send_audio()`.

## 5. Tool-Export Seam for Realtime / Custom Pipeline

Hermes core's `enabled_toolsets` decides which tools the agent sees. For realtime backends we need a **parallel** whitelist because the surface area is audio-user facing, not agent facing.

### Default-exposed (safe, read-only or idempotent)

`web_search`, `get_current_time`, `get_weather`, `calculator`, `hermes_meta_new_session`, `hermes_meta_title`, `hermes_meta_branch`, `hermes_meta_topic`, any registered MCP tool marked `voice_safe=True`.

### Permission-gated (prompt user once per session)

`file_read`, `directory_list`, `memory_read`, `session_search`, `hermes_meta_compress`, image-gen tools. Gate via a `s2s.voice.tool_policy.ask` list; first invocation triggers a spoken confirmation from the realtime backend ("I'd like to read your notes file — okay?").

### Never-exposed in realtime mode

`terminal`, `file_write`, `file_edit`, `patch`, `computer_use`, any tool whose schema contains `destructive: True`, subagent spawning, credential tools, kanban write ops. These require eyes-on-screen confirmation that voice can't reliably provide.

### Config shape

```yaml
s2s:
  voice:
    tool_policy:
      default_exposed: [web_search, get_current_time, calculator, hermes_meta_*]
      ask:             [file_read, memory_read, hermes_meta_compress]
      deny:            [terminal, file_write, patch, computer_use]
```

Implementation: `tool_bridge.HermesToolBridge` grows a `build_tool_manifest(hermes_ctx, policy) -> list[ToolSchema]` method that filters `hermes_ctx.enabled_toolsets` through the policy before handing schemas to the backend's `session.update`/`session.created` payload.

## 6. Migration Path 0.3.x → 0.4.0

**Recommendation: additive with one soft deprecation, bump minor to 0.4.0.**

Additive:
- New `VoiceSessionFactory` / `ModeRouter` abstractions live alongside existing code.
- New `VoiceMode.PIPELINE` is added; existing `cascaded` / `realtime` / `s2s-server` keep their string values and map 1:1 onto the enum.
- `/voice join mode:` option is additive; omitted → legacy path.

Soft deprecation:
- `s2s.mode` (top-level) is still honored but logs a deprecation warning suggesting `s2s.voice.default_mode`.

Clean break avoided because 0.3.x users explicitly onboarded through `hermes s2s setup` profiles; breaking that flow would violate the ADR-0009 plug-and-play promise. Version bumps minor not major because public Python APIs (`register_stt`, `register_tts`, `register_realtime`, `register_pipeline`) are unchanged.

## 7. Risk Register

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | Upstream `/voice join` signature changes → slash-option injection breaks | high | version-gate in `SUPPORTED_HERMES_RANGE`; fall back to prefix-command `!s2s-mode` |
| R2 | `MetaCommandSink` intercepts a user sentence that *looks* like a command ("new session" said conversationally) | medium | require wakeword OR explicit `"slash"` prefix; log every intercept for offline review |
| R3 | Gemini/OpenAI transcript feeds differ in timing → `/resume` matcher sees stale text | medium | debounce on final-transcript flag per provider; don't act on interim |
| R4 | Tool-policy whitelist drift when Hermes core adds new destructive tools | high | deny-by-default for tools with `destructive`/`requires_confirmation` metadata; audit in `doctor.py` |
| R5 | Per-session `VoiceSession` leaks if `leave_voice_channel` isn't patched for every adapter subclass | medium | instance-level cleanup via `weakref.finalize(vc, session.stop)` in addition to the leave-wrap |
| R6 | Realtime tool-call floods (model calls `hermes_meta_new_session` in a loop) | low | dispatcher enforces 1 meta-command/3s per session and refuses `/new` within 10s of prior |
| R7 | M4 (external s2s-server) crash leaves VC stuck | medium | `S2SServerSession.start` supervises subprocess; on exit flips session to `CASCADED` and notifies user |
| R8 | Test coverage for four modes × Discord adapter quickly combinatorial | medium | contract-test `VoiceSession` protocol once; per-mode integration tests stay isolated |

## 8. ADR Candidates (Phase 4 drafts)

1. **ADR-0010: Per-session voice mode via `VoiceSessionFactory`.** Supersedes ADR-0006's "mode is a startup constant" assumption. Owns the dispatch boundary.
2. **ADR-0011: Slash-option UX for voice mode selection.** Locks in `/voice join mode:` as the primary surface; documents the config/env precedence.
3. **ADR-0012: Meta-command voice triggers.** Defines `MetaCommandSink` (M1/M2) + `hermes_meta_*` tool family (M3/M4), the wakeword/prefix grammar, and why `/resume` is gateway-direct only.
4. **ADR-0013: Tool-export policy for realtime backends.** Formalizes the three-bucket whitelist (default / ask / deny) and its interaction with `enabled_toolsets`.
5. **ADR-0014: 0.4.0 additive migration + `s2s.mode` soft deprecation.** Documents version-bump rationale and the config-key rename path.

## 9. Open Questions

- Should M2's `CustomPipelineSession` also surface the realtime-style tool export (so a Moonshine→Kokoro stack with a local LLM could be wired to `hermes_meta_*` for symmetry)? Leaning yes, but parks cleanly in 0.4.1.
- Fuzzy `/resume` match: reuse `SessionDB` FTS5 or add a dedicated voice-facing index? Prefer FTS5 with a voice-tuned rank function.
- Should `MetaCommandSink` grammar be user-extensible via config YAML? Useful for non-English deployments; defer to 0.4.1.
