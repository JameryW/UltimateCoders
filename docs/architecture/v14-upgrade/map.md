# V14 Architecture Upgrade Map

This map records the v14 upgrades requested in the architecture ledger
(eighth period, items #43–#48). The implementations are additive and opt-in;
the Python feature flags are read per call and default off. The batch adds
two deterministic modules to the Rust engine, three decision libraries to the
Python agent, acceptance, and search packages, and two trajectory/skill
planners beside the existing orchestrator RL modules. It does not create a
second scheduler, a model service, or a second knowledge authority.

## Status

| Area | Modules | Flag | Wired at | State |
| --- | --- | --- | --- | --- |
| HerAgent setup blocks | `crates/uc-engine/src/her_block_checkpoint.rs`, `crates/uc-engine/src/lib.rs` | n/a (additive Rust library) | `HerBlockController` exported from `uc-engine` | module only |
| CAMP-II file memory distillation | `python/ultimate_coders/agent/camp2_file_memory_opd.py` | `UC_CAMP2_FILE_MEMORY_OPD` | agent library; no worker caller yet | module only |
| Foresight / residual audit | `python/ultimate_coders/acceptance/foresight_residual_auditor.py` | `UC_FORESIGHT_RESIDUAL_AUDITOR` | acceptance library; no worker caller yet | module only |
| CrossSWE contract propagation | `crates/uc-engine/src/cross_repo_coordinator.rs`, `crates/uc-engine/src/lib.rs` | n/a (additive Rust library) | `CrossRepoCoordinator` exported from `uc-engine` | module only |
| RepoNorm compliance | `python/ultimate_coders/search/reponorm_compliance.py` | `UC_REPONORM_COMPLIANCE` | search library; no merge caller yet | module only |
| XRepoSkill evolution | `packages/uc-orchestrator/src/skills/xreposkill_distiller.ts` | n/a (library) | orchestrator skill planning; no export caller yet | module only |
| DiDPO / A2Agent credit | `packages/uc-orchestrator/src/rl/didpo_trainer.ts` | n/a (library) | orchestrator RL data planning; no export caller yet | module only |

## HerAgent setup blocks (#43)

`crates/uc-engine/src/her_block_checkpoint.rs` splits environment setup into
seven semantic block kinds: system packages, language runtime, project
dependencies, extra runtime, native build configuration, test toolchain, and
verification. A `SetupBlock` carries an id, name, purpose, persistent
`Prelude`, non-interactive `BashScript`, and a lightweight verify command.
Blocks are added in order and limited by `DEFAULT_MAX_BLOCKS`; checkpoints are
limited by `DEFAULT_MAX_CHECKPOINTS`.

`HerBlockController.checkpoint` records the verified block and its environment
variables. `relocate_failure` selects only an earlier block as the root and
rolls the tail back to the checkpoint before it. `patch_back` rewrites the
root block, truncates every checkpoint at or after the patched block, and
marks a replay required. `replay_plan` starts in a fresh clean-room container
after the latest surviving checkpoint. A `HerSnapshot` reports blocks,
checkpointed ids, patch-backs, and the replay flag.

## CAMP-II file-memory distillation (#44)

`python/ultimate_coders/agent/camp2_file_memory_opd.py` uses the native file
interfaces `.uc_memory/notes.md` and `.uc_memory/continuation.md` instead of a
dedicated memory tool. `validate_memory_path` rejects every path outside that
allowlist. `FileMemorySession` appends structured writes with step, evidence,
and next-action fields; the continuation file must carry a next action.

`MemoryWriteSample` validates aligned teacher/student log-probs and a boolean
write mask, then exposes per-token deltas. `Camp2OpdDistiller` weights
memory-write tokens twice and returns `accepted` only when the weighted mean
advantage is positive. This is the ledger's online policy distillation signal;
it is a pure scorer and does not update weights.

## Foresight and residual audit (#45)

`python/ultimate_coders/acceptance/foresight_residual_auditor.py` treats a
workspace inherited from a failed attempt as untrusted. `ForesightAuditor`
measures the dirty ratio, labels every pre-modified file
`UNVERIFIED_RESIDUAL_TAINT`, and requires a zero-trust re-audit against the
original issue when the dirty ratio or issue overlap crosses its threshold.
`ResidualAudit.clean` is true only when no dirty file remains untainted.

`ForesightSynthesizer` derives one anticipation test for each call-chain
boundary kind (null, concurrency, type overflow, contract compatibility) and
`ForesightResidualAuditor` combines that synthesis with the residual audit.
The tests are descriptions and targets, not executed test code.

## CrossSWE contract propagation (#46)

`crates/uc-engine/src/cross_repo_coordinator.rs` stores a contract per
repository and a directed dependency per consumer/provider/symbol pair.
`register_repo`, `declare_export`, and `add_dependency` reject empty fields,
unknown repositories, duplicate edges, self-dependencies, cycles, and capacity
overflow. Created edges default to required; `mark_optional` makes an edge
non-blocking.

`propagate` verifies the old signature, bumps the exported symbol version,
and checks every consumer. A required consumer becomes blocked on a signature
mismatch or a missing symbol; optional consumers are reported but do not fail
the report. `topological_order` provides a deterministic provider-first order.

## RepoNorm compliance (#46)

`python/ultimate_coders/search/reponorm_compliance.py` models the ledger's
machine-checkable policy package. `RepoNormExtractor.extract` derives atomic
rules from documentation and Git history using policy cues (must, never,
required, forbidden, prefer) and assigns a category and severity. Each rule
may carry a forbidden or required regular expression; invalid expressions are
rejected and pattern-less rules are marked not machine-checkable.

`NormPackage` rejects duplicate ids and exposes the machine-checkable count.
`RepoNormCompliance.audit` reports a violation for a forbidden-pattern hit or
a missing required pattern, and only blocking violations make the report
non-compliant.

## XRepoSkill evolution (#47)

`packages/uc-orchestrator/src/skills/xreposkill_distiller.ts` keeps harness
configuration isolated by task family (Repair, TestGen, Feature, Refactor).
`TaskFamilyHarnessRouter` resolves and updates one family without leaking into
another. `firstDivergence` compares a success/failure trace pair and returns
the first differing action; `XRepoSkillDistiller` turns that fork into a
deduplicated candidate with a deterministic statement.

Candidates are evaluated only after independent validation in several
repositories. `evaluate` keeps a skill only when it has at least two
repository validations, a positive mean gain, and a significant z-score, so a
single-repository or noisy win is rejected.

## DiDPO sub-diff credit (#48)

`packages/uc-orchestrator/src/rl/didpo_trainer.ts` splits a unified diff into
per-file sub-diffs and computes a groupability score from hunk count.
`buildAnchors` groups the same sub-diff signature across trajectories and
averages its advantage. `applyDiffInDiff` subtracts that anchor mean from each
sample advantage and weights the result by groupability, making the credit
independent of the trajectory-level outcome.

`a2Credit` separates localization discovery from commitment, and
`DiDPOTrainer.encode` combines the A2 credit with the diff-in-diff sub-diff
advantage. The module is a pure planner; it does not run RL or update model
weights.

## Rollout and verification

All Python flags are read per call and default off. In-memory stores are
process-local and bounded. No database migration or new service is required.
The Rust additions are additive module exports.

Verification performed on 2026-11-23:

```bash
.venv/Scripts/python.exe -m pytest tests/python -k v14 -q --no-cov
# 30 passed, 4077 deselected

.venv/Scripts/python.exe -m ruff check \
  python/ultimate_coders/agent/camp2_file_memory_opd.py \
  python/ultimate_coders/acceptance/foresight_residual_auditor.py \
  python/ultimate_coders/search/reponorm_compliance.py \
  tests/python/test_v14_*.py
# All checks passed

cd packages/uc-orchestrator && bun test src/rl src/skills
# 80 pass, 0 fail

cargo check -p uc-engine
# passed with the rsproxy sparse index and CARGO_PROFILE_DEV_DEBUG=0

cargo test -p uc-engine --lib
# 657 passed; 0 failed; 0 ignored

cargo fmt -p uc-engine -- --check
# clean
```

The Rust modules are rustfmt-clean. On Windows the test harness needs
`CARGO_PROFILE_DEV_DEBUG=0` and a workspace-local `TEMP`/`TMP` directory
because the MSVC `cl.exe` debug-record path fails on the very long default
temp path.

## Non-goals

- `her_block_checkpoint` does not run setup commands or start a container; it
  validates block order and plans checkpoint, patch-back, and replay.
- `camp2_file_memory_opd` does not update model weights or call a teacher; it
  scores caller-supplied teacher/student memory-write tokens.
- `foresight_residual_auditor` does not execute tests or parse an issue; it
  audits caller-supplied files and synthesises test descriptions.
- `cross_repo_coordinator` does not clone repositories or edit manifests; it
  validates declared contracts and reports blocked consumers.
- `reponorm_compliance` does not mine a live repository or call a model; it
  extracts rules from caller-supplied text and audits a patch.
- `xreposkill_distiller` does not record live traces or write the skill
  registry; it consumes caller-supplied trace pairs and outcomes.
- `didpo_trainer` does not run RL or persist checkpoints; it computes
  deterministic credit from caller-supplied trajectories.
