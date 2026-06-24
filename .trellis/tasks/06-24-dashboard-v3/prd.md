# Dashboard 可观测性与指标增强 v3

## Goal

为 Web Dashboard 增加运维级别的指标和可观测性能力，让开发者/运维人员能实时监控系统健康、定位性能瓶颈、追踪任务执行效率。

## Requirements

### R1: MetricsAggregator (Python 后端)

在 Python 端新增 `MetricsAggregator` 类，维护 in-memory 滑动窗口指标，随 DashboardSnapshot 下发：

- **Task 效率指标**：
  - `task_duration_histogram`: 最近 1h 内 completed 任务的耗时分布（P50/P95/P99）
  - `avg_task_duration_ms`: 平均耗时
  - `retry_rate`: 重试率 (subtask_retrying / subtask_completed + subtask_failed)
  - `slow_tasks_count`: 耗时超过阈值（默认 5min）的任务数

- **Worker 效率指标**：
  - `per_worker_tool_calls`: 每个 worker 的工具调用计数（最近 1h）
  - `per_worker_subtask_count`: 每个 worker 完成的 subtask 数
  - `avg_heartbeat_age_seconds`: 集群平均心跳延迟

- **Event 流量指标**：
  - `events_per_minute`: 最近 5min 的 event/s 滑动窗口
  - `error_spike`: 最近 5min 错误率是否超过阈值（>30%）
  - `event_type_counts`: 按 event type 分组的计数

- **系统概览**（已有数据的聚合）：
  - `uptime_seconds`, `circuit_breaker_state`, `rate_limiter_remaining_ratio`
  - `cluster_utilization_pct`

- **趋势采样**（为后续 sparkline 预留数据）：
  - 每 1min 采样一次当前指标快照，保留最近 60 个采样点
  - 采样数据随 MetricsSnapshot 下发，前端可缓存但不渲染（本次无 sparkline UI）

### R2: Proto + DashboardSnapshot 扩展

在 `engine.proto` 的 `DashboardSnapshot` 中新增 `MetricsSnapshot` 字段：

```protobuf
message MetricsSnapshot {
  TaskMetrics task = 1;
  WorkerMetrics worker = 2;
  EventMetrics event = 3;
  SystemMetrics system = 4;
  repeated MetricsSample trend = 5;  // 最近 60 个 1min 采样点
}

message MetricsSample {
  int64 timestamp = 1;      // unix seconds
  double events_per_minute = 2;
  double avg_duration_ms = 3;
  double error_rate = 4;
  double cluster_utilization = 5;
}

message TaskMetrics {
  double avg_duration_ms = 1;
  double p50_duration_ms = 2;
  double p95_duration_ms = 3;
  double p99_duration_ms = 4;
  double retry_rate = 5;
  uint32 slow_tasks_count = 6;
  uint32 total_completed = 7;   // 用于 StatsBar throughput
  uint32 total_failed = 8;     // 用于 StatsBar error rate
  double success_rate = 9;     // 用于 StatsBar success rate
}

message WorkerMetrics {
  double avg_heartbeat_age_seconds = 1;
  map<string, uint32> per_worker_tool_calls = 2;
  map<string, uint32> per_worker_subtask_count = 3;
  double cluster_load_pct = 4;  // 用于 StatsBar cluster load
}

message EventMetrics {
  double events_per_minute = 1;
  bool error_spike = 2;
  map<string, uint32> event_type_counts = 3;
}

message SystemMetrics {
  uint64 uptime_seconds = 1;
  string circuit_breaker_state = 2;
  double rate_limiter_remaining_ratio = 3;
  double cluster_utilization_pct = 4;
}
```

DashboardSnapshot 新增字段：
```protobuf
optional MetricsSnapshot metrics = 9;
```

### R3: Python DashboardApp 集成

- `MetricsAggregator` 从 Orchestrator 事件流中实时聚合指标
- `_get_full_snapshot()` 返回值增加 `metrics` 字段
- SSE `task_event` 流推送时同步更新 MetricsAggregator
- 零额外 API endpoint — 指标通过现有 stream 下发

### R4: 前端 MetricsPanel

新增 `MetricsPanel.tsx`，4 个指标卡片区域：

1. **System Overview** — uptime, circuit breaker state, rate limiter ratio, cluster utilization
2. **Task Efficiency** — P50/P95/P99 duration, retry rate, slow tasks
3. **Worker Performance** — avg heartbeat age, per-worker tool call bar chart, subtask count
4. **Event Flow** — events/min, error spike indicator, event type breakdown

### R5: StatsBar 指标去重

StatsBar 不再前端自行计算指标，全部从后端 `metrics` 字段获取：

- **Total Tasks** ← `metrics.task.total_completed + metrics.task.total_failed + tasks.status_counts[pending/in_progress/...]`
- **Success Rate** ← `metrics.task.success_rate`
- **Throughput** ← `metrics.task.total_completed` / uptime_hours（或用 trend 最近采样点差值）
- **Error Rate** ← `metrics.task.total_failed / (total_completed + total_failed) * 100`
- **Latency P95** ← `metrics.task.p95_duration_ms`（后端有数据时），fallback `—`
- **Cluster Load** ← `metrics.worker.cluster_load_pct`

趋势箭头：每个指标与上一次 snapshot 的同值对比，↑↓→ 三态。

## Acceptance Criteria

- [ ] `MetricsAggregator` Python 类有 unit test，覆盖滑动窗口逻辑
- [ ] `DashboardSnapshot` proto 包含 `MetricsSnapshot`，Rust 编译通过
- [ ] Python `_get_full_snapshot()` 返回 metrics 字段
- [ ] 前端 `MetricsPanel` 展示 4 类指标，数据来自 `metrics` 字段
- [ ] StatsBar 所有 6 个指标从后端 `metrics` 获取，前端不再自行计算
- [ ] StatsBar 趋势箭头正确显示（与上一次 snapshot 对比）
- [ ] 后端有数据时 StatsBar P95 不再显示 "Need 3+ samples"
- [ ] 无数据时 MetricsPanel 显示 empty state，不崩溃
- [ ] CI green

## Definition of Done

* Tests added/updated (unit for MetricsAggregator, proto compiles, frontend renders)
* Lint / typecheck / CI green
* Empty state handled gracefully

## Technical Approach

### 后端

1. 新建 `python/ultimate_coders/dashboard/metrics.py` — `MetricsAggregator` 类
   - 滑动窗口用 `collections.deque` + `time.monotonic()`，默认保留 1h 数据
   - `record_event(event_type, data)` — 每次事件调用，更新内部计数器/直方图
   - `snapshot() -> dict` — 返回当前聚合指标
   - 趋势采样：内部 `_sample_trend()` 每 60s 被 `snapshot()` 调用时检查，保留 60 个采样点
   - `ponytail: 全局锁，per-metric locks if throughput matters`

2. 修改 `python/ultimate_coders/dashboard/app.py`:
   - `__init__` 创建 `MetricsAggregator` 实例
   - `event_generator` 中每次收到 NATS event 调用 `metrics.record_event()`
   - `_get_full_snapshot()` 增加 `metrics` 字段
   - `_record_event()` 同步调用 `metrics.record_event()`

3. 修改 `engine.proto` — 新增 `MetricsSnapshot` + 相关 message

### 前端

4. `dashboard/src/types/dashboard.ts` — 新增 `MetricsSnapshot`, `TaskMetrics`, `WorkerMetrics`, `EventMetrics`, `SystemMetrics` 接口

5. `dashboard/src/hooks/useDashboard.ts` — 新增 `metrics` state，`handleSnapshot` 中提取

6. 新建 `dashboard/src/components/panels/MetricsPanel.tsx`

7. 修改 `dashboard/src/components/panels/StatsBar.tsx`:
   - Props 增加 `metrics: MetricsSnapshot | null`
   - 所有指标优先从 `metrics` 获取，前端 event 聚合作为 fallback
   - 新增 `prevMetricsRef` 用于趋势箭头计算

## Decision (ADR-lite)

**Context**: 需要选择指标数据来源策略——纯前端聚合 vs 后端 MetricsAggregator vs 混合
**Decision**: 后端增加 MetricsAggregator（方案 2），通过 DashboardSnapshot 下发
**Consequences**: 页面刷新可恢复近期数据；需改 Python + proto + 前端；不引入外部 metrics 系统

## Out of Scope

* Prometheus / OpenTelemetry 集成
* 历史时序数据库 / 持久化指标
* TUI (Ink) 改动
* Python TUI 改动
* 告警/通知系统
* Sparkline UI 渲染（数据已预留，渲染留后续）
* gRPC Rust 服务端实现 metrics（Python 端已有全部数据，通过 SSE 下发即可）

## Technical Notes

* `DashboardSnapshot` proto 位于 `crates/uc-grpc/proto/engine.proto:519`
* Python `_get_full_snapshot()` 在 `python/ultimate_coders/dashboard/app.py:1241`
* 前端 `useDashboard` hook 在 `dashboard/src/hooks/useDashboard.ts`
* StatsBar 在 `dashboard/src/components/panels/StatsBar.tsx`
* Event 数据通过 NATS → SSE/gRPC-Web 推送到前端
* Python 端已有 `TaskEventEmitter` (`python/ultimate_coders/agent/event_emitter.py`) 管理 event ring buffer
