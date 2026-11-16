# V10 Architecture Upgrade Map

This map records the v10 upgrades requested in the architecture ledger
(second through fourth periods). The implementations are opt-in and default
off. They extend the existing planner, worker, search and acceptance
boundaries; they do not create a second scheduler, knowledge authority, or
mandate store.

## Status

| Area | Modules | Flag | Wired at | State |
| --- | --- | --- | --- | --- |
| Boundary contract | `crates/uc-types/src/contract.rs`, `crates/uc-engine/src/lib.rs` | n/a (library type) | planner/engine contract boundary | tested primitive |
| PerceptionPass | `python/ultimate_coders/agent/planner.py` | `UC_PERCEPTION_PASS` | worker `_apply_perception_pass` | wired |
| Plan critic | `python/ultimate_coders/agent/planner_critic.py` | `UC_PLANNER_CRITIC` | worker PerceptionPass preflight | wired (partial) |
| Prefix context assembly | `python/ultimate_coders/search/context_assembler.py` | `UC_PREFIX_CONTEXT_ASSEMBLY` | prompt assembly boundary | tested primitive |
| Anti-pattern gate | `python/ultimate_coders/acceptance/anti_pattern_gate.py` | `UC_ANTI_PATTERN_GATE` | worker `_verify_output_anti_patterns` | wired |
| Mutation augmentation | `python/ultimate_coders/acceptance/mutation_augment.py` | `UC_MUTATION_AUGMENT` | acceptance dataset builder | tested primitive |
| SWE-Prime | `packages/uc-orchestrator/src/rl/swe_prime.ts` | n/a (library) | trajectory distillation caller | tested primitive |
| Failure heatmap | `dashboard/src/lib/failureHeatmap.ts`, `dashboard/src/components/FailureHeatmap.tsx` | n/a (presentation) | dashboard failure-events surface | component ready; not mounted |
| AgentRadio | `python/ultimate_coders/agent/radio.py` | `UC_AGENT_RADIO` | worker tool/step boundary | tested primitive |
| ExecCritic | `python/ultimate_coders/acceptance/exec_critic.py` | `UC_EXEC_CRITIC` | acceptance test generation/repair | tested primitive |
| RETRACE | `python/ultimate_coders/acceptance/retrace_verifier.py` | `UC_RETRACE_VERIFIER` | acceptance review | tested primitive |
| Drift monitor | `python/ultimate_coders/agent/drift_monitor.py` | `UC_DRIFT_MONITOR` | local-harness loop (`drift_advisor`) | wired (local harness) |
| Cumulative regression | `crates/uc-engine/src/dag.rs` | n/a (library type) | DAG completion gate | tested primitive |
| Dockerless verifier | `python/ultimate_coders/acceptance/dockerless_verifier.py` | `UC_DOCKERLESS_VERIFIER` | Oracle fallback / TITO filter | wired (diagnostic) |
| ECLoop gate | `python/ultimate_coders/agent/ecloop_gate.py` | `UC_ECLOOP_GATE` | local-harness edit/submit tool boundary | wired (local harness) |
| TRACE credit | `packages/uc-orchestrator/src/rl/trace_credit.ts` | n/a (library) | RL trajectory scoring | tested primitive |
| GDPO + skills | `python/ultimate_coders/acceptance/gdpo_scorer.py`, `python/ultimate_coders/knowledge/skill_curriculum.py` | `UC_GDPO_SCORER`, `UC_SKILL_CURRICULUM` | acceptance scoring / skill cards | tested primitive |
| SWE-Doctor | `python/ultimate_coders/acceptance/swe_doctor_diag.py` | `UC_SWE_DOCTOR` | worker failed-run diagnosis | wired (partial) |
| RL hardening | `crates/uc-engine/src/rl_hardening.rs` | n/a (library type) | test-evaluation process boundary | tested primitive |

## Boundary contract and planning

`BoundaryContract` assigns each path one of `editable`, `inspect_only`,
`required`, and `protected`. A `BoundaryChallengeRequest` carries the target
file, AST symbol, evidence line, and reason. The engine exposes the challenge
state (`BoundaryChallengeState`) and outcome; approval can update the contract
in place. The type is deliberately independent of the existing task-identity
`agent/contract.py`.

`PerceptionPass` is a zero-edit proposal-to-spec compiler. It uses the
Python AST and, where available, Tree-sitter-style call/import metadata to
walk a two-hop dependency closure. `ImplementationSpec` records target file
boundaries, public API signatures, dependency closure, and exactly eight
static mapping checks. `PerceptionPass.compile(task_id, request, *,
repo_root=".", file_constraints=())` is read-only. When `UC_PERCEPTION_PASS`
is on, the worker appends the rendered spec before the task prompt and falls
back to the original context on any compile error.

When `UC_PLANNER_CRITIC` is also on, the worker runs `PlanCritic` over the
spec's target boundaries before rendering: each target is an observation with
evidence only when the path exists in the working directory. A rejected
critique appends the bounded corrections prompt. This is a partial
preflight, not a full exploration review: the caller does not yet collect
independent read/grep observations or a two-hop evidence graph, so the
precision/recall values describe static target coverage only.

## Context assembly and failure map

`ContextAssembler` builds the prompt from five fixed slots in invalidation
order: immutable system/tool schema, declarative pages and BoundaryContract,
procedural attempt diffs and verified skills, compacted ACP checkpoints, and
the working observation tail plus MemCredit snippets. Each `ContextBlock`
carries a deterministic SHA-256; only a changed suffix is recomputed.

`dashboard/src/lib/failureHeatmap.ts` defines the 7 × 24 failure catalogue (18 deterministic
modes) and `classifyPtaQuality` / `buildFailureHeatmap`. `dashboard/src/components/FailureHeatmap.tsx`
is a pure presentational table; a future events adapter must supply
`FailureEvent[]`, and unknown modes are ignored rather than invented.

## Acceptance and diagnosis

`anti_pattern_gate` is deterministic and LLM-free. It reports
`no_test_after_final_edit`, `test_file_tampering`,
`authored_test_never_run`, and `internal_symbol_leak`; the worker turns
report errors into a failed subtask result when enforcement is on. The
`authored_test_never_run` signal is suppressed when the adapter cannot report
exact commands.

`mutation_augment` generates section-revert, requirement-mismatch, and
semantic-mutation candidates and computes killed-mutant percentage.

`exec_critic` separates `TestGenNode` from `SourceRepairNode`: generated
tests are Gold-free, must clean-fail on the base tree, and are frozen before
source repair; repair feedback is capped at 6,000 characters and a protected
test change raises `ProtectedTestError`.

`retrace_verifier` builds a forward rationale from the issue and trajectory,
a blind backward reconstruction from the patch/AST without the issue, and
arbitrates agreement, contradiction, or mismatch into a bounded repair
instruction.

`dockerless_verifier` uses hybrid retrieval and AST call-chain checks as a
no-container Oracle fallback; it returns bounded pass/fail signals rather
than executing untrusted code. The worker calls it for changed files when
`UC_DOCKERLESS_VERIFIER` is on, synthesizes a unified diff from the `HEAD` and
worktree snapshots, and emits a `subtask_progress` event with the score,
pass flag, and bounded missing-evidence list. That path passes an empty
`repo_index` and no call chain, so it is a patch-local diagnostic today; it
does not replace an Oracle and never changes the subtask outcome.

## Coordination, drift, and cumulative regression

`radio` defines NATS-compatible worklog and mention subjects, an in-memory
bus for local tests, and a bounded `BackgroundMentionWatcher` that drains at
tool-step boundaries. `DriftMonitor` records edit failures, boundary
escapes, repeated test traces, and zero required-file coverage; it emits one
short advisor prompt per episode, never one per signal. The local harness
wires it when `UC_DRIFT_MONITOR` is on: `UC_DRIFT_BOUNDARY` is a
comma/semicolon path-prefix list, `UC_DRIFT_REQUIRED_FILES` is the required
coverage list, an advisor is injected once as a system message before the
next completion, and a `drift_advisor` event (plus final `drift_signals`)
exposes the deterministic signal. The worker adapter does not yet set these
two harness-side env hints from `BoundaryContract`; that remains the engine
integration boundary.

`crates/uc-engine/src/dag.rs` models cumulative regression obligations: a
node inherits every topological ancestor's required test ids, and an
ancestor regression is a blocking completion condition.

## Execution, credit, and trajectory scoring

`ecloop_gate` computes a bounded evidence gap over one-hop callers,
overrides, and tests; it blocks edit/submit at most three times per target
and returns a structured missing-evidence prompt. The local harness wires it
when `UC_ECLOOP_GATE` is on: a successful `read_file` adds its normalized
path to the evidence set, and a `write_file` or `done` whose target has no
recorded read evidence is rejected with an `ecloop_blocked` event and the
structured gap in the tool result. A warning from an allowed decision emits
`ecloop_warning`. The final envelope carries `ecloop_blocks`. This is a
partial evidence boundary: the harness currently treats any successfully
read file as evidence and does not build the one-hop caller/override/test
graph; the AST-backed evidence source remains a future integration. The
worker does not yet feed a target contract into the harness.

`packages/uc-orchestrator/src/rl/trace_credit.ts` implements the log-ratio turn credit with a three-turn
truncated horizon, gamma 0.8, lambda 2.0, and slow/fast learning rates
0.2/1.0. `packages/uc-orchestrator/src/rl/swe_prime.ts` labels exploration, localization, editing,
verification, and repair segments, masks reverted or repeated-ineffective
segments, and keeps at most 10% high-value trajectories.

`python/ultimate_coders/acceptance/gdpo_scorer.py` normalizes pass, F2P, and P2P components (weights 0.5/0.3/0.2)
within a group and then across the batch. `python/ultimate_coders/knowledge/skill_curriculum.py` defines the
four-field skill card and four-stage validation/curriculum.

`python/ultimate_coders/acceptance/swe_doctor_diag.py` produces a `RuntimeDiagnosisReport`: expected behaviour
clauses, bounded local-variable snapshots, and the first state-divergence
frame from pytest `--tb=short --showlocals` or `sys.monitoring` input. The
local harness reports the bounded output of its most recent failed
`run_command` as `AgentOutput.test_output`; when `UC_SWE_DOCTOR` is on, a
failed worker run publishes a `subtask_progress` event with
`phase: "runtime diagnosis"` plus the bounded expected clauses, snapshots,
and divergence frame. This is a partial diagnosis boundary: only pytest-like
text the harness actually observed is parsed, adapters that cannot observe
command output report nothing, and the report is never used to pass a run.

## Sandbox/RL boundary

`crates/uc-engine/src/rl_hardening.rs` models cgroup v2/PID-namespace process-group cleanup,
BLAKE3 state-hash receipts before and after test evaluation, and an
environment anomaly when the receipt changes. It is a library contract for
the Rust executor; it does not claim that an unprivileged process can create
cgroups or namespaces by itself.

## Rollout

Every Python/TypeScript feature flag is read per call and defaults off.
In-memory stores are process-local and bounded. No database migration or new
service is required. The v10 Rust modules are additive exports.

Verification:

```bash
.venv/Scripts/python.exe -m pytest tests/python -k v10 -q --no-cov
.venv/Scripts/python.exe -m ruff check python/ultimate_coders/acceptance python/ultimate_coders/agent/planner.py
cd packages/uc-orchestrator && bun test src/rl
cd dashboard && node --test tests/*.test.mjs && node_modules/.bin/tsc -b
```

The Rust modules are rustfmt-clean. A full workspace `cargo check` requires
crates.io access, which is unavailable in the development sandbox and whose
offline registry is incomplete. The `uc-types` crate was checked from a
standalone copy against the repository cache, and the three new Rust modules
were compiled as standalone test harnesses: 23/23 new Rust tests passed. The
`uc-engine` crate as a whole remains compile-unverified in this environment;
CI is the authority for the workspace build.

## Non-goals

- `BoundaryContract` is not a second task identity or a replacement for the
  Rust governance mandate.
- `FailureHeatmap` does not synthesize failure events; callers supply the
  bounded event window.
- AgentRadio's in-memory bus is not a durable event store.
- RL hardening types do not perform privileged host setup; the executor owns
  cgroup/namespace creation.
