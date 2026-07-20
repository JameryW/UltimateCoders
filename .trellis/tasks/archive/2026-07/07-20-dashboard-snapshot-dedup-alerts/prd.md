# PRD: Dashboard 快照去重 + 告警透传 + trend 依赖（审计 #4/#5/#6）

## 背景

/loop 第 31 轮，dashboard 重审 MED 簇。

## 清单（已核实）

### F70: 空闲时每 ~30s 重复事件 + 重复 toast（审计 #4，MED/M）

gRPC WatchDashboard 路径每次快照把**全部** `recentEvents` + `recentTaskEvents` 当 task event 重发（useDashboardGrpc.ts:460+）。空闲快照 30s 一次（dashboard_service.rs:219），超 useDashboard 的 5s dedup 窗口 → 同一 completed/failed 事件反复进 `handleTaskEvent` → 事件/交互日志重复行 + 空闲集群上**每 30s 重弹 "Task completed/failed" toast**。

修：useDashboardGrpc 内加长生命 seen-set（键 `${type}:${task_id}:${subtask_id}:${timestamp}`，与 useDashboard dedupEventKey 同构；上限 500，溢出清空），快照重发循环里已见事件跳过。每唯一事件页面生命周期内只发一次（快照重放同一事件共享 timestamp → 键同）。

### F71: SSE snapshot 丢 alert 字段（审计 #5，MED/S）

后端每全量快照附 `alert_events`/`alert_resolved`（app.py:1394），`handleSnapshot` 消费（useDashboard.ts:82-88），但 SSE `update` 转换器（useDashboardGrpc.ts:368-389）只拷 health/workers/scheduler/metrics/tasks → **activeAlerts 恒空，服务端告警永不到 AlertBar**。

修：converted 类型加两字段 + 拷贝 `snapshot.alert_events`/`snapshot.alert_resolved`。

### F72: 扩展 trend 每快照重拉（审计 #6，MED/S）

MetricsPanel effect deps `[trendRange, metrics]`——`metrics` 每快照新对象 → 选 6h/24h 时每快照重拉 `/dashboard/api/trend` + 每次重置 `extendedFailed`（fallback 提示闪烁）。

修：`const hasMetrics = metrics != null`（布尔仅在 null↔非 null 翻转），deps 改 `[trendRange, hasMetrics]`；effect 内 guard 不变（`!metrics` 检查保留防御）。

## 验收

- 手动推理三路径 + 类型：converted 类型含 alert 字段（tsc 验）。
- `cd dashboard && npx tsc -p tsconfig.app.json --noEmit`：所触文件零新错（既有错误不变）。
- feature branch + PR + CI green（ci-dashboard：tsc + vite build）。

## 不做（后续轮）

#7 connectionError 不可达；#8 FileBrowser 竞态；#9 unary 超时；#10 sync_required 单次；#11-#15 LOW 杂项。
