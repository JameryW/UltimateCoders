# Inference Infrastructure Contract

## 1. Scope / Trigger

Applies to changes in `ultimate_coders.inference`, its UC adapter, domain routing, benchmark acceptance, and artifact or Memory propagation. UC owns global planning and acceptance; the external MetaInfer service owns specialized execution and GPU allocation.

## 2. Signatures

```python
await MetaInferAdapter.execute(task: MetaInferTask) -> MetaInferResult
await BenchmarkRunner.measure(root: str, baseline: bool = False) -> BenchmarkResult
Oracle.evaluate(baseline, candidate) -> OracleVerdict
await OptimizationWorkflow.run(task, benchmark, candidate, artifact_dir, max_iterations=1) -> dict
InferenceInfraAgent.route(description, config=None) -> dict | None
await NatsPublisher.publish_submit(task_id, description, project_id="", agent_config=None)
```

The sandbox adapter launches `python -m ultimate_coders.inference.runner --request <json>` and parses a final JSON envelope with strict `success` and structured evidence. Optional domain evidence uses the existing Worker project Memory API; no execution-envelope or protobuf version change is involved.

## 3. Contracts

- Environment: `UC_METAINFER_URL` enables routing; live probes determine remote operation readiness. Mutating work additionally requires the `uc-metainfer/1` contract, a stable `backend_id`, workspace/quiescence capabilities and (when set) the pinned `UC_METAINFER_REVISION`. `UC_METAINFER_BACKEND_ID` is the operator's stable alias. `inference_benchmark` is local. `UC_DATABASE_URL` is shared by Gateway/coordinator/Worker/API; no configured-DB failure fallback. `UC_RUNTIME_STATE_DIR` supplies local-only SQLite. `UC_METAINFER_TASK_TYPES` and `UC_METAINFER_MAX_CONCURRENCY` are shared backend policy. `UC_OUTBOX_RETENTION_SECONDS` controls indexed delivered-outcome tombstoning.
- Domain config: `inference_task` contains operation/repository/objective/framework/model/hardware/constraints/parameters and optional `upstream_type`; `benchmark` contains immutable argv/workload/protected paths; `oracle`, `apply_command`, `max_iterations` are optional policy/execution settings.
- Explicit agent selection overrides heuristics. Explicit domain tasks remain one node. Remote nodes require `inference_infra` and the matching operation capability; benchmarks require only `inference_benchmark`. Default routing requires framework/inference context and engineering intent.
- Actual upstream: discover `/api/sys-shell/task-types/{type}/schema`, POST `/api/sys-shell/tasks` with type/answers/raw_request, poll `/api/sys-shell/{id}`, stop via `/{id}/control` with action kill and force true. Never retry POST without upstream idempotency support.
- Defaults: port-model, evolve-kernel, gen-infer-framework (runtime **generation**), sglang-trace-analyze. Existing-runtime optimization selects a real service plugin explicitly. Required form fields come from the live schema, never guessed prose. Validate non-null select values against the declared option labels and multiselect values as lists of those labels before reserving backend capacity, persisting submission intent or POST. Preserve label types; do not coerce numeric values to strings or guess an allowed iteration count.
- UC's assigned repository path overrides submitted paths. Kernel files stay within it; model-porting target_framework_dir is redirected there. Service and worker must share these absolute paths.
- BenchmarkResult requires workload_id plus strict compile/correctness booleans. Metrics use milliseconds, tokens/second and GB; numerical_error is optional unless constrained. A final_status from MetaInfer is not correctness/performance evidence.
- Graph version 1 validates unique node IDs and edge endpoints. Declared task nodes carry provenance; measured evidence is attached per iteration. Accepted graph/measurements enter shared project Memory; all verdicts persist in local history.
- Candidate rollback manages tracked/nonignored files in an exclusively owned, initially clean code worktree. It never resets HEAD. Backend commits/submodules are unsupported; ignored caches and external files are excluded. Include untracked source in accepted patches.
- UC cancellation uses a control file and eight-second cleanup grace before local termination. A remote kill acknowledgement, finished flag or PID status is not quiescence evidence. Mutating completion/cancellation must receive a contract receipt proving all writers stopped and retaining the job scope; otherwise preserve `cleanup_pending`, the worktree and resource reservations. Use Python-controlled execution for this adapter even with an Engine attached. POSIX nested command groups register under the runner owner; Windows commands start through a gated Job Object wrapper. Force-stop covers local descendants. Hard OS termination cannot guarantee remote cleanup or rollback.
- Python 3.9 remains supported: bound async waits with `asyncio.wait_for` and catch `asyncio.TimeoutError`, which differs from built-in `TimeoutError` before Python 3.11. Cancellation and forced-termination tests must run on both CI Python versions.
- The final adapter envelope carries structured file path/change-type records. Successful workflow steps retain their domain evidence with step index and agent through Worker Memory publication, including when a later ordinary coding step has no domain result.

## 4. Validation & Error Matrix

| Condition | Behavior |
|---|---|
| Disabled service | No automatic domain routing/capability advertising |
| Missing/unknown form parameters or invalid select/multiselect choices | Fail before capacity reservation, submission intent and POST |
| Unknown service plugin or transport error | Explicit domain failure; no generic fallback |
| Deadline/cooperative cancellation | Request remote stop; release only after all-writer quiescence evidence, otherwise retain the task ID and quarantine |
| Compile/correctness failure, mismatched workload, invalid/missing metrics | Reject |
| Slower TPOT/TTFT, lower throughput, exceeded memory/error limits | Reject according to policy |
| Benchmark harness mutation | Fail and restore candidate code |
| Dirty/shared repository, changed HEAD, initialized submodule | Refuse acceptance |
| Memory write failure | Existing best-effort Memory behavior; artifact evidence remains authoritative |

## 5. Good / Base / Bad Cases

Good: fixed workload TPOT 43 -> 37, correct numerics, memory under limit: retain patch and evidence. Base: service unset and React task: existing coding path. Bad: service says success but TPOT 43 -> 78, or no measurement: rollback/reject. Backend generation without importing an output leaves the input unchanged and cannot pass as optimization.

## 6. Tests Required

Exercise Oracle metric direction, missing/nonfinite values, correctness/compile/workload/ceilings; schema-valid HTTP submission, unsupported parameters, nonretried POST, cancellation/kill confirmation; real git rollback including additions, file/directory replacement and protected-harness mutation; explicit and heuristic routing, capability opt-in, Dashboard/NATS config forwarding, Worker adapter choice and accepted workflow Memory; real sandbox runner execution/report persistence, coroutine/control cancellation via an HTTP service, command descendant timeout and forced nested-session termination.

## 7. Wrong vs Correct

Wrong: accept a backend's success string or apply a returned shell command automatically. Correct: use explicit argv import hooks, fixed immutable benchmarks and the shared Oracle. Wrong: describe gen-infer-framework as existing SGLang runtime optimization. Correct: document its generation semantics and configure a genuine optimization plugin when needed. Forced OS termination cannot guarantee remote HTTP cleanup; preserve that limitation in operational docs.

## 7-eval-role. Eval role identity (Agent Trust P0 / T1)

### 1. Scope / Trigger

Any change that constructs, serializes, accepts, or publishes `BenchmarkResult` / `OracleVerdict`, or that writes public uplift / report figures from evaluation products.

### 2. Signatures

```python
class EvalRole(str, Enum):  # search | select | report
def eval_role_enforce_enabled() -> bool
def require_report_role(result_or_verdict) -> None
def assert_report_identity(result_or_verdict) -> None  # alias
BenchmarkResult(..., eval_role: EvalRole = EvalRole.SELECT)
OracleVerdict(..., eval_role: EvalRole = EvalRole.SELECT)
BenchmarkResult.with_role(role) / OracleVerdict.with_role(role)
Oracle.evaluate(baseline, candidate) -> OracleVerdict  # verdict.eval_role == candidate.eval_role
```

### 3. Contracts

- Every `BenchmarkResult` and `OracleVerdict` carries `eval_role ∈ {search, select, report}`.
- `to_dict` emits the role string; `from_dict` missing key defaults to `select` (legacy).
- Env: `UC_EVAL_ROLE_ENFORCE` — unset/empty/`0`/`false`/`no`/`off` → enforcement **off** (default). Truthy (`1`/`true`/`yes`/`on`) → on.
- When enforce is on: `require_report_role` / `assert_report_identity` reject non-report; `with_role` refuses search/select → report relabel. Explicit construction with `eval_role=report` remains allowed.
- `require_report_role` also enforces when `UC_REPORT_UPLIFT_ENFORCE` is on (ticket 02 publish boundary), even if eval-role enforce is off.
- Accept path: Oracle may consume select-role evidence. Public uplift must call report-identity helpers (see §7-report-uplift).

### 4. Validation & Error Matrix

| Condition | Behavior |
|---|---|
| Enforce off + non-report passed to `require_report_role` | No-op |
| Enforce on + select/search passed to `require_report_role` | `ValueError` |
| Enforce on + `with_role(REPORT)` from search/select | `ValueError` |
| Enforce on + mint `BenchmarkResult(..., eval_role=REPORT)` | Allowed |
| `Oracle.evaluate` with select candidate | Verdict inherits `select`; accept rules unchanged |

### 5. Good / Base / Bad Cases

Good: select candidate accepted by Oracle; report remasure later passes `require_report_role` under enforce. Base: legacy dict without `eval_role` loads as select; enforce off → helpers no-op. Bad: under enforce, writing select scores into a report-only consumer or relabeling search→report via `with_role`.

### 6. Tests Required

`tests/python/test_eval_role.py` plus existing `test_inference_oracle.py`: role round-trip, legacy default, enforce flag matrix, refuse relabel, accept still consumes select.

### 7. Wrong vs Correct

Wrong: treat select/search scores as report uplift, or rely on `dataclasses.replace` to bypass `with_role` in product code. Correct: mint report via protected remasure / explicit report construction; call `require_report_role` at publish boundaries when enforce is on.

## 7-report-uplift. Report-only public uplift (Agent Trust P0 / T2)

### 1. Scope / Trigger

Accept-vs-publish separation: `OptimizationWorkflow.run` after a select-role accept, and `runner._execute_inference` before writing `report.json` / manifest `verdict` used for publication.

### 2. Signatures

```python
REPORT_UPLIFT_ENFORCE_ENV = "UC_REPORT_UPLIFT_ENFORCE"
def report_uplift_enforce_enabled() -> bool
def mint_report_result(result: BenchmarkResult) -> BenchmarkResult  # explicit eval_role=report
def published_uplift_verdict(result: dict) -> dict | None
# require_report_role also enforces when report-uplift enforce is on
await OptimizationWorkflow.run(...) -> dict  # may include report_verdict, public_uplift
```

### 3. Contracts

- Env: `UC_REPORT_UPLIFT_ENFORCE` — same truthy/off parsing as `UC_EVAL_ROLE_ENFORCE`; default **off**.
- When off: workflow returns select-sourced nested verdicts only (no remasure); `published_uplift_verdict` uses the last iteration verdict (legacy).
- When on and the workflow **accepts** a candidate: remasure baseline (HEAD) + accepted working tree via the same `BenchmarkRunner`, mint both with `mint_report_result`, `Oracle.evaluate` → `report_verdict`; attach `public_uplift.improvement_pct` from that verdict only. Select `iterations` history stays intact.
- Publish boundary (`runner` before `report.json` / manifest verdict): call `published_uplift_verdict` → `require_report_role`. Select/search scores must not populate public uplift fields.
- Missing/failed report remasure: workflow raises (`report remasure failed`); `published_uplift_verdict` raises if optimization `success` with iterations but no `report_verdict` — **no** select fallback. Analyze/benchmark successes (no iterations) publish no uplift verdict.
- Oracle accept loop remains select-role evidence.

### 4. Validation & Error Matrix

| Condition | Behavior |
|---|---|
| Flag off | No remasure; public verdict = last select iteration |
| Flag on + accept + remasure OK | `report_verdict` / `public_uplift` from report Oracle |
| Flag on + accept + remasure fails | Raise; publication must not use select uplift |
| Flag on + optimization success (iterations) without `report_verdict` | `published_uplift_verdict` → `ValueError` |
| Flag on + non-optimization success (no iterations) | `None` (no uplift published) |
| Flag on + `report_verdict` with select role | `require_report_role` → `ValueError` |

### 5. Good / Base / Bad Cases

Good: select accept, report remasure yields report-tagged uplift used in manifest. Base: flag off preserves prior nested-verdict publication. Bad: stuffing select `improvement_pct` into public fields under enforce, or falling back to iteration verdicts when remasure is missing.

### 6. Tests Required

`tests/python/test_report_uplift.py` (+ existing eval_role / workflow suites): flag matrix, accept-vs-publish separation, no select fallback, remasure failure fail-closed.

### 7. Wrong vs Correct

Wrong: publish last-iteration select uplift when remasure failed. Correct: fail closed; only `report_verdict` may fill public uplift under enforce.

## 7a. Gotcha: decoded wire payloads are not `@dataclass` types

**Warning** The dataclass rule in `.trellis/spec/frontend/component-guidelines.md`
("All data types use `@dataclass`, never TypedDict") governs **UC's own domain and
config types** -- `EngineConfig`, `Subtask`, `BenchmarkResult` and friends. It does
NOT govern JSON decoded from the external MetaInfer service.

`python/ultimate_coders/inference/service_contract.py` deliberately keeps `proof`,
`contract`, `receipt` and `evidence` as `dict` and probes them with `.get()`
against the versioned `uc-metainfer/1` contract. That is the correct shape for a
third-party boundary:

- the schema is owned by the service, not by UC, and moves under `contract_version`;
- a missing key is a *contract* verdict (`DeploymentContractError`), not a
  `KeyError` from a parser UC wrote;
- wrapping each payload in a dataclass would assert a schema UC cannot enforce
  and would turn a defensive probe into an AttributeError.

Wrong: `@dataclass class QuiescenceProof: ...` constructed from `response.json()`,
so an unknown service revision raises `AttributeError` instead of
`DeploymentContractError`. Correct: probe the dict, raise the contract error, and
type the *return* as `dict[str, Any]`. Reserve `@dataclass` for types UC defines
and serialises itself.

## 7b. Convention: the `retryable` exception channel

**What**: an inference exception may carry a class attribute `retryable: bool`.
`python/ultimate_coders/inference/runner.py` reads it through
`getattr(exc, "retryable", not isinstance(exc, asyncio.CancelledError))` and writes
the value into the outcome record, where it decides whether the failure spends an
execution retry.

**Why**: R3 requires that waiting for a shared backend slot must NOT consume
execution retries. The channel is how `python/ultimate_coders/inference/resources.py`
marks the capacity wait non-retryable in the *execution* sense while still leaving
it cancellable and re-dispatchable. It is a Python-side classification and does not
duplicate `EngineError::is_retryable` (`crates/uc-types/src/error.rs`), which
classifies transport errors across the Rust boundary.

**Rule**: default is retryable. Set `retryable = False` for errors that must not
spend a dispatch attempt -- contract violations, ambiguous submissions, resource
waits and unconfirmed stops. Never set it `True` for an error whose cause is not
known to be transient.

## 8. Durable inference recovery

- Strict real-worktree allocation applies to mutating tasks and steps, independent of file constraints; never execute a mutating domain parallel group. Preserve noncompleted leases during cleanup, including after manager restart.
- Runner startup validates the original lease claim and live Worker process birth, then registers its own process birth inside the same lease mutation used for takeover. A delayed child cannot execute after owner death/adoption; an already registered live runner prevents takeover. Transient owner tokens never enter stable experiment identity.
- Persist intent before POST, then remote ID before polling. UC graph/node/attempt/step and iteration identify stable experiments/operations. Configuration/backend/task digest mismatch, ambiguous submit, or unconfirmed stop is nonretryable and cleanup_pending. Preserve rollback snapshots and code; do not release a live remote writer's lease/budget.
- Explicit Gateway retry is a separate CAS command with a new dispatch attempt; generic projections cannot reopen a controlled or terminal task. Graph shadow retry records `FAILED → READY` in one graph transaction. Old stamped and unstamped projections are fenced after an attempt changes.
- Resource waits are durable FIFO tickets. Cancellation cannot release another operation's active holder, and a cancelled wait does not consume an execution retry. Backend URL aliases converge on the stable backend identity.
- Operator reconciliation uses record versions and audit rows. `attach`, `confirm_stop` and workspace recovery require explicit actor identity and matching remote identity/evidence; stale heartbeats never prove remote death.
- Runtime pending/delivery queries are bounded and indexed. Connection pools are bounded for PostgreSQL, SQLite uses one thread-local connection, and delivered outbox rows compact to tombstones rather than disappearing.
- Accepted edits are committed by UC before merge. Failed commit/merge/push retains branch/worktree. Persist delivery before cleanup so recovery cannot reexecute after merge. Memory acceptance must be distinguishable from committed/merged delivery.
- Repeated samples (>=3), warmup, per-metric dispersion and environment identity are required. Timing uses medians; peak memory/error use maxima. A missing numerical-error sample cannot be hidden by other repetitions. Noise/dispersion, workload, immutable harness, correctness and hard limits all gate acceptance.
- Atomic checkpoints retain the original baseline and transaction. Artifact manifests record identity, policy, code SHA, checksums and size. Authorized API downloads whitelist report/benchmark/graph/patch, never internal rollback snapshots; missing remote files are explicit.
- Required fault tests: simultaneous duplicate dispatch, crash between remote completion and slot release, crash after merge before final checkpoint, coordinator restart/future/obsolete attempts, uncertain writer, lost publish/replay, authenticated artifact integrity and per-sample hard-limit violations.

## 9. Contained service extension

### 9.1 Scope / Trigger

Applies to the separately deployed MetaInfer contract extension. Reuse the
pinned orchestrator and shell routes; do not vendor upstream, invoke its stock
app factory, native reaper or plugin janitors. The extension is an execution
API. Optional plugin/log readers are omitted because they can follow job-owned
paths on the host. GPU/model and complete optimization acceptance remain a
separate release criterion.

### 9.2 Signatures

Command: python -m ultimate_coders.metainfer_service --source PATH --root PATH
--control PATH --workspace-root PATH [repeatable] --backend-id ID --image IMAGE
[--runtime podman|docker] [--gpu none|wsl|nvidia] [--job-env JSON_FILE]
[--port 48766]. It listens only on 127.0.0.1.

HTTP: GET /api/uc/contract; POST /api/uc/workspaces/verify;
GET /api/uc/jobs/TASK_ID/quiescence; GET /api/uc/jobs/TASK_ID/identity;
GET /api/uc/hardware. Keep the pinned shell's
discovery/schema, POST tasks and kill control. Fixed-file task observations use
the existing GET tasks and GET task-ID response shapes.

### 9.3 Contracts

Use uc-metainfer/1 and the configured stable backend ID/pinned revision.
Workspace receipts carry read_sha256 and the actual OCI writer identity.
Nonce directories use mode 1777, source/token files mode 0644, O_EXCL creation
and no-follow writes; tokens contain no secrets. The client allows 30 seconds
for the workspace POST's live identity observation, OCI startup and stopped
writer receipt, retaining nonce files until the HTTP operation completes.
Apply an absolute asyncio.wait_for deadline around the entire HTTP operation,
including continuously trickling bodies; an HTTPX idle timeout is insufficient.
Timeout, cancellation or an invalid response after submission retains nonces,
is nonretryable/cleanup_pending and requires stopped-scope reconciliation before
rollback or reuse. Adapter records probe context before POST under
remote_jobs[operation_id + ":workspace-probe"] and refuses an unresolved replay.
Completion matches its probe directory before changing the record.

The additive `job_identity` capability implements the identity endpoint used
by `RemoteJobs.reconcile(..., "attach", remote_id=..., actor=...)`. For UC
submissions the Launcher binds the complete `UC <operation_id>` label and
SHA-256 of `json.dumps(json.loads(raw_request), sort_keys=True).encode()`
before OCI create. `ContainedJobs.launch(..., submission_identity=None)`
validates an optional dict containing exactly `operation_id` (1..256 ASCII
letters/digits/underscore/dot/colon/hyphen) and `request_sha256` (64 lowercase
hex digits), copies it into its private intent and binds both values to OCI
labels `uc.operation` / `uc.task_request`. Job-owned requirements/run files
are never identity evidence. Missing bindings cannot be retrofitted.

Identity responses contain `contract_version`, `backend_id`, `task_id`,
`operation_id`, `request_sha256` and the actually observed `container_id`.
Refuse with HTTP 503 on legacy/missing jobs, changed boot, unavailable OCI,
altered labels or immutable container identity. Reading identity does not
publish a stopped-writer receipt, stop/relaunch code, release workspace
ownership or capacity. Attach still requires the current local record version
and matching operation/request/backend; only separate `confirm_stop` with
actual quiescence can release capacity. Roll out the service first; existing
clients already use this route. Missing old submissions remain quarantined.

The additive workspace_probe_identity capability accepts a caller-generated
probe-<32 lowercase hex> ID and returns a matching quiescence receipt. A durable
exclusive probe claim prevents duplicate requests from stopping another scope.
For an unknown ID, quiescence durably fences delayed submissions and may return
workspace_fenced; an active pre-create claim remains unknown. Failed pre-create
launches become fenced only when no durable launch intent exists. Recorded OCI
jobs still require kernel scope emptiness. Older services remain compatible,
but uncertain probes without an ID require service-side identification.
Timeout cannot establish stopped writers or a passing deployment. The image resolves to an
immutable content ID. Explicit job environment is string-valued JSON and never
inherits host secrets. Containers match worktree UID/GID; rootless Podman uses
keep-id and requires service UID/GID to match.

Persist intent before create, ID before start and boot/cgroup/generation before
the launch gate. Never replay a create/start after an unknown result. Receipts
include container_id, cgroup, execution_scope, observed_at and evidence_id;
persist the hashed receipt before CAS release of that task's workspace owner.
Old receipts cannot release newer owners. Stop acknowledgements do not release
ownership. Upstream ProcStatus.finished_at uses Unix epoch seconds.

### 9.4 Validation & Error Matrix

| Condition | Behavior |
| --- | --- |
| Source/task/control roots overlap allowed workspaces | Refuse service startup |
| Configured workspace-root symlink changes after startup | Keep the validated canonical roots; never authorize its new target |
| Configured control-store symlink changes after startup | Keep the validated canonical store; never redirect ownership records into the workspace |
| Control store overlaps a mount; invalid runtime UID/env/mount parameters | Reject before claiming the workspace or changing ownership |
| Non-linked/shared Git repository | Refuse mutating launch |
| Foreign/restarted container, changed boot, missing scope, unreadable cgroup | No stop proof; retain ownership |
| Kill races with natural exit | Fresh identity-checked stopped observation may acknowledge; proof remains separate |
| Short probe exits during inspection | Reobserve once; on failure stop and confirm proof, otherwise retain claim and report probe ID |
| Unsafe metadata symlink/FIFO/oversized file | HTTP 503; never follow links or arbitrary log_file paths |
| Failed submission | Remains listable without a false writer proof |
| Legacy/missing submission binding or changed OCI identity | Identity HTTP 503; retain uncertainty and reservations |
| Valid matching identity for an uncertain local submission | Versioned attach only; retain live writer ownership/capacity |
| Reset/restart/cluster mutation | HTTP 409 |
| Optional plugin/agent-log observation | HTTP 404 |
| GPU/model runtime not configured | Hardware HTTP 503; GPU release gate rejects |

### 9.5 Good / Base / Bad Cases

Good: detached child survives service exit, is found by immutable container and
kernel scope after restart, and stops before a durable receipt releases its
workspace. Base: no GPU image, CPU workspace/lifecycle gates pass while the GPU
gate rejects. Bad: a job replaces requirements.json with a host-file symlink;
GET task returns 503 and cannot publish that file through experiment evidence.
Good: a lost create response is attached using its private pre-create identity
and actual OCI observation. Base: legacy jobs continue execution with identity
unavailable. Bad: deriving operation identity from a writer-modified label/file
or treating a matching identity as stopped-writer evidence.

### 9.6 Tests Required

Assert full descendant cgroup emptiness; permission errors fail closed;
foreign/restarted identity rejection; launch-response loss never replays;
validation failures precede claims; repeated old receipts fence newer owners;
short probe/kill exit races; no-follow metadata/FIFO rejection; discovery and
create survive router filtering; Unix timestamps and sanitized timeout causes;
identity persisted before create, stable after writer metadata changes and
caller dict mutation; invalid binding refused before ownership; real public
identity -> production attach -> separate stop confirmation with capacity
retained until quiescence; legacy/missing/foreign identity refused. Record real
OCI/HTTP recovery separately from fixtures and full GPU optimization.
Retargeting a configured workspace-root symlink cannot authorize protected paths.
Retargeting the configured control-store alias cannot redirect private records.
Run real OCI detached-writer and cross-UID HTTP gates separately from fixtures.
Record actual GPU optimization evidence separately; CPU passes cannot close R8.

### 9.7 Wrong vs Correct

Wrong: call the stock app factory then remove its HTTP routes; its background
janitor can still start host writers. Correct: build only the shell router,
filter it before include_router, and add bounded no-follow observations.
Wrong: treat kill success, a missing PID or Path.exists=false as a stop proof.
Correct: verify immutable OCI state and every owned descendant cgroup; only
FileNotFoundError establishes a missing scope, while permission errors propagate.

## 10. Contained kernel candidate publication

### 10.1 Scope / Trigger

Pinned evolve-kernel keeps its ranked library in the generated workspace.
An optimization workflow benchmarks the assigned repository, so the candidate
must reach that repository before independent Oracle validation.

### 10.2 Signatures

Inside OCI: python -m ultimate_coders.metainfer_service.execution
--upstream-module metainfer.tasks.evolve_kernel.orchestrator.cli
--requirements PATH --repository PATH --state-dir PATH --generated-dir PATH.
publish_kernel(repository: Path, state: Path, generated: Path, kernel: Path) -> dict.
UC: validate_kernel_publication(repository: str, kernel_path: str, run: dict) -> dict.

### 10.3 Contracts

Only image label uc.metainfer.publisher=1 enables the wrapper and advertises
kernel_publication. OptimizationWorkflow sets repository_candidate_required=true
for optimize_kernel. Adapter refuses missing capability before task submission.
Legacy images retain discovery and lifecycle mechanics without this capability.

After a successful terminal upstream outcome, select the first ranked library
entry, require a finite positive exec_time_ms, bounded nonempty code and candidate
ID, and atomically replace the original regular kernel within the owned OCI scope.
Record run.uc_candidate with kernel_path (repository-relative), candidate_id,
code_sha256, publication_kind=oci_generated_candidate and oracle_acceptance=pending.
Preserve file permissions; do not execute code, commit or modify Git metadata.

Upstream's finished flag precedes publication. Adapter waits for quiescence,
refetches final run state, verifies terminal success and receipt identity, and
checks the actual stopped worktree bytes against code_sha256. Only then may UC
run its protected benchmark and Oracle. Publication does not mean acceptance.
Cached completed results revalidate the saved stop proof and current kernel
bytes without repeating POST. A missing proof retains quarantine/capacity;
a valid proof permits budget recovery even if the candidate checksum is invalid.
Refuse nonempty worker_nodes in flat or legacy form/answers before launch;
remote cluster writers are outside this service's cancellation proof.

### 10.4 Validation & Error Matrix

| Condition | Behavior |
| --- | --- |
| Old image lacks publisher label | No capability; workflow fails before POST |
| Run failed/running; library absent; metric nonpositive/nonfinite/bool | No publication |
| Outside repository, Git metadata, parent/leaf symlink, nonregular target | Refuse publication |
| Publication fails after upstream finished | Persist failed terminal outcome; UC rolls back after stop proof |
| Receipt missing, final status changed after stop, path or checksum mismatch | Nonretryable contract failure; no completed result |
| Nonempty remote worker selection | Refuse before workspace claim |

### 10.5 Good / Base / Bad Cases

Good: measured candidate reaches kernel.py, durable receipt matches its bytes,
then Oracle accepts or rejects independently. Base: an old CPU image still
serves workspace/lifecycle checks. Bad: accepting the upstream finished flag
while the publisher has not copied the candidate or has failed.

### 10.6 Tests Required

Public Launcher tests use real linked Git worktrees; assert both old/new image
commands and HTTP capability discovery. Test successful publication, unsafe
paths, symlinks, unsuccessful runs and invalid metrics. HTTP adapter tests assert
refetch after quiescence, missing capability, missing receipt, late failure and
checksum mismatch. Keep the existing Oracle rollback and protected harness tests.
Real GPU runtime/model smokes remain separate from full R8 acceptance.

### 10.7 Wrong vs Correct

Wrong: copy or execute generated code on the host after declaring the job done.
Correct: publish in the already owned OCI scope, prove every writer stopped,
verify the receipt against repository bytes, then run independent UC validation.

## 11. Contained GPU and native Ollama attestation

### 11.1 Scope / Trigger

Provide R8's service-owned hardware endpoint for an explicitly selected local
Ollama deployment. Preflight does not close optimization/Oracle/rollback,
active-run recovery or actual two-host artifact acceptance.

### 11.2 Signatures

ContainedJobs.enable_hardware(environment, *, observe_url=None) -> None.
ContainedJobs.hardware() -> dict. HardwareAttestation.initialize() -> None.
OllamaProfile(url, model, digest, num_ctx=16384, num_predict=4096, think=False).identity() -> dict;
.chat(messages, tools=list, timeout=600) -> dict.
http_exchange(profile, path, payload, timeout) uses the caller's remaining
operation budget. from_environment reads UC_OLLAMA_NUM_CTX.
save_session(directory, thread, profile, messages) and
load_session(directory, thread, profile) persist/validate effective num_ctx.
Native write_file(path, content, workspace) -> path/kind/bytes/sha256.
read_workspace_artifacts(workspace, names) -> bounded no-follow file bytes.
Service: --gpu wsl|nvidia --attest-ollama [--ollama-observe-url URL].
Native client: /opt/uc/ultimate_coders/metainfer_service/coding.py exec
[resume UUID] --json --sandbox workspace-write -C WORKSPACE - (stdin prompt).
RuntimeState.query(..., task_id_present=True, limit=1) uses indexed ownership.

### 11.3 Contracts

Require uc.metainfer.ollama=1, the explicit native client path, codex event
backend and UC_OLLAMA_URL/MODEL/DIGEST. Restrict environment overrides and host
proxy inheritance. No automatic CLI/provider fallback. This initial profile
supports one physical/visible GPU and evolve-kernel jobs.

UC_OLLAMA_NUM_CTX is optional, defaults to 16384 and must be an integer from
1024 through 32768. Every chat sends this value as options.num_ctx. The explicit
environment value enters the private profile hash; changing it cannot reuse
the previous profile cache or trigger a fresh probe while writers are owned.
Launch compares the effective profile, including its default context, before
workspace claims or OCI create. Adding a context to a proof that omitted the
key cannot bypass this check. Sessions store effective num_ctx; resume refuses
a different context. Legacy sessions without the field can resume only at the
legacy default. Keep the provider identity response's existing wire shape.
Keep the legacy default and model/digest validation. Check actual model size,
resident VRAM and context rather than inferring size from a local alias name.
Provider templates must match the selected thinking mode; repair a faulty
template through a new pinned alias, never by removing nonce response text.
UC_OLLAMA_THINK is optional and accepts exactly "true" or "false", default
"false". OllamaProfile.think must be a strict boolean. Every chat sends this
boolean, preserving separate provider thinking/content/tool_calls fields.
Thinking-only weights need an explicitly selected true mode; never infer the
mode from an alias or silently retry with another configuration. The explicit
environment enters the profile hash; the effective boolean participates in
launch equality and saved sessions. A changed mode cannot reuse attestation
with owned writers or resume an existing session. Legacy sessions without
think resume only with false; a numeric 0/1 field is invalid. Keep the public
identity wire shape and nonce/tool verification unchanged.
UC_OLLAMA_NUM_PREDICT is optional, defaults to 4096 and accepts integers from
128 through 8192. Every chat sends it as options.num_predict. It is part of the
effective profile, cache hash and session record. A different output budget
cannot override an implicit attested default or resume the previous session;
legacy sessions without the field remain compatible only at 4096. Record
output exhaustion as failure, never as a completed tool or passing artifact.
Reject done_reason=length at the stream completion boundary, before returning
accumulated text or tools to the CLI. A nonempty final message or syntactically
complete tool call does not override the provider's incomplete result.

A private owned probe runs trusted image code, real kernels and an actual model
nonce request without model-generated commands. Require matching image code and
stopped-writer receipt before evidence. Never accept an operator-authored JSON
as initial runtime proof. The optional host observation URL must see the same
pinned model. Model availability and coding quality are distinct checks.

Cache evidence privately after live observation; bind checksum, boot, backend,
revision, image, profile and executable code. Restart validates the stopped
probe and live GPU/model without another inference or benchmark. A changed
profile cannot probe while any indexed workspace owner has a nonempty task ID,
including legacy records without state. Hardware GET and launch detect drift.
Bound live nvidia-smi observation at five seconds, allowing WSL GPU cold starts;
provider identity keeps its two-second operation deadline. The UC hardware HTTP
request allows ten seconds for both observations. Timeout still returns 503
and prevents a new launch; cached identity cannot substitute for a failed read.

Native chat validates actual alias digest before and after inference. Preserve
real streamed tools; never derive commands from prose. Bound conversation and
stream sizes, turns, commands and deadlines. Use the existing ProcessTree for
tools and a deadline-owned HTTP request process for DNS/headers/body trickling.
Pass the remaining operation budget to the actual HTTP connection; do not add
an independent shorter socket/header ceiling. The owning request process still
enforces the absolute deadline, including continuous header/body trickling.
Native execution requires a nonroot read-only capless OCI job. Session
resume binds model identity. Publication and protected Oracle remain separate.

The native write_file tool accepts actual provider tool calls, bounded UTF-8
content (64 KiB) and paths within the current workspace. Traverse directories
through no-follow FDs; reject traversal, links and special targets. Publish
atomically, fsync and preserve existing permissions. Emit a file_change only
after the write succeeds; command and message item IDs must be distinct. File
tool results are returned to the model. Never turn an agent message into an
artifact or infer a command from it. A requested file still needs independent
existence and content checks; the native client's final text is not that file.
Close directory FDs even when temporary-file cleanup raises an error.

For local acceptance diagnostics, distinguish the documented Python callable
from a raw Triton JIT launch: a module's run(x, y) wrapper is not
JITFunction.run, which requires launch grid and warmup arguments. Check the
actual callable/signature selected by kernel_function_name. Replay a rejected
generated harness against its reference inside OCI before spending another
full optimization deadline. Record unchanged-artifact and diagnostic-copy
hashes separately; an operator-corrected copy cannot establish successful
model generation or optimization. Verify that the review file requested by the
pinned pipeline exists; an exit-zero agent message is not that file and does
not supply file-based regeneration feedback. Do not synthesize a review verdict
or accepted candidate from prose.
For the exact pinned correctness/performance harness generator first lines,
require the corresponding correctness_harness.py/perf_harness.py to be a
nonempty regular file within the workspace, no links, bounded at 64 KiB.
If final prose leaves it missing/empty, send at most two explicit user feedback
messages requesting the real write_file tool and reference execution. Persist
the actual reply and feedback in the session; preserve the 16-turn/900s bounds.
After exhaustion, fail before agent_message/turn.completed. Links/special or
oversized files fail immediately. Generic prompts retain ordinary text final
responses. This existence check does not establish harness correctness or
execution; pinned self-tests and protected UC Oracle remain authoritative.
Native harness instructions request actual file-backed reference loading and
execution before completion. These are model instructions, not a correctness
proof: independently replay the resulting artifact against the reference and
interface-compatible incorrect candidates. Retain the raw kernel when testing
an incorrect public wrapper, so an artifact that bypasses run(x,y) cannot pass
merely by comparing that unchanged raw kernel. A malformed missing-symbol
fixture does not establish numeric rejection. Never silently substitute a
reference implementation or change the requested callable/JSON contract.

### 11.4 Validation & Error Matrix

| Condition | Behavior |
| --- | --- |
| No profile, foreign client or incompatible image | No hardware/probe launch |
| Pinned harness uses `torch.randn` / `randn_like` | At most two input-range feedback turns, then fail before success |
| Invalid context size/type | Reject before provider HTTP or hardware launch |
| Context profile changed while writers are owned | Refuse re-attestation before create |
| Implicit default proof followed by a different context override | Refuse launch before workspace claims |
| Saved session context differs from the current context | Refuse before chat inference |
| Output budget invalid or different from the proof/session | Refuse before launch/inference |
| Thinking mode ambiguous, nonboolean or different from proof/session | Refuse before launch/inference |
| Thinking-only model selected with true | Send actual think:true; preserve separated provider fields |
| Stream completion reports output exhaustion | Fail before tools, session publication or success events |
| File tool escapes workspace, follows links or exceeds its bound | Refuse without writing the target |
| Valid streamed file call | Persist actual bytes, emit file_change, return checksum |
| Changed digest, wrong model or incomplete stream | Fail closed; no substitute tool |
| Headers arrive after 180s but within the requested deadline | Continue within the caller's budget; no premature socket ceiling |
| Headers/body remain incomplete at the operation deadline | Terminate the request process; do not accept partial data |
| Probe failure, code mismatch or unknown writers | No accepted evidence; retain unknown scope |
| Matching restart cache | Fresh stop/GPU/model checks; no new GPU/model inference |
| New profile with active/uncertain ownership | Refuse before OCI create |
| GPU/driver/model drift | Hardware 503; refuse new launches |
| Unsupported task/environment override | Refuse before workspace claim |
| Harness calls JIT internals as its business entrypoint | Record self-reference failure; correct only an explicitly identified diagnostic copy |
| Reviewer returns prose without its requested file | No completed review artifact; retain failure, not a synthetic file/verdict |
| Exact pinned generator returns prose with no nonempty harness | At most two real model feedback turns, then fail before completion |
| Required harness is a link, special file or larger than 64 KiB | Refuse without following/rewriting it |
| Checked native review lacks one leading supported verdict declaration | Fail before final message/turn completion; preserve original bytes |
| Pinned harness generator encounters an unchanged review retained from a rejected prior phase | Preserve its bounded snapshot; do not block generation or approve the review |
| Harness passes a reference but also accepts an incorrect compatible wrapper | Reject the harness; preserve both artifacts and actual numeric outcomes |

### 11.5 Good / Base / Bad Cases

Good: kernels agree with Torch, the pinned model answers a nonce, every writer
stops and restart reuses observed evidence. Base: CPU containment remains usable
while hardware is 503. Bad: declaring full optimization from GPU inventory or
prose that describes a tool invocation.
Good: an explicit context reaches the provider and resumes only with that same
context. Base: omitted context and legacy sessions retain 16384. Bad: silently
adding 4096 to a launch proved at the implicit default, or restoring its session.
Good: explicitly enable thinking for thinking-only weights and receive final
nonce text through the provider's content field. Base: omitted thinking remains
false. Bad: strip reasoning from a failed nonce or change mode during resume.

### 11.6 Tests Required

Use real local HTTP peers for digest, streamed tools, wrong models, truncated
streams, prose and proxy inheritance. Assert rejected profile before create,
thinking mode on the actual request, separated response fields, invalid mode
refusal, legacy-false/session drift and stopped cache reuse with owned writers,
and absolute HTTP deadlines against peers continuously sending no-newline data,
deadline with a delayed writer, legacy active ownership, stopped cache reuse,
model drift and code mismatch. Record actual GPU/model, native CLI and HTTP
deployment checks separately. Keep full R8 acceptance open until measured.
Observe the remaining budget at a real HTTP connection and cover delayed
headers both within and beyond the absolute deadline. For the long-generation
fault, retain the same actual 183s header-delay probe before/after the repair.
For harness diagnostics retain the original failing trace, positive reference
replay, a deliberately wrong candidate's rejection and all-writer stop receipts.
If the user defers cross-host verification for lack of a second host, preserve
that criterion as incomplete while continuing single-host checks; two local
containers cannot satisfy it.

Context tests must observe actual HTTP options, preserve the legacy default,
exercise both allowed endpoints and invalid sizes/types, refuse explicit
profile changes with owned writers, and refuse implicit-default overrides
before claim/create. Exercise same-context restore, changed-context rejection
and legacy-session compatibility through real bounded session files.
File tests exercise real bytes, checksum, permissions, traversal/links/FIFO and
size refusal. A real HTTP peer drives the native CLI and verifies file_change,
distinct item IDs and the tool-result round trip; prose remains unexecuted.
Observe explicit output budget and its default in real HTTP payloads; test
invalid sizes/types, implicit-default launch override and changed-budget resume.
Checked native review files must start with **Verdict**: PASS or FAIL
(an optional leading bullet is supported) and contain one verdict declaration.
Reject heading-only, competing, example-only or missing declarations before
native completion. Both valid PASS and FAIL are delivered unchanged; format
validation does not establish correctness. Bounded no-follow regular-file reads
also cover shell-created files. Exercise symlink/FIFO/oversize refusal and FD
closure. Do not rewrite model verdicts to compensate for the pinned parser's
fallback word counts.

Only the pinned generator prompt's complete original first line may select a
generation boundary: `You are the **CORRECTNESS HARNESS GENERATOR** for GPU
kernel optimization, iteration #N.` or the same PERFORMANCE header, with ASCII
digits for N. At invocation entry, `read_review_files(workspace)` snapshots
bounded no-follow regular files. `verify_review_files(workspace,
unchanged_reviews=snapshot)` may preserve a review only if it existed at entry
and its final bytes are identical. New or changed reviews remain strict;
reviewer and generic turns use no exemption. Select this boundary from the
original caller prompt, never model output or inferred review verdict. Preserve
the rejected review as generation feedback; unchanged bytes are not a PASS.
Test both generators and real file/shell tools: old rejected reviews preserved
with actual generation completion; new/changed malformed reviews, reviewers
and generic calls still refuse. Keep the existing FD/link/FIFO/size limits and
record actual image/model checks separately from genuine harness acceptance.
For both pinned generators, assert missing/empty artifacts cause at most two
feedback requests; retain original responses and observe actual file_change
only after a tool write. Verify no final success on exhaustion or a linked
artifact. Generic prose remains text and old review snapshots stay unchanged.

### 11.7 Wrong vs Correct

Wrong: benchmark the GPU on every active-run service restart. Correct: recheck
private evidence and live identity, and require quiescent ownership before a
fresh probe. Wrong: let model shell tools write their hardware witness.
Correct: trusted attestation executes no model-generated commands; test actual
coding quality separately.
Wrong: compare only the environment keys explicitly present in a proof.
Correct: compare its effective profile, including defaults, before launch.
Wrong: block harness regeneration on an untouched rejected review, or ignore
every review in a generator. Correct: preserve only the entry snapshot's same
bytes during the pinned generation phase and strictly check all new/changed
reviews; reviewers/generic completions retain their original boundary.
Wrong: accept a reference self-test while a harness ignores the public wrapper.
Correct: require a compatible wrong-wrapper control to fail numerically.
Wrong: infer bounded-input compliance from reference/wrong-wrapper pass counts
or the model's repair explanation. Correct: verify the actual input generator
against its range contract separately. `torch.randn * 200 - 100` remains
unbounded; a schema/numeric diagnostic pass cannot establish full harness
acceptance. Preserve the original bytes/report and append the failed assessment.
The native correctness/performance generators run
`assess_harness_input_range` on the required harness artifact before final
completion: AST detection of `torch.randn` / `randn_like` yields at most two
real model feedback turns, then fails without emitting success. Bounded
samplers such as `torch.rand(...) * 200 - 100` and deterministic ranges pass
this gate only; it does not replace reference replay, wrong-wrapper controls
or the protected Oracle.

## 12. Windows checkpoint publication under readers

### 12.1 Scope / Trigger

Applies to runtime_state.atomic_write and all JSON checkpoints using it.
Windows readers can temporarily deny deletion while a replacement is ready.

### 12.2 Signatures

atomic_write(path: Path, data: bytes) -> None; atomic_json(path, data) -> None.

### 12.3 Contracts

Flush and fsync a same-directory temporary file before atomic replacement.
On Windows only, PermissionError with winerror 5 or 32 permits at most five
replace attempts, with 10/20/40/80 ms backoff. Never delete the destination or
copy into it as a fallback. Persistent denial propagates and removes only the
temporary file; the previous complete checkpoint remains. Other errors and
POSIX replacement failures propagate immediately. This retries publication,
not a remote task submission, container creation or execution operation.

### 12.4 Validation & Error Matrix

| Condition | Behavior |
| --- | --- |
| Reader releases after first actual sharing denial | Retry atomic replacement |
| Reader keeps target locked | Fail after five attempts; preserve old bytes |
| Other permission error or POSIX failure | Fail immediately |

### 12.5 Good / Base / Bad Cases

Good: a transient reader releases and the complete new record appears. Base:
uncontended replacement follows its existing path. Bad: deleting the target
or copying bytes into it to bypass a sharing denial exposes missing/partial state.

### 12.6 Tests Required

Use actual Windows CreateFileW handles omitting FILE_SHARE_DELETE. Verify a
released reader permits the second attempt and a retained reader causes five
failed attempts without changing old bytes or leaking the temporary file.

### 12.7 Wrong vs Correct

Wrong: suppress all PermissionError or retry indefinitely. Correct: retry only
the bounded Windows sharing/access cases and propagate the final failure.
