# V16 Architecture Upgrade Map

This map records the v16 upgrades requested in the architecture ledger (tenth
period, items #55–#60). The additions are opt-in or additive libraries: three
deterministic modules in the Rust engine, two evaluation/planning modules in the
Python layer, and one TypeScript skill-adaptation library in the orchestrator.
No new service, database migration, or scheduler is introduced.

## Status

| Area | Modules | Flag | Wired at | State |
| --- | --- | --- | --- | --- |
| DSH A.I.G taint guard | `crates/uc-engine/src/dsh_aig_taint_guard.rs`, `crates/uc-engine/src/lib.rs` | n/a (additive Rust library) | `AigTaintGuard` exported from `uc-engine` | module only |
| Decomposition-tax controller | `python/ultimate_coders/agent/decomposition_tax_controller.py` | `UC_DECOMPOSITION_TAX_CONTROLLER` | agent library; no planner caller yet | module only |
| TokenCast budgeter | `crates/uc-engine/src/tokencast_budgeter.rs`, `crates/uc-engine/src/lib.rs` | n/a (additive Rust library) | `TokenCastBudgeter` exported from `uc-engine` | module only |
| Meta-skill adapter | `packages/uc-orchestrator/src/skills/meta_skill_adapter.ts` | n/a (library) | orchestrator skill loading; no live caller yet | module only |
| SchrodingerRepo / ProMax | `python/ultimate_coders/acceptance/schrodinger_promax_eval.py` | `UC_SCHRODINGER_PROMAX` | acceptance evaluator; no benchmark runner caller yet | module only |
| Expert rubric calibrator | `crates/uc-engine/src/expert_rubric_calibrator.rs`, `crates/uc-engine/src/lib.rs` | n/a (additive Rust library) | `ExpertRubricCalibrator` exported from `uc-engine` | module only |

## DSH A.I.G three-channel taint guard (#55)

`dsh_aig_taint_guard.rs` models the three boundary channels from A.I.G:
`ToolResult`, `AdditionalContext`, and `SkillFile`. `AigTaintGuard::sanitize`
strips Tag characters (`U+E0000..=U+E007F`) and zero-width/bidi controls,
records the number of removed characters, and reports two deterministic
injection classes. `FakeCompletion` blocks text that claims the task is complete
or tells the executor not to run tests; `PrivilegeInstruction` blocks attempts
to override the system/developer prompt, exfiltrate a token, or run a
destructive command.

The optional `LlmJudge` trait adds a semantic risk score; a score of `0.8` or
higher blocks the payload even when the deterministic rules are clean. The
guard never calls a model itself. `AigTaintGuard::envelope` wraps a sanitized
tool result in a fresh 32-character nonce envelope so the model can distinguish
harness-owned output from text that merely imitates the envelope.

## Decomposition-tax controller (#56)

`decomposition_tax_controller.py` implements the break-even test
`alpha < 1 / price_ratio`, where `alpha = sum(output_tokens) / O_1`. Equality
routes to a single call, matching the ledger's finding that the interface cost
consumes the benefit at the boundary.

`PipelinePlan` validates unique stage names and positive output sizes.
`DecompositionTaxController.reground` returns a `Regrounding` block only for a
lossy stage that is not the first stage; the block re-anchors the original issue
and declares that every entity, number, identifier, and relation must be
preserved. `reground_many` requires one downstream input per lossy stage. The
controller does not rewrite the issue or invoke a model.

## TokenCast budgeter (#57)

`tokencast_budgeter.rs` gives every execution segment the triple
`(direct_tokens, context_growth, future_reread_multiplier)`. The contribution
is additive:
`direct + context_growth * future_reread_multiplier`, so observed segments and
the predicted remainder can be combined without recomputing the prefix.

`TokenCastBudgeter::decide` returns `WithinBudget`, `Relax`, `Compact`, or
`Prune` from the forecast, budget, and caller-supplied progress. A task at
`>= 0.8` progress may spend past the budget; an early high-growth task
compacts; a mid-progress overrun prunes. The module makes no LLM call.

## Meta-skill adapter (#58)

`meta_skill_adapter.ts` maps an executor tier to the explicit contract it needs.
Flagship executors keep the original body. Mid-tier executors receive tool
execution, context management, and instruction-following contracts. Small
executors additionally receive verification guidance with exact test and
evidence requirements.

The adapter is idempotent: an existing `## Executor-specific execution
contract` block is replaced, never duplicated. `adaptMany` preserves input
order. It deliberately does not rewrite the skill's domain knowledge; it only
makes the execution obligations explicit.

## SchrodingerRepo / ProMax (#59)

`schrodinger_promax_eval.py` implements the four latent-variable levels:
issue rewriting (paths and symbols redacted), namespace remapping (salted,
deterministic symbol names), layout shuffling (salted file order), and
equivalent rewriting (removing redundant grouping parentheses around simple
conditions). `SchrodingerTransformer.transform` records which levels ran and
the complete namespace map.

`AtomicRefactorTransaction` validates that every target file exists and contains
the old symbol, rejects duplicate old symbols and rename cycles, and applies
renames in dependency order so `A -> B, B -> C` does not turn `A` into `C`.
`apply` updates file content and symbol lists atomically; `rollback` restores the
original snapshot. The transaction is in-memory and does not touch the working
tree.

## Expert rubric calibrator (#60)

`expert_rubric_calibrator.rs` supports weighted partial credit over
caller-supplied criteria. A missing required criterion or a patch that bypasses
or tampers with tests is forced to `0.0`, even if every optional criterion is
satisfied.

`ExpertRubricCalibrator::calibrate` runs the five control classes:
reference `1.0`, unmodified `0.0`, inert `0.0`, alternative `1.0`, and
adversarial `0.0`. A control case outside its expected score fails the report;
the calibrator never executes a patch.

## Verification

Verification performed on 2026-11-23:

```bash
.venv/Scripts/python.exe -m pytest tests/python -k v16 -q --no-cov
# 30 passed, 4120 deselected

.venv/Scripts/python.exe -m ruff check \
  python/ultimate_coders/agent/decomposition_tax_controller.py \
  python/ultimate_coders/acceptance/schrodinger_promax_eval.py \
  tests/python/test_v16_*.py
# All checks passed

cd packages/uc-orchestrator && bun test src/skills src/rl src/harvest src/harness
# 128 pass, 0 fail

cargo check -p uc-engine
# passed with the rsproxy sparse index and CARGO_PROFILE_DEV_DEBUG=0

cargo test -p uc-engine --lib
# 713 passed; 0 failed; 0 ignored

cargo fmt -p uc-engine -- --check
# clean
```

On Windows the Rust test harness needs `CARGO_PROFILE_DEV_DEBUG=0` and a
workspace-local `TEMP`/`TMP` directory because the MSVC `cl.exe` debug-record
path fails on the very long default temp path.

## Non-goals

- `dsh_aig_taint_guard` does not call an LLM judge, execute a tool, or persist a
  quarantine record; callers supply the semantic judge and own the policy.
- `decomposition_tax_controller` does not estimate stage sizes or call a model;
  the caller supplies the output-token plan.
- `tokencast_budgeter` does not tokenize prompts or enforce a hard truncation;
  it is a forecast and an action recommendation.
- `meta_skill_adapter` does not load a skill registry or select a model tier; it
  rewrites one caller-supplied skill for one caller-supplied tier.
- `schrodinger_promax_eval` does not compile or execute the transformed
  repository and does not write a Git branch.
- `expert_rubric_calibrator` does not run tests or judge prose; it consumes
  caller-supplied criterion satisfaction and control scores.
