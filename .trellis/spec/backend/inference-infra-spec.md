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

- Environment: `UC_METAINFER_URL` enables routing; live probes determine remote operation readiness. `inference_benchmark` is local. `UC_DATABASE_URL` is shared by Gateway/coordinator/Worker/API; no configured-DB failure fallback. `UC_RUNTIME_STATE_DIR` supplies local-only SQLite. `UC_METAINFER_TASK_TYPES` and `UC_METAINFER_MAX_CONCURRENCY` are shared backend policy.
- Domain config: `inference_task` contains operation/repository/objective/framework/model/hardware/constraints/parameters and optional `upstream_type`; `benchmark` contains immutable argv/workload/protected paths; `oracle`, `apply_command`, `max_iterations` are optional policy/execution settings.
- Explicit agent selection overrides heuristics. Explicit domain tasks remain one node. Remote nodes require `inference_infra` and the matching operation capability; benchmarks require only `inference_benchmark`. Default routing requires framework/inference context and engineering intent.
- Actual upstream: discover `/api/sys-shell/task-types/{type}/schema`, POST `/api/sys-shell/tasks` with type/answers/raw_request, poll `/api/sys-shell/{id}`, stop via `/{id}/control` with action kill and force true. Never retry POST without upstream idempotency support.
- Defaults: port-model, evolve-kernel, gen-infer-framework (runtime **generation**), sglang-trace-analyze. Existing-runtime optimization selects a real service plugin explicitly. Required form fields come from the live schema, never guessed prose.
- UC's assigned repository path overrides submitted paths. Kernel files stay within it; model-porting target_framework_dir is redirected there. Service and worker must share these absolute paths.
- BenchmarkResult requires workload_id plus strict compile/correctness booleans. Metrics use milliseconds, tokens/second and GB; numerical_error is optional unless constrained. A final_status from MetaInfer is not correctness/performance evidence.
- Graph version 1 validates unique node IDs and edge endpoints. Declared task nodes carry provenance; measured evidence is attached per iteration. Accepted graph/measurements enter shared project Memory; all verdicts persist in local history.
- Candidate rollback manages tracked/nonignored files in an exclusively owned, initially clean code worktree. It never resets HEAD. Backend commits/submodules are unsupported; ignored caches and external files are excluded. Include untracked source in accepted patches.
- UC cancellation uses a control file and eight-second cleanup grace before local termination. Use Python-controlled execution for this adapter even with an Engine attached. POSIX nested command groups register under the runner owner; Windows commands start through a gated Job Object wrapper. Force-stop covers local descendants. Hard OS termination cannot guarantee remote cleanup or rollback.
- Python 3.9 remains supported: bound async waits with `asyncio.wait_for` and catch `asyncio.TimeoutError`, which differs from built-in `TimeoutError` before Python 3.11. Cancellation and forced-termination tests must run on both CI Python versions.
- The final adapter envelope carries structured file path/change-type records. Successful workflow steps retain their domain evidence with step index and agent through Worker Memory publication, including when a later ordinary coding step has no domain result.

## 4. Validation & Error Matrix

| Condition | Behavior |
|---|---|
| Disabled service | No automatic domain routing/capability advertising |
| Missing/unknown form parameters | Fail before POST |
| Unknown service plugin or transport error | Explicit domain failure; no generic fallback |
| Deadline/cooperative cancellation | Kill remote task, await ok; unconfirmed termination reports task ID |
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

## 8. Durable inference recovery

- Strict real-worktree allocation applies to mutating tasks and steps, independent of file constraints; never execute a mutating domain parallel group. Preserve noncompleted leases during cleanup, including after manager restart.
- Runner startup validates the original lease claim and live Worker process birth, then registers its own process birth inside the same lease mutation used for takeover. A delayed child cannot execute after owner death/adoption; an already registered live runner prevents takeover. Transient owner tokens never enter stable experiment identity.
- Persist intent before POST, then remote ID before polling. UC graph/node/attempt/step and iteration identify stable experiments/operations. Configuration/backend/task digest mismatch, ambiguous submit, or unconfirmed stop is nonretryable and cleanup_pending. Preserve rollback snapshots and code; do not release a live remote writer's lease/budget.
- Accepted edits are committed by UC before merge. Failed commit/merge/push retains branch/worktree. Persist delivery before cleanup so recovery cannot reexecute after merge. Memory acceptance must be distinguishable from committed/merged delivery.
- Repeated samples (>=3), warmup, per-metric dispersion and environment identity are required. Timing uses medians; peak memory/error use maxima. A missing numerical-error sample cannot be hidden by other repetitions. Noise/dispersion, workload, immutable harness, correctness and hard limits all gate acceptance.
- Atomic checkpoints retain the original baseline and transaction. Artifact manifests record identity, policy, code SHA, checksums and size. Authorized API downloads whitelist report/benchmark/graph/patch, never internal rollback snapshots; missing remote files are explicit.
- Required fault tests: simultaneous duplicate dispatch, crash between remote completion and slot release, crash after merge before final checkpoint, coordinator restart/future/obsolete attempts, uncertain writer, lost publish/replay, authenticated artifact integrity and per-sample hard-limit violations.
