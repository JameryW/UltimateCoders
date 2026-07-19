# PRD: uc_task Verbs — gRPC 宕机误归因修复（审计 #4，round 12 最后一项）

## 背景

/loop 第 15 轮。round 12 审计唯一剩项。grpc-bridge `withReconnect`（L291-318）把**一切传输失败折叠成 fallback**（false/null/[]）→ task-bridge 的 LLM 工具误报：server 宕机 → cancel 说 "not found or not cancellable"、pause 说 "not in progress"、单 status 说 "Task X not found"、列表说 "(no tasks)"。LLM 据此可能判定任务不存在而**重复提交**。

## 改（task-bridge.ts 单文件，orchestrator 已注入——F30 起用于 submit 回落 + 子任务 cancel）

### F40: 连接性前置 + 本地回落

各动词（cancel 整任务路径 / pause / resume / status 单 / status 列表）：

1. `bridge.isConnected()` false 时：
   - 控制动词（pause/resume/cancel 整任务）→ 有 orchestrator 则路由 `orchestrator.pauseTask/resumeTask/cancelTask`（F27 ControlOutcome）：ok → "Paused task X (local — gRPC server unavailable)"；not_found → "gRPC server unavailable and no local task matches ...（recent: ...）"；bad_state → 本地状态权威报错。无 orchestrator → "failed: gRPC server unavailable"。
   - status 单 → `orchestrator.resolveTask`：命中 → 本地 TaskState 渲染 + "(local view — gRPC server unavailable)" 注记；未命中 → "server unavailable" 错误（非 "not found"）。
   - status 列表 → `orchestrator.getAllTaskStates()` 渲染 + "(local view — gRPC server unavailable — remote tasks not shown)" 注记。
2. connected → 原 bridge 路径不变（远程权威）。

本地渲染：TaskState 形状（id/status/subtasks[id,status,description]），复用现有 subtaskLines 格式（无 steps tag——TaskState.subtasks 无 steps 字段）。

## 验收

- task-bridge.test.ts 新用例（扩 mock：bridge.isConnected + pauseTask/getTask/listTasks，orchestrator 全动词）：
  - pause 断连 + orchestrator ok → 消息含 "local" + "unavailable"，bridge.pauseTask 不调用。
  - pause 断连无 orchestrator 回落（not_found）→ 消息含 "unavailable" 非 "not in progress"。
  - status 列表断连 → 本地任务渲染 + "local view" 注记。
  - pause 连通 → bridge 路径（回归）。
- tsc src/ 零错误（**pwd 验证在包目录**，round 14 教训）；bun test test src 全绿。
- feature branch + PR + CI green。

## 不做

- grpc-bridge withReconnect 返回结构改造（{ok, reason} 大改签名，波及所有桥调用方——isConnected 前置以最小改动达 80% 效果）。
- /uc 斜杠命令侧同问题（extension 命令走本地 orchestrator，不经 bridge 控制动词——无此 bug；status 命令已 F26 本地解析）。

## 意义

round 12 审计 17 finding 至此**全部完成**（#14/#15 桥透传部分以客户端预检达上限，注释在案）。
