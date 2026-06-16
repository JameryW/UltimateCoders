# TUI Layout Optimization — Reference Claude Code + Codex Design

## Goal

参考 Claude Code 和 Codex CLI 的 TUI 设计，优化 UltimateCoders TUI 的界面布局、交互体验和视觉效果，使其达到同级别 AI Coding CLI 的 UX 水准。

## What I already know

### Claude Code 关键设计模式
- **Input 固定底部**：fullscreen 模式下输入框始终在底部，不随输出滚动
- **Shimmer spinner**：工作时显示带 shimmer 动画的 spinner + 耗时显示 `(5s)` + 可中断提示
- **Tool call 折叠**：默认单行折叠 "Called slack 3 times"，点击/Ctrl+O 展开
- **Progressive footer collapse**：状态栏按终端宽度逐级折叠（完整→缩短→仅模式→空）
- **Middle truncation**：命令输出显示 head+tail + `... +N lines` 中间省略
- **Theme token system**：40+ 可定制颜色 token，含 shimmer 配对

### Codex CLI 关键设计模式
- **Ratatui alternate screen**：默认全屏模式，flicker-free
- **10 种 spinner 变体**：用户可选 ASCII art spinner 风格
- **Elapsed time compact format**：`(5s)` / `(1m 30s)` / `(2h 15m 00s)`
- **Collaboration mode 指示**：Plan(magenta) / PairProgramming(cyan) / Execute(dim)
- **Diff 三层色彩**：TrueColor → ANSI-256 → ANSI-16 自动降级
- **Explore merging**：连续相似 tool call 合并显示
- **Footer 智能折叠**：根据终端宽度从完整快捷键→缩短→仅模式→空

### 当前 UC TUI 差距
1. **Input 不固定**：input 区域在主区域下方，随内容滚动
2. **无 working indicator**：submit 后没有 spinner/动画，只有文字
3. **Tool call 无折叠**：所有事件全量展示
4. **Footer 不智能折叠**：StatusBar 有 segment budget，但没有 progressive collapse
5. **无 elapsed time**：任务执行不显示耗时
6. **Message 格式单调**：system/user/assistant 消息视觉区分不够
7. **无 shimmer/动画**：连接/工作中没有视觉动效

## Requirements

### P0: Input 固定底部 + Working indicator
- Input 区域始终固定在终端底部（不随 ChatLog 滚动）
- Submit 后显示 shimmer spinner + elapsed time + "Esc to cancel"
- Input 上方显示单行 status indicator（类似 Codex 的 StatusIndicatorWidget）

### P1: Message 视觉层级 + Tool call 折叠
- User message：左边框 + 亮色标签
- System message：dim + icon 前缀
- Assistant/tool：可折叠，默认只显示 summary 行
- Tool call output 默认折叠，? 或 Ctrl+O 展开完整输出

### P2: Elapsed time + Progress 增强
- Status indicator 显示 elapsed time compact format：`(5s)` / `(1m 30s)`
- SubtaskTree 进度条 + 百分比
- 任务完成/失败有明确的 ✅/❌ 视觉反馈

### P3: Footer progressive collapse
- StatusBar 按 terminal width 智能折叠：
  - >100 cols：完整信息
  - 80-100 cols：去掉 help，缩短 label
  - 60-80 cols：只保留 connection + progress + focus
  - <60 cols：只保留 connection + progress

## Acceptance Criteria

- [ ] Input 始终固定在终端底部
- [ ] Submit 后显示 working spinner + elapsed time
- [ ] Esc 可取消/中断任务
- [ ] User/System/Tool 消息有视觉区分
- [ ] Tool call 默认折叠，快捷键可展开
- [ ] Footer 在 60/80/100+ 列下合理显示
- [ ] 所有现有测试通过

## Definition of Done

- Tests added/updated
- TypeScript 编译通过
- 终端实际验证布局正确
- 不换行、不溢出

## Out of Scope

- Markdown 渲染增强（code block syntax highlighting）
- Diff 渲染（需要专门的 diff 组件）
- Shimmer 动画（Ink 不支持动画 shimmer，需要自定义）
- 自定义 theme token system
- 全屏 alternate screen 模式

## Technical Approach

### Input 固定底部
当前布局：`<Box column>` → Header + Main(Chat+Subtask) + Input + StatusBar
改为：`<Box column>` → Header + Main(flex-grow) + StatusBar，Input 叠加在 Main 底部

关键文件：
- `tui/src/components/App.tsx` — 主布局
- `tui/src/components/TaskInput.tsx` — 输入组件
- `tui/src/components/ChatLog.tsx` — 聊天日志

### Working indicator
新增 `StatusIndicator` 组件，显示在 Input 上方：
- 提交中：`⠋ Working... (3s)  Esc cancel`
- 流式中：`● Streaming... (12s)`
- 空闲：不显示

### Message 视觉层级
- `ChatLog.tsx` 的 `formatMessage` 增加 user/system/tool 分组样式
- Tool call 事件默认折叠，只显示 `⚙ tool_name (2s)`
- `?` 或点击展开完整输出

### Elapsed time
- `useTaskEvents.ts` 增加 `startedAt` 时间戳
- `App.tsx` 计算 elapsed 并传给 StatusIndicator

## Technical Notes

- Ink 的 `<Box>` flex 布局可以实现 input 固定底部
- Ink 不支持自定义 ANSI 动画，spinner 用 setInterval 切换字符实现
- Middle truncation 需要计算可见行数（已由 ChatLog slicing 实现）
- Footer progressive collapse 已由 StatusBar segment budget 部分实现
