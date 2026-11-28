# V17 Architecture Upgrade Map

This map records the v17 upgrades requested in the architecture ledger
(eleventh period, items #61–#66). The additions are opt-in or additive
libraries: three deterministic modules in the orchestrator, two in the Python
acceptance/search layer, and one planner-side gate. No new service, database
migration, or scheduler is introduced.

## Status

| Area | Modules | Flag | Wired at | State |
| --- | --- | --- | --- | --- |
| RiseHarness flipping-task diagnosis | `packages/uc-orchestrator/src/harness/rise_harness_evolver.ts` | n/a (library) | orchestrator harness evolution; no live caller yet | module only |
| Governed self-evolution gate | `packages/uc-orchestrator/src/harness/governed_evolver_gate.ts` | n/a (library) | proposal promotion; no live caller yet | module only |
| SkillForge cold start | `python/ultimate_coders/search/skillforge_coldstart.py` | `UC_SKILLFORGE_COLDSTART` | search layer; no worker caller yet | module only |
| T1 token-exact / SAT RL batch | `packages/uc-orchestrator/src/rl/t1_sat_async_rl.ts` | n/a (library) | offline RL pipeline; no live caller yet | module only |
| PAIChecker / SWE-DIVE | `python/ultimate_coders/acceptance/paichecker_swedive.py` | `UC_PAICHECKER_SWEDIVE` | benchmark curation and session simulation; no runner caller yet | module only |
| Scientific grounding gate | `python/ultimate_coders/agent/scientific_grounding_gate.py` | `UC_SCIENTIFIC_GROUNDING_GATE` | planner-side external-knowledge injection; no planner caller yet | module only |

## RiseHarness flipping-task evolver (#61)

`rise_harness_evolver.ts` separates a visible agent trace from the internal
harness trace. `findFlippingTasks` keeps only tasks where the same model and the
same `configHash` produced at least one success and one failure. `alignTraces`
joins the two traces by step and marks a step as suspicious when the harness
event is missing or is one of `timeout`, `truncated`, `empty_observation`,
`compaction_missing_state`, `false_completion`, or `gate_closed`.

`HarnessHandbook` maps the four responsibilities — command execution, feedback
truncation, context compression, completion gate — to a source file and
function. `RiseHarnessEvolver.diagnose` emits one proposal per responsible area
that has both handbook evidence and a suspicious step. `port` records an
external-framework mechanism for the same responsibility. The evolver never
edits a file or calls a model.

## Governed self-evolution gate (#62)

`governed_evolver_gate.ts` makes harness updating cheap and harness promotion
strict. Every `HarnessProposal` names its immutable `parentHash` and a
`newSnapshotHash`; `GovernedEvolverGate.promote` rejects a proposal whose parent
is not the current snapshot. The evaluation must cover a non-empty historical
regression suite at 100%, at least one new task at the configured pass rate, and
no poison trigger term in the patch, new tasks, or declared trigger terms.

`evaluate` counts regression and new-task evidence and reports the matched
poison terms; `promote` is the only method that changes the snapshot. An
accepted proposal advances the snapshot, so the next proposal must branch from
the accepted child rather than from the original parent.

## SkillForge cold start (#63)

`skillforge_coldstart.py` turns tested functions into deterministic synthetic
issues. `generate` emits one issue per tested function, capped by
`max_issues`, and cycles the three mutation kinds (`off_by_one`,
`none_return`, `wrong_operator`). `distill` accepts only a successful
`RepairTrace` and produces two cards: a global diagnostic card and a local
intervention card bound to the repaired AST symbol.

`register` deduplicates by name and scope and respects `max_cards`. `inject`
returns the local cards for the requested symbol first, then global cards, and
never exceeds `limit`. The module does not execute tests, mutate a repository,
or write to TiKV; the caller owns sandbox execution and persistence.

## T1 token-exact batches and SAT trust regions (#64)

`t1_sat_async_rl.ts` keeps the sampler's original token ids. `buildTitoBatch`
repairs a decreasing turn boundary in place, counts the repairs, and preserves a
caller-supplied loss mask. `replayMoERoutes` keeps the first route observed in
each turn so a later token does not silently switch experts.

`StalenessAdaptiveTrustRegion` uses `|log ratio|` as the staleness proxy. The
threshold is the configured quantile of the batch's staleness values; a token at
or above the threshold gets the outward clipping bound multiplied by
`shrinkFactor`. Fresh tokens keep the base bound. `t1DenseReward` adds a bounded
sub-verifier bonus over the pass fraction. The module is a batch-construction
and objective helper; it does not run a trainer.

## PAIChecker and SWE-DIVE (#65)

`paichecker_swedive.py` implements the three-stage cleaner. `detect` finds
five patterns: unrelated module, unrelated tests, patch-scope mismatch,
under-specified issue, and test-only change. `synthesize` can use cross-agent
votes as a tie-break, and `code_verify` rejects only when the code-level
evidence supports the finding. `clean` returns a bounded report with accepted and
rejected counts.

`DynamicUserSimulator` produces a three-turn session: the initial requirement,
a `mind_changer` turn that preserves the old API while adding a filter, and an
`expert_nitpicker` turn that removes the dead compatibility branch and demands a
regression test. `run` validates each response against required and forbidden
terms. The simulator is deterministic; it does not call a model.

## Scientific grounding gate (#66)

`scientific_grounding_gate.py` requires every external domain claim to bind to
the repository's executable symbols. A `DomainClaim` must contain a formula, a
source, at least one `SymbolBinding`, and at least one boundary condition. Each
binding can constrain the repository symbol's shape, dtype, and signature.

`ScientificGroundingGate.validate` reports every missing symbol and every
shape, dtype, or signature mismatch. `gate` returns a rendered prompt block only
when the report is aligned; otherwise it returns `None`. This is the deterministic
anchoring guard: a generic theorem that cannot be bound is not injected into the
planner context.

## Verification

Verification performed on 2026-11-23:

```bash
.venv/Scripts/python.exe -m pytest tests/python -k v17 -q --no-cov
# 29 passed, 4150 deselected

.venv/Scripts/python.exe -m ruff check \
  python/ultimate_coders/search/skillforge_coldstart.py \
  python/ultimate_coders/acceptance/paichecker_swedive.py \
  python/ultimate_coders/agent/scientific_grounding_gate.py \
  tests/python/test_v17_*.py
# All checks passed

cd packages/uc-orchestrator && bun test src/rl src/skills src/harvest src/harness
# 157 pass, 0 fail

cargo test -p uc-engine --lib
# 713 passed; 0 failed; 0 ignored (no Rust change in v17)
```

On Windows the Rust test harness needs `CARGO_PROFILE_DEV_DEBUG=0` and a
workspace-local `TEMP`/`TMP` directory because the MSVC `cl.exe` debug-record
path fails on the very long default temp path. Rust sources were unchanged in
v17, so the v16 cargo result was re-used after a clean `cargo check`.

## Non-goals

- `rise_harness_evolver` does not edit a harness source file, run a task, or
  call a model; it only diagnoses aligned traces and records proposals.
- `governed_evolver_gate` does not execute an evaluator or compute a diff; it
  consumes caller-supplied regression/new-task outcomes.
- `skillforge_coldstart` does not execute tests, mutate the repository, or write
  a memory store; callers supply synthetic-issue repair evidence.
- `t1_sat_async_rl` does not run a trainer, GPU kernel, or sampler; it builds
  token-exact batches and clips caller-supplied ratios.
- `paichecker_swedive` does not call GitHub, parse a real AST, or run the
  agent; callers supply task records and session responses.
- `scientific_grounding_gate` does not import a scientific package, execute a
  formula, or rewrite the planner prompt; it validates and renders one caller
  supplied claim.
