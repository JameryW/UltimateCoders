# Task Scheduling with Night-time Orchestration

## Goal

为 UltimateCoders 增加任务调度能力，支持夜间任务编排，让系统可以在低负载时段自动执行批量任务（如代码审查、索引重建、知识库整理等），最大化资源利用率。

## Requirements

### 核心功能

1. **定时任务调度**
   - Cron 表达式支持（循环任务，如 "每天 22:00 重建索引"）
   - 一次性延迟任务（execute_after 时间戳，如 "今晚执行此审查"）
   - 基于 `tokio-cron-scheduler` 实现，支持 timezone-aware 调度

2. **夜间时间窗口**
   - 可配置夜间执行窗口（如 `22:00-06:00`，支持跨午夜）
   - Time-Window Guard：调度触发时检查是否在窗口内，窗口外延迟到下一个窗口
   - 夜间窗口内调度任务独占 Worker 资源，实时任务排队等待窗口结束
   - 窗口配置存储在 PostgreSQL，支持运行时修改

3. **两类调度任务**
   - **系统维护任务**：索引重建、代码审查、知识库整理等，cron 循环触发，通过 YAML 配置文件定义（启动时加载）
   - **用户延迟任务**：用户提交时指定"夜间执行"，一次性延迟到夜间窗口，通过 Python API 提交

4. **调度管理 API**
   - Python API：`scheduler.create_job()`, `scheduler.list_jobs()`, `scheduler.cancel_job()`, `scheduler.get_execution_history()`
   - YAML 配置文件：启动时加载系统维护任务到调度器
   - PyO3 桥接：Python API 调用 Rust 调度器核心

5. **与 Orchestrator 集成**
   - 调度触发 → `Orchestrator.submit_task()` → LLM 分解 → Worker 执行
   - 调度任务与手动提交任务共用 Task/Subtask 类型系统

6. **持久化与恢复**
   - 调度配置持久化到 PostgreSQL（`scheduled_tasks` 表）
   - 执行历史持久化到 PostgreSQL（`execution_history` 表）
   - tokio-cron-scheduler PostgreSQL 后端保证重启后恢复
   - 系统重启后自动加载调度配置并恢复调度

7. **时区支持**
   - 存储统一 UTC，配置支持 IANA 时区名（chrono-tz）
   - 夜间窗口基于配置时区计算

## Acceptance Criteria

* [ ] 可配置夜间时间窗口（支持跨午夜），配置存储在 PostgreSQL
* [ ] 可通过 Python API 提交 cron 定时任务和一次性延迟任务
* [ ] 可通过 YAML 配置文件定义系统维护任务，启动时自动加载
* [ ] 调度器在指定时间自动触发任务（窗口内执行，窗口外延迟）
* [ ] 夜间窗口内调度任务独占 Worker，实时任务排队
* [ ] 调度任务触发后通过 Orchestrator.submit_task() 执行
* [ ] 任务执行状态和历史可查询（Python API）
* [ ] 系统重启后调度配置和状态恢复
* [ ] Rust 单元测试 + Python 单元测试通过

## Definition of Done

* Tests added/updated (Rust unit + Python unit)
* `cargo clippy` + `cargo test` + `pytest` green
* Docs/architecture.md 更新（调度架构章节）
* 调度配置 YAML 示例文档

## Technical Approach

### 架构分层

```
[YAML Config] ──load──> [Python Scheduler API] ──PyO3──> [Rust Scheduler Core]
[User API call] ───────> [Python Scheduler API] ──PyO3──> [Rust Scheduler Core]
                                                                    |
                                                            [tokio-cron-scheduler]
                                                                    |
                                                          [Time-Window Guard]
                                                                    |
                                                          [NATS: trigger event]
                                                                    |
                                                          [Orchestrator.submit_task()]
                                                                    |
                                                          [Worker execution]
                                                                    |
                                                          [PostgreSQL: execution_history]
```

### 关键组件

1. **Rust: `SchedulerService`** (`crates/uc-engine/src/scheduler/`)
   - 封装 `tokio-cron-scheduler`，提供 `add_job()`, `remove_job()`, `list_jobs()`, `start()`, `stop()`
   - PostgreSQL 持久化后端（tokio-cron-scheduler postgres_storage feature）
   - Time-Window Guard 逻辑：job callback 执行前检查 `is_within_night_window()`
   - 夜间窗口配置从 PostgreSQL 加载

2. **Rust: `NightWindow`** (`crates/uc-engine/src/scheduler/`)
   - `night_window_start: NaiveTime`, `night_window_end: NaiveTime`, `timezone: Tz`
   - `is_within_window(now: DateTime<Tz>) -> bool`：处理跨午夜逻辑
   - `next_window_start(now: DateTime<Tz>) -> DateTime<Tz>`：计算下一个窗口起始时间

3. **Rust: `ScheduledTask` / `ExecutionHistory`** (`crates/uc-types/src/scheduler.rs`)
   - 类型定义：ScheduledTask（id, description, cron, execute_after, night_window, enabled...）
   - ExecutionHistory（id, scheduled_task_id, started_at, completed_at, status, result_summary）

4. **Python: `Scheduler`** (`python/ultimate_coders/agent/scheduler.py`)
   - PyO3 桥接 Rust SchedulerService
   - `create_job()`, `list_jobs()`, `cancel_job()`, `get_execution_history()`
   - `load_config(path)`: 解析 YAML 并调用 create_job 加载系统维护任务

5. **Python: 配置文件** (`config/scheduled_tasks.yaml`)
   - YAML 格式定义系统维护任务 + 夜间窗口配置

6. **PostgreSQL Schema**
   - `scheduled_tasks` 表：id, description, project_id, cron_expression, execute_after, night_window_start/end, timezone, enabled, last_execution, next_execution
   - `execution_history` 表：id, scheduled_task_id, started_at, completed_at, status, result_summary

### 依赖变更

- 新增: `tokio-cron-scheduler` (features: postgres_storage, english)
- 新增: `chrono-tz` (已有 chrono，补充 tz feature)
- 升级: `async-nats` 0.38 → 0.43（NATS 2.12+ 兼容性）
- 升级: NATS Server Docker image → 2.12+

## Decision (ADR-lite)

**Context**: 需要为 UltimateCoders 增加任务调度能力，支持夜间任务编排。需选择调度引擎和夜间执行模式。
**Decision**: 方案 A — Time-Window Guard + tokio-cron-scheduler
- Rust 侧用 `tokio-cron-scheduler` 实现 cron 触发 + 持久化（PostgreSQL/NATS 后端）
- 执行前检查 Time-Window Guard，窗口外延迟到下一个窗口
- Python 侧通过 PyO3 桥接调度器 API，不引入独立 Python 调度库
- NATS JetStream 延迟消息用于一次性延迟任务（非 cron）
- 夜间窗口内调度任务独占 Worker，实时任务排队等待
**Consequences**:
- 需升级 `async-nats` 0.38→0.43（兼容 NATS 2.12+ 和 tokio-cron-scheduler）
- tokio-cron-scheduler 用 `tokio-postgres`，与项目现有 `sqlx` 形成双连接池（可接受）
- 夜间窗口逻辑需自定义实现（库无内置支持）

## Out of Scope

* Web UI 调度管理界面
* 分布式调度（多节点协调选主）
* 复杂的 DAG 调度（依赖任务编排）
* 动态负载感知调度（基于系统负载自动调整）
* 调度任务重试策略（复用现有 Orchestrator 重试机制）

## Technical Notes

### 相关文件
* `crates/uc-engine/src/scheduler/` — 已有 placeholder，需实现
* `python/ultimate_coders/agent/orchestrator.py` — Orchestrator 实现，需集成调度触发
* `python/ultimate_coders/agent/types.py` — Task/Subtask 类型定义
* `crates/uc-types/src/engine.rs` — EngineApi trait，可能需扩展调度方法
* `crates/uc-engine/src/local.rs` — LocalEngine，需集成 SchedulerService
* `crates/uc-python/src/lib.rs` — PyO3 暴露，需暴露调度 API

### 约束
* 需要与现有 async/await 架构兼容
* 需要支持 PyO3 FFI（调度 API 桥接）
* 夜间窗口独占模式意味着窗口切换时需要优雅处理进行中的任务

### Implementation Plan (small PRs)

* **PR1**: Rust 类型定义 + NightWindow 逻辑 + SchedulerService 骨架（uc-types + uc-engine）
* **PR2**: PostgreSQL schema + 持久化集成 + tokio-cron-scheduler 后端
* **PR3**: PyO3 桥接 + Python Scheduler API + YAML 配置加载
* **PR4**: Orchestrator 集成 + 夜间独占模式 + 执行历史记录 + 文档
