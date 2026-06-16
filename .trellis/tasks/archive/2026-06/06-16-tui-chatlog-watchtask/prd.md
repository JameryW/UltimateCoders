# PRD: TUI 体验优化 — ChatLog 美化、进度条、消息折叠、WatchTask 流

## 目标
把 TUI 从"功能完整"推到"专业级终端工具"的视觉和交互水准。

## Acceptance Criteria

### AC1: Subtask 进度条
- SubtaskTree 标题显示 ASCII 进度条 `▓▓░░ 50%`（unicode）或 `[##--] 50%`（ascii）
- 进度条宽度自适应 maxWidth

### AC2: Subtask 依赖指示
- subtask 行末显示简短依赖指示，如 `→2,3` 表示被 subtask 2/3 依赖
- 只在有依赖时显示，窄屏下截断

### AC3: 空输入轻量提示
- 空 Enter 时在 input 行右侧显示灰色提示 `↵ Enter a task description`
- 提示 2 秒后自动消失，不写入 ChatLog

### AC4: ChatLog 消息美化
- 用户消息前缀 `>` + bold，system 消息无前缀
- subtask 状态变更事件用颜色区分（completed=green, failed=red, in_progress=cyan）
- 时间戳格式从 `[HH:MM:SS]` 精简为 `[HH:MM]`

### AC5: 多行消息折叠
- 超过 3 行的消息默认只显示第 1 行 + `[+N more]`
- Chat focus 下 Enter/click 展开全文，再 Enter 折叠
- 用户消息和短消息（≤3行）不受影响

### AC6: gRPC WatchTask 实时流
- connected 时自动调用 `client.watchTask({taskId})` 建立流
- 实时接收 subtask 状态变更事件 → 更新 ChatLog + SubtaskTree
- 断连时停止流，重连后自动恢复
- 与现有 useTaskEvents hook 集成，复用事件处理逻辑

## Implementation Priority
- P0: AC1 + AC2 + AC3（低难度组合，快速见效）
- P1: AC4（ChatLog 美化）
- P2: AC5（多行消息折叠）
- P3: AC6（gRPC WatchTask，架构级）
