# StatusBar 宽度预算 + gRPC 错误降噪

## Goal

修复 TUI StatusBar 在 gRPC 连接失败时溢出换行的问题，并对启动期连接错误降噪，让"server unavailable"表现为静默的 offline 状态而非红色错误轰炸。

## Requirements

### P0: 止血 — StatusBar 短信息

- StatusBar 只保留优先级 segment：connection | Worker | Backend | progress | focus | view | help
- 移除：mode / Task ID / serverAddr / lastError 长文本
- 连接状态只显示短码：`grpc` / `offline` / `retry 3/5`
- 不在 StatusBar 显示完整 lastError，只显示 ERR 或 offline
- Ctrl+R 在 connecting 状态下不追加日志，或更新同一条状态消息

### P1: 宽度预算

- StatusBar 按 terminalWidth 逐段追加 segment，放不下就省略
- segment 优先级：connection > progress > focus > view > retry > help
- 错误文本只显示短码（如 ECONNREFUSED），不显示完整 gRPC message
- Worker / Backend 保留（用户偏好）

### P2: 连接错误降噪

- UNAVAILABLE / ECONNREFUSED / timeout 启动期错误统一视为 offline，不标红色 error
- 自动重试静默进行，StatusBar 显示 `retry N/5` 即可
- 非 UNEXPECTED 错误才写入 ChatLog（proto 缺失、权限错误、提交失败等）
- offline 提示每个 offline session 只出现一次（已有 hasShownOfflineMsg 机制）

### P3: 帮助文案预算

- StatusBar 只显示 2-3 个当前最有用快捷键
- 完整快捷键放到 ? overlay（已有）
- getStatusBarHelp() 按宽度预算输出，不再拼全部

## Acceptance Criteria

- [ ] StatusBar 在 80 列终端不换行，error 状态也不换行
- [ ] StatusBar 在 60 列终端不换行（只显示 connection + progress + focus）
- [ ] gRPC 连接失败时 StatusBar 显示 `✗ offline | P 0/0 | F Input | retry 3/5  ? help`
- [ ] gRPC 连接成功时 StatusBar 显示 `● grpc | Worker grpc-worker | Backend grpc | P 0/0 | F Input | View Chat  ? help`
- [ ] Ctrl+R 在 connecting 状态下不追加重复日志
- [ ] lastError 不再出现在 StatusBar
- [ ] UNAVAILABLE 类错误不标红色，用黄色 offline
- [ ] 现有测试通过

## Definition of Done

- Tests added/updated（StatusBar 宽度预算纯函数可测）
- Lint / typecheck / CI green
- 终端实际验证不换行

## Out of Scope

- Help overlay 重新设计（已有 ? overlay，保持现状）
- gRPC client 重连逻辑修改（重连策略本身没问题，只是展示降噪）
- Python TUI StatusBar（独立实现，不在本次范围）

## Technical Approach

核心改动三个文件：

1. **StatusBar.tsx** — 重写为 segment-based 布局，按宽度预算逐段输出
2. **keymap.ts** — getStatusBarHelp() 改为按预算输出 2-3 个快捷键
3. **App.tsx** — Ctrl+R connecting 状态去重；connection error 颜色从 red 改 yellow

推荐最终格式：
```
● grpc | grpc-worker | grpc | P 2/5 | F Input | View Chat  ? help
○ offline | offline | subprocess | P 0/0 | F Input | C-R reconnect  ? help
✗ offline | offline | disconnected | P 0/0 | F Input | retry 3/5  ? help
```

## Technical Notes

- StatusBar.tsx:56 — 当前实现把所有字段拼进一个 Box，无宽度预算
- useGrpcClient.ts:167 — timeout 错误是长字符串 `Connection timeout to localhost:50051`
- App.tsx:352-354 — Ctrl+R 每次 addMessage，需要去重
- App.tsx:120-128 — connection state change 已有 ChatLog 通知，但 error 态用了红色
- keymap.ts:105-139 — getStatusBarHelp() 在宽屏拼全部快捷键，容易超宽
