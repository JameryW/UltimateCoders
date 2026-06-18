# TUI Chat Message Select + Expand

## Goal

让 ChatLog 中 Enter 展开单条消息而非 toggle 全部，Up/Down 在消息间移动。

## Requirements

1. ChatLog 维护 `selectedVisibleIndex`（本地 state，不在 reducer）
2. Up/Down 移动 selectedVisibleIndex（同时滚动窗口使选中消息保持可见）
3. Enter 展开/折叠选中消息（替换 `expandAll` toggle）
4. 选中消息显示 `▸` 前缀
5. ChatMessageItem `expanded` 从 `expandAll` prop 改为自身控制（Enter 切换）

## Acceptance Criteria

- [ ] Up/Down 在 chat focus 移动选中消息（带 ▸ 标记）
- [ ] Enter 展开选中消息（非全部）
- [ ] 选中消息滚动时自动跟随（不会滚出可视区域）
- [ ] reducer 不再需要 TOGGLE_EXPAND_ALL_MESSAGES
- [ ] 386 测试不回归

## Technical Approach

- ChatLog 加 `selectedVisibleIndex` local state + `expandedLocalIds` Set
- Up/Down dispatch SCROLL_UP/DOWN + 同时移动 selectedVisibleIndex
- Enter 不再 dispatch TOGGLE_EXPAND_ALL_MESSAGES，改为 toggle expandedLocalIds
- App.tsx 删除 `expandAllMessages` 传给 ChatLog 的 prop
- ChatMessageItem 不接收 `expandAll` prop，而是 `isExpanded` prop

## Out of Scope

* 鼠标点击选中
* 多选
* 搜索消息
