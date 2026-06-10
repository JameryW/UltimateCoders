# Task Scheduling with Night-time Orchestration

## Goal

为 UltimateCoders 增加任务调度能力，支持夜间任务编排，让系统可以在低负载时段自动执行批量任务（如代码审查、索引重建、知识库整理等），最大化资源利用率。

## What I already know

### 现有架构
* **Orchestrator-Worker 模式**：`Orchestrator` 接收任务 → LLM 分解 → 分配给 `Worker` → 聚合结果
* **任务类型**：`Task` (顶层) + `Subtask` (子任务)，状态机 `TaskStatus` / `SubtaskStatus`
* **容错机制**：Event Sourcing (NATS JetStream) + Checkpoint/Resume + Rate Limiter + Circuit Breaker
* **通信**：gRPC (同步) + NATS JetStream (异步事件流)
* **存储**：TiKV (短期 Memory) + Qdrant (长期 Memory) + PostgreSQL (元数据)

### 用户需求
* "增加任务调度能力，支持夜间任务编排"
* 隐含需求：定时触发、批量任务、低负载时段执行

## Assumptions (temporary)

* 夜间任务主要是后台维护类任务（索引重建、代码审查、知识库整理）
* 调度器需要与现有 Orchestrator 集成
* 可能需要持久化调度配置（PostgreSQL）
* 可能需要 NATS JetStream 的延迟消息功能

## Open Questions

* **Blocking**: 调度器的触发方式？(cron 表达式 / 固定时间窗口 / 动态负载感知)
* **Blocking**: 夜间任务的具体类型？(用户提交的延迟任务 / 系统维护任务 / 两者都支持)
* **Preference**: 调度配置的管理方式？(配置文件 / API / CLI / Web UI)
* **Preference**: 任务优先级机制？(夜间任务 vs 实时任务的资源竞争)

## Requirements (evolving)

* 支持定时任务调度（cron 或类似机制）
* 支持夜间时间窗口配置
* 与现有 Orchestrator 集成
* 调度状态持久化（重启后恢复）
* 任务执行历史记录

## Acceptance Criteria (evolving)

* [ ] 可配置夜间时间窗口
* [ ] 可提交定时任务（指定执行时间或 cron 表达式）
* [ ] 调度器在指定时间自动触发任务
* [ ] 任务执行状态可查询
* [ ] 系统重启后调度状态恢复

## Definition of Done

* Tests added/updated (Rust + Python)
* Lint / typecheck / CI green
* Docs/architecture.md 更新
* 调度配置示例文档

## Out of Scope (explicit)

* Web UI 调度管理界面
* 分布式调度（多节点协调）
* 复杂的 DAG 调度（依赖任务编排）

## Technical Notes

### 相关文件
* `python/ultimate_coders/agent/orchestrator.py` — Orchestrator 实现
* `python/ultimate_coders/agent/types.py` — Task/Subtask 类型定义
* `python/ultimate_coders/agent/rate_limiter.py` — Rate Limiter (Token Bucket)
* `crates/uc-types/src/engine.rs` — EngineApi trait
* `crates/uc-engine/src/local.rs` — LocalEngine 实现

### 技术选项
* **Rust 侧**: `tokio-cron-scheduler` / `tokio::time::interval` + 持久化
* **Python 侧**: `APScheduler` / `Celery Beat` / 自定义调度器
* **存储**: PostgreSQL (调度配置 + 执行历史) + TiKV (运行时状态)
* **通信**: NATS JetStream (延迟消息 / 定时触发)

### 约束
* 需要与现有 async/await 架构兼容
* 需要支持 PyO3 FFI (如果调度逻辑在 Rust 侧)
* 需要考虑时区处理
