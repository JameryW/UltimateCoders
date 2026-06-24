# @ultimate-coders/uc-orchestrator

UltimateCoders Orchestrator extension for oh-my-pi.

## Commands

- `/uc submit <description>` — Submit a task for orchestration
- `/uc status [task-id]` — Check task status
- `/uc help` — Show help

## Architecture

UC Orchestrator uses omp's agent runtime as the execution layer:

1. **Task decomposition** — omp `decomposer` agent breaks tasks into subtasks
2. **DAG scheduling** — Topological sort with wave-based parallel execution
3. **Worker execution** — omp `worker` agents execute subtasks via `runSubprocess`
4. **Supervision** — omp `supervisor` agent reviews results
5. **Memory bridge** — UC layered memory (TiKV/Qdrant/PostgreSQL) via gRPC

## Development

```bash
# From the omp workspace root
bun install
omp --extension ./packages/uc-orchestrator
```
