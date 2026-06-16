# TUI功能完善 — subtask overlay交互、retry、清理deprecated

## Goal

补全 TUI 已有但未接线的交互功能（subtask overlay 键盘导航、detail panel），实现 subtask retry，清理 deprecated 代码和死依赖，使 TUI 功能完整可用。

## What I already know

* SubtaskTree 组件已有 selectedIndex/detailOpen props 和 SubtaskDetail 子组件，但 overlay 没接线
* App.tsx overlay 模式下没有 useInput 处理 Up/Down/Enter，detailOpen 写死 false
* RETRY_SUBTASK action 在 reducer 中是 no-op placeholder
* gRPC client 没有 retry RPC（TaskServiceClient 只有 submit/get/list/watch/pause/resume）
* README 描述 3-area focus (input/chat/subtask) 但实际是 2-area (input/chat)
* Ctrl+W SWAP_MAIN_PANE 是 no-op，keymap 中未定义
* ink-text-input 是死依赖（package.json 有但未 import）
* reducer 有 @deprecated 的 SelectedPane/ActiveMainPane 类型和 selectedPane/activeMainPane 状态字段
* StatusBar 接受 @deprecated props（activeMainPane, serverAddr, activeTaskId 等）

## Assumptions (temporary)

* Subtask retry 暂时走 offline 模拟路径（无 gRPC retry RPC），标记 subtask 为 retrying 后重新模拟
* gRPC retry RPC 留给 backend hardening 任务
* 清理 deprecated 代码是安全的（单列布局已稳定）

## Open Questions

* Subtask overlay 交互细节：键盘导航范围、detail panel 展示内容、retry 触发方式

## Requirements (evolving)

* P0: Subtask overlay 键盘导航 — Up/Down 选择 subtask，Enter 切换 detail，Esc 返回
* P0: Subtask detail panel — 展示完整 description/worker/deps/error
* P1: Subtask retry — R 键直接 retry + detail 内也有 retry 提示（offline 模拟）
* P2: 清理 deprecated — 删除 SelectedPane/ActiveMainPane/selectedPane/activeMainPane，移除 ink-text-input
* P2: README 同步 — 更新为实际 2-area focus 模型

## Acceptance Criteria (evolving)

* [ ] Ctrl+T overlay 中 Up/Down 可选择 subtask 行
* [ ] Enter 在 overlay 中切换 detail panel 显示/隐藏
* [ ] Detail panel 显示 subtask 完整信息
* [ ] R 键对 failed subtask 触发 retry（overlay 中直接触发 + detail 内提示）
* [ ] Retry 在 offline 模式下重新模拟进度
* [ ] 无 @deprecated 类型/字段残留
* [ ] ink-text-input 从 package.json 移除
* [ ] README 与实际行为一致

## Definition of Done

* Lint / typecheck / CI green
* 手动验证 overlay 交互
* README 同步

## Out of Scope (explicit)

* gRPC retry RPC（属于 backend hardening 任务）
* 3-area focus 模型（subtask 不作为独立 focus area，只在 overlay 中交互）
* Ctrl+W pane swap（单列布局不需要）

## Technical Notes

* 关键文件：App.tsx (overlay useInput), SubtaskTree.tsx (已有 props), reducer.ts (RETRY_SUBTASK), keymap.ts
* SubtaskTree 已有完整的 SubtaskDetail 组件，只需接线
* Offline retry：重置 subtask status → 重新跑 simulateOfflineSubmit 的进度逻辑
