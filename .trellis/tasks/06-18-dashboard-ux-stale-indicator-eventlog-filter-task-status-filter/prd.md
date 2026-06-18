# Dashboard UX: stale indicator, EventLog filter, task status filter

## Goal

提升 Dashboard 的可用性：让用户知道数据是否实时、让 EventLog 可过滤、让 TasksPanel 支持按状态筛选。

## Requirements

1. **Stale/reconnecting indicator**: 当 SSE 断开时，面板显示数据可能过时的视觉提示（dimmed overlay 或 subtle banner）
2. **EventLogPanel 过滤**: 添加事件类型过滤 + 文字搜索
3. **TasksPanel 状态过滤**: 状态计数 badge 变为可点击过滤按钮

## Acceptance Criteria

- [ ] SSE 断开时面板显示 stale 指示
- [ ] EventLogPanel 可按事件类型过滤 + 文字搜索
- [ ] TasksPanel 状态 badge 可点击过滤

## Out of Scope

* WorkersPanel expand-on-click
* TaskSubmitForm 提交历史
* Chart legend / time range
* Theme 一致性 pass
* Skeleton component
