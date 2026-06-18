# TUI Enhancements: Polish, Keybindings, UX Improvements

## Goal

打磨 Ink TUI 的现有功能——修复键位问题、增强状态反馈、改善输入体验、优化聊天显示，使 TUI 更接近 Claude Code 的使用体验。

## What I already know

* **架构**: Ink 5 + React 18, TypeScript, 单列垂直布局 (v3)
* **交互模型**: focusedArea (input|chat) + Ctrl+T subtask overlay
* **已知限制**: Home/End 运行时实际不工作、Delete=Backspace、StatusBar 倒计时不刷新、无 undo/redo、无 word navigation、无 paste 处理

## Research References

* [`research/ink-keyboard-handling.md`](research/ink-keyboard-handling.md) — Ink v5 键位限制及 workaround 方案
* [`research/tui-ux-patterns.md`](research/tui-ux-patterns.md) — 状态反馈、输入 UX、聊天显示、状态栏最佳实践

## Requirements

### 1. 键位修复 (P0)

**1a. Home/End 键修复**
- 当前 `(key as any).home/.end` 在 Ink v5 运行时返回 `undefined`，Home/End 实际不工作
- 方案：通过 `useStdin()` 监听 raw `'input'` 事件，用 `parseKeypress` 解析完整键名，绕过 `useInput` 的 Key 类型限制
- 在 App.tsx 添加独立的 raw input handler，检测 home/end 后 dispatch reducer action
- keymap.ts 添加 Home/End 命令定义
- 升级路径：Ink v6.6.0 原生支持 home/end（需 React 19），可后续升级时移除 workaround

**1b. Ctrl+W 作为 Shift+Tab 备选**
- 研究确认 Shift+Tab 在 tmux/screen 下实际正常工作
- 但添加 Ctrl+W 作为备选快捷键，提升可发现性
- keymap.ts 添加命令定义，App.tsx 添加分派

**1c. Word navigation (Ctrl+Left/Right)**
- 添加 Ctrl+Left (word backward) 和 Ctrl+Right (word forward) 支持
- 在 `cjk-input-utils.ts` 添加 `wordBoundaryBackward`/`wordBoundaryForward` 函数
- 使用 `GraphemeSplitter` + Unicode word boundary 规则

### 2. 状态反馈增强 (P1)

**2a. StatusBar 重连倒计时刷新**
- 当前 `retrySecondsLeft` 只在状态变化时计算，不每秒刷新
- 方案：在 StatusBar 组件中使用 `useAnimation({interval: 1000, isActive: isRetrying})` 驱动每秒重渲染
- `useAnimation` 是 Ink 5 内置 hook，多个实例共享同一 timer，不会造成性能问题

**2b. StatusIndicator 迁移到 useAnimation**
- 当前使用手动 `setInterval` + `useState` 驱动 spinner 和 elapsed time
- 迁移到 `useAnimation({interval: 80, isActive: isSubmitting || isStreaming})`
- `frame` 替代手动计数器，`time` 替代手动 elapsed 计算
- Spinner 间隔从 100ms 调整为 80ms（匹配标准 `dots` pattern）

**2c. Subtask 进度条**
- 在 StatusIndicator 或 StatusBar 中添加 subtask 进度条
- 使用 `symbols.ts` 已有的 `barFilled`/`barEmpty` 字符
- 格式：`[████░░░░░░] 4/10`

### 3. 输入体验改善 (P1)

**3a. 多行输入改进**
- 添加 Alt+Enter 作为 Ctrl+J 之外的新行快捷键
- 在 TaskInput 中显示多行指示器（如 `2L` 表示 2 行输入）
- keymap.ts 添加 Alt+Enter 命令

**3b. Undo/Redo**
- 在 CjkTextInput 中添加 undo/redo 栈（参考 Gemini CLI TextBuffer）
- 栈深度限制 50
- 每次变更前创建快照（text + cursorOffset）
- 快捷键：macOS Cmd+Z/Cmd+Shift+Z，Linux Alt+Z/Alt+Shift+Z（避免 Ctrl+Z SIGTSTP 冲突）
- 在 StatusBar 显示 undo 可用状态提示

**3c. Paste 处理**
- 使用 Ink 5 的 `usePaste` hook 处理 bracketed paste mode
- 粘贴文本作为单个操作插入（一次 undo 可撤销整个粘贴）
- 避免长粘贴文本被逐字符处理导致卡顿

### 4. 聊天显示优化 (P2)

**4a. 滚动改进**
- PageUp/PageDown 增大滚动步长（当前 Up/Down 一次滚 1 行，添加 1 页 = 终端高度 - 2）
- Home/End 在 chat focus 下跳转到顶部/底部（依赖 1a 修复）
- 添加 scroll-to-bottom 快捷提示（当 followLog 关闭时，StatusBar 显示 "End: follow"）

**4b. 消息时间戳改进**
- 消息间隔超过 5 分钟时，插入分隔线 `── 12:30 ──`
- 保留当前 HH:MM 格式不变

**4c. 消息折叠改进**
- 展开单条消息后，Enter 切换为仅折叠当前消息（而非 toggle all）
- 折叠的 tool_result 消息显示退出码（成功 ✓ / 失败 ✗）

## Acceptance Criteria

- [ ] Home/End 键在 input focus 和 chat focus 下正常工作
- [ ] Ctrl+W 可以切换焦点（Shift+Tab 备选）
- [ ] Ctrl+Left/Right 在 input 中按 word 跳转
- [ ] StatusBar 重连倒计时每秒更新
- [ ] StatusIndicator 使用 useAnimation 替代手动 setInterval
- [ ] 多行输入支持 Alt+Enter 新行
- [ ] Undo/Redo 在 CjkTextInput 中正常工作
- [ ] Paste 粘贴文本作为单次操作
- [ ] Chat PageUp/PageDown 滚动一页
- [ ] 消息间隔 >5min 显示时间分隔线

## Definition of Done

* 新功能有对应 vitest 测试
* `npm run build` 成功
* `vitest` 全部通过
* 手动运行验证
* keymap.ts 是快捷键的唯一真相来源

## Out of Scope

* Python Textual TUI
* gRPC proto 变更
* 真正的流式 Markdown 渲染
* 鼠标滚轮支持（需要 Ink 额外支持）
* 虚拟化列表（当前 2000 条上限足够）
* Ink v6/v7 升级（需 React 19，属于独立任务）

## Technical Approach

**键位 workaround**: 在 App.tsx 添加 `useStdin()` + `parseKeypress` raw handler，与 `useInput` 并行运行。home/end 事件通过 reducer action 传递。

**useAnimation 迁移**: 直接替换 StatusIndicator.tsx 和 StatusBar.tsx 中的手动 timer，利用 Ink 5 内置 hook。

**Undo/Redo**: 在 CjkTextInput 组件内维护 `undoStack: Snapshot[]` 和 `redoStack: Snapshot[]`，Snapshot = `{text, cursorOffset}`。每次变更前 push 当前状态到 undoStack。按键分派 undo/redo action。

**Paste**: 使用 Ink 5 的 `usePaste(onPaste)` hook，在 paste 回调中将文本插入到光标位置，作为单次 undo 操作。

**Word navigation**: 在 `cjk-input-utils.ts` 添加 `findWordBoundaryBackward(text, offset)` 和 `findWordBoundaryForward(text, offset)`，基于 `\s` + CJK character 边界检测。

## Technical Notes

* Ink v5 `useAnimation` 已在当前版本可用（无需升级）
* Ink v5 `usePaste` 已在当前版本可用
* Ink v5 `parseKeypress` 是内部 API，需 `import { parseKeypress } from 'ink/lib/parse-keypress'`
* Ctrl+Z 在 Linux/macOS 发送 SIGTSTP，不能用做 undo 快捷键
* `useStdin()` 提供 `stdin` 和 `internal_eventEmitter` 访问
