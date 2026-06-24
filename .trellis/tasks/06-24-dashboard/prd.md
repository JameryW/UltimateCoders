# Dashboard可观测性增强：指标面板+状态洞察+错误追踪

## Goal

增强 Dashboard 的可观测性：补齐关键运行指标（吞吐量、延迟分布、错误率趋势），让用户一眼看懂系统健康度和瓶颈，不再需要在 Event Log 里翻找。

## What I already know

### 当前已渲染的指标
- StatsBar: 4 卡片 — Total Tasks, Success Rate, Avg Duration, Active Workers
- HealthPanel: 组件状态 + version + uptime
- CircuitBreakerPanel: CB state + failures + threshold + RPM gauge + engine metrics block
- WorkersPanel: load bar + heartbeat + active subtasks + capabilities
- TaskTrendChart: completed/failed 柱状图, 1h/6h/24h/7d
- EventLogPanel: 完整事件流（虚拟滚动 + 过滤 + 搜索）
- TaskDetail: subtask 进度条 + DAG + timeline + output files

### 当前缺失的可观测性
1. **无错误率趋势** — StatsBar 只显示当前 success rate，无时间维度
2. **无吞吐量指标** — tasks/min 或 subtasks/min 不可见
3. **无延迟分布** — 只有 avg duration，无 P50/P95/P99，无延迟趋势
4. **无活跃告警面板** — 当前失败/重试/心跳 stale 需要手动翻找
5. **Workers 无聚合指标** — 无集群级别负载/容量摘要
6. **EventLog 无错误专属视图** — 没有快速筛选 "only errors" 的方式
7. **TaskTrendChart 只有 completed/failed** — 无 submitted/started 分层

### 数据可用性
- `eventLog` 已包含所有事件类型，有 `duration_ms` 字段
- `tasks.status_counts` 有实时状态分布
- `workers` 有 `current_load/max_capacity/load_percent`
- `circuitBreaker` 有实时 CB/RL 指标
- 所有数据都已在前端内存中，无需新增 gRPC 调用

## Requirements

### R1: StatsBar 增强指标
- 替换 Avg Duration 为 **Throughput** (tasks completed in last hour)
- 新增 **Error Rate** 卡片 (failed/total in last hour)
- 新增 **Latency P95** 卡片 (从 eventLog duration_ms 计算)
- Active Workers 保留，补充 **集群负载** (total_current_load / total_max_capacity)

### R2: 错误趋势图
- TaskTrendChart 增加 `submitted` 层 (蓝色)，图表变为 stacked bar: submitted → completed (绿) / failed (红)
- 新增 "errors" 线图叠加，显示累计错误数

### R3: 活跃告警摘要
- StatsBar 下方或 Header 下方显示 **告警条**: 集中显示当前问题
  - N 个 stale worker
  - Circuit breaker open
  - M 个 failed subtasks (最近 1h)
  - Rate limiter > 80% 使用
- 点击可跳转到对应面板
- 无告警时不显示

### R4: 错误专属视图
- EventLogPanel 新增 "Errors" 快速过滤按钮 (等同于 typeFilter=failed)
- 过滤显示: task_failed, subtask_failed, circuit_breaker_reset
- 错误行高亮 + 错误消息截断显示

### R5: Workers 聚合指标
- WorkersPanel header 补充集群负载摘要: "Total: X/Y (Z%)"
- 可用容量不足 20% 时黄色警告

## Acceptance Criteria

- [ ] StatsBar 显示 6 卡片: Total Tasks, Success Rate, Throughput, Error Rate, Latency P95, Cluster Load
- [ ] TaskTrendChart 显示 submitted/completed/failed 三层 stacked bar
- [ ] 有活跃告警时 Header 下方显示告警条，无告警时隐藏
- [ ] 告警条至少覆盖: stale workers, CB open, recent failures, RL high usage
- [ ] EventLogPanel 有 "Errors" 快速过滤按钮
- [ ] WorkersPanel 显示集群负载摘要
- [ ] tsc --noEmit 通过
- [ ] Lint green

## Definition of Done

- 所有指标从现有前端数据计算，无需后端改动
- 移动端响应式正常
- Lint / typecheck / CI green

## Out of Scope

- 新增 gRPC 端点或后端数据源
- P99 延迟（样本量不足时无意义）
- 持久化指标历史（仅用当前 session 的 eventLog）
- 告警通知推送（email/webhook）
- Dashboard 性能优化（memo + virtual scroll 已有）

## Technical Approach

### R1: StatsBar

从 `eventLog` 计算最近 1h 的 completed/failed/submitted 事件：
- Throughput = completed events in last 1h
- Error Rate = failed / (completed + failed) in last 1h
- Latency P95 = percentile(duration_ms) from completed events in last 1h
- Cluster Load = sum(workers.current_load) / sum(workers.max_capacity)

### R2: TaskTrendChart

修改 `bucketEvents()` 增加 submitted 计数，SVG 增加 submitted 蓝色层。

### R3: 告警条

新增 `AlertBar` 组件，从现有 state 推导：
- `workers.workers.filter(w => w.heartbeat_stale).length`
- `circuitBreaker.circuit_breaker.state === "open"`
- `eventLog` 中最近 1h 的 subtask_failed 数
- `circuitBreaker.rate_limiter.remaining_ratio < 0.2`

### R4: EventLogPanel

在 type filter 按钮组前加 "Errors" toggle，等同于 `typeFilter` 设为匹配 failed 类型的正则。

### R5: WorkersPanel

在 Badge 旁增加 "X/Y (Z%)" 集群负载文字。

## Technical Notes

### 关键文件

| 文件 | 修改点 |
|------|--------|
| `dashboard/src/components/panels/StatsBar.tsx` | R1: 6 卡片 |
| `dashboard/src/components/charts/TaskTrendChart.tsx` | R2: submitted 层 |
| `dashboard/src/App.tsx` | R3: AlertBar 集成 |
| `dashboard/src/components/layout/Header.tsx` | R3: AlertBar 渲染位置 |
| `dashboard/src/components/panels/EventLogPanel.tsx` | R4: Errors 过滤 |
| `dashboard/src/components/panels/WorkersPanel.tsx` | R5: 集群负载 |
| `dashboard/src/lib/utils.ts` | R1: percentile helper |
