# PRD: Reconnect Progress in TUI Footer

## 背景

/loop TUI 优化第 6 轮（审计候选：reconnect 退避不可见）。GrpcBridge 断线后 tryReconnect 指数退避 500ms→30s（backoff.ts），期间 footer 只显示静态 "UC: disconnected"（extension.ts L200-202，connection_state 仅 boolean）。用户不知系统在重试、第几次、下次多久——像死了一样。

## 改（最小接线，4 文件 + tests）

### F10: reconnect 进度事件链

1. **backoff.ts**: `BackoffOptions` 加 `onAttempt?: (attempt: number, delayMs: number) => void`。`sleepBackoff` 算出 delay 后、sleep 前调用——jitter 下显示值与实际 sleep 精确一致（不二次计算）。
2. **grpc-bridge.ts**: config 加 `onReconnectAttempt?: (attempt: number, nextDelayMs: number) => void` + setter `setOnReconnectAttempt`（mirror setOnConnectionChange L159-161）。tryReconnect 循环内：`sleepBackoff(attempt, { ...backoff, onAttempt: (_a, d) => this.config.onReconnectAttempt?.(attempt + 1, d) })`（对外 1-based，人看的）。
3. **events.ts**: `OrchestratorEvents` 加 `reconnect_progress: { attempt: number; nextRetryMs: number }`。不复用 connection_state（那是状态变更事件，重试中重复发 false 语义漂移）。
4. **orchestrator.ts**: setOnConnectionChange 接线旁（L265-272）加 `this.bridge.setOnReconnectAttempt((attempt, nextRetryMs) => this.events.emit("reconnect_progress", { attempt, nextRetryMs }))`。外部注入 bridge 同样吃接线（setter 模式）。
5. **extension.ts**: `case "reconnect_progress"` → `statusRenderer.setField("conn", \`UC: reconnecting · try ${attempt} · ${secs}s\`)`。connection_state true 到来时自然覆盖回 "UC: connected"。

## 验收

- backoff.test.ts：onAttempt 收到 (attempt, 精确 delay)；delay null（耗尽）时不调用。
- grpc-bridge.test.ts：reconnect 失败路径触发 onReconnectAttempt，attempt 1-based，delay 匹配退避曲线。
- 既有 126 bun tests 全绿；tsc src/ 零错误。
- feature branch + PR + CI green。

## 不做

- WatchTask 流重连进度（第二恢复路径，bridge 连接为主；流重连仅在 bridge connected 时触发，用户感知弱）。
- footer 倒计时逐秒刷新（F7 式 timer）——每次 attempt 事件刷新一次已够；两次 attempt 间隔本身是退避时长，静态 "· 16s" 可接受。
- Dashboard 侧 reconnect 显示（useGrpcClient 自有退避，另立）。
