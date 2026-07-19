# PRD: uc_task Cancel Routing + Concurrent Footer/Working-Message

## 背景

/loop 第 13 轮，round 12 审计剩项 #2/#9/#10（全 S）。

## Bug 清单（已核实）

### F30: uc_task cancel 带 subtask_id 时整任务被 cancel 却报子任务成功（审计 #2）

task-bridge.ts L102：`bridge.cancelTask(p.task_id, p.subtask_id)`——grpc CancelTaskRequest **无 subtaskId 字段，server 忽略**（grpc-bridge.ts L506-507 注释为证）→ 整任务被 cancel，工具却回 `Cancelled subtask X in task Y`——主动说谎。本地 orchestrator.cancelTask 支持真子任务 cancel + 级联（F27 后返回 ControlOutcome），工具却不用（除 submit 回落外完全忽略传入的 orchestrator）。

修：`subtask_id` 有值且有 orchestrator → 路由 `orchestrator.cancelTask(task_id, subtask_id)`，按 ControlOutcome 报成功/失败原因；无 subtask_id → 保持 bridge（远程任务权威），且不再传 subtask_id（server 反正忽略），消息不再声称子任务级成功（无 orchestrator 兜底时注明 "server-side"）。

### F31: task_complete 清全局 workingMessage 抹掉并发任务（审计 #9）

extension.ts task_complete handler `ctx.ui.setWorkingMessage(undefined)`——workingMessage 是单全局槽。任务并发（RPC fire-and-forget / 恢复任务）：A 完成时 B 在跑 → A 的 handler 抹掉 B 的 "UC: Wave 2/3"，直到 B 下个事件。

修：闭包记 `lastWorkingTaskId`；所有 workingMessage 设置点（task_planning/decomposed/wave_start/subtask_start）记归属；task_complete 仅当 `lastWorkingTaskId === d.taskId` 时清。

### F32: footer active 字段无归属且从不清（审计 #10）

`setField("active", ...)` 只写不清（机制存在：status-renderer 传 undefined → setStatus）。任务失败后 "UC: failed" 显示到会话结束；并发任务互踩（last event wins 无归属）。

修：所有 active 设置带短 id（`UC: <id8> · planning/paused/...`）；task_complete 时查 orchestrator.getAllTaskStates()：有其他活动任务（planning/in_progress）→ 显示其状态，否则 `setField("active", undefined)` 清除。

## 验收

- task-bridge.test.ts：cancel 带 subtask_id + orchestrator → orchestrator.cancelTask 调用（非 bridge），消息含 "cascade"；ControlOutcome 失败 → 消息含 reason；无 subtask_id → bridge 路径不变。
- tsc src/ 零错误；bun test test src 全绿。
- extension 侧（F31/F32）无单测基建——tsc + 逐 handler 模式对照。
- feature branch + PR + CI green。

## 不做

- #4 bridge 控制动词 gRPC 宕机误归因（M，需 {ok,reason} 桥返回，下轮）。
- #11 id 补全、#12-#17 杂项（后续轮）。
