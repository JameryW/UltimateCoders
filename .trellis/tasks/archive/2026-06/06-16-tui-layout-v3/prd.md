# TUI Layout v3 — Reference Claude Code & Codex Single-Column Design

## Goal

将 UltimateCoders TUI 从当前 split-pane（Chat + Subtask 左右分栏）布局改为 Claude Code / Codex CLI 风格的 **single-column vertical** 布局，同时保留 v2 已完成的视觉增强（StatusIndicator、tool collapse、footer collapse），提升整体 UX 到同级别 AI Coding CLI 水准。

## What I already know

### Claude Code & Codex 核心设计模式（research 已完成）
- **Single-column vertical layout**：对话区 + 固定底部 input，无左右分栏
- **消息纵向流**：User/System/Tool/Assistant 消息按时间纵向排列，tool output 折叠
- **Subtask 内联展示**：子任务进度作为消息流的一部分（而非独立面板）
- **Status indicator 在 input 上方**：`Working [12s] · Esc to interrupt`
- **Footer 最底部**：单行 segment-based status line

### 当前 UC TUI（v2 已完成的）
- ✅ StatusIndicator（braille spinner + elapsed time）
- ✅ Message visual layers（user/system/tool 区分）
- ✅ Tool call collapse（默认折叠，Ctrl+O 展开）
- ✅ Footer progressive collapse（segment-based，按宽度折叠）
- ✅ LogoBanner（pixel-game UC logo）
- ✅ Focus model v2（focusedArea + activeMainPane）

### 当前 UC TUI 痛点（vs Claude Code/Codex）
1. **Split pane 布局过重**：Chat + Subtask 左右分栏在小终端极窄，信息密度低
2. **SubtaskTree 独立面板**：占右侧 25-40 列但信息密度低（仅 icon + description + progress）
3. **消息流被截断**：对话区只有 60-75% 宽度，tool output 阅读体验差
4. **Ctrl+W 切换 pane 复杂**：用户需要记住 pane 切换逻辑
5. **Header 冗余**：connection status + task ID + width 显示重复了 StatusBar 信息

## Requirements

### P0: Single-column vertical layout（核心变更）
- 消除左右分栏，改为单列纵向布局
- 主区域 = 消息流（ChatLog 全宽），包含 user/system/tool/assistant 消息
- Subtask 进度作为消息流的一部分（类似 Codex 的 plan cell / progress notice）
- Input 固定底部（已有），StatusIndicator 在 input 上方（已有）

### P1: Subtask 内联化（替代独立面板）
- SubtaskTree 不再作为独立面板存在
- 子任务状态变化作为 system message 内联到消息流
- Subtask summary 行：`📋 Subtask 1/3 completed │ 2 in_progress │ ETA ~30s`
- 需要查看详情时：Ctrl+T 打开 overlay（类似 Codex transcript viewer）

### P2: Header 精简
- 移除独立 Header 行（connection + task ID + width）
- 这些信息已由 StatusBar 提供，无需重复
- 只保留 LogoBanner（session start 或 narrow mode）

### P3: 消息流增强
- User message：左侧彩色边框 + username label
- Tool output 折叠优化：只显示 `⚙ Read(file.ts) (2s)`，展开看完整输出
- Subtask 事件消息：带 icon + 进度条 inline
- Separator：assistant 回复之间有轻量分割线

## Acceptance Criteria

- [ ] 主区域为单列全宽消息流（无左右分栏）
- [ ] Subtask 状态作为消息内联展示
- [ ] Ctrl+T overlay 可查看完整 subtask 详情
- [ ] Header 精简（StatusBar 覆盖其信息）
- [ ] 消息流在不同终端宽度下正常渲染
- [ ] 所有现有 keyboard shortcut 正常工作（移除 Ctrl+W pane swap）
- [ ] 现有测试通过

## Definition of Done

- TypeScript 编译通过
- vitest 测试通过
- 终端实际验证布局正确
- 无换行溢出

## Out of Scope

- Markdown rendering 增强（syntax highlighting）
- Diff 渲染组件
- Alternate screen / fullscreen 模式
- Shimmer 动画（Ink 不原生支持）
- 自定义 theme token system
- Vim input mode

## Technical Approach

### Layout 重构
当前：`<Box column>` → LogoBanner + Header + `<Box row>`(Chat | Subtask) + Separator + StatusIndicator + Input + StatusBar
改为：`<Box column>` → LogoBanner + `<ChatLog flexGrow>` + Separator + StatusIndicator + Input + StatusBar

### Subtask 内联
- 移除 SubtaskTree 作为独立组件
- 在 ChatLog 中增加 subtask-related message 类型：
  - `subtask_summary`: 进度概要行（每次状态变化时插入）
  - `subtask_detail`: 可折叠的详细输出
- Ctrl+T: 打开全屏 subtask overlay（替代 SubtaskTree 面板）

### Header 精简
- 移除 App.tsx 中的 Header `<Box>` 行
- LogoBanner 保留（可配置只在 session start 或 narrow mode 显示）

### Keyboard shortcut 变更
- 移除：Ctrl+W (pane swap) — 不再需要
- 新增：Ctrl+T (subtask overlay) — 类似 Codex transcript viewer
- 保留：Shift+Tab (focus cycle), Ctrl+F (event filter), Ctrl+P (pause/resume)

### 代码变更范围
- `App.tsx`: layout 重构（主要变更）
- `ChatLog.tsx`: 增加 subtask message 类型渲染
- `SubtaskTree.tsx`: 保留但改为 overlay 模式（Ctrl+T 触发）
- `StatusBar.tsx`: 可能增加 subtask count segment
- `reducer.ts`: 移除 SWAP_MAIN_PANE，增加 TOGGLE_SUBTASK_OVERLAY
- `keymap.ts`: 移除 Ctrl+W，增加 Ctrl+T

## Research References

- [`research/claude-codex-tui-patterns.md`](research/claude-codex-tui-patterns.md) — Claude Code (Ink) 和 Codex CLI (Ratatui) 的 TUI 设计模式完整分析

## Decisions

### D1: Single-column vertical layout ✅
- 消除左右分栏，ChatLog 全宽
- 参考 Claude Code / Codex CLI 布局范式

### D2: Subtask 进度 = single mutable summary line ✅
- 消息流中只有一条 subtask summary 行，每次状态变化时更新
- 格式：`📋 1/3 ✅ │ 2 ⏳ │ ETA ~30s`
- 需要 ChatLog 支持消息内容更新（新 action: UPDATE_MESSAGE）

### D3: SubtaskTree → Ctrl+T overlay ✅
- 保留 SubtaskTree 组件，改为 overlay 模式
- Ctrl+T 打开全屏 overlay，Esc 关闭
- 类似 Codex transcript viewer

## Open Questions

(None — all resolved)
