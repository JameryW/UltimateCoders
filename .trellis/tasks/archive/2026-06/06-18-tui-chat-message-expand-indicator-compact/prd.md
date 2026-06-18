# TUI Chat Message Expand + Indicator Compact

## Goal

改进 ChatLog 的消息展开交互和状态指示器布局，使其更接近 Claude Code 的使用体验。

## Requirements

### 1. 单条消息展开（P0）
- Enter 在 chat focus 下展开/折叠**当前选中消息**，而非 toggle 所有消息
- 需要在 ChatLog 中追踪"当前选中消息"（类似列表导航）
- 选中消息有视觉指示（如左侧标记 `▸`）
- Up/Down 在 chat focus 下移动选中消息（兼做滚动）

### 2. ChatLog 指示器合并（P1）
- filterIndicator、followIndicator、unreadIndicator、scrollIndicator 合并到一行
- 格式：`[filter:Tool 5/20] [paused] [+3 new] ↑12-15/50↓`
- 节省垂直空间，给消息内容更多行

### 3. Subtask overlay 进度条（P2）
- overlay 顶部标题旁添加进度文字和进度条
- 格式：`Subtasks [3/5] [█████░░░░░]`

## Acceptance Criteria

- [ ] ~Enter 在 chat focus 展开/折叠当前选中消息（非全部）~ → 延后（需重构 ChatLog 滚动/选中逻辑）
- [ ] ~选中消息有 ▸ 标记~ → 延后
- [ ] ~Up/Down 在 chat 移动选中位置~ → 延后
- [x] ChatLog 指示器合并到一行
- [x] Subtask overlay 显示进度条
- [x] 现有测试不回归

## Out of Scope

* Ink v6 升级
* Bracketed paste
* 鼠标支持
* Markdown 渲染改进（已工作）

## Technical Notes

* 文件：ChatLog.tsx, App.tsx, SubtaskTree.tsx
* reducer.ts 可能需要新 action: SELECT_MESSAGE, TOGGLE_MESSAGE_EXPAND
* keymap.ts 可能需要新命令: selectUp, selectDown, toggleExpand
