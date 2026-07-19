# PRD: TUI Display Correctness Round 2

## 背景

/loop 第 8 轮。第 7 轮审计剩 9 finding，本 PR 取显示正确性子集（#2/#3/#9-icon/#5，全 S，同主题：渲染器显示与实际不符）。

## Bug 清单（已对当前代码核实）

### F15: task-result 展开视图死码（审计 #2，HIGH）

`task-result-renderer.ts` L41 展开行读 `details.task`，但 emitter（orchestrator.ts L721-727）只发 `{taskId, status, subtaskCount}`——`task` 永为 undefined → 用户展开只见 summary header。selfcheck 自造 details.task 掩盖。

修：`createTaskResultRenderer(getTask?: (id) => TaskState | undefined)`，渲染时 `details.task ?? getTask?.(details.taskId)`。extension.ts L67 传 `(id) => orchestrator.getTaskState(id)`。evictCompletedTasks 清掉的老任务展开行空白——可接受（消息本已陈旧）。

### F16: status-formatter width 预算忘前缀（审计 #3，HIGH）

- L53 detail desc：前缀 `"  Description: "` 15 字符，预算 `width-2` → 溢出 13。
- L55 task error：前缀 `"  Error: "` 9 字符，预算 `width-2` → 溢出 7；且 raw slice 无省略号、不走 formatErrorForDisplay（与 subtask error 路径 L112 不一致，无分类 label）。

修：desc 预算 `width-15`；task error 改走 `formatErrorForDisplay(task.error, width-9, ...)`（同 subtask 路径，得分类 label + 省略号 + ANSI 安全）。

### F17: stIcon.length 含 ANSI 当可视宽（审计 #9 之 icon 部分）

status-formatter L101 `descBudget` 减 `stIcon.length`——真实终端含 ~11 escape 字符（`\x1b[32m✓\x1b[0m`），desc 多砍 ~10 字符。selfcheck 无 ANSI theme 掩盖。

修：减 1（icon 可视宽恒 1）。deps 后缀预算留待下轮（低频、需 cap 策略）。

### F18: 多行 error 破单行契约（审计 #5，MED）

`error-format.ts` classifyError rootCause（L80-83）不拍平 `\n`；三处 consumer（progress-widget L206、status-formatter L112、task-result-renderer L57）按单行 push → stderrTail/stack trace error 注入额外未截断行，破坏 widget 结构 + 绕过 width cap。

修：classifyError 内 `rootCause = rootCause.replace(/\s*\n+\s*/g, " ").trim()`。errorStr 本体不动，仅 rootCause。

## 验收

- task-result-renderer.selfcheck.ts：getter 路径（无 details.task，getter 供 task）展开行渲染；getter 缺失 → 仅 header（优雅降级）。
- status-formatter.selfcheck.ts：**整行**长度 ≤ width（desc 行 + task error 行，ANSI-stripped 测量）；task error 行走 formatErrorForDisplay（含 ⚠ label）；ANSI theme 下 desc 预算按 icon 可视宽 1 算。
- error-format test：多行 rootCause 拍平为单行。
- bun test test src + tsc（src/ 零错误）+ feature branch + PR + CI green。

## 不做（下轮）

#6 progress elapsed（firstSeen stamping）、#10 0% 无数据、#12 error 分类词边界、#13 小打磨、clipboard、#9 之 deps 后缀预算。
