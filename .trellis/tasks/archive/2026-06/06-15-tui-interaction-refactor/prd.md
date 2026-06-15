# TUI 交互打磨 — 增量补齐

## Goal

在已完成的重构（commit 846087c: focusedArea/activeMainPane + keymap.ts + unreadCount + Subtask 导航 + 指数退避）基础上，补齐 8 个缺口，使 TUI 从"功能完整"到"长时间使用也顺手"。

## Requirements

### P3: Subtask 导航增强

1. **Home/End 跳首尾** — subtask focus 下 Home 跳到第一个，End 跳到最后一个
2. **`f` 跳到下一个 failed** — subtask focus 下按 `f` 循环跳到 failed 状态的子任务
3. **详情显示 dependsOn** — SubtaskItem 增加 `dependsOn?: string[]`，detail 面板显示依赖关系
4. **详情显示最近事件 + 错误摘要** — detail 面板显示与该 subtask 相关的最近 5 条事件；failed 时显示错误摘要

### P4: 连接反馈增强

5. **Ctrl+R 后显示反馈** — 按下 Ctrl+R 时在 Chat 中显示 "Reconnecting to {serverAddr}..."，成功后显示 "Connected to {serverAddr}"，失败后自动已有错误提示

### P5: 输入体验 + 文档

6. **placeholder 根据连接状态变化** — connected: "type task description..." / offline: "offline demo: type task..."
7. **`?` 显示快捷键帮助** — keymap 中预留帮助命令，App 中 `?` 键显示/隐藏 help overlay（简易 Box 列出当前 focus area 的快捷键）
8. **TUI 快捷键文档** — 新增 `tui/README.md` 包含键盘快捷键参考表

## Technical Approach

基于已有 focusedArea/activeMainPane/keymap.ts 架构，增量修改：

- reducer.ts: 新增 `JUMP_TO_FAILED_SUBTASK` action
- SubtaskItem: 增加 `dependsOn?: string[]`
- App.tsx: subtask focus 下增加 Home/End/f/`?` 处理
- ChatLog.tsx 或 App.tsx: Ctrl+R 后 addMessage 反馈
- TaskInput: 接受 `connectionState` prop 控制 placeholder
- keymap.ts: 增加 `help` 命令、subtask Home/End/f 命令
- 新增 help overlay 组件或 App 内联渲染
- 新增 tui/README.md

## Out of Scope

- Help overlay 的可搜索功能
- 完整的事件详情面板（仅显示最近 5 条）
- Subtask retry 实际执行
