# ADR-0013: Four-mode VoiceSession architecture (Cascaded / Pipeline / Realtime / S2SServer)

**Status:** proposed
**Date:** 2026-05-10
**Driven by:** research-12 §2–3 (mode taxonomy + routing), research-15 §1–2 (session lifecycle + factory)
**Supersedes (partially):** ADR-0006 (single-mode bridge); extends ADR-0009 (wizard profiles)

## Context

The 0.3.x bridge grew a de-facto second mode (Gemini Live / Realtime) bolted on top of the original cascaded STT→LLM→TTS path via a global `s2s.mode` key and a monkey-patch flag. Research-12 §2 identifies four distinct voice topologies we actually need to support, each with different latency, cost, and dependency profiles:

1. **Cascaded** — Discord VC → STT → Hermes LLM → TTS → VC (current default; cheapest, highest latency).
2. **Pipeline** — structured multi-stage provider graph (e.g. Deepgram STT → Hermes → ElevenLabs TTS) with per-stage config, barge-in, and interruption handling decoupled from transport.
3. **Realtime** — single bidirectional provider session (Gemini Live, OpenAI Realtime) where the provider owns VAD, turn-taking, and audio I/O.
4. **S2SServer** — a locally-hosted speech-to-speech model (Moshi-class, Ultravox-class) reached over WS/gRPC; behaves like Realtime but with self-hosted capability gating.

Research-15 §1 further requires: (a) a single unified async lifecycle so `gateway/run.py` doesn't need to know which mode is active, (b) per-channel session keying (the same guild can have two VCs with different modes during migration), and (c) capability gating so we fail fast when a mode is selected without its optional extras or API key.

The plugin-vs-core question is decided in research-15 §2: **mode dispatch stays inside hermes-s2s**. Pushing a `VoiceMode` concept into Hermes core's `gateway/run.py` would couple core to provider taxonomy that changes on a different cadence than core, and would block third-party voice plugins from shipping their own modes.

## Decision

### 1. `VoiceMode` enum (single source of truth)

```python
class VoiceMode(StrEnum):
    CASCADED   = "cascaded"
    PIPELINE   = "pipeline"
    REALTIME   = "realtime"
    S2S_SERVER = "s2s_server"
```

Stored as strings on disk and in slash-command args. `StrEnum` keeps YAML/JSON serialisation trivial.

### 2. `ModeRouter` — deterministic precedence

Resolution order, highest priority first (research-12 §3):

1. **Slash-command argument** (`/voice join mode:realtime`) — per-invocation, ephemeral.
2. **Environment variable** `HERMES_S2S_VOICE_MODE` — ops/dev override.
3. **Channel override** — `s2s.voice.channel_overrides[<channel_id>].mode`.
4. **Guild override** — `s2s.voice.guild_overrides[<guild_id>].mode`.
5. **Config default** — `s2s.voice.default_mode`.
6. **Hard default** — `"cascaded"` (works with zero API keys).

`ModeRouter.resolve(ctx) -> (VoiceMode, ModeSource)` returns both the mode and *why* it was chosen, for logging and the `s2s doctor` tool.

### 3. `VoiceSessionFactory`

```python
session = await VoiceSessionFactory.create(mode, channel, config, deps)
```

The factory (a) calls `ModeRouter` if `mode` is `None`, (b) runs **capability gating** (see §5), (c) constructs the concrete `VoiceSession` subclass, (d) registers it under a **per-channel key** `(guild_id, channel_id)` — not per-guild, because research-15 §1 requires parallel VCs during rollout and A/B.

### 4. Four `VoiceSession` subclasses, one lifecycle

All four inherit from `BaseVoiceSession` and share:

- **State machine**: `IDLE → CONNECTING → READY → SPEAKING → LISTENING → CLOSING → CLOSED` with a single `asyncio.Lock` guarding transitions.
- **Threading model**: audio I/O on the Discord voice thread, provider I/O on the asyncio loop, bridged via bounded `asyncio.Queue`s (ADR-0007 frame callback).
- **Unified lifecycle** via `AsyncExitStack`: STT client, TTS client, provider WS, VAD, metrics span, and the Discord `VoiceClient` are all entered as async context managers into one stack. `aclose()` unwinds in LIFO order, so partial-init failures never leak sockets or threads.

The subclasses differ only in what they push onto the stack:

| Subclass              | Stack contents (mode-specific)                                 |
|-----------------------|----------------------------------------------------------------|
| `CascadedSession`     | STT client, Hermes LLM handle, TTS client, VAD                 |
| `PipelineSession`     | Stage graph (N providers), per-stage barge-in controller       |
| `RealtimeSession`     | Single provider bidi WS (Gemini Live / OpenAI Realtime)        |
| `S2SServerSession`    | WS/gRPC to local S2S server + health-check supervisor          |

### 5. Capability gating + fail-closed-on-explicit-request

Each mode declares `requirements: ModeRequirements` (API env vars, optional extras, system libs). The factory checks them **before** allocating any resources.

- If the mode was **explicitly requested** (slash arg or env var), missing requirements raise `ModeUnavailableError` — we **fail closed**; no silent downgrade, because the user asked for that mode specifically.
- If the mode came from config/default, we log a WARNING and fall through to the next-lower-priority source, ultimately landing on `cascaded`. This preserves zero-config boot.

### 6. Plugin-owned dispatch (deliberate non-goal)

We explicitly **do not** add `VoiceMode` to Hermes core `gateway/run.py`. Core continues to dispatch a generic `voice_state_update` event; hermes-s2s subscribes and performs mode routing internally. Rationale (research-15 §2):

- Mode taxonomy evolves with the provider ecosystem (S2SServer didn't exist 6 months ago); core shouldn't re-release for that.
- Third-party voice plugins remain first-class — they register their own factory without core changes.
- Keeps the core/plugin boundary at "events + services", not "voice topology".

### 7. Soft-deprecation of `s2s.mode`

Old key `s2s.mode: realtime` is auto-translated at config load to `s2s.voice.default_mode: realtime` with a single WARNING and a one-shot upgrade hint in `s2s doctor`. Removal scheduled for 0.5.0. The wizard (ADR-0009) writes only the new key.

## Consequences

**Positive**
- Users can run cascaded and realtime side-by-side on one bot (per-channel keying).
- New modes (e.g. a future `local-llm` S2S) plug in as a subclass + factory entry — no core change.
- `AsyncExitStack` lifecycle removes the leaked-thread bugs that dogged 0.3.1 realtime.
- Fail-closed on explicit request makes operator mistakes loud instead of silently-expensive.

**Negative / risks**
- Four subclasses = more surface area; mitigated by shared base + tight protocol.
- `ModeRouter` precedence must be documented in user-facing docs (the six-level chain is not obvious).
- Config auto-translation is a 2-release burden we must remember to retire.

## References
- research-12 §2 (mode taxonomy), §3 (router precedence)
- research-15 §1 (unified session lifecycle, per-channel keying), §2 (plugin-owned dispatch)
- ADR-0006 (Discord voice bridge), ADR-0007 (frame callback), ADR-0009 (wizard profiles)
