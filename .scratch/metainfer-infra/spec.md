# MetaInfer inference infrastructure integration

## Goal

Implement the user's supplied four-step plan as an optional inference domain in UltimateCoders. UC owns planning, worktree isolation, result acceptance and shared memory. An external MetaInfer service supplies specialized execution; no source vendoring or WebUI integration.

## Decisions

- The public HTTP task/form API is pinned for compatibility research to HuangPuStar/MetaInfer `b3f6505a11ab704ee1cfb68e9c1b2c13c95ac890`.
- Expose five typed operations: model porting, kernel optimization, runtime optimization, trace analysis and benchmarking. Default upstream types are `port-model`, `evolve-kernel`, `gen-infer-framework` and `sglang-trace-analyze`; runtime generation is explicitly documented and may be replaced by a configured plugin.
- Query live form schemas before submission; pass explicit backend parameters, never invent weight/kernel paths from prose. Shared filesystem paths are required for patch-producing service runs.
- A subprocess adapter preserves UC sandbox timeout and cancellation boundaries. HTTP jobs are killed on cancellation/deadline; uncertain remote termination is a failure.
- Benchmark correctness, compilation, workload identity and finite metrics gate acceptance. Missing measurements cannot pass. Candidate regressions roll back only changes produced in a dedicated clean git worktree.
- Infrastructure routing is opt-in with `UC_METAINFER_URL`. Explicit task configuration and agent choices win. Ordinary coding remains on its existing adapter.
- Persist benchmark history and a typed execution/adaptation graph alongside result artifacts; publish accepted evidence to project-scoped UC memory.

## Test seams

Public Oracle evaluation; BenchmarkRunner command protocol; MetaInferAdapter HTTP lifecycle using an injected HTTP transport; OptimizationWorkflow against a temporary git repository; InfraAgent routing and Worker/Orchestrator submission; sandbox adapter final envelope and environment filtering.

## Out of scope

Vendoring MetaInfer, replacing UC's global planner, provisioning GPUs, deploying a live MetaInfer cluster, promising measured GPU improvements without access to real hardware.
