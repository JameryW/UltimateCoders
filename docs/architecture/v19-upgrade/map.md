# V19 Architecture Upgrade Map

This map records the v19 upgrades requested in the architecture ledger
(thirteenth period, items #73–#78). The additions are opt-in libraries:
four Python components, two Rust modules in `uc-engine`, and one
orchestrator RL helper. No new service, database migration, scheduler, or
live route is introduced, and every feature flag defaults off.

## Status

| Area | Modules | Flag | Wired at | State |
| --- | --- | --- | --- | --- |
| CodeSpec dual specs | `python/ultimate_coders/agent/codespec_compiler.py` | `UC_CODESPEC_COMPILER` | feature planning; no planner caller yet | module only |
| STAIR plan reuse | `python/ultimate_coders/search/stair_tree.py` | `UC_STAIR_TREE` | repair planning; no search caller yet | module only |
| CodeGrep retrieval | `python/ultimate_coders/search/codegrep_retriever.py` | `UC_CODEGREP_RETRIEVER` | retrieval tool; no worker caller yet | module only |
| PerfAgent optimization | `python/ultimate_coders/acceptance/perf_agent.py` | `UC_PERF_AGENT` | acceptance/optimization; no runner caller yet | module only |
| Argus + PMG runtime | `crates/uc-engine/src/argus_pmg.rs` | n/a (library) | runtime strategy state; no engine caller yet | module only |
| Agent Lightning / Self-Harness | `packages/uc-orchestrator/src/rl/agent_lightning.ts` | n/a (library) | offline RL/sample curation; no live caller yet | module only |

## CodeSpec dual executable specifications (#73)

`codespec_compiler.py` turns a set of `Requirement` records and
`ComponentEvidence` records into two complementary specs. `ArchitectureSpec`
records the participating components, their repository paths, and dependency
edges that are supported by evidence. `BehaviorSpec` records the de-duplicated
behavioral clauses from the requirements. `FunctionalChain` keeps one
requirement-to-component mapping with its expected symbols.

`CodeSpecCompiler.compile` rejects empty requirement lists, duplicate
requirement ids, and duplicate component evidence. It reports missing
components and missing expected symbols as deterministic `SpecGap` values; an
edge is emitted only when the dependency is itself present in the evidence.
`validate` checks that touched components implement every chain and that the
supplied test names cover each requirement/component hint. It does not inspect a
real AST or run tests.

## STAIR hierarchical trajectory planning (#74)

`stair_tree.py` stores historical repairs at three levels: `STRATEGY`,
`SUBGOAL`, and `DIAGNOSTIC`. `StairTree.add_trajectory` creates a strategy root
with subgoal and diagnostic children. `search` scores nodes by text overlap
(weighted ×2) plus tag overlap, selects the best strategy, then mixes subgoals
and diagnostics into a bounded `StairPlan`. Failed trajectories are excluded
unless `include_failed=True`.

`StairPlan.render` produces the compact plan that can be inserted into a new
issue prompt. The module never calls an LLM or reads a repository; callers
supply previously distilled trajectory text.

## CodeGrep precision-gated retrieval (#75)

`codegrep_retriever.py` models a GRPO-trained parallel retrieval policy over a
caller-provided `CodeIndex`. The policy executes `grep`, falls back to
`glob`+`read` when grep is empty, deduplicates by path/symbol, and computes
retrieval precision against the supplied relevant paths.

When precision falls below `precision_threshold` (default 0.6), the report sets
`fallback=True` and includes a bounded RepoMap built from indexed file/symbol
names, clipped to `repo_map_budget_tokens * 4` characters. The 0.677 published
threshold is the target for a trained retriever; the deterministic default keeps
the gate conservative and configurable. The module does not open files or run a
model.

## PerfAgent profiler-guided optimization (#76)

`perf_agent.py` ranks `ProfileSample` hotspots by `samples * stack_share` and
evaluates candidate patches one at a time. `PerfAgent.evaluate` accepts a
candidate only when all functional tests pass, its speedup is at least the
larger of `min_speedup` and the current best, and the targeted hot-stack share
strictly decreases. `optimize` stops at `max_rounds`, records every attempt as
`PerfAttempt`, and returns a `PerfReport` with the accepted ids and best
speedup.

The module does not run a profiler or apply a patch; callers supply measured
samples and test results.

## Argus verification-gated runtime and PMG (#77)

`argus_pmg.rs` keeps a persistent `ArgusRuntime` of registered
`ArgusStrategy` records and recorded `WaveOutcome` values. A wave succeeds only
when external verification passed and the physical mapping was satisfied.
The runtime reports a `Cold` → `Warm` → `Mature` phase progression and
first-to-last token/latency savings.

`PmgGuard` removes the agent's self-verification authority: a verifier must be
one of the explicitly trusted external verifier ids and must not equal the
agent id. `S2PMapping` validates that every declared `PhysicalConstraint` id
was observed. `PmgDecision` distinguishes self-verification, untrusted
verifier, missing physical constraint, and allowed outcomes. `ArgusRuntime`
is pure state; no model weights or sandbox processes are touched.

## Agent Lightning harness-native RL and Self-Harness (#78)

`agent_lightning.ts` models a harness-owned interaction loop. `HarnessLoop`
records raw `LlmRequest`/`LlmResponse` pairs plus outcome metadata, while
`trainerView` exposes only the raw pair to the trainer. `mineWeaknesses` ranks
failure tags by failure rate; `proposeMinimalPatch` emits a one-line explicit
verification change; `verifyRegression` checks a candidate against a baseline
with a tolerance.

`selectTrajectoryCentroid` keeps the configured fraction (default 10%) of
trajectories closest to the feature centroid and reports the median distance.
`SelfHarnessEvolver` combines weakness mining, minimal proposals, regression
scores, and the centroid subset in one deterministic pass. Published constants
record the 6K-sample / Qwen3.5-9B 41.8→56.4% result and the 132% relative
Self-Harness gain. The module does not call a model, a harness, or a filesystem.

## Verification

Verification performed on 2026-11-24:

```bash
.venv/Scripts/python.exe -m pytest tests/python -k v19 -q --no-cov
# 32 passed, 4219 deselected

.venv/Scripts/python.exe -m ruff check \
  python/ultimate_coders/agent/codespec_compiler.py \
  python/ultimate_coders/search/stair_tree.py \
  python/ultimate_coders/search/codegrep_retriever.py \
  python/ultimate_coders/acceptance/perf_agent.py \
  tests/python/test_v19_*.py
# All checks passed

cd packages/uc-orchestrator && bun test src/rl src/skills src/harvest src/harness
# 175 pass, 0 fail

cargo test -p uc-engine --lib
# 751 passed; 0 failed; 0 ignored
```

On Windows the Rust test harness needs `CARGO_PROFILE_DEV_DEBUG=0` and a
workspace-local `TEMP`/`TMP` directory. `cargo fmt -p uc-engine -- --check`
is clean.

## Non-goals

- `codespec_compiler` does not parse source, edit files, or execute tests.
- `stair_tree` does not mine live trajectories or call an LLM.
- `codegrep_retriever` does not open a repository; `CodeIndex` is supplied by
  the caller.
- `perf_agent` does not run a profiler or apply patches.
- `argus_pmg` does not run a model or sandbox; it is a state and authorization
  library.
- `agent_lightning.ts` does not invoke a trainer, harness, or model.
