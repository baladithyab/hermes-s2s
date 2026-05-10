# Wave 0.4.2 — Audio Clicks + Realtime History + Quality-of-Life

**Status:** plan v2 (incorporates parallel critique 2026-05-10)
**Date:** 2026-05-10
**Predecessor:** v0.4.1 (silent-VC fix)
**Successor:** wave-0.5.0-tiers-and-meta.md (planned)

## Critique-driven revisions from v1

Three parallel reviews (red-team / UX / architecture) caught critical issues. v2 reflects all:

| Finding | Source | v1 plan | v2 plan |
|---|---|---|---|
| Resampler overlap-save math wrong (trailing-edge click persists) | red-team P0-1 | custom StreamResampler | **drop custom; use `soxr.ResampleStream`** |
| K=32 history insufficient at 48k→16k | red-team P0-2 | hardcoded K=32 | soxr handles internally |
| `_drop` integer-truncation drift | red-team P0-3 | broken formula | soxr handles internally |
| Cache-reset paths under-specified | red-team P0-4 | "session end / barge-in" | enumerate: `stop()`, reconnect, barge-in, activity_start |
| Gemini `model`-role injection unverified | red-team P0-6 | trust the docs | **smoke-test against real endpoint before merging S2** |
| OpenAI history-injection not gated against concurrent audio | red-team P0-7 | none | block `send_audio_chunk` on `history_injection_complete` Event |
| History/mirror race, double-injection on rejoin | red-team P0-5, UX §2 | "skip on session resumption" | tag voice turns at write-time + dedupe in builder |
| Reply-onset pop (Fix B deferred) | UX §3 | defer to v0.4.3 | **ship Fix B in v0.4.2** |
| Synthetic model-closer text | UX §5 | `"(ready)"` | `"(voice session starting)"` |
| Persona drift after history injection | UX §1, §4 | none | one sentence in `systemInstruction`: "no tools in voice; references to past tool results are facts not actions" |
| Kwarg growth on `connect()` | arch Q1 | new `history` kwarg | **`ConnectOptions` dataclass**; reserves `tools` slot for v0.5.0 |
| `SessionDB()` direct instantiation | arch Q2 | `SessionDB()` from plugin | cache on adapter (`adapter._s2s_session_db`); upstream `ctx.get_conversation_history()` is v0.4.3 follow-up |
| `voice/history.py` placement | arch Q3 | `voice/` | **`_internal/history.py`** |
| Session-id resolution single-path | arch Q5 | `_entries[key]` only | 4-tier fallback cascade with explicit `try/except` |
| Config schema | arch Q7 | `s2s.voice.realtime.history.*` | nested under `s2s.voice.realtime.{history, tools, audio}` with typed `RealtimeConfig` envelope |

## Why this wave (re-stated)

Two user-visible defects on top of v0.4.1's working realtime voice:

1. **Audio clicks** during ARIA's speech in Discord VC — root-caused in research/17.
2. **No conversation context** — voice ARIA has amnesia of the prior text thread — root-caused in research/18.

Plus four UX adjacencies (LED, names, status output, barge-in) and two architecture forward-compat moves (ConnectOptions, RealtimeConfig).

## Out of scope

Tier system, voice-confirm flow, monkey-patch removal, structured logging, OTel metrics, multi-guild registry rework — all v0.5.0+. Multi-language detection, voice cloning, recording opt-in — v0.6+.

## Scope

### S1 — Audio clicks (revised)

**Drop the custom `StreamResampler`.** Replace `audio/resample.py` callers with `soxr.ResampleStream`:

- `soxr 1.1.0` is already installed in the Hermes venv.
- Provides streaming resample with internal state, `clear()`, and `delay()` introspection.
- Quality `"HQ"` default; `dtype='int16'` matches our pipeline natively (no f32 round-trip).
- Cache one `ResampleStream` per `(in_rate, out_rate, channels)` tuple on `RealtimeAudioBridge`.
- **Reset triggers** (explicit, all wired):
  - `RealtimeAudioBridge.stop()` — clear all streams
  - On `backend.connect()` reuse (reconnect path) — `clear()` per stream
  - On `interrupt()` / barge-in (when output is being truncated) — `clear()` per stream  
  - On `activity_start` event from user (new utterance starting; current output is being abandoned) — `clear()` per stream

**Plus Fix B (reply-onset fade-in)** in `BridgeBuffer`:
- Track `_last_was_silence: bool` in `BridgeBuffer.__init__`.
- In `read_frame`, after constructing a non-silence frame following a silence frame, apply a 5 ms raised-cosine fade-in to the leading 240 stereo s16 samples.
- Reset `_last_was_silence` on every silence return.

Tests in `tests/test_resample_streaming.py`:
- Continuity: chunked input through `ResampleStream` vs one-shot reference, max abs delta ≤ 8 LSB end-to-end (soxr is HQ but not bit-exact).
- No-click boundary: 1 kHz sine through 13 prime-length chunks, assert `max(diff) < 3 * percentile(diff, 99)` over the whole stream.
- Cache reset: drive `_dispatch_event` with two `audio_chunk` events, then `bridge.stop()`, then a third — assert no leaked state.
- Fade-in: push silence then DC-offset frame, assert leading 240 samples form a monotonic envelope.

### S2 — Realtime history injection (revised)

New module `hermes_s2s/_internal/history.py` (NOT `voice/`):

```python
def build_history_payload(
    session_db: SessionDB,
    session_id: str,
    *,
    max_turns: int = 20,
    max_tokens: int = 8000,
    skip_voice_metadata: bool = True,
) -> list[dict]:
    """Build OpenAI-format history list for realtime backends.
    
    Filters: drops system / tool / function roles. Coerces multimodal
    content to text. Drops empty content. Drops turns marked
    metadata.source == "voice" if skip_voice_metadata=True (avoids
    re-injecting prior voice utterances on rejoin).
    
    Truncates oldest first to fit max_tokens budget (heuristic len//4).
    """
```

Session-id resolution cascade in `discord_bridge._resolve_session_id_for_thread`:

1. `getattr(adapter, '_s2s_session_db', None)` — cached SessionDB on adapter.
2. If absent, `adapter._s2s_session_db = SessionDB()` (cached on first use; future-compat trap acknowledged in arch review §2 — v0.4.3 promotes to `ctx.get_conversation_history`).
3. Resolve `session_key` via `adapter.session_store._generate_session_key(synthetic_source)`.
4. Get `session_id`:
   - **Tier 1:** `getattr(adapter.session_store, 'get', None)` if a public getter is added upstream.
   - **Tier 2:** `adapter.session_store._entries.get(session_key)` (today's path; private).
   - **Tier 3:** `session_db.get_session_by_title(session_key)` (DB lookup fallback).
   - **Tier 4:** Discord REST `thread.history(limit=N)` — last-resort.
5. Wrap each tier in `try/except` with `logger.debug` on miss; `logger.warning` only when all four fail.

**Voice-mirror metadata tagging:** `voice/transcript.py:TranscriptMirror.write_to_db` (or wherever it persists) gains a `metadata={"source": "voice"}` parameter. SessionDB write-path must accept and persist this. If SessionDB's existing schema doesn't have a JSON metadata column, we either (a) add one with a migration, or (b) tag inside `content` as a magic prefix `[voice] ...` and strip in `build_history_payload`. **Decide during implementation** — prefer (b) if SessionDB schema is locked.

**`ConnectOptions` dataclass** in `hermes_s2s/voice/connect_options.py`:

```python
@dataclasses.dataclass(frozen=True)
class ConnectOptions:
    system_prompt: str
    voice: str
    tools: list[dict]
    history: Optional[list[dict]] = None
    extras: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    # Reserved for v0.5.0:
    # tier: Optional[Literal["read", "act", "meta", "off"]] = None
    # confirm_policy: Optional[ConfirmPolicy] = None
```

`_BaseRealtimeBackend.connect()` becomes overloaded:
- New: `async def connect(self, opts: ConnectOptions) -> None`
- Compat shim: if called with positional `(prompt, voice, tools)`, build `ConnectOptions` internally.

Existing tests pass without modification (positional triple still works).

**Gemini `_send_history`** wedged after `setupComplete` in `connect()`:
- Map roles: `user`→`user`, `assistant`→`model`.
- If final turn is `user`, append `{"role": "model", "parts": [{"text": "(voice session starting)"}]}`.
- Single `clientContent{turns, turnComplete:true}` frame.
- **Real-endpoint smoke test before merging:** `scripts/smoke_history_gemini.py` connects to live Gemini Live, injects 5 turns ending in user, asserts model stays silent (no `serverContent.modelTurn` for ≥3s). Run via `HERMES_S2S_E2E_GEMINI=1`.

**OpenAI `_send_history`** after `session.update`:
- One `conversation.item.create` per turn.
- No `response.create` tail.
- **Set `_history_injection_complete` event (asyncio.Event)** before returning.
- `send_audio_chunk` awaits `_history_injection_complete.wait()` before sending. (Gemini path same gating in case `clientContent` interleaves with `realtimeInput.audio`.)

**Persona note** appended to `systemInstruction` when history is non-empty:
> "In this voice session you cannot call tools. References in the prior conversation to tool calls or actions you took describe completed work — treat them as known facts, not ongoing tasks. Keep replies short and conversational."

Tests in `tests/test_realtime_history_injection.py` (12 tests):
1. Order: setup → setupComplete → clientContent → no further pre-audio frames.
2. Payload shape: roles correctly mapped.
3. Final-role closer: history ending in user gets synthetic model closer with exact text `"(voice session starting)"`.
4. Empty history: no clientContent frame emitted.
5. Budget truncation: 100-turn history → ≤ max_turns reach wire AND ≤ max_tokens*4 chars.
6. Resumption skip: `sessionResumption.handle` set → no clientContent.
7. Voice-mirror dedup: turns with `source=voice` filtered out.
8. SessionDB read failure: `build_history_payload` returns `[]` defensively, no raise.
9. OpenAI dual-event: 5 turns → 5 `conversation.item.create` events, zero `response.create`.
10. Gating: `send_audio_chunk` blocks until `_history_injection_complete.wait()` returns.
11. ConnectOptions back-compat: positional `connect(prompt, voice, tools)` still works.
12. Persona note injection: history non-empty → systemInstruction has tool-disclaimer suffix.

Plus `tests/smoke/test_smoke_history_gemini.py` — opt-in real-endpoint test gated on env var.

### S3 — UX & quality-of-life slip-ins

From audit P1, slipped into 0.4.2 because they compound with S1+S2:

- **Audit #21** (interruption / barge-in clears output buffer) — M effort, same file as S1. On `activity_start` from user: `voice_client.stop()` + `buffer.clear_output()` + `resampler.clear()` per stream.
- **Audit #28** (voice activity LED via bot presence) — S effort. Drive presence: `🎙 listening` / `🤖 thinking` / `🔊 speaking`. Throttle to 1 update/sec via deque.
- **Audit #24** (real display names in mirror) — S effort. Thread `event.source.user_display_name` through bridge attach; replace `"@user"` placeholder.
- **Audit #42** (better `s2s_status`) — S effort. Add: effective mode, provider, model, connected-since, thread mirror target, **history.injected_turns / injected_tokens** (so verification step is checkable).

### S4 — P0 quick-wins from audit (defensive batch)

Same five as v1 plan:

- **Audit #5** — `TranscriptMirror.schedule_send`: deprecated `asyncio.get_event_loop()` → `get_running_loop()`.
- **Audit #6** — `_pending_first_msg` initialised in `GeminiLiveBackend.__init__`.
- **Audit #8** — `_prev_lens` dict moved onto `voice_receiver` instance + cleaned up in leave-path.
- **Audit #10** — Dedicated `type="session_cap"` event in OpenAI realtime.
- **Audit #45** — Delete unused `prev_in` in heartbeat.

### S5 — Architecture forward-compat (v0.5.0 prep)

- `voice/connect_options.py` — `ConnectOptions` dataclass (covered in S2).
- `config/realtime_config.py` — typed `RealtimeConfig` envelope:
  ```python
  @dataclasses.dataclass
  class HistoryConfig:
      enabled: bool = True
      max_turns: int = 20
      max_tokens: int = 8000
  
  @dataclasses.dataclass
  class AudioConfig:
      resampler: str = "soxr"        # "soxr" | "scipy"
      silence_fade_ms: int = 5
  
  @dataclasses.dataclass
  class RealtimeConfig:
      history: HistoryConfig = dataclasses.field(default_factory=HistoryConfig)
      audio: AudioConfig = dataclasses.field(default_factory=AudioConfig)
      tools: Optional[Any] = None  # reserved for v0.5.0 ToolsConfig
      
      @classmethod
      def from_dict(cls, raw: dict) -> "RealtimeConfig": ...
  ```
- Config keys (nested):
  ```yaml
  s2s:
    voice:
      realtime:
        history: { enabled: true, max_turns: 20, max_tokens: 8000 }
        audio:   { resampler: soxr, silence_fade_ms: 5 }
        # tools: <reserved for v0.5.0>
  ```

## Test budget

- New tests: ~20 (resample-streaming 4 + history 12 + UX/audit-quickwin 4)
- Smoke (opt-in): 1 (Gemini real-endpoint)
- Existing 213 tests must remain green → projected 233 pass, 0 fail.

## Migration

- `OpenAIRealtimeBackend` event type change from `error{reason=session_cap}` → `session_cap` (Audit #10) — semver-minor breaking. Documented in CHANGELOG.
- `RealtimeBackend.connect` signature accepts both positional and `ConnectOptions` — back-compat preserved.
- `s2s.voice.realtime.history.*` keys are new; defaults make existing configs work unchanged.

## Rollout plan

1. Implement S5 first (`ConnectOptions`, `RealtimeConfig`) — unblocks rest.
2. Implement S1 (clicks) using `soxr.ResampleStream` — independent.
3. Implement S2 (history) using `ConnectOptions` from step 1.
4. Implement S3 (UX slip-ins) — independent of S2.
5. Implement S4 (quick-wins) — batched commits.
6. Run unit tests; bump version to 0.4.2.
7. Run real-endpoint smoke (`HERMES_S2S_E2E_GEMINI=1`).
8. Tag, push.
9. Reinstall in plugin checkout via Hermes venv.
10. User live-VC verifies on a thread with prior chat:
    - (a) clicks gone — no audible pops at reply onset OR mid-reply.
    - (b) ARIA references at least one fact from prior text turn.
    - (c) Discord presence cycles "listening / thinking / speaking" appropriately.
    - (d) Real Discord display name appears in mirrored transcripts.
    - (e) `/s2s_status` shows `history.injected_turns > 0`.
    - (f) Barge-in test: user speaks over ARIA → ARIA stops within ~100 ms (no 300 ms tail).
11. If any verification fails → diagnose, hotfix, re-tag as 0.4.2-rcN.

## Success criteria

- Clicks: no user-reported clicks during 2-min back-and-forth.
- Context: ARIA references prior text turn within first reply.
- Barge-in: ≤ 100 ms tail on interrupt.
- Tests: 233/233 green locally + Hermes venv.
- Real-endpoint smoke pass.

## Anti-goals

- **No new monkey-patches.** S2 must use existing thread-resolution path.
- **No protocol changes that break v0.4.1 callers.** ConnectOptions is additive.
- **No tier work.** Strictly v0.5.0.
- **No multi-guild registry rework.** v0.4.3+.

## Implementation order (deep-work-loop)

| Phase | Stream | Owner | Deliverables |
|---|---|---|---|
| P3-S5 | Forward-compat | inline | `ConnectOptions`, `RealtimeConfig`, tests |
| P3-S1 | Clicks | inline | soxr stream cache + Fix B fade-in + tests |
| P3-S2 | History | inline | `_internal/history.py` + Gemini/OpenAI wiring + tests |
| P3-Smoke | Real endpoint | inline | scripts/smoke_history_gemini.py + manual run |
| P3-S3 | UX slip-ins | inline | barge-in, presence, names, status |
| P3-S4 | Quick-wins | inline | batched 5 fixes |
| P3-Ship | Tag & push | inline | version bump, CHANGELOG, tag |
| P4 | Verify | user | live VC ≥ 6 criteria |
