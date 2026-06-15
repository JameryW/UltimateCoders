# TUI 状态模型重构与交互优化

## Goal

重构 TUI 的状态管理和交互模型，解决 render 中 setState、subtask 状态同步不一致、ChatLog 无滚动/缓存、离线 timer 未清理等根本问题，然后补齐键盘交互、响应式布局、终端兼容性。

## Status: ✅ COMPLETED

## Requirements

### PR1：状态稳定化 + 基础交互 ✅ (commit 3422727)

**1.1 useReducer + 单一 state 对象** ✅
* TuiState + tuiReducer in tui/src/reducer.ts (185→220 lines)
* 15+ action types
* render 路径中零 setState

**1.2 subtask 状态同步修复** ✅
* 深度比较 (id + status + assignedWorker)
* 不再只按长度判断

**1.3 stream event → chat message 转换移出 render** ✅
* formatTaskEvent() in tui/src/formatters.ts
* useEffect dispatch ADD_MESSAGES

**1.4 离线 timer 统一追踪和清理** ✅
* ADD_OFFLINE_TIMER / CLEAR_OFFLINE_TIMERS actions
* 新任务提交时清理旧 timer

**1.5 全局 useInput + Tab 切换 pane** ✅
* selectedPane: 'input' | 'chat' | 'subtask'
* Tab cycles panes, Ctrl+P/Ctrl+L per-pane

**1.6 输入历史** ✅
* Up/Down in input pane
* MAX 50 entries, dedup most recent

**1.7 ChatLog 窗口切片渲染** ✅
* logOffset + followLog in reducer
* visibleLines from stdout.rows
* SCROLL_UP/SCROLL_DOWN actions

**1.8 状态栏增强** ✅
* serverAddr, connectionState, isStreaming, activeTaskId, lastError, mode, selectedPane

### PR2：信息架构 + 响应式视觉 ✅ (commit 9131177)

**2.1 ChatLog 事件过滤** ✅
* EventFilter type: all/task/subtask/tool/error
* Ctrl+F cycles filter
* eventType on ChatMessage

**2.2 Subtask 面板增强** ✅
* assignedWorker display for running items
* string-width CJK-safe truncation
* Task description header in panel
* maxWidth prop for responsive

**2.3 响应式布局** ✅
* >=100 cols: dual pane
* 80-99 cols: compressed right pane (maxWidth=25)
* <80 cols: single pane, Tab switches

**2.4 符号策略** ✅
* tui/src/symbols.ts with unicode/ascii/auto
* auto detects TERM/NO_COLOR/CI/LC_ALL
* ASCII: [ ], [~], [*], [x], [!]

**2.5 分隔线自适应** ✅
* Based on stdout.columns - 2

**2.6 useCursor pos.y fix** ✅
* pos.y now offsets row from bottom
* BOTTOM_RESERVED = 2 constant

**2.7 颜色语义固定** ✅
* cyan: active/live, green: success, yellow: offline/warning, red: error, dim: metadata

## Acceptance Criteria

- [x] gRPC 收到 subtask_started/completed/failed 后，即使数量不变，右侧即时更新
- [x] App render 过程中无 setState
- [x] 80x24、100x30、140x40 三种终端尺寸下无明显溢出 (响应式布局实现)
- [x] 中文输入、移动光标、删除、提交后 IME 候选框位置正确 (pos.y fix)
- [x] 断开 gRPC 后能看到原因 (lastError in StatusBar)，服务恢复后 Ctrl+R 可恢复
- [x] Ctrl+P 可暂停/恢复当前任务
- [x] Tab 可切换 pane，当前 pane 在状态栏显示
- [x] ChatLog 可 PageUp/PageDown 滚动，自动跟随底部
- [x] npm run typecheck 和 npm run build 保持通过

## Definition of Done

* [x] typecheck + build passing
* [ ] Tests (pending — event reducer, event formatter, CJK input, offline timer cleanup)
* [ ] Docs/notes updated (pending)

## Decision (ADR-lite)

**Context**: TUI 状态分散在多个 useState + render-side-effect，导致状态同步不一致、后续功能难加
**Decision**:
1. useReducer + 单一 TuiState — 所有状态变更走 dispatch，render 零 setState
2. 全局 useInput + Tab 切换 pane — 单一键盘入口，selectedPane 驱动焦点
3. ChatLog 窗口切片渲染 — logOffset + followLog，只渲染一屏
4. 分 2 个 PR：PR1 状态+交互，PR2 信息架构+响应式
5. Symbol strategy: unicode/ascii/auto with env detection
**Consequences**: reducer 文件~220 行；Tab 切换时 input 暂时不可编辑；窗口切片每帧 re-render 一屏内容但量级可控

## Out of Scope

* 不替换 Ink 框架
* 不实现 gRPC streaming 指数退避重试（仅 Ctrl+R 手动重连）
* 不做 Web Dashboard 功能
* 不改后端 proto/接口
* 不用 ink-text-input 或其他 UI 库

## Technical Notes

* PR1 新增: tui/src/reducer.ts, tui/src/formatters.ts
* PR2 新增: tui/src/symbols.ts
* Modified: App.tsx, ChatLog.tsx, SubtaskTree.tsx, TaskInput.tsx, CjkTextInput.tsx, StatusBar.tsx, useCursor.ts
* Total delta: +1196 -147 lines across 2 commits
