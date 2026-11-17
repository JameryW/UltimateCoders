# V10 Architecture Upgrade Map

This map records the v10 upgrades requested in the architecture ledger
(second through fourth periods). The implementations are opt-in and default
off. They extend the existing planner, worker, search and acceptance
boundaries; they do not create a second scheduler, knowledge authority, or
mandate store.

## Status

| Area | Modules | Flag | Wired at | State |
| --- | --- | --- | --- | --- |
| Boundary contract | `crates/uc-types/src/contract.rs`, `crates/uc-engine/src/lib.rs`, `python/ultimate_coders/agent/boundary_contract.py` | `UC_BOUNDARY_CONTRACT`, `UC_BOUNDARY_CONTRACT_ENFORCE` | worker `_apply_perception_pass` / `_verify_output_boundary` | wired (partial) |
| PerceptionPass | `python/ultimate_coders/agent/planner.py` | `UC_PERCEPTION_PASS` | worker `_apply_perception_pass` | wired |
| Plan critic | `python/ultimate_coders/agent/planner_critic.py` | `UC_PLANNER_CRITIC` | worker PerceptionPass preflight | wired (partial) |
| Prefix context assembly | `python/ultimate_coders/search/context_assembler.py` | `UC_PREFIX_CONTEXT_ASSEMBLY` | worker prompt assembly | wired (partial) |
| Anti-pattern gate | `python/ultimate_coders/acceptance/anti_pattern_gate.py` | `UC_ANTI_PATTERN_GATE` | worker `_verify_output_anti_patterns` | wired |
| Mutation augmentation | `python/ultimate_coders/acceptance/mutation_augment.py` | `UC_MUTATION_AUGMENT` | acceptance dataset builder | tested primitive |
| SWE-Prime | `packages/uc-orchestrator/src/rl/swe_prime.ts`, `packages/uc-orchestrator/src/rl/task_trajectory.ts` | `UC_SWE_PRIME` | orchestrator progress collector → task completion | wired (orchestrator diagnostic) |
| Failure heatmap | `dashboard/src/lib/failureHeatmap.ts`, `dashboard/src/components/FailureHeatmap.tsx` | n/a (presentation) | dashboard overview failure panel | wired (overview feed) |
| AgentRadio | `python/ultimate_coders/agent/radio.py` | `UC_AGENT_RADIO` | worker tool/step boundary | wired (local harness tools) |
| ExecCritic | `python/ultimate_coders/acceptance/exec_critic.py` | `UC_EXEC_CRITIC` | worker test-artifact gate | wired (partial) |
| RETRACE | `python/ultimate_coders/acceptance/retrace_verifier.py` | `UC_RETRACE_VERIFIER` | worker diagnostic | wired (diagnostic) |
| Drift monitor | `python/ultimate_coders/agent/drift_monitor.py` | `UC_DRIFT_MONITOR` | local-harness loop (`drift_advisor`) | wired (local harness) |
| Cumulative regression | `crates/uc-engine/src/dag.rs`, `python/ultimate_coders/agent/orchestrator.py` | n/a (observed-data gate) | Python orchestrator completion gate; Rust plan remains canonical | wired (Python orchestrator) |
| Dockerless verifier | `python/ultimate_coders/acceptance/dockerless_verifier.py` | `UC_DOCKERLESS_VERIFIER` | Oracle fallback / TITO filter | wired (diagnostic) |
| ECLoop gate | `python/ultimate_coders/agent/ecloop_gate.py` | `UC_ECLOOP_GATE` | local-harness edit/submit tool boundary | wired (local harness) |
| TRACE credit | `packages/uc-orchestrator/src/rl/trace_credit.ts`, `packages/uc-orchestrator/src/rl/trace_runtime.ts` | `UC_TRACE_LOGPROBS` | worker turn likelihoods → orchestrator collector | wired (partial) |
| Observed test execution | `python/ultimate_coders/acceptance/test_execution.py` | `UC_TEST_EXECUTION` | worker test-execution diagnostic + opt-in P2P gate | wired (partial) |
| GDPO + skills | `python/ultimate_coders/acceptance/gdpo_scorer.py`, `python/ultimate_coders/knowledge/skill_curriculum.py` | `UC_GDPO_SCORER`, `UC_SKILL_CURRICULUM` | worker declared acceptance-score diagnostic and validated skill-card selection/injection | wired (partial) |
| SWE-Doctor | `python/ultimate_coders/acceptance/swe_doctor_diag.py` | `UC_SWE_DOCTOR` | worker failed-run diagnosis | wired (partial) |
| RL hardening | `crates/uc-engine/src/rl_hardening.rs`, `crates/uc-engine/src/sandbox/cgroup.rs` | `UC_RL_CGROUP` | Linux subprocess sandbox cgroup guard | wired (Linux, default-off) |

## Boundary contract and planning

`BoundaryContract` assigns each path one of `editable`, `inspect_only`,
`required`, and `protected`. A `BoundaryChallengeRequest` carries the target
file, AST symbol, evidence line, and reason. The engine exposes the challenge
state (`BoundaryChallengeState`) and outcome; approval can update the contract
in place. The type is deliberately independent of the existing task-identity
`agent/contract.py`.

The worker now consumes the same four roles through
`python/ultimate_coders/agent/boundary_contract.py`: `target_files` from the
PerceptionPass spec become `editable`, the dependency closure becomes
`inspect_only`, and unlisted paths fail closed as `inspect_only`. When
`UC_BOUNDARY_CONTRACT` is on, `_verify_output_boundary` reports changed paths
that are not editable as a `boundary contract` progress event; with
`UC_BOUNDARY_CONTRACT_ENFORCE` a violation fails the attempt. This is a Python
read-model boundary, not the Rust engine hot-update path: `required` and
`protected` entries are accepted but not inferred automatically, and
`BoundaryChallengeRequest` adjudication remains unimplemented here.

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
carries a deterministic SHA-256; only a changed suffix is recomputed. The
worker projects the already-compiled `ContextManifest` into those slots when
`UC_PREFIX_CONTEXT_ASSEMBLY` is on, preserving every item exactly once and
recording `full_sha256`, `prefix_cache_hint`, slot names and token counts on
the started context summary. Slot stability is monotonic: once a slot has a
volatile item, later slots are volatile too. The harness still owns the system
prompt/tool schema and adds it outside this block, so slot 0 is not byte-level
identical to the final provider prefix yet.

`dashboard/src/lib/failureHeatmap.ts` defines the 7 × 24 failure catalogue (18 deterministic
modes), `classifyPtaQuality`, `buildFailureHeatmap`, and `failureEventsFromDashboard`. The
dashboard overview mounts `FailureHeatmap.tsx` in a collapsed failure panel and feeds it the
bounded live `eventLog`; only `subtask_failed` / `task_failed` events with an explicit
`failure_mode`/`mode` in the catalogue or a recognised deterministic error signature are
projected, and unknown events are dropped rather than invented. The overview panel appears only
when the current window contains at least one classified event.

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
test change raises `ProtectedTestError`. The worker consumes an optional
`agent_config["test_artifact"]` when `UC_EXEC_CRITIC` is on: `TestGenNode`
validates the declared test patch, command, JSON contract, hidden/frozen flags,
and clean-base evidence before any source edit. An invalid artifact is a
diagnostic by default; `"enforce": true` fails the subtask before it starts.
A valid artifact is retained and `SourceRepairNode` rejects any changed source
patch that touches the artifact's protected test files. This is a
declared-artifact boundary, not test generation and not an Oracle: no runner is
invented here, and `retryable` is false only for an enforced preflight
rejection.

`retrace_verifier` builds a forward rationale from the issue and trajectory,
a blind backward reconstruction from the patch/AST without the issue, and
arbitrates agreement, contradiction, or mismatch into a bounded repair
instruction. The worker runs it after a changed patch when
`UC_RETRACE_VERIFIER` is on, reusing the same `HEAD`↔worktree diff as the
dockerless verifier, and emits a `subtask_progress` event with the status,
pass flag, missing/contradictory claims and bounded repair hint. That is a
diagnostic only; it never turns a run into a pass or failure.

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
tool-step boundaries. The local harness wires the local half of AgentRadio
when `UC_AGENT_RADIO` is on and both `UC_RADIO_GRAPH_ID` and
`UC_RADIO_NODE_ID` are present: `uc_radio_post` and
`uc_radio_create_thread` publish worklog/thread messages, an optional
`UC_RADIO_INBOX_JSON` seeds incoming mentions for the run, and pending
mentions are injected as a `Radio updates:` system block at each turn and
tool boundary (`teammate_radio_update` event). The worker fills the graph and
node hints for `local-harness`/`local-llm` steps. There is no NATS transport
inside the harness yet; the in-process bus is a bounded test/development
bridge, not a durable event store.

`DriftMonitor` records edit failures, boundary
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
ancestor regression is a blocking completion condition. The Python
orchestrator applies the same rule to measured data that already exists:
when `SubtaskResult.test_execution` is an `ok` delta and its `broken` list
intersects a transitive ancestor's `f2p_tests`, the descendant is marked
failed with a bounded `Cumulative regression:` error before completion.
Missing or errored measurements stay "not measured", never "no regression",
and the Rust plan is still the canonical engine-side model.

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
0.2/1.0. `packages/uc-orchestrator/src/rl/trace_runtime.ts` is the bounded
runtime collector: it accepts `turn_likelihoods` from a worker
`subtask_progress` event, normalises JSON-encoded or decoded arrays, drops
invalid values instead of substituting a default, and computes turn credits at
`finishTask` when `UC_TRACE_LOGPROBS` is on. The local harness requests token
logprobs only under that flag and averages them into one geometric-mean answer
likelihood per turn; a provider that returns no logprobs produces no sample, so
the collector still cannot invent a likelihood. If a provider rejects the
`logprobs` request parameter (`BadRequestError`/`UnsupportedParamsError`), the
harness emits `logprobs_unavailable` and retries the completion once without it
rather than failing the subtask. Likelihoods are bucketed per subtask, so a
task DAG never computes credits across an artificial subtask boundary; other
adapters leave `AgentOutput.turn_likelihoods` empty.
`packages/uc-orchestrator/src/rl/swe_prime.ts` labels exploration, localization, editing,
verification, and repair segments, masks reverted or repeated-ineffective
segments, and keeps at most 10% high-value trajectories.
`packages/uc-orchestrator/src/rl/task_trajectory.ts` projects the orchestrator's
real `subtask_progress` stream into those segments and distils one loss mask at
`finishTask` when `UC_SWE_PRIME` is on, logging a bounded kept-segment summary.
This is a diagnostic/data-collection boundary: no token likelihoods are
invented and no training or export pipeline runs here.

`python/ultimate_coders/acceptance/gdpo_scorer.py` normalizes pass, F2P, and P2P components (weights 0.5/0.3/0.2)
within a group and then across the batch. When `UC_GDPO_SCORER` is on, the worker consumes an optional
`agent_config["acceptance_scores"]` rollout group (a list or `{"rollouts": [...]}`) and publishes a bounded
`acceptance scores` progress event. At least two rollout mappings are required for group normalisation; invalid
entries and undersized groups are reported without failing the run. This is a declared-data boundary: the worker
does not execute tests or invent F2P/P2P rates. `python/ultimate_coders/knowledge/skill_curriculum.py` validates a
declared four-field skill card with its declared replay/transfer/safety evidence; when `UC_SKILL_CURRICULUM` is on,
the worker accepts one `agent_config["skill_card"]` or a declared `skill_cards` list, validates every card, and
injects only the highest fully validated card ordered by stage, caller priority, then skill id. A failed validation
is diagnostic and leaves the prompt unchanged; evidence is never inferred from the card itself, and curriculum
selection is not evidence generation.

`python/ultimate_coders/acceptance/test_execution.py` is the observed counterpart to those declared scores:
when `UC_TEST_EXECUTION` is on and `agent_config["test_execution"]` names a `command`, the worker runs that
pytest command once against the `HEAD` snapshot and once against the working tree, parses JUnit XML, and
publishes a bounded `test execution` event with pass/F2P/P2P rates and the affected test ids. A non-pytest
command, missing report, or timeout is reported as a diagnostic error rather than a pass. With
`agent_config["test_execution"]["enforce"] = true`, missing measured data fails closed and any broken P2P test
rejects the subtask; otherwise the event is diagnostic only. This is a worker-local regression gate, not the
Rust engine's cumulative ancestor-obligation graph, and it is not an Oracle. The command is a worker-host
subprocess, not a coding-sandbox command: enable this only on disposable/containerized workers and only for
task sources whose commands are trusted.

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
environment anomaly when the receipt changes. It is a declarative library
contract for the Rust executor.

On Linux, `crates/uc-engine/src/sandbox/cgroup.rs` supplies the first runtime
half: with `UC_RL_CGROUP=1` the subprocess sandbox creates a dedicated cgroup
v2 subtree (default `/sys/fs/cgroup/uc-rl`, override `UC_RL_CGROUP_ROOT`),
attaches the spawned child PID, and uses `cgroup.kill` on timeout and drop so
descendants cannot escape the rollout. It requires a writable cgroup v2 host
and root/host permission; without that it logs and runs without the guard.
PID namespace creation and privileged host setup remain outside the executor;
the guard does not claim kernel isolation by itself.

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

The Rust modules are rustfmt-clean. On 2026-11-17 the Ubuntu-24.04 WSL
toolchain (rustc 1.100.0, rsproxy sparse index) ran
`cargo check -p uc-types -p uc-engine` successfully, and
`cargo test -p uc-engine --lib` passed **580/580** tests, including 2 cgroup
tests (one performs an actual cgroup.kill as root). Full workspace tests for
the remaining crates and Windows cgroup behavior remain CI-owned; Windows
cannot run the Linux cgroup module.

## Non-goals

- `BoundaryContract` is not a second task identity or a replacement for the
  Rust governance mandate.
- `FailureHeatmap` does not synthesize failure events; callers supply the
  bounded event window.
- AgentRadio's in-memory bus is not a durable event store.
- The Linux cgroup guard requires a writable cgroup v2 host with root/host
  permission; PID-namespace creation and privileged host setup remain outside
  the executor.
