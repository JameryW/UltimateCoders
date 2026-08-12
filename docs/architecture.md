# UltimateCoders Architecture

UltimateCoders is a distributed AI Coding platform. The TUI is only one entry point; the product is organized as a control plane, execution plane, knowledge plane, and observable event plane.

![Product capabilities](screenshots/product-capabilities.png)

![Product use cases](screenshots/product-scenarios.png)

![Execution architecture](screenshots/execution-architecture.png)

## System layers

| Layer | Main components | Responsibility |
| --- | --- | --- |
| Interaction | OMP extension, OMP PTY, React TUI, gRPC-Web | Accept natural-language intent, expose controls, stream terminal output |
| Control plane | Rust Gateway, TaskService, EngineService, WorkerService | Persist task state, build DAG waves, match workers, broadcast events |
| Execution plane | Python Worker/Sandbox, NATS, Grok Build, Claude Code, Codex | Execute isolated subtasks, report progress, return artifacts and status |
| Knowledge plane | Hybrid index, TiKV, Qdrant, PostgreSQL, MemoryBridge | Provide text/semantic/AST context and short/long-term memory |
| Event plane | TaskEvent broadcast, WatchTask, dashboard/TUI API | Deliver one consistent stream to OMP, TUI and API consumers |

The React dashboard has two complementary surfaces:

- `/` is the product overview. It explains the OMP ↔ UC execution loop, exposes the shared Command Deck (`run`, `status`, `tasks`, `workers`, `search`, `logs`), and reads the live Gateway snapshot through the existing gRPC-Web hook.
- The overview also renders a five-layer Product Map: command surface, control plane, execution plane, knowledge plane, and event plane. This is the user-facing summary of the same boundaries described below.
- `#/tui` is the real terminal surface. It connects the command bar to TaskService over gRPC-Web and the OMP PTY over `/ws/tui`; the overview never replaces this execution path.

## End-to-end task flow

1. A user submits a natural-language task from OMP or the TUI command bar.
2. `TaskService.SubmitTask` creates the task and the orchestrator decomposes it into subtasks.
3. The scheduler groups subtasks into dependency-safe DAG waves and records checkpoints.
4. `WorkerService` matches each subtask to a worker by declared capability and current load.
5. NATS dispatches the subtask to the selected worker; the worker runs the configured coding agent in its sandbox.
6. Search and Memory provide repository context, prior decisions, and structured task metadata.
7. Progress and terminal events are broadcast through `WatchTask` to OMP, the TUI, and API consumers.
8. Completed, failed, paused, or cancelled states remain queryable and recoverable.

## Why this architecture matters

- **Observable by default**: task state, worker health, subtask progress, and terminal output use shared event paths.
- **Distributed without losing local fallback**: NATS workers can scale out, while the Gateway can execute locally when external services are unavailable.
- **Model-agnostic execution**: the worker contract separates orchestration from the coding agent, allowing Grok Build, Claude Code, or Codex.
- **Context-aware coding**: Text + Semantic + AST retrieval and layered Memory reduce repeated discovery across repositories and tasks.
- **Recoverable control**: pause/resume/cancel, checkpoints, heartbeats, and task event replay protect long-running work.

## Representative use cases

| Scenario | How the platform helps | Outcome |
| --- | --- | --- |
| Large-repo modernization | Index multiple repositories, retrieve Text/Semantic/AST context, and keep decisions in Memory | Safer cross-repo changes with less rediscovery |
| Parallel delivery | Split a broad request into dependency-safe DAG waves and dispatch by worker capability | Higher throughput with visible ownership |
| Incident and debugging | Combine code search, task events, and prior context; recover from checkpoints | Faster diagnosis with controlled recovery |
| Local to cluster | Start with local fallback and add Docker/NATS workers without changing service contracts | Elastic capacity without rewriting workflows |

## Deployment shapes

```text
Local:        OMP + Gateway + local worker
Standalone:   OMP → containerized Rust Gateway → optional storage containers
Distributed:  OMP → Gateway → NATS → N Python Workers
Knowledge:    Gateway/Workers ↔ TiKV + Qdrant + PostgreSQL
```

See the root README for the runnable commands in `run-omp.sh`, `run-gateway.sh`, `run-cluster.sh`, and Docker Compose.
