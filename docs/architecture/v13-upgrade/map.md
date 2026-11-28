# V13 Architecture Upgrade Map

This map records the v13 upgrades requested in the architecture ledger
(seventh period, items #37–#42). The implementations are additive and opt-in;
the Python feature flags are read per call and default off. The batch adds two
deterministic guard modules to the Rust engine and four decision libraries to
the Python agent, search, and acceptance packages, plus one TypeScript
trajectory curator beside the existing RL data-planning modules. It does not
create a second scheduler, a model service, or a second knowledge authority.

## Status

| Area | Modules | Flag | Wired at | State |
| --- | --- | --- | --- | --- |
| AsynCodeBench dependency checker | `crates/uc-engine/src/async_dep_checker.rs`, `crates/uc-engine/src/lib.rs` | n/a (additive Rust library) | `AsyncDependencyChecker` exported from `uc-engine` | module only |
| Risa sparse-MoE routing | `python/ultimate_coders/agent/risa_moe_controller.py` | `UC_RISA_MOE` | agent library; no worker caller yet | module only |
| RepoAtlas repository views | `python/ultimate_coders/search/repo_atlas_view.py` | `UC_REPO_ATLAS_VIEW` | search library; no planner caller yet | module only |
| L3 independent review | `python/ultimate_coders/acceptance/independent_reviewer.py`, `python/ultimate_coders/agent/completion_review.py` | `UC_INDEPENDENT_REVIEWER`, `UC_COMPLETION_REVIEW` | local harness `done()` gate over the run diff + test exits | wired (opt-in) |
| ExLabel trajectory curation | `packages/uc-orchestrator/src/rl/exlabel_curator.ts` | n/a (library) | orchestrator RL data planning; no export caller yet | module only |
| Clean regrade + ProjectGuard | `crates/uc-engine/src/clean_regrader.rs`, `python/ultimate_coders/agent/project_guard.py` | `UC_PROJECT_GUARD` (Python) | engine regrade policy; agent progressive-spec library | module only |

## AsynCodeBench dependency checker (#37)

`crates/uc-engine/src/async_dep_checker.rs` makes cross-node interface
dependencies explicit and executable. Each `DependencyEdge` names an upstream
node, a downstream node, a symbol, and an expected signature; `register`
rejects empty fields, duplicate ids, and capacity overflow. `observe` compares a
`DependencyProbe` symbol and whitespace-normalised signature with the expected
values and marks the edge `Resolved` or `Broken`.

`AsyncDependencyChecker` reports the ledger's two metrics without a model call:
`adpr` is the resolved-edge ratio, and `drs_tilde` is the sum of resolution
steps divided by `last_step + 1`. `integration_checkpoint` returns an
`IntegrationReport` that includes the test pass rate and marks the report
`clean` only when every edge resolved and the observed tests did not pass while
dependency resolution lagged. `hopping_window` resets every core-interface edge
owned by a committed upstream node and returns the downstream nodes that must
rebase and re-probe.

## Risa sparse-MoE routing (#38)

`python/ultimate_coders/agent/risa_moe_controller.py` turns native MoE router
activations into a test-time scaling policy. `RisaMoEController.select` has two
objectives. During exploration and localization it maximises the distance
between a candidate route and the recent history centroid, so the agent does not
repeat the same search. During editing, verification, and commit it maximises
the mean cosine similarity to same-role peers, so the group converges before
writing. The recommendation size defaults to 16 candidate actions and four
concurrent attempts; history is bounded and resettable.

`arbitrate` compares complete attempts. Its score combines the group's
pairwise route consistency with a bonus for low decision-token confidence, so a
locally uncertain attempt that agrees with the group can win. Ties are resolved
deterministically by lower attempt id and then lower confidence. The module
does not read model internals itself; a caller supplies the route vectors that
the policy consumes.

## RepoAtlas repository views (#39)

`python/ultimate_coders/search/repo_atlas_view.py` implements the
Select-Project-Refresh loop. `RepoGraph` validates node ids, paths, and edges;
`neighbors` performs bounded hop expansion. Before the first substantive edit,
`RepoAtlasController.select` returns two projections: a topology view over the
seed's dependency neighbourhood and a source index whose reference keys stay
stable across refreshes. After the first substantive edit it unloads the
topology view and returns only a local plain-text neighbourhood around the
active patch paths.

Every refresh replaces the previous projection instead of accumulating it, and
the controller records how many refreshes have happened. When a projection
exceeds the token budget it is trimmed to the budget and marked `degraded`
rather than silently dropped.

## L3 independent review (#40)

`python/ultimate_coders/acceptance/independent_reviewer.py` isolates the
reviewer from the producing trajectory. `IndependentReviewer.review` accepts
only a patch, a `TestReport`, and an optional `BoundaryView`; it never receives
the agent's reasoning trace. A clean patch with passing, non-zero test evidence
is approved. Missing, failed, or zero-execution tests, test-file edits, deleted
assertions, skip/xfail/`--no-verify` markers, and protected-path writes are
rejected. A required path that the patch does not touch yields
`NEEDS_CHANGES` instead of a hard rejection.

`build_brief` emits a bounded, trajectory-free brief for an external critic.
`changed_paths` parses `+++ b/...` headers and ignores `/dev/null`.

**Production wiring (local harness).** `python/ultimate_coders/agent/completion_review.py`
(`UC_COMPLETION_REVIEW`, default off) is the reviewer's first caller.
`CompletionReviewGate` builds the run's own evidence — `workspace_diff` pins
`git diff` to the base commit captured at run start and synthesizes new-file
hunks for the paths the harness wrote (untracked files never appear in
`git diff`, and `git add -N` would mutate the caller's index), while
`TestRun`/`build_test_report` turn the exit codes of test-looking commands into
a `TestReport`. `harness_local_loop.run_loop` runs the gate at `done()` and
blocks the completion once per run for hard findings (`test_file_modified`,
`assertion_deleted`, `verification_bypass`, `tests_failed`); missing test
evidence is only advisory, because the harness cannot know whether a task has
tests, and documentation-only diffs skip the test requirement entirely. Blocked
`done()` calls emit `completion_review_block` and return a `to_tool_error()`
payload that names the offending paths; the run envelope carries
`completion_review` (`blocks` plus the last verdict).

## ExLabel trajectory curation (#41)

`packages/uc-orchestrator/src/rl/exlabel_curator.ts` is the self-evolving
experience bank for task clarity and test coverage. `learn` increases a
label's penalty weight by `1 - clarity`, so labels observed in ambiguous tasks
become stronger signals. `evaluateClarity` subtracts the capped learned-label
penalty and a fixed penalty when the task does not mention a source file.
`evaluateCoverage` weights F2P coverage at 0.7 and P2P coverage at 0.3; a task
with no P2P tests is treated as fully P2P-covered rather than penalised.

`curate` keeps a trajectory only when both thresholds pass, F2P tests exist,
and neither coverage axis nor the weighted score is below threshold. The
curator is deterministic, does not call a model, and preserves batch order.

## Clean regrade and ProjectGuard (#42)

`crates/uc-engine/src/clean_regrader.rs` is the clean-container regrade gate
for SWE-Bench Pro V2. `SandboxEgressPolicy` normalises host names and can be
restricted to an inference-only allowlist. `CleanRegrader.regrade` accepts a
plain-text patch and rejects empty tasks or patches, binary/oversized patches,
Git metadata references (`.git/`, refs, packed-refs, log/show/reflog), any
non-allowlisted egress, a dirty container, or a mounted source workspace. An
accepted report carries the grader id, task id, BLAKE3 patch digest, and no
violations.

`python/ultimate_coders/agent/project_guard.py` is the external state layer for
progressive requirements. `disclose` accepts monotonic turns and converts each
requirement, constraint, or acceptance criterion into an `AtomicComponent`
with derived keywords and dependency ids. Re-disclosing the same id supersedes
the previous version. `conflict_graph` reports dependencies whose target is not
active. `scan` matches every required component's keywords against the current
patch and tests (with a stable stem-prefix match for inflections), reports
missing components and unresolved dependencies, and computes fidelity. Scans
are bounded and the `to_dict` snapshot exposes only the retained state.

## Rollout and verification

All Python flags are read per call and default off. In-memory stores are
process-local and bounded. No database migration or new service is required.
The Rust additions are additive module exports.

Verification performed on 2026-11-23:

```bash
.venv/Scripts/python.exe -m pytest tests/python -k v13 -q --no-cov
# 46 passed, 4031 deselected

.venv/Scripts/python.exe -m ruff check \
  python/ultimate_coders/agent/risa_moe_controller.py \
  python/ultimate_coders/search/repo_atlas_view.py \
  python/ultimate_coders/acceptance/independent_reviewer.py \
  python/ultimate_coders/agent/project_guard.py \
  tests/python/test_v13_*.py
# All checks passed

cd packages/uc-orchestrator && bun test src/rl
# 58 pass, 0 fail

cargo check -p uc-engine
# passed with the rsproxy sparse index and CARGO_PROFILE_DEV_DEBUG=0

cargo test -p uc-engine --lib
# 639 passed; 0 failed; 0 ignored
```

The Rust modules are rustfmt-clean. On Windows the test harness needs
`CARGO_PROFILE_DEV_DEBUG=0` and a workspace-local `TEMP`/`TMP` directory
because the MSVC `cl.exe` debug-record path fails on the very long default
temp path.

## Non-goals

- `async_dep_checker` does not schedule nodes or mutate a DAG; it validates
  edges and reports readiness at an integration checkpoint.
- `risa_moe_controller` does not run a model, read router logits, or launch
  attempts; it is the selection and arbitration policy over caller-supplied
  routes.
- `repo_atlas_view` does not render images or query a live repository service;
  it projects caller-built graph nodes into bounded text views.
- `independent_reviewer` does not execute tests or replace the Oracle; it
  consumes recorded test evidence and a patch.
- `exlabel_curator` does not mine adjudications or export training data; it
  scores and filters caller-supplied records.
- `clean_regrader` does not start a container; it is the deterministic
  admission policy applied before a fresh-container regrade.
- `project_guard` does not parse natural-language requirements or call a model;
  callers supply atomic components and patch/test evidence.
