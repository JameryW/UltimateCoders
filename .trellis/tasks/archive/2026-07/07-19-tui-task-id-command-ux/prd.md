# PRD: Task ID Prefix Resolution + Command UX

## 背景

/loop 第 12 轮，新审计（/uc 命令面 + tool bridges）17 finding。本轮取最高价值簇：**task id 前缀解析 + 控制命令 UX**（#1 HIGH/M、#3/#5 S、#7/#8 S）。#2/#4（bridge 误归因，M×2）、#9/#10（并发槽位）、#11（补全，依赖 #1）留下轮。

## Bug 清单（已核实）

### F26: task id 前缀解析（审计 #1，HIGH）

UI 处处截断显示 id（status 14 字符、renderer 12、toast 8），但所有查找精确匹配（orchestrator tasks Map.get）——复制显示 id 进 `/uc status|cancel|pause|resume` 必 "not found"。

修：orchestrator 加 `resolveTask(idOrPrefix)`：精确 > 唯一前缀 > ambiguous/not_found（带 candidates：ambiguous 列匹配项，not_found 列最近 5 个 task id）。cancelTask/pauseTask/resumeTask 内部解析；extension status handler 同用；对外返回可判别结果。

### F27: 控制命令判别式结果（审计 #3 + #5）

- **#3**: cancelTask 无终态守卫——cancel 已完成任务静默改 status 为 cancelled 并重发事件（pause/resume 有守卫）。修：task 级 cancel 对 completed/cancelled/failed 返回 bad_state。
- **#5**: cancel 返回 boolean，extension toast 硬编码 "task not found"——subtask id 打错也怪 task。修：`ControlOutcome = { ok: true; taskId } | { ok: false; reason: "not_found"|"ambiguous"|"subtask_not_found"|"bad_state"; candidates? }`。subtask_not_found 带该 task 的 subtask id 列表。extension toast 按 reason 定制。

调用方适配（4 处）：extension 命令 handler（定制消息）、overlay onAction（取 .ok）、uc-rpc-server ×3（取 .ok）。

### F28: 未知子命令静默显示 help（审计 #7）

`/uc submti` 与 `/uc help` 输出完全相同，typo 像成功。修：default 分支 `parts[0]` 非空且不在 SUBCOMMANDS → 前缀 `Unknown subcommand "X".` + warning 级。

### F29: pause/resume 不切词（审计 #8）

`const tid = rest.trim()`——`/uc pause uc-1 why` → tid "uc-1 why" → not found（cancel 已切词）。修：`rest.trim().split(/\s+/)[0]`。

## 验收

- 回归测试（真实 UCOrchestrator，复用 decompose-failure 脚手架 mock.module）：failed task 入 map 后——精确/前缀解析；cancel failed task → bad_state；cancel 未知 → not_found 带 candidates；resume failed → ok；pause in_progress → ok；cancel in_progress → ok；subtask_not_found 带 subtask 列表。
- tsc src/ 零错误（4 调用方签名适配编译验证）；bun test test src 全绿。
- feature branch + PR + CI green。

## 不做（下轮）

- #2 uc_task 远程 subtask cancel 误报（bridge 无 subtaskId 字段，需路由 orchestrator）。
- #4 bridge 控制动词 gRPC 宕机误归因（需 {ok, reason} 桥返回）。
- #9/#10 workingMessage/footer active 并发共享槽位。
- #11 task id 补全（依赖 F26，下轮顺手）。
- #12-#17 tool bridge 杂项。
