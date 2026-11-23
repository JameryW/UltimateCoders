# V25 Architecture Upgrade Map

This map records the v25 upgrades requested in the architecture ledger
(nineteenth period, items #109–#114). The additions are opt-in libraries:
one Rust module in `uc-engine`, three Python components, two TypeScript
modules, and no new service, database migration, scheduler, container
runtime, or live route. Every feature flag defaults off.

## Status

| Area | Modules | Flag | Wired at | State |
| --- | --- | --- | --- | --- |
| Agent workflow IR compiler | `python/ultimate_coders/agent/agentpro_compiler.py` | `UC_AGENTPRO_COMPILER` | planner workflow assembly; no caller yet | module only |
| Memory lifecycle and sleep merge | `python/ultimate_coders/search/memoir_lifecycle.py` | `UC_MEMOIR_LIFECYCLE` | retrieval and consolidation; no caller yet | module only |
| Verified-trace skill compiler | `python/ultimate_coders/search/zep_skill_compiler.py` | `UC_ZEP_SKILL_COMPILER` | skill library; no caller yet | module only |
| Safe Rust binding wavefront | `crates/uc-engine/src/crustify_wavefront.rs` | n/a (library) | Rust/FFI binding generation; no engine caller yet | module only |
| Process reward and release gate | `packages/uc-orchestrator/src/rl/pnp_process_verifier.ts` | n/a (library) | RL export and release gating; no caller yet | module only |
| Infra RSI and compiler TCB | `packages/uc-orchestrator/src/rl/infra_rsi.ts` | n/a (library) | training infrastructure; no caller yet | module only |

## Agent workflow IR compiler (#109)

`agentpro_compiler.py` compiles a flat `WorkflowOp` sequence into fused
regions. Adjacent LLM calls that do not require an intermediate observation
share one region; reads already satisfied by earlier writes are removed;
every region carries a guard and fallback label. The result reports the
removed reads, original and compiled token estimates, and the token-saving
ratio. The module does not execute the workflow.

## Memory lifecycle and sleep merge (#110)

`memoir_lifecycle.py` implements query-time utility attribution with a UCB
exploration bonus, exposure counting, and deterministic ranking. Offline
sleep consolidation proposes similar pairs and merges only when the
secondary content adds no missing evidence tokens; otherwise the decision
records the lost evidence and keeps both entries. The module does not call
a model or an embedding service.

## Verified-trace skill compiler (#111)

`zep_skill_compiler.py` separates a `Reflector` from a `Curator`.
`Reflector.compile` accepts only verified, successful traces and needs at
least two traces with the same action sequence before compiling a skill;
the skill carries trace/step evidence. `Curator.admit` rejects unsafe
command patterns, and `record_use` plus `rank` reorder skills by verified
win rate. `expose` produces a bounded progressive-disclosure block.

## Safe Rust binding wavefront (#112)

`crustify_wavefront.rs` models a Wavefront plan. Each `FfiSymbol` may
depend on other symbols; `plan` topologically groups independent symbols
into waves and rejects unknown dependencies or cycles. `CrustifyVerifier`
combines the plan with a `BindingInventory` and Miri findings, failing when
coverage is below the threshold or any reachable UB remains. The module
does not run bindgen, a C compiler, or Miri.

## Process reward and release gate (#113)

`pnp_process_verifier.ts` aggregates only verified intermediate artifacts
into a bounded dense reward. `ThreeValuedGate` reports `APPLICABLE`,
`NOT_APPLICABLE`, or `UNKNOWN` from policy preconditions, keeping missing
facts distinct from mismatches. `NullModelGate` flags a candidate whose
improvement over the unchanged-system baseline is below the configured
delta as a pseudo repair.

## Infra RSI and compiler TCB (#114)

`infra_rsi.ts` fits a censored peak-memory model: OOM trials contribute a
lower-bound estimate at 1.1x the configured limit while observed trials
contribute their maximum. `CompilerTcb` checks a patch against known
miscompile tokens for the exact compiler version. `InfraRsi.optimize`
reports the best trial MFU gain capped by the target. The module does not
train, allocate memory, or invoke a compiler.

## Verification

Verification performed on 2026-11-24:

```bash
.venv/Scripts/python.exe -m pytest tests/python -k v25 -q --no-cov
# 30 passed, 4422 deselected

.venv/Scripts/python.exe -m ruff check \
  python/ultimate_coders/agent/agentpro_compiler.py \
  python/ultimate_coders/search/memoir_lifecycle.py \
  python/ultimate_coders/search/zep_skill_compiler.py \
  tests/python/test_v25_*.py
# All checks passed

cd packages/uc-orchestrator && \
  bun test src/rl src/skills src/harvest src/harness src/telemetry
# 217 pass, 0 fail

cargo test -p uc-engine --lib
# 871 passed; 0 failed; 0 ignored
```

On Windows the Rust test harness needs `CARGO_PROFILE_DEV_DEBUG=0` and a
workspace-local `TEMP`/`TMP` directory. `cargo fmt -p uc-engine -- --check`
is clean.

## Non-goals

- `agentpro_compiler` does not execute the workflow it compiles.
- `memoir_lifecycle` does not call a model or an embedding service.
- `zep_skill_compiler` does not execute skills or rank by live model runs.
- `crustify_wavefront` does not run bindgen, rustc, or Miri.
- `pnp_process_verifier` does not grade artifacts itself; callers supply
  verification results.
- `infra_rsi` does not allocate GPU memory or inspect compiler binaries.
