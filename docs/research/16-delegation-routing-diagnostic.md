# 16 — Delegation Routing Diagnostic

**Session:** 2026-05-10 · **Orchestrator:** `anthropic/claude-opus-4.7` via `openrouter` with `provider_routing.order=[amazon-bedrock, anthropic, google-vertex]` · **Symptom:** route-fidelity probe returned 3/3 subagents reporting `anthropic/claude-opus-4.7` instead of the dispatched slugs (`openai/gpt-5.5`, `google/gemini-3.1-pro-preview`, `moonshotai/kimi-k2-thinking`). All subagents reported `OPENROUTER_API_KEY:False`.

## 1. Config inspection — `~/.hermes/config.yaml`

Delegation block (lines 274–286):

```yaml
delegation:
  model: anthropic/claude-opus-4.7      # line 275
  provider: openrouter                  # line 276
  base_url: ''                          # line 277
  api_key: ''                           # line 278 — empty → inherit parent key
  ...
  max_concurrent_children: 6
  max_spawn_depth: 2
  orchestrator_enabled: true
```

Top-level model (lines 1–5): `anthropic/claude-opus-4.7` via `openrouter`. `provider_routing.order` (lines 398–403) pins `[amazon-bedrock, anthropic, google-vertex]`.

**Key observation:** `delegation.provider = openrouter` matches the orchestrator's provider; `delegation.model` matches the orchestrator's model. Per-task overrides (model/provider) would need to come through `tasks=[{model:..., provider:...}]` kwargs — but (see §3) that plumbing **does not exist**.

## 2. Auth inspection — `~/.hermes/auth.json`

OpenRouter key IS present in `credential_pool.openrouter[0]`:
```json
"label":"OPENROUTER_API_KEY", "source":"env:OPENROUTER_API_KEY",
"access_token":"<REDACTED sk-or-v1-…>", "base_url":"https://openrouter.ai/api/v1"
```

`providers:{}` is empty and `active_provider:null`. The key flows through the **credential pool** only; there is no `OPENROUTER_API_KEY` in the running agent's `os.environ` (confirmed: `env | grep OPENROUTER` returns empty). This explains the subagents' `OPENROUTER_API_KEY:False` env-check — the probe was reading `os.environ`, not the credential pool. That signal is a red herring (the parent uses the pool, not the env var), but it does indicate the probe never materialised a dedicated key for the child.

## 3. Source-code trace — `/home/codeseys/.hermes/hermes-agent/tools/delegate_tool.py`

**Public entry:** `delegate_task(... tasks=[...], ...)` at **line 1898**. No `delegation_orchestrator.py` file exists — delegation lives in `tools/delegate_tool.py` (2767 lines).

**Credential resolution** happens ONCE at the top of `delegate_task`, outside the per-task loop:

- **Line 1976:** `creds = _resolve_delegation_credentials(cfg, parent_agent)` — reads only the top-level `cfg` (i.e., `delegation.*` from config.yaml).
- **`_resolve_delegation_credentials` (lines 2325–2374):** reads `cfg.get("model")`, `cfg.get("provider")`, `cfg.get("base_url")`, `cfg.get("api_key")`. **No awareness of per-task fields.**

**Per-task loop** (lines 2036–2057):

```python
for i, t in enumerate(task_list):
    ...
    child = _build_child_agent(
        task_index=i,
        goal=t["goal"],
        context=t.get("context"),
        toolsets=t.get("toolsets") or toolsets,
        model=creds["model"],                    # ← line 2046: HARD-CODED from top-level creds
        ...
        override_provider=creds["provider"],     # ← line 2050: same
        override_base_url=creds["base_url"],     # ← line 2051
        override_api_key=creds["api_key"],       # ← line 2052
        override_api_mode=creds["api_mode"],     # ← line 2053
        ...)
```

**A grep of the entire file for `t.get("model")`, `t["model"]`, `t.get("provider")`, `task.get("model")`, `task["model"]`, `t.get("base_url")`, `t.get("api_key")` returns ZERO matches.** Per-task `model`/`provider` fields in the `tasks=[{…}]` array are **silently dropped**. The only task-level fields consulted are `goal`, `context`, `toolsets`, `role`, `acp_command`, `acp_args` (line 2037–2058).

**Inheritance fallback** (`_build_child_agent`, lines 1015–1019):
```python
effective_model    = model or parent_agent.model               # creds["model"]="anthropic/claude-opus-4.7" → used
effective_provider = override_provider or parent_agent.provider # creds["provider"]="openrouter" → used
effective_api_key  = override_api_key or parent_api_key         # creds["api_key"]=None → parent_api_key
```

With `delegation.model = anthropic/claude-opus-4.7` and `delegation.provider = openrouter`, every child is constructed with the orchestrator's exact model/provider, inheriting the parent's API key from `parent_agent.api_key` (pulled from the credential pool by the parent's client init). The child then makes a single call to OpenRouter for `anthropic/claude-opus-4.7` — exactly what the probe observed.

**Providers-order handling** (lines 1076–1088): when `override_provider` is set, `child_providers_order` is set to `None`, so Bedrock-pinning WOULD be cleared — but because `override_provider="openrouter"` equals the parent provider, this code path still runs and (correctly) strips the `[amazon-bedrock]` order. So the Bedrock pinning is NOT the direct cause; the per-task model is simply never read. (If it were read, the Bedrock-strip code at L1081 would handle the routing correctly.)

## 4. Log inspection

`grep -iE 'delegat|child|spawn|override|fallback' ~/.hermes/logs/agent.log`: only benign INFO entries (`tool delegate_task completed (Ns, Nchars)`). **No WARNING for override-failure, no credential-fallback log, no Bedrock-rejection log.** This is consistent with the bug being a silent drop at the Python level — no code path ever attempts to honour the per-task override, so nothing logs its failure.

## 5. Bedrock complication

Tangential. Lines 1076–1088 correctly null out `providers_order` when `override_provider` is set, so a cross-provider override would NOT inherit `[amazon-bedrock]`. The Bedrock pinning is only an issue IF the per-task override were actually honoured (it isn't) AND the override pointed at the same `openrouter` provider without clearing the order (the code already handles this). **Not the root cause.**

## 6. Slug sanity-check

Slugs the user's memory confirms working ARE exactly what `skills/software-development/deep-work-loop/references/PHASES.md` and `SKILL.md` enumerate: `google/gemini-3.1-pro-preview`, `moonshotai/kimi-k2.6`, `deepseek/deepseek-v4-pro`. `openai/gpt-5.5` is in the "dead-on-OpenRouter" cohort per user memory. `moonshotai/kimi-k2-thinking` is a lesser-used variant but was plausibly valid — however, slug validity is moot here because **no slug was ever sent.**

---

## Root cause (1 paragraph)

**The per-task `model` / `provider` fields in `delegate_task(tasks=[{model:…, provider:…, goal:…}, …])` are silently ignored.** `delegate_task` resolves credentials ONCE from `delegation.*` config (line 1976, `_resolve_delegation_credentials`) and passes `creds["model"]` / `creds["provider"]` to every child in the per-task loop (`delegate_tool.py` lines 2046, 2050–2053) without ever reading `t.get("model")` or `t.get("provider")`. Because `delegation.model = anthropic/claude-opus-4.7` and `delegation.provider = openrouter` in this session's config, every subagent inherits the orchestrator's model regardless of what the task dict says. The `OPENROUTER_API_KEY:False` env signal from subagents is a red-herring artefact of key-via-credential-pool-not-env; the real failure is the missing `t.get("model") or creds["model"]` plumbing at line 2046. This is the canonical "router trap" documented in the `parallel-critique` skill's Hard Rule #0.

## Quick fix

Three options, ordered by reversibility:

1. **Config-only workaround (session-local, no code changes).** Set a *cheap default* in `~/.hermes/config.yaml` under `delegation.*`, then accept that ALL children run as that single model. E.g. `delegation.model: google/gemini-3.1-pro-preview`. This gives you single-family review, not cross-family scatter. **Does not restore cross-family scatter.**
2. **Env-only workaround: none available.** There is no env var the delegate_tool consults for per-task routing.
3. **Source fix (out of scope per task instructions; not applied).** At `delegate_tool.py` line 2046 and 2050–2053, change `creds["model"]` → `t.get("model") or creds["model"]`, and similarly for `provider`/`base_url`/`api_key`. Then re-resolve per-task provider credentials if `t.get("provider")` differs from `creds["provider"]` (invoke `_resolve_delegation_credentials` with a per-task cfg overlay).

## Workaround (cross-family scatter IS impossible in this session)

Until the source fix lands:
- **Do NOT claim cross-family scatter.** Any `delegate_task(tasks=[…])` will run N copies of `anthropic/claude-opus-4.7`.
- **For orthogonal review,** use single-family review with explicit context-isolation: fire one `delegate_task` at a time with distinct `goal`/`context` framings. Context isolation still produces some orthogonality even without model diversity (per v0.3.0 observation in deep-work-loop SKILL.md).
- **If single-model-different-framing isn't enough,** change `delegation.model` between scatters (restart not required per config reload — but verify: YAML is re-read in `_load_config()`).

## Detection signals for future sessions

1. **Hard Rule #0 probe (canonical).** Run `scripts/route_probe.py` from `~/.hermes/skills/autonomous-ai-agents/parallel-critique/` (or the inline JSON honesty probe in `references/router-trap-hermes.md`). <5s, ~50 tokens. GREEN = overrides taking; RED = route broken.
2. **Result-dict inspection.** Every `delegate_task` result contains `model` in each task's result; if all N tasks show the orchestrator's model despite different dispatched slugs, route is RED.
3. **Findings homogeneity.** 4-of-4 reviewers using near-identical phrasing → single model role-playing (weak post-hoc signal; probe is the reliable detector).
4. **No log warnings.** Absence of override-failure warnings in `agent.log` is itself a signal — the bug is a silent drop, not a loud rejection.

## Recommendation: mandate route-fidelity probe as a hard-gate

**YES — the skill already mandates it, the orchestrator should enforce it.** `skills/autonomous-ai-agents/parallel-critique/SKILL.md` explicitly states Hard Rule #0: *"MANDATORY pre-flight: route-fidelity probe BEFORE the first scatter of any session. NEVER skip the probe. NEVER assume a fix from a prior session is still in effect."* Today's failure is the exact scenario that rule exists to catch. The orchestrator running parallel-critique workflows should (a) run the ≤5s probe before any multi-task `delegate_task` scatter, (b) hard-gate: if RED, abort the scatter and either fall back to single-family review with a writeup acknowledgement or halt and ask for user confirmation. This is already the documented discipline; enforcement has been informal. Recommend making it a mechanical pre-flight step in any skill that fans out via `delegate_task`.
