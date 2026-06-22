# 多 Worker 分布式架构

## Goal

Rust gRPC server 作为集中调度器，通过 NATS 队列竞争将 ready subtask 分发给多个 Worker 进程并发执行。Worker 完成后汇报结果，Rust 推进下一轮依赖调度。文件冲突通过 EditIntent 检测，Worker 失败后自动重分配。

## Architecture

```
TUI/Dashboard
    ↓ gRPC (SubmitTask)
Rust gRPC Server (调度器)
    ├─ 调 Python 分解 (LocalWorkerBridge/uc.task.submit)
    ├─ 依赖解析 → 发布 ready subtask → NATS uc.subtask.execute
    ├─ 监听 uc.task.update → 更新 TaskStore → 推进下一轮 ready subtask
    └─ 监听 uc.heartbeat → 追踪 Worker 存活

NATS uc.subtask.execute (queue group: "workers")
    ├─→ NatsWorker-A (竞争消费, 执行沙箱)
    ├─→ NatsWorker-B (竞争消费, 执行沙箱)
    └─→ NatsWorker-C (竞争消费, 执行沙箱)

Worker 完成 → uc.task.update → Rust 推进
Worker 崩溃 → 心跳超时 → Rust 重分配 subtask
```

## Decisions (ADR-lite)

### D1: 调度模式 — NATS 队列竞争

**Context**: 多 Worker 如何分配 subtask
**Decision**: NATS queue group。Rust 发布 `uc.subtask.execute`，Worker 加入同一 queue group 竞争消费，NATS 保证每个 subtask 只被一个 Worker 收到。
**Consequences**: 去中心化，无需 Leader 选举；但 Worker 不感知全局依赖，Rust 侧必须确保只发布依赖已满足的 subtask。

### D2: 调度器位置 — Rust gRPC server

**Context**: 谁负责 LLM 分解和 subtask 调度
**Decision**: Rust 侧调度。收到 SubmitTask 后通过 LocalWorkerBridge 调 Python 分解，分解完成后 Rust 侧做依赖解析，发布 ready subtask 到 NATS。收到 uc.task.update 后推进下一轮。
**Consequences**: Rust 成为调度中枢；Python Worker 只执行不调度，逻辑简化。

### D3: Worker 进程 — 复用 NatsWorker

**Context**: Worker 进程如何组织
**Decision**: 复用现有 NatsWorker，增加模式切换。`--mode worker` 启动为纯 Worker 模式（只消费 uc.subtask.execute）；默认模式保持现有行为（兼容单 Worker 场景）。
**Consequences**: 不新增进程类型；但 NatsWorker 代码变复杂，需要模式分支。

## Requirements

1. **Rust 调度器**：分解完成后，按依赖拓扑发布 ready subtask 到 `uc.subtask.execute`
2. **Rust 推进**：收到 `uc.task.update` 后，检查是否有新 subtask 变为 ready，继续发布
3. **NATS 队列竞争**：Worker 以 queue group 消费 `uc.subtask.execute`，NATS 保证每个 subtask 只被一个 Worker 执行
4. **文件冲突检测**：Worker 执行前通过 ConflictDetector 检查 EditIntent（基于 subtask 的 file_constraints），冲突则等待
5. **Worker 失败重分配**：心跳超时 → Rust 标记 subtask 失败 → 重新发布给其他 Worker（最多重试 N 次）
6. **多 Worker 状态可视化**：Dashboard/TUI 显示所有 Worker 的 ID、负载、状态
7. **向后兼容**：不破坏现有单 Worker 流程（无 `--mode worker` 时行为不变）

## Acceptance Criteria

* [ ] 可以启动 2+ NatsWorker (--mode worker) 并注册到 NATS queue group
* [ ] 2 个无依赖的 subtask 被不同 Worker 并发执行
* [ ] 有依赖的 subtask 等待依赖完成后才被 Rust 发布
* [ ] 同一文件约束的 subtask 不并发执行（ConflictDetector）
* [ ] Worker 崩溃后其 subtask 在超时后被重分配给其他 Worker
* [ ] Dashboard 显示多 Worker 的 ID、负载、心跳状态
* [ ] 无 --mode worker 时，现有 NatsWorker 行为不变

## Definition of Done

* Tests added/updated (Rust unit + Python unit + 集成)
* Lint / typecheck / CI green
* NATS bridge spec 更新
* 不破坏现有单 Worker 流程

## Out of Scope

* 远程机器上的 Worker（需要 gRPC 远程调度，单独规划）
* Worker 自动扩缩容
* Subtask 结果的分布式合并（CRDT 等）
* 跨 Task 的 Worker 共享/隔离
* 单 Worker 进程内并发执行多个 subtask（方案 A）

## Technical Notes

### NATS 新 Subject

| Subject | Direction | Purpose |
|---------|-----------|---------|
| `uc.subtask.execute` | Rust → Worker (queue group) | Ready subtask 分发 |

### uc.subtask.execute Payload

```json
{
  "message_id": "task-abc:execute:st-1:1700000000",
  "task_id": "task-abc",
  "subtask_id": "st-1",
  "description": "Fix the authentication bug in auth.rs",
  "expected_output": "auth.rs compiles and tests pass",
  "file_constraints": ["src/auth.rs", "src/auth_test.rs"],
  "timeout_seconds": 600
}
```

### 关键文件

* `crates/uc-grpc/src/server.rs` — 调度逻辑：分解完成后发布 subtask，收到 update 后推进
* `crates/uc-engine/src/task_store.rs` — 依赖解析 + ready subtask 计算
* `python/ultimate_coders/nats_worker.py` — Worker 模式：queue group 消费 + 执行 + 汇报
* `python/ultimate_coders/agent/worker.py` — execute_subtask（可能需要适配无 Orchestrator 的场景）
* `python/ultimate_coders/agent/conflict_detector.py` — EditIntent 检查
* `crates/uc-types/src/agent.rs` — 新增 subtask execute payload 类型

### 实现计划（子任务拆分）

**子任务 1: Rust 调度器 — 依赖解析 + subtask 发布**
- TaskStore 新增 `get_ready_subtasks()` 方法
- submit_task 完成分解后，发布 ready subtask 到 `uc.subtask.execute`
- NATS subscriber 收到 `uc.task.update` 后，推进下一轮 ready subtask
- 新增 NATS subject `uc.subtask.execute` + payload 类型

**子任务 2: NatsWorker Worker 模式**
- `--mode worker` 参数解析
- 订阅 `uc.subtask.execute` (queue group: "workers")
- 执行 subtask → 发布 `uc.task.update` + `uc.task.event`
- 启动时注册 Worker → 发布心跳

**子任务 3: 文件冲突检测**
- Worker 收到 subtask execute 时，检查 file_constraints
- 如果文件有活跃 EditIntent → 延迟执行或拒绝（NATS re-delivery）
- 执行前 declare_edit_intent，完成后 release_edit_intent

**子任务 4: Worker 失败重分配**
- Rust 心跳监控检测 Worker 离线
- 标记其 subtask 为 FAILED (recoverable=true)
- 重新发布 ready subtask 给其他 Worker
- 重试次数限制（避免无限循环）

**子任务 5: Dashboard 多 Worker 可视化**
- Worker 注册/心跳 → Rust TaskStore 追踪 WorkerInfo
- Dashboard SSE snapshot 包含多 Worker 数据
- TUI 显示 Worker 列表
