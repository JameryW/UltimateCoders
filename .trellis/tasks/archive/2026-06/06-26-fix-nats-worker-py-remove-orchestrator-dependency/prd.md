# Fix nats_worker.py + local_worker.py — Remove Orchestrator Dependency

## Goal

修复 `nats_worker.py` 和 `local_worker.py` 对已删除 `Orchestrator` 模块的依赖，使 NATS worker 集群和 LocalWorkerBridge 都能正常启动。

## What I already know

* Python `Orchestrator` 在 PR #161 中被移除，但 `nats_worker.py` 和 `local_worker.py` 仍 import 它
* `from ultimate_coders.agent.orchestrator import Orchestrator` 直接 ModuleNotFoundError
* `local_worker.py` 也 broken — LocalWorkerBridge spawn 后立即 crash
* gRPC server 有 local decomposition fallback，所以 submit 不会完全挂，但走不了 Worker 执行路径
* 两个文件对 Orchestrator 的使用深度不同：
  - `local_worker.py`：中等（submit_task, assign_subtask, handle_subtask_result, get_task_status, select_next_subtask）
  - `nats_worker.py`：深度（上述 + pause/resume/cancel, conflict_detector, scheduler, event_emitter, tasks/workers dict, heartbeat, dashboard snapshot）

## Requirements

* `local_worker.py` 能正常 import 和启动
* `nats_worker.py` 能正常 import 和启动
* 保持现有 NATS 协议兼容（Rust gRPC server 不改）
* Worker mode (`--mode worker`) 只需执行 subtask，不需要 Orchestrator

## Technical Approach

创建 `MinimalOrchestrator` — 只包含 local_worker/nats_worker 实际用到的方法：
- `submit_task()` — 用简单分解（split by newlines）或 LLM 分解
- `assign_subtask()` / `handle_subtask_result()` / `get_task_status()` / `select_next_subtask()`
- `register_worker()` / `refresh_heartbeat()`
- `pause_task_local()` / `resume_task_local()` / `cancel_task()`
- `event_emitter` / `tasks` / `workers` / `config` / `conflict_detector` / `scheduler`

放在 `python/ultimate_coders/agent/orchestrator.py`（恢复文件但用精简实现），这样两个 worker 不需要改 import。

## Acceptance Criteria

* [ ] `python -m ultimate_coders.local_worker` 不报 import error
* [ ] `python -m ultimate_coders.nats_worker` 不报 import error
* [ ] `./run-cluster.sh --no-omp` workers 正常启动并连接 NATS
* [ ] `./run-omp.sh` 中 LocalWorkerBridge spawn 后 worker 正常响应 ping

## Out of Scope

* 完整 Orchestrator 功能（LLM 分解、调度器、dashboard）
* Rust 侧改动
