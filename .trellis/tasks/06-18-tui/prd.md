# 完善 TUI 功能

## Goal

在现有成熟的 Ink TUI 基础上，全面补全交互体验、视觉优化、功能补全，使 TUI 从"功能完整"升级为"日常可用"。

## Requirements

### R1: 任务控制 (P0/P1)

1. **Ctrl+C 退出确认** — 活跃任务时 Ctrl+C 显示确认提示，防止误杀
2. **任务取消** — 新增 `/cancel` 命令 + Esc 取消提交中的任务（StatusIndicator 已显示 "Esc cancel" 但未实现）
3. **Pause/Resume 错误反馈** — gRPC 失败时显示 toast/inline 错误，不再静默

### R2: 交互体验 (P1)

4. **Overlay 滚动** — SubtaskOverlay 和 TaskListOverlay 内容超出终端高度时可滚动
5. **焦点指示器** — 主布局中显示当前焦点区域（input vs chat），类似 StatusBar 的 focus segment 但更明显
6. **消息搜索** — Ctrl+S 或 `/search` 进入搜索模式，高亮匹配消息，n/N 跳转上/下一个
7. **gRPC 断线重连后错误提示** — retry 耗尽后在 ChatLog 显示错误消息，不只 console.warn
8. **任务完成通知** — 后台任务完成/失败时短暂高亮 StatusBar 或显示通知

### R3: 视觉优化 (P1/P2)

9. **符号系统统一** — 6 处硬编码 Unicode/emoji 改为使用 symbols.ts（走 symbolMode 逻辑）
10. **代码块语法高亮** — tool_call 结果中的代码块使用 cli-highlight 着色（已有传递依赖）
11. **Diff 视图** — file_modified 事件显示 +/- 行着色（绿/红），而非纯文本
12. **Markdown 渲染器缓存** — marked-terminal 实例按 width 缓存，不每次重建

### R4: 功能补全 (P2)

13. **symbolMode 切换** — reducer 有 symbolMode state 但无 action 切换，添加 `/symbols <mode>` 命令
14. **Expand all messages** — 添加 `Ctrl+E` 展开/折叠所有消息
15. **日志导出** — `/export [path]` 将 ChatLog 写入文件（JSON + plain text）
16. **命令自动补全增强** — Tab 循环所有匹配项，不只取第一个
17. **命令历史持久化** — inputHistory 写入 ~/.ultimate-coders/history.json，重启保留

### R5: 代码质量 (P2)

18. **App.tsx 拆分** — 994 行 monolith 拆为 hooks + handlers + 子组件
19. **Status icon 去重** — 3 处重复的 status→icon 映射合并到 symbols.ts
20. **Error boundary** — Ink 组件包裹错误边界，渲染失败时显示降级 UI

## Acceptance Criteria

- [ ] 活跃任务 Ctrl+C 显示确认，/cancel 可取消任务
- [ ] Esc 在提交中状态取消任务（StatusIndicator "Esc cancel" 实际生效）
- [ ] Subtask/TaskList overlay 可滚动
- [ ] 消息搜索可用（Ctrl+S 进入，n/N 跳转，Esc 退出）
- [ ] 代码块有语法高亮
- [ ] file_modified 事件显示 diff 着色
- [ ] 所有硬编码 emoji/unicode 走 symbols.ts
- [ ] /symbols 命令可切换 symbolMode
- [ ] /export 命令可导出日志
- [ ] Tab 循环匹配命令
- [ ] 所有现有测试仍通过 + 新功能有测试

## Definition of Done

- Tests added/updated
- Lint / typecheck / CI green
- Docs/notes updated if behavior changes

## Technical Approach

### PR 分解

**PR1: 任务控制** (R1) — Ctrl+C 确认 + /cancel + Esc 取消 + pause/resume 错误反馈
**PR2: 交互打磨** (R2) — overlay 滚动 + 焦点指示 + 消息搜索 + 错误提示 + 完成通知
**PR3: 视觉增强** (R3) — 符号统一 + 代码高亮 + diff 视图 + markdown 缓存
**PR4: 功能补全** (R4+R5) — /symbols + /export + 补全增强 + App.tsx 拆分 + error boundary

### 关键约束

- **Ink v5 限制**：无 useMouse、useAnimation、usePaste；需 React 19 才能升 Ink v7
- 鼠标支持需从零构建 SGR-1006 协议，复杂度高，本期不做
- 语法高亮用 cli-highlight（已有传递依赖），不引入新包
- Diff 视图用简单的 +/- 行着色，不引入 delta 级库

## Decision (ADR-lite)

**Context**: TUI 功能完整但交互有粗糙感，缺少任务控制、搜索、代码渲染等日常使用必需功能
**Decision**: 按 PR1-PR4 四期推进，优先任务控制和交互体验，视觉和功能补全次之
**Consequences**: 鼠标支持延迟到 Ink v7 迁移后；Python TUI 不在本期范围

## Out of Scope

- 鼠标支持（需 SGR-1006 从零实现，等 Ink v7 迁移）
- Python Textual TUI 更新
- gRPC 服务端改动
- Dashboard 前端改动
- Ink v5 → v7 升级（需 React 19，单独任务）
- 命令历史持久化（磁盘 I/O 复杂度偏高，延迟到下期）
- Bracketed paste（等 Ink v7 usePaste）

## Research References

- [`research/tui-ux-patterns.md`](research/tui-ux-patterns.md) — 7 大 TUI UX 模式研究：鼠标、搜索、代码渲染、进度、配置、主题、导出
- 审计发现：1 P0 + 12 P1 + 17 P2，详见 sub-agent 返回摘要

## Technical Notes

- 关键文件: reducer.ts, keymap.ts, App.tsx, ChatLog.tsx, CjkTextInput.tsx, StatusBar.tsx
- Memory: [[tui-layout-v3]], [[tui-interaction-model-v2]] (部分过时)
- cli-highlight v2.1.11 已是传递依赖（via marked-terminal）
- Ink v5 Key 类型缺 Home/End/Alt，现有 workaround 用 raw escape sequence
