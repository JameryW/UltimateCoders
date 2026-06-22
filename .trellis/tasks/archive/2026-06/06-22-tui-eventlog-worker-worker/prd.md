# TUI 界面优化 — EventLog 虚拟滚动 + Worker 面板增强 + 多 Worker 状态摘要

## Goal

三项高 ROI 优化：Dashboard EventLog 加虚拟滚动解决大量事件卡顿；WorkersPanel 补充 subtask 分配详情可视化；Ink TUI StatusBar 支持多 Worker 状态摘要。

## What I already know

- **EventLogPanel**：原生 `<ul>` + `max-h-64 overflow-y-auto`，无虚拟化，上限 200 事件，无虚拟化依赖
- **WorkersPanel**：已显示 per-worker subtask（展开态），有 load bar、heartbeat 年龄、stale 警告；但无 subtask→worker 交叉视图
- **Ink TUI StatusBar**：`workerId` 是硬编码 `'grpc-worker'`/`'offline'`，无多 worker 支持；TuiState 无 workers 数组
- **gRPC**：TUI 的 TaskService 不暴露 ListWorkers；只有 SubtaskProto.assignedWorker 字段

## Requirements

### 1. Dashboard EventLog 虚拟滚动

- 引入 `@tanstack/react-virtual` 替代原生滚动
- 保持现有 typeFilter + searchQuery 过滤功能
- 保持事件行的样式不变
- 虚拟列表高度沿用 `max-h-64`
- 稳定 key：`evt.timestamp + evt.type + i` → 改用 `evt.timestamp + evt.type`（去 index）

### 2. EventLog 自动滚动到底 (tail 模式)

- 新事件到达时自动滚动到底部
- 用户手动向上滚动时暂停自动滚动（显示 "返回最新" 按钮）
- 点击按钮或滚到底部时恢复自动滚动
- 预埋后续持久化查询扩展点

### 3. WorkersPanel Subtask 增强

- 展开态 subtask 列表增加点击跳转 TaskDetail（如果 task id 可解析）
- Worker 行显示 active subtask 小进度条（completed/total）
- 无需新增 gRPC 调用，从现有 task 数据派生

### 4. Ink TUI 多 Worker 状态摘要 + 点击展开

- StatusBar 显示多 worker 摘要格式：`3/5 active` 代替单个 `grpc-worker`
- 从 SubtaskItem[] 派生 unique workers 数量（reduce assignedWorker）
- 连接态显示 `N/M workers`（N=有 subtask 的 worker 数，M=unique workers）
- 离线态保持 `offline`
- 点击/Enter 展开简短 worker 详情（worker id + subtask 数），再按收起
- 预埋后续 worker 操作（kill/reassign）

## Acceptance Criteria

- [ ] EventLog 200 事件滚动不卡（vs 之前原生渲染）
- [ ] EventLog 过滤/搜索功能不变
- [ ] EventLog 自动滚动到底，手动上滚暂停 + "返回最新" 按钮
- [ ] WorkersPanel 展开态 subtask 有进度指示
- [ ] Ink TUI StatusBar 连接时显示 worker 摘要而非硬编码字符串
- [ ] Ink TUI StatusBar worker 摘要可点击展开/收起
- [ ] 现有测试通过

## Definition of Done

- Tests added/updated（EventLog 虚拟化 + StatusBar worker 摘要）
- Lint / typecheck / CI green
- 无新依赖以外的 package.json 变更

## Out of Scope

- Panel 拖拽重排 + 布局持久化
- Cmd+K 全局搜索
- TaskDetail Mermaid DAG
- TUI gRPC ListWorkers 集成（当前架构不支持）
- Python Textual TUI 统一布局
- Worker 操作（kill/reassign）— 仅预埋展开交互
- EventLog 持久化查询 — 仅预埋自动滚动

## Technical Notes

- EventLog: `@tanstack/react-virtual` ~8KB gzipped, API: `useVirtualizer({ count, getScrollElement, estimateSize })`
- WorkersPanel: subtask 数据已在 `workerSubtasks` useMemo 中，只需增加进度计算
- Ink TUI: 从 `subtasks` 派生 worker 摘要，无需新 gRPC 调用
- 关键文件:
  - `dashboard/src/components/panels/EventLogPanel.tsx` (142 lines)
  - `dashboard/src/components/panels/WorkersPanel.tsx` (216 lines)
  - `tui/src/components/StatusBar.tsx` (314 lines)
  - `tui/src/components/App.tsx` (~1200 lines, workerId at line 363)
  - `tui/src/reducer.ts` (~660 lines)
