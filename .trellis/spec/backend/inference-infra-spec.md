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
- Defaults: port-model, evolve-kernel, gen-infer-framework (runtime **generation**), sglang-trace-analyze. Existing-runtime optimization selects a real service plugin explicitly. Required form fields come from the live schema, never guessed prose.
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
| Missing/unknown form parameters | Fail before POST |
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
GET /api/uc/jobs/TASK_ID/quiescence; GET /api/uc/hardware. Keep the pinned shell's
discovery/schema, POST tasks and kill control. Fixed-file task observations use
the existing GET tasks and GET task-ID response shapes.

### 9.3 Contracts

Use uc-metainfer/1 and the configured stable backend ID/pinned revision.
Workspace receipts carry read_sha256 and the actual OCI writer identity.
Nonce directories use mode 1777, source/token files mode 0644, O_EXCL creation
and no-follow writes; tokens contain no secrets. The image resolves to an
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
| Reset/restart/cluster mutation | HTTP 409 |
| Optional plugin/agent-log observation | HTTP 404 |
| GPU/model runtime not configured | Hardware HTTP 503; GPU release gate rejects |

### 9.5 Good / Base / Bad Cases

Good: detached child survives service exit, is found by immutable container and
kernel scope after restart, and stops before a durable receipt releases its
workspace. Base: no GPU image, CPU workspace/lifecycle gates pass while the GPU
gate rejects. Bad: a job replaces requirements.json with a host-file symlink;
GET task returns 503 and cannot publish that file through experiment evidence.

### 9.6 Tests Required

Assert full descendant cgroup emptiness; permission errors fail closed;
foreign/restarted identity rejection; launch-response loss never replays;
validation failures precede claims; repeated old receipts fence newer owners;
short probe/kill exit races; no-follow metadata/FIFO rejection; discovery and
create survive router filtering; Unix timestamps and sanitized timeout causes;
retargeting a configured workspace-root symlink cannot authorize protected paths.
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
OllamaProfile(url, model, digest).identity() -> dict; .chat(messages, tools=list) -> dict.
Service: --gpu wsl|nvidia --attest-ollama [--ollama-observe-url URL].
Native client: /opt/uc/ultimate_coders/metainfer_service/coding.py exec
[resume UUID] --json --sandbox workspace-write -C WORKSPACE - (stdin prompt).
RuntimeState.query(..., task_id_present=True, limit=1) uses indexed ownership.

### 11.3 Contracts

Require uc.metainfer.ollama=1, the explicit native client path, codex event
backend and UC_OLLAMA_URL/MODEL/DIGEST. Restrict environment overrides and host
proxy inheritance. No automatic CLI/provider fallback. This initial profile
supports one physical/visible GPU and evolve-kernel jobs.

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

Native chat validates actual alias digest before and after inference. Preserve
real streamed tools; never derive commands from prose. Bound conversation and
stream sizes, turns, commands and deadlines. Use the existing ProcessTree for
tools and a deadline-owned HTTP request process for DNS/headers/body trickling.
Native execution requires a nonroot read-only capless OCI job. Session
resume binds model identity. Publication and protected Oracle remain separate.

### 11.4 Validation & Error Matrix

| Condition | Behavior |
| --- | --- |
| No profile, foreign client or incompatible image | No hardware/probe launch |
| Changed digest, wrong model or incomplete stream | Fail closed; no substitute tool |
| Probe failure, code mismatch or unknown writers | No accepted evidence; retain unknown scope |
| Matching restart cache | Fresh stop/GPU/model checks; no new GPU/model inference |
| New profile with active/uncertain ownership | Refuse before OCI create |
| GPU/driver/model drift | Hardware 503; refuse new launches |
| Unsupported task/environment override | Refuse before workspace claim |

### 11.5 Good / Base / Bad Cases

Good: kernels agree with Torch, the pinned model answers a nonce, every writer
stops and restart reuses observed evidence. Base: CPU containment remains usable
while hardware is 503. Bad: declaring full optimization from GPU inventory or
prose that describes a tool invocation.

### 11.6 Tests Required

Use real local HTTP peers for digest, streamed tools, wrong models, truncated
streams, prose and proxy inheritance. Assert rejected profile before create,
and absolute HTTP deadlines against peers continuously sending no-newline data,
deadline with a delayed writer, legacy active ownership, stopped cache reuse,
model drift and code mismatch. Record actual GPU/model, native CLI and HTTP
deployment checks separately. Keep full R8 acceptance open until measured.

### 11.7 Wrong vs Correct

Wrong: benchmark the GPU on every active-run service restart. Correct: recheck
private evidence and live identity, and require quiescent ownership before a
fresh probe. Wrong: let model shell tools write their hardware witness.
Correct: trusted attestation executes no model-generated commands; test actual
coding quality separately.
