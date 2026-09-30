# Inference infrastructure with MetaInfer

UltimateCoders retains the global planner, distributed dispatch, worktree ownership and result acceptance. MetaInfer is an optional external execution service. The integration adds typed tools, a shared BenchmarkRunner and Oracle, an InferenceInfraAgent, and an execution/adaptation graph stored with experiment history and accepted project memory.

## Configure a service

Run MetaInfer separately according to its [upstream instructions](https://github.com/HuangPuStar/MetaInfer). Compatibility was checked against commit `b3f6505a11ab704ee1cfb68e9c1b2c13c95ac890`; each submission discovers the live plugin form schema. Set these on the UC planner and eligible workers:

```dotenv
UC_METAINFER_URL=http://metainfer-host:8765
```

Leave the variable empty to retain ordinary coding behavior. An enabled worker advertises `inference_infra`, `metainfer`, `model_porting`, `kernel_optimization`, `runtime_optimization`, `trace_analysis`, `benchmarking` and `hardware_adaptation`. Service availability and plugin schemas are checked during execution. Explicit agent choices win over automatic routing. Routing requires both inference context and optimization/analysis intent; ordinary frontend work and references to a Linux kernel do not select this backend.

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

Optimization requires an exclusively owned, initially clean git worktree. Each iteration snapshots tracked/nonignored code, requests a candidate, checks the immutable harness, compiles/benchmarks/profiles, and compares against the best accepted measurement. A rejected or failed candidate restores its code and removes its newly added nonignored files. Accepted code stays unstaged; UC's existing worktree release/merge arbitration owns commits. Backend commits, initialized submodules and snapshots over 100 MiB are refused. Ignored caches/weights and external files are outside code rollback. Do not execute concurrent writers in the assigned worktree.

## Evidence, cancellation and deployment

Artifacts include a complete JSON report, benchmark/Oracle iteration history, a versioned execution/adaptation graph and an accepted patch including newly created source files. The graph distinguishes declared model/runtime/dispatch/kernel/hardware relationships from measured evidence; the service's phase graph is retained separately. Accepted results are written to UC project Memory with inference/benchmark/graph tags. Rejected candidate evidence remains in local experiment history and feeds the next hypothesis.

Native workers write unique experiment directories under `.uc-inference-artifacts` beside the repository, or under `UC_INFERENCE_ARTIFACT_DIR`. Compose mounts a persistent `worker_inference_artifacts` volume at `/artifacts/inference`. These are worker-local artifact paths; archive them through your normal shared artifact storage when using multiple hosts. The benchmark/graph content in shared Memory remains available through the gateway.

Deadlines and cooperative cancellation send MetaInfer's `kill` control action and await confirmation. An unconfirmed kill reports the remote task ID for manual service inspection. UC gives the runner eight seconds to stop remote work and roll back before forcing local termination. Commands and their descendants are contained with POSIX process groups and Windows Job Objects; registered nested POSIX groups are also stopped during forced cancellation. The runner handles SIGTERM/SIGINT on Unix. A hard OS kill or an expired cleanup grace cannot guarantee HTTP cleanup or rollback; inspect remote jobs after forced worker termination. The upstream submission API has no idempotency token: UC does not retry submissions, and a lost POST response can leave a job whose ID was never returned. Configure service-side operational limits for that case.

The automated suite exercises the real HTTP contract, rollback in temporary git repos, Oracle failures, routing/config forwarding, and a real UC sandbox subprocess. Real correctness and GPU speedups need a configured service, shared code/weights and hardware; no production performance improvement is claimed by these CPU contract tests.
