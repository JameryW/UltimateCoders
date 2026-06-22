# TUI界面9项优化

## Goal

对 Ink TUI 界面进行 9 项渐进式优化，提升信息密度、交互效率和使用舒适度，不改变核心架构。

## What I already know

* TUI 基于 Ink 5 (React for CLI)，单列垂直布局，组件：ChatLog / StatusBar / TaskInput / SubtaskTree / TaskListOverlay
* Reducer (TuiState) 已有：searchActive/searchQuery/searchMatchIndex、eventFilter、subtaskOverlayOpen、workersExpanded、focusedArea
* ChatLog 已有消息折叠 (COLLAPSE_THRESHOLD=3)、markdown 渲染、diff 着色
* StatusBar 已有分段式布局 (selectSegments) 适配窄/中/宽终端
* SubtaskTree 已有选中/详情面板
* LayoutMode = narrow/medium/wide 但 wide mode 仍是单列
* Dashboard (Web) 已有 WorkersPanel 微型进度条、InteractionLog 事件着色

## Assumptions (temporary)

* 所有 9 项优化均在现有 Ink 框架内实现，不引入新依赖
* 不改变 reducer 核心结构，只扩展 state 和 action
* 优先渲染逻辑改动，最小化 reducer 变更

## Open Questions

* (待补充)

## Requirements (evolving)

### P0 高价值 / 低成本

1. **Subtask 实时进度条（内联）** — SubtaskTree 中每个 subtask 行末加 `████░░` 进度指示
2. **工具事件折叠改进** — 折叠的 tool 事件支持 Enter 展开单条详情
3. **搜索高亮闭环** — ChatLog 渲染时对 searchQuery 匹配文本加反色/下划线

### P1 中等价值

4. **多 Worker 分布视图** — StatusBar expanded 模式加迷你负载柱状图
5. **输入历史模糊搜索** — Ctrl+R 增量搜索 inputHistory
6. **消息时间间隔标记** — ChatLog 中间隔 >5min 插入 `── 3m gap ──`

### P2 进阶优化

7. **虚拟滚动** — ChatLog 只渲染视口附近消息
8. **多窗格布局 (wide mode)** — >120col 时 ChatLog + SubtaskTree 并排
9. **关键事件通知** — StatusBar 3s 闪烁提醒 subtask_failed / task_completed / circuit_breaker_open

## Acceptance Criteria (evolving)

* [ ] 每项优化可独立验证，不影响其他功能
* [ ] 不引入新依赖
* [ ] 所有现有测试通过

## Definition of Done

* 所有 9 项优化实现并通过手动验证
* Lint / typecheck green
* 现有测试不受影响

## Out of Scope (explicit)

* Dashboard (Web) 界面改动
* Rust/gRPC 后端改动
* 新的 gRPC endpoint

## Technical Notes

* 关键文件：tui/src/components/ChatLog.tsx, SubtaskTree.tsx, StatusBar.tsx, TaskInput.tsx, App.tsx
* Reducer：tui/src/reducer.ts (TuiState, TuiAction)
* 已有搜索 state 但缺少渲染端高亮逻辑
