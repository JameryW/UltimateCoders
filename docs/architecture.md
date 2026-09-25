# UltimateCoders Architecture

UltimateCoders is a distributed AI Coding platform. The Web Dashboard is its default interface; an OMP extension remains available for native terminal use. The product is organized as a control plane, execution plane, knowledge plane, and observable event plane.

![Product capabilities](screenshots/product-capabilities.png)

![Product use cases](screenshots/product-scenarios.png)

![Execution architecture](screenshots/execution-architecture.png)

## System layers

| Layer | Main components | Responsibility |
| --- | --- | --- |
| Interaction | Web Dashboard, gRPC-Web; optional native OMP extension | Accept natural-language intent, expose controls and task status |
| Control plane | Rust Gateway, TaskService, EngineService, WorkerService | Persist task state, build DAG waves, match workers, broadcast events |
| Execution plane | Python Worker/Sandbox, NATS, Grok Build, Claude Code, Codex | Execute isolated subtasks, report progress, return artifacts and status |
| Knowledge plane | Hybrid index, TiKV, Qdrant, PostgreSQL, MemoryBridge | Provide text/semantic/AST context and short/long-term memory |
| Event plane | TaskEvent broadcast, WatchTask, Dashboard API | Deliver one consistent stream to Dashboard and API consumers |

The React dashboard has two routes:

- `/` is the product overview. It explains the execution chain and reads the live Gateway snapshot through gRPC-Web.
- The overview also renders a five-layer Product Map: command surface, control plane, execution plane, knowledge plane, and event plane. This is the user-facing summary of the same boundaries described below.
- `#/dashboard` is the operations surface. It submits tasks through TaskService and displays task, worker, scheduler, search, and event data.

## End-to-end task flow

1. A user submits a natural-language task from the Dashboard or optional native OMP extension.
2. `TaskService.SubmitTask` creates the task and the orchestrator decomposes it into subtasks.
3. The scheduler groups subtasks into dependency-safe DAG waves and records checkpoints.
4. `WorkerService` matches each subtask to a worker by declared capability and current load.
5. NATS dispatches the subtask to the selected worker; the worker runs the configured coding agent in its sandbox.
6. Search and Memory provide repository context, prior decisions, and structured task metadata.
7. Progress events are broadcast through `WatchTask` to Dashboard and API consumers.
8. Completed, failed, paused, or cancelled states remain queryable and recoverable.

## Why this architecture matters

- **Observable by default**: task state, worker health, and subtask progress use shared event paths.
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
