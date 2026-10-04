# Inference infrastructure with MetaInfer

UltimateCoders retains the global planner, distributed dispatch, worktree ownership and result acceptance. MetaInfer is an optional external execution service. The integration adds typed tools, a shared BenchmarkRunner and Oracle, an InferenceInfraAgent, and an execution/adaptation graph stored with experiment history and accepted project memory.

## Configure a service

Run MetaInfer separately according to its [upstream instructions](https://github.com/HuangPuStar/MetaInfer). The adapter is pinned to commit `b3f6505a11ab704ee1cfb68e9c1b2c13c95ac890` for release evidence, but the stock server at that commit has no UC stop/workspace contract. UC therefore allows schema discovery while failing closed before any mutating POST. A compatible deployment must expose `uc-metainfer/1`, a stable `backend_id`, the pinned `revision`, workspace probes and quiescence receipts scoped to every writer -- the implementable endpoint-by-endpoint contract is [metainfer-service-contract.md](metainfer-service-contract.md), extracted from the code that consumes it. Set these on the UC planner and eligible workers:

```dotenv
UC_METAINFER_URL=http://metainfer-host:8765
UC_METAINFER_REVISION=b3f6505a11ab704ee1cfb68e9c1b2c13c95ac890
UC_METAINFER_BACKEND_ID=metainfer-gpu-a
```

Workers always advertise local `inference_benchmark`. Remote capabilities require successful live schema probes: `inference_infra` plus the supported `model_porting`, `kernel_optimization`, `runtime_optimization` or `trace_analysis` operation. Startup and heartbeats refresh these probes and withdraw unavailable operations. A URL alone does not prove readiness or GPU availability. Explicit agent choices win over automatic routing. Routing requires inference context and engineering intent; ordinary frontend work and references to a Linux kernel retain their coding adapter.

UC and MetaInfer must see an assigned worktree at the **same absolute path**, including on remote GPU workers. Configure the service's shared filesystem/NFS mounts accordingly. A UC container's `/workspace` volume is not automatically shared with an external service. Model weights may live outside the code worktree. MetaInfer `worker_nodes` is passed as a plugin form parameter for plugins that expose it; GPU allocation and execution remain owned by MetaInfer's cluster. UC does not infer GPU availability from a local CUDA install.

The UC adapter needs no MetaInfer package, source checkout or GPU library in its Worker image. MetaInfer credentials and coding-agent backend configuration belong to that service. The UC runner receives the existing sandbox environment allowlist and needs only the service URL and experiment configuration.

## Submit an experiment

The Dashboard submit API and NATS `uc.task.submit` accept `agent_config`. Existing gRPC workflow nodes can select the registered `metainfer` adapter (alias `inference-infra`) and provide the same settings through agent configuration. The Python API is also available:

```python
task = await orchestrator.submit_task(
    "Optimize the decode kernel and verify its performance",
    project_id="inference-repo",
    agent_config={
        "inference_task": {
            "task_type": "optimize_kernel",
            "repository": "/shared/original-checkout",
            "framework": "sglang",
            "model": "Qwen",
            "hardware": "A800",
            "objective": "minimize TPOT",
            "constraints": {"memory_gb": 80, "numerical_error": 0.01},
            "parameters": {
                "kernel_file_path": "kernels/decode.py",
                "max_iterations": "10",
                "worker_nodes": "gpu-a",
            },
        },
        "benchmark": {
            "command": ["python", "/shared/harness/decode_benchmark.py"],
            "workload_id": "qwen-a800-fp8-tp8-bs1-seq512-seed42",
            "protected_paths": ["/shared/harness/decode_benchmark.py"],
            "timeout_seconds": 300,
        },
        "oracle": {"objective": "tpot_ms", "min_improvement_pct": 2},
        "apply_command": ["python", "/shared/harness/import_verified_kernel.py"],
        "max_iterations": 3,
    },
)
```

The harness/import paths above represent scripts supplied by your experiment, rather than bundled model-specific benchmarks. Explicit `inference_task` submissions become a single domain node without a second global planning pass. The adapter replaces its `repository` with the UC-assigned worktree, redirects model-porting `target_framework_dir` to that worktree, and rebases an in-repository `kernel_file_path`. It refuses kernel paths outside that tree.

An optional `UC_INFERENCE_TASK_JSON` contains the same `agent_config` object as worker/planner defaults. This enables natural-language routing with preconfigured weight paths and benchmark commands. Prose does not supply those paths: incomplete task parameters fail before starting a GPU job. Use explicit `agent_config` for different experiments on a shared worker.

## Operations and upstream mappings

| UC operation | Default upstream plugin | Required experiment input |
|---|---|---|
| `port_model` | `port-model` | `model_params_path`; assigned target framework path is injected |
| `optimize_kernel` | `evolve-kernel` | `kernel_file_path` |
| `optimize_runtime` | `gen-infer-framework` | `target_model`, `target_hardware` |
| `analyze_trace` | `sglang-trace-analyze` | `model_path`, `version`, `batch_sizes`, `gpu_model`; numeric defaults come from the live schema |
| `benchmark` | UC BenchmarkRunner | Local benchmark configuration; no service is needed |

Runtime's default plugin **generates a purpose-built runtime**. For optimization of an existing vLLM/SGLang runtime, configure an appropriate service plugin with `inference_task.upstream_type` and its actual `parameters`. An unavailable plugin fails explicitly; it never silently runs another task type. Kernel/runtime generation stores outputs in a service workspace rather than necessarily changing the input repo. Supply an `apply_command` to import the selected output into the assigned worktree. Its environment includes `UC_METAINFER_WORKSPACE` and `UC_METAINFER_TASK_ID`. UC never guesses which generated file is the winning kernel or automatically executes a returned shell string.

Programmatic tools are `MetaInferAdapter.port_model`, `optimize_kernel`, `optimize_runtime`, `analyze_trace` and `benchmark`, with typed `MetaInferTask` / `MetaInferResult` contracts exported from `ultimate_coders.inference`. Direct service-tool completion only means the backend finished. Patch-producing runs should use `OptimizationWorkflow` to obtain UC acceptance.

## Benchmark and Oracle protocol

Benchmark commands are argv arrays executed without a shell. The final stdout line must be a JSON object:

```json
{
  "workload_id": "qwen-a800-fp8-tp8-bs1-seq512-seed42",
  "compile_success": true,
  "correctness": true,
  "numerical_error": 0.0001,
  "metrics": {"tpot_ms": 37.1, "peak_memory_gb": 62.4},
  "evidence": ["/shared/results/correctness.json"]
}
```

Supported metrics are `latency_ms`, `ttft_ms`, `tpot_ms`, `throughput_tokens_s` and `peak_memory_gb`. Times are milliseconds, throughput is tokens/second and memory is GB. Encode model, hardware, precision, parallelism, batch, sequence lengths and seed into the workload identity. Booleans, numbers and workload identity are validated; missing, negative, infinite or NaN measurements cannot pass.

Baseline and candidate use the same fixed command and workload. `baseline_command` can invoke a known-correct reference implementation when porting a model that the target cannot yet run. Optional `compile_command` runs first; `profile_command` captures profiling text in the measured result. Protect the harness and reference/correctness data with `protected_paths`. UC fingerprints these files before the first run and verifies they remain unchanged. Keep independent oracle scripts outside candidate-editable code where practical. Harnesses must return real correctness/compile assertions; a process exit of zero alone does not imply either.

`OraclePolicy` selects an objective, `min_improvement_pct` and a tolerance `max_regression_pct` (both default to zero). Every baseline metric must also be present in the candidate; regressions beyond tolerance reject it. The objective must improve, so an unchanged input kernel does not count as optimization. Memory and numerical-error limits require those measurements. Task constraints `memory_gb` and `numerical_error` become Oracle ceilings and cannot be relaxed by an Oracle override. Correctness and compilation are always required.

Mutating domain tasks and steps require a real, exclusively leased Git worktree, including when file constraints are empty. Allocation failure stops execution. Each iteration snapshots tracked/nonignored code outside the candidate tree and compares against the best accepted measurement. Rejected candidates restore code and remove newly added nonignored files. Accepted code remains unstaged until UC commits it and completes local merge/optional remote delivery. Commit, merge or push failure preserves the worktree and branch. Backend commits, initialized submodules and snapshots over 100 MiB are refused. Ignored caches/weights and external files are outside rollback.

## Evidence, cancellation and deployment

Artifacts include a complete JSON report, benchmark/Oracle iteration history, a versioned execution/adaptation graph and an accepted patch including newly created source files. The graph distinguishes declared model/runtime/dispatch/kernel/hardware relationships from measured evidence; the service's phase graph is retained separately. Accepted results are written to UC project Memory with inference/benchmark/graph tags. Rejected candidate evidence remains in local experiment history and feeds the next hypothesis.

Experiment IDs derive from graph/node/attempt/workflow-step identity, so restarts reuse the same artifact directory. Native artifacts default to `.uc-inference-artifacts` beside the UC project; `UC_INFERENCE_ARTIFACT_DIR` overrides the root. Compose shares `worker_inference_artifacts` at `/artifacts/inference`, read-only in the Dashboard API. Cross-host deployments must share or replicate these artifacts: metadata alone does not make remote files available.

Remote operation intent is committed before POST, and the returned remote ID is committed before polling. A known ID resumes without another submission. A lost response leaves `submission_unknown`; an unconfirmed stop leaves `cleanup_pending`. Both are nonretryable and preserve code, checkpoints, leases and concurrency reservations. No rollback or release runs while a remote writer may remain alive. Deadlines survive restart. Confirmed completion or termination releases the shared backend slot, including recovery after a crash between completion and slot release. UC gives the runner eight seconds to stop remote work before forcing local process-group/Job Object termination. A hard OS kill cannot guarantee HTTP cleanup; inspect the remote job before manually reconciling its quarantined workspace.

The automated suite exercises the UC HTTP contract, rollback in temporary git repos, Oracle failures, routing/config forwarding, and a real UC sandbox subprocess. The fixture service is deliberately separate from a production MetaInfer deployment. Real correctness and GPU speedups need a configured contract-compatible service, shared code/weights and hardware; no production performance improvement is claimed by these CPU contract tests.

## Durable execution and evidence

Compose uses the same `UC_DATABASE_URL` for Gateway, coordinator, workers and Dashboard API. Python runtime records use PostgreSQL table `uc_runtime_records`, with row-locked mutations and the Gateway migration advisory lock. With no URL, native single-host runs use SQLite under `UC_RUNTIME_STATE_DIR` (default `.uc/runtime`). Set that directory outside candidate worktrees and consistently across the UC processes. A configured database failure never silently falls back to local state. SQLite does not coordinate separate hosts.

Operation records bind remote IDs to UC identity, backend, task type, configuration digest and deadline. `UC_METAINFER_MAX_CONCURRENCY` defaults to one per shared backend. `UC_METAINFER_TASK_TYPES` is a JSON map from UC operation names to actual service plugin IDs; both probing and execution honor it. All hosts targeting one backend must use the same database and concurrency limit. An orphan on another host is not assumed dead from a local PID check; uncertain ownership requires operator reconciliation.

The Worker persists an immutable terminal outbox before acknowledging JetStream dispatch. A separate replay loop resends results without rerunning code. Active duplicate dispatches defer to the attempt owner. The coordinator recovers current nodes/attempts from the Gateway's read-only `uc.task.gateway-snapshot.request` and fences obsolete outcomes. It confirms only after the Gateway durably writes the complete snapshot and events; the Gateway also confirms the Worker partial result. Memory-only Gateway fallback leaves results pending in the outbox. PostgreSQL task persistence remains distinct from optional `UC_GRAPH_SHADOW` execution-graph writes.

Benchmarks default to one warmup and three measured repetitions (`repetitions` must be 3–100). Timing/throughput aggregates use medians; peak memory and numerical error use maxima. Missing numerical-error evidence in any repetition remains missing. Results retain sample values, dispersion, environment identity and protected-harness fingerprints. Declare model/hardware/precision/parallelism/seed in `benchmark.environment` as well as `workload_id`. Oracle rejects absent statistical evidence, changed environment, dispersion above 5%, and improvement at or below twice the combined baseline/candidate dispersion. Configure `max_dispersion_pct` and `noise_multiplier` explicitly for a different measured policy.

Atomic checkpoints preserve the original baseline, best accepted candidate, harness hashes, rollback transaction and iteration history. `manifest.json` records task/policy/code identity and artifact SHA-256/size. Experiment acceptance and code delivery are separate: inspect `delivery.status`, `commit_sha` and optional `push_status` before treating an accepted candidate as delivered. Phase events use the existing task progress stream.

Authenticated Dashboard API routes:

- `GET /dashboard/api/experiments?task_id=<graph-id>` lists state, UC identity and delivery metadata.
- `GET /dashboard/api/experiments/<experiment-id>/artifacts/<name>` serves `report.json`, `benchmarks.json`, `graph.json` or `accepted.patch` with an integrity ETag. Tampering returns 409; missing host artifacts return 404; files over 16 MiB return 413. Internal transaction checkpoints are not downloadable.

For quarantined operations, inspect the experiment/remote-job record and remote service status first. Stop or establish completion of the remote writer, review preserved code and the original benchmark evidence, then reconcile the lease/operation with the responsible UC operator. Do not clear a slot, delete a worktree or resubmit merely because a heartbeat expired. For a Gateway event-persistence failure, fix storage and restart the Gateway to clear its failed event barrier; the durable outbox continues replaying. Delivered outcomes are compacted to replay-safe tombstones after `UC_OUTBOX_RETENTION_SECONDS`.

Version-checked operator recovery is available from the worker environment:

```powershell
python -m ultimate_coders.inference.reconcile inspect <operation-id>
python -m ultimate_coders.inference.reconcile attach <operation-id> --remote-id <id> --expected-version <n> --actor <operator>
python -m ultimate_coders.inference.reconcile confirm_stop <operation-id> --expected-version <n> --actor <operator>
python -m ultimate_coders.inference.reconcile recover_workspace <lease-key> --expected-version <n> --actor <operator>
```

Regression and deployment evidence for this repair is recorded in [MetaInfer reliability verification](metainfer-reliability-verification.md).
