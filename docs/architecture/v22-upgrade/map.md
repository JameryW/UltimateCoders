# V22 Architecture Upgrade Map

This map records the v22 upgrades requested in the architecture ledger
(sixteenth period, items #91–#96). The additions are opt-in libraries:
one Rust module in `uc-engine`, four Python components, one TypeScript
module, and no new service, database migration, scheduler, or live route.
Every feature flag defaults off.

## Status

| Area | Modules | Flag | Wired at | State |
| --- | --- | --- | --- | --- |
| Parallel conflict mesh | `python/ultimate_coders/agent/parallact_mesh.py` | `UC_PARALLACT_MESH` | agent coordination; no worker caller yet | module only |
| Cooperative swarm RL | `packages/uc-orchestrator/src/rl/s3_swarm_rl.ts` | n/a (library) | orchestrator RL helpers; no trainer hook yet | module only |
| Append-only personal memory | `python/ultimate_coders/search/reglos_memory.py` | `UC_REGLOS_MEMORY` | memory and harness evolution; no caller yet | module only |
| Security-guard context | `python/ultimate_coders/search/case_safeguard_ctx.py` | `UC_CASE_SAFEGUARD_CTX` | retrieval context; no caller yet | module only |
| Monitor firewall | `crates/uc-engine/src/monitor_firewall.rs` | n/a (library) | supervision boundary; no engine caller yet | module only |
| Loop planner and repository rubric | `python/ultimate_coders/acceptance/repo_rubric_eval.py` | `UC_REPO_RUBRIC_EVAL` | acceptance; no caller yet | module only |

## Parallel conflict mesh (#91)

`parallact_mesh.py` separates a write scope into symbols and paths. Two
agents conflict when either set intersects. `ParallactMesh::plan` refuses
parallelism when scopes overlap, serialises when there is no evidence
partition, external verification, or heterogeneous error diversity, and
otherwise creates a parallel plan with role failover to a healthy peer.
A missing healthy peer for a role produces a blocked plan. The module
models the decision only; it does not start or stop agents.

## Cooperative swarm RL (#92)

`s3_swarm_rl.ts` gives each agent three actions: work alone, broadcast a
finding, or read its inbox. `chooseAction` prioritises inbox reads, then
broadcasts, then solo work. `S3Swarm::step` deposits a new finding in every
peer inbox, and `dedupeMetrics` scores unique coverage with a duplicate
penalty. `S3Swarm::runRound` replays caller-supplied discoveries in a
deterministic agent order. The module keeps all state in memory and does
not call a model.

## Append-only personal memory (#93)

`reglos_memory.py` appends preference and fact records; a later record for
the same key is the active one, while `forget` appends a tombstone and
leaves history intact. `CrossFamilyCritic` refuses a reviewer from the
author's model family and refuses proposals carrying gaming flags.
`FrozenEvaluator` compares a proposal's measured score against a fixed
baseline, and `ReglosLoop` writes a fact only when both gates accept. The
module does not call a model, benchmark, or durable database.

## Security-guard context (#94)

`case_safeguard_ctx.py` builds a reverse call graph over repository
symbols. `GuardIndex::upstream_guards` walks callers to a bounded depth,
deduplicates cycles, and orders auth, input-sanitisation, then bounds-check
guards at the top of the context. `CaseSafeguardContext::build` renders a
`## Safety guards (must remain)` block and `coverage` reports whether any
guard was observed. This is a context-selection helper; it does not inspect
or edit source files.

## Monitor firewall (#95)

`monitor_firewall.rs` admits only structured evidence: a diff-AST digest,
a test exit result, and an optional SAST summary. `MonitorFirewall::review`
denies free text, missing required evidence, and over-capacity batches,
then returns a stable evidence digest. `admit_message` refuses every
non-empty free-text message. The default required evidence is DiffAst plus
TestExit; callers can replace it. The module does not run a monitor model.

## Loop planner and repository rubric (#96)

`repo_rubric_eval.py` pairs a deterministic Dory-style loop plan with a
repository-grounded rubric. `DoryLoopPlanner::plan` assigns one, two, four,
or six loops by difficulty, keeps a 256K context and a one-loop KV budget,
and reports the output-token factor. `RepositoryGroundedRubric::evaluate`
scores weighted rubric items against repository evidence and reports
missing evidence. `RepoRubricEvaluator` returns a patch digest plus the
plan and rubric result. The module does not run a model or execute tests.

## Verification

Verification performed on 2026-11-24:

```bash
.venv/Scripts/python.exe -m pytest tests/python -k v22 -q --no-cov
# 40 passed, 4322 deselected

.venv/Scripts/python.exe -m ruff check \
  python/ultimate_coders/agent/parallact_mesh.py \
  python/ultimate_coders/search/reglos_memory.py \
  python/ultimate_coders/search/case_safeguard_ctx.py \
  python/ultimate_coders/acceptance/repo_rubric_eval.py \
  tests/python/test_v22_*.py
# All checks passed

cd packages/uc-orchestrator && bun test src/rl src/skills src/harvest src/harness src/telemetry
# 192 pass, 0 fail

cargo test -p uc-engine --lib
# 803 passed; 0 failed; 0 ignored
```

On Windows the Rust test harness needs `CARGO_PROFILE_DEV_DEBUG=0` and a
workspace-local `TEMP`/`TMP` directory. `cargo fmt -p uc-engine -- --check`
is clean.

## Non-goals

- `parallact_mesh` does not launch, pause, or migrate agents.
- `s3_swarm_rl` is an in-memory policy and reward model, not a trainer.
- `reglos_memory` does not call an LLM or persist outside process memory.
- `case_safeguard_ctx` does not parse source files; callers supply guards
  and call edges.
- `monitor_firewall` does not run a monitor model; it validates evidence.
- `repo_rubric_eval` does not execute tests or load Dory weights.
