# V27 Architecture Upgrade Map

This map records the v27 upgrades requested in the architecture ledger
(twenty-first period, items #121–#126). The additions are opt-in libraries:
one Rust guard, one Rust telemetry extension, three Python components, one
TypeScript RL module, and no new service, database migration, scheduler,
container runtime, or live route. Every feature flag defaults off.

## Status

| Area | Modules | Flag | Wired at | State |
| --- | --- | --- | --- | --- |
| System-1 pointer routing | `python/ultimate_coders/architecture/anys1_router.py` | `UC_ANYS1_ROUTER` | behavior router; no caller yet | module only |
| Compaction-immune mandates | `crates/uc-engine/src/governance/compaction_rule_guard.rs` | n/a (library) | tool-boundary authorization; no engine caller yet | module only |
| Stateful programmatic tools | `python/ultimate_coders/agent/stateful_ptc.py` | `UC_STATEFUL_PTC` | worker tool execution; no caller yet | module only |
| Token-level communication and rubric credit | `packages/uc-orchestrator/src/rl/token_credit_opsd.ts` | n/a (library) | RL export; no caller yet | module only |
| Token heat telemetry | `crates/uc-engine/src/runtime_metrics.rs` | n/a (library) | runtime metrics; no engine caller yet | module only |
| Decision-impact clarification | `python/ultimate_coders/knowledge/decision_clarifier.py` | `UC_DECISION_CLARIFIER` | planner clarification; no caller yet | module only |

## System-1 pointer routing (#121)

`anys1_router.py` implements a calibrated pointer router for routine
decisions. A `DecisionOption` carries an action, a model confidence, and a
cost. `AnyS1Router.observe` records realised outcomes, and `calibrate`
blends the label-smoothed observation accuracy with an accuracy prior after
three observations. `route` accepts System-1 only when the calibrated
confidence clears the threshold and otherwise escalates to System-2. The
module does not load a model or call an inference endpoint.

## Compaction-immune mandates (#122)

`compaction_rule_guard.rs` separates hard mandates from compactable history.
`MandateRule` declares forbidden actions and protected paths; `CompactionAnchor`
records the rules that were present when a summary was produced.
`anchor_after_compaction` re-registers the rules outside the compressed
region, and `authorize` rejects forbidden actions and protected-path writes.
The guard is deterministic and does not inspect model summaries with an LLM.

## Stateful programmatic tools (#123)

`stateful_ptc.py` keeps typed variables across turns. `ProgrammaticSession`
stores `TypedValue` entries under a capacity bound, records `CallRecord`
results, and retries only `TransientToolError`. `DeterministicPlan.execute`
runs a fixed sequence of graph-query calls without an LLM decision in the
loop. The module does not execute real tools itself; callers provide the
typed transport.

## Token-level communication and rubric credit (#124)

`token_credit_opsd.ts` filters multi-agent communication facts by a
leave-one-out gain and distributes the gain over the tokens the fact
produced. `communicationGain` computes the improvement from keeping a fact,
and `filterCommunicationFacts` keeps positive-gain facts. `rubricAdvantage`
turns weighted dynamic rubric items into a uniform token advantage, and
`combineTokenAdvantages` adds outcome, dense, and rubric credit. The module
does not train a policy or call a sampler.

## Token heat telemetry (#125)

`runtime_metrics.rs` adds `TokenHeatTelemetry`, which records the text-token
count that did and did not change the Git tree around a tool call. A
`TokenHeatReport` reports the heat fraction and the free-energy fraction and
flags a breach when the heat ratio exceeds `DEFAULT_HEAT_THRESHOLD` (0.8).
`record` is bounded and `reset` clears the samples. The module does not
inspect prompts or call a model.

## Decision-impact clarification (#126)

`decision_clarifier.py` ranks missing parameters by their impact on the
current plan. `DecisionClarifier.plan` asks only questions whose impact
clears the threshold and whose answer could change the selected action;
`apply_answers` applies the supplied values. `GovernanceSpec` rejects an
enforced obligation without a concrete `blocking_action`, so a requirement
cannot be recorded as merely traceable. The module does not call a model.

## Verification

Verification performed on 2026-11-24:

```bash
.venv/Scripts/python.exe -m pytest tests/python -k v27 -q --no-cov
# 30 passed, 4474 deselected

.venv/Scripts/python.exe -m ruff check \
  python/ultimate_coders/architecture/anys1_router.py \
  python/ultimate_coders/agent/stateful_ptc.py \
  python/ultimate_coders/knowledge/decision_clarifier.py \
  tests/python/test_v27_*.py
# All checks passed

cd packages/uc-orchestrator && \
  bun test src/rl src/skills src/harvest src/harness src/telemetry
# 247 pass, 0 fail

cargo test -p uc-engine --lib
# 910 passed; 0 failed; 0 ignored
```

On Windows the Rust test harness needs `CARGO_PROFILE_DEV_DEBUG=0` and a
workspace-local `TEMP`/`TMP` directory. `cargo fmt -p uc-engine -- --check`
is clean.

## Non-goals

- `anys1_router` does not load a model or call an inference endpoint.
- `compaction_rule_guard` does not summarise or inspect a model summary.
- `stateful_ptc` does not execute real tools or manage a sandbox.
- `token_credit_opsd` does not train a policy or call a sampler.
- `runtime_metrics` token heat does not parse the prompt or call a model.
- `decision_clarifier` does not generate questions with a language model.
