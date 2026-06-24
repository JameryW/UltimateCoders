# Dashboard日志+任务对比增强v2

## Goal

增强 Dashboard 的实时交互：让日志流更像终端体验（自动跟随+暂停+筛选），任务列表显示持续时间和排序，补充失败子任务的快速摘要。

## What I already know

### 当前已有
- InteractionLog: 虚拟滚动 + subtask 过滤 + expand detail
- EventLogPanel: 虚拟滚动 + autoScroll + type filter + errors filter + search + export
- TasksPanel: status/project filter + inline submit + pause/resume + highlight
- TaskDetail: subtask progress bar + DAG + timeline + output files
- StatsBar: 6 卡片
- AlertBar: 活跃告警条

### 当前缺失
1. **InteractionLog 无自动跟随** — EventLogPanel 有 autoScroll，InteractionLog 没有
2. **InteractionLog 无事件类型过滤** — 只有按 subtask 筛选，无法过滤 tool_call/llm_request
3. **任务列表无持续时间** — 看不到任务跑了多久
4. **任务列表无排序** — 只有默认时间序
5. **TaskDetail 无失败摘要** — 失败子任务需逐个展开看
6. **EventLogPanel 无时间范围过滤** — 无法快速看"最近5分钟"的事件

## Requirements

### R1: InteractionLog 自动跟随+事件类型过滤
- 自动跟随新事件（类似 EventLogPanel 的 autoScroll）
- 用户滚动时暂停跟随，"↓ Latest" 按钮恢复
- 新增事件类型过滤按钮（tool_call, llm_request, subtask_*）

### R2: TasksPanel 持续时间+排序
- 每个任务行显示持续时间（从 created_at 到 now/completed_at）
- 排序选项：time（默认）、status、duration

### R3: TaskDetail 失败摘要
- 如果有 failed subtasks，在 subtask 列表上方显示失败计数 badge
- 每个失败子任务显示 error 摘要（1 行，truncated）

### R4: EventLogPanel 时间范围快捷
- 新增 "5m" / "30m" / "1h" / "All" 时间范围按钮
- 过滤只显示指定范围内的事件

## Acceptance Criteria

- [ ] InteractionLog 自动跟随新事件，用户上滚暂停
- [ ] InteractionLog 有事件类型过滤按钮
- [ ] TasksPanel 任务行显示持续时间
- [ ] TasksPanel 有排序选项
- [ ] TaskDetail 显示失败子任务摘要和计数 badge
- [ ] EventLogPanel 有时间范围过滤按钮
- [ ] tsc --noEmit 通过
- [ ] Lint green (0 new errors)

## Definition of Done

- Lint / typecheck / CI green
- 移动端响应式正常

## Out of Scope

- 后端改动
- 新增 gRPC 端点
- Dashboard 性能优化
- WebSocket 替代 SSE

## Technical Notes

### 关键文件

| 文件 | 修改点 |
|------|--------|
| `dashboard/src/components/panels/InteractionLog.tsx` | R1: autoScroll + type filter |
| `dashboard/src/components/panels/TasksPanel.tsx` | R2: duration + sort |
| `dashboard/src/components/panels/TaskDetail.tsx` | R3: failed summary |
| `dashboard/src/components/panels/EventLogPanel.tsx` | R4: time range filter |
