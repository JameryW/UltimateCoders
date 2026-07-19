# PRD: TUI Overlay Refresh Timer + Running Elapsed

## 背景

/loop TUI 优化第 4 轮（Explore 审计 #2/#6，M effort）。ui/ 下**无任何 setInterval**——所有 widget 纯事件驱动。后果：

1. overlay 打开后时间相关显示冻结：task-list `formatAge()`（"5s ago"）只在 render 时算，overlay 开着不动就永远 "5s ago"。
2. running subtask 无 elapsed 指示——worker 挂死时 %/phase 冻结，hung 与 active 看起来完全一样（`SubtaskResult.startedAt` 已有，orchestrator.ts:124）。

## 改

### F7: 两 overlay 1s 刷新 timer

task-list-overlay + subtask-tree-overlay：
- 字段 `refreshTimer: ReturnType<typeof setInterval> | null`
- constructor 启 `setInterval(() => tui.requestRender?.(), 1000)`
- `dispose()` clearInterval（现为空实现）

render 重算 → age 活。modal overlay 寿命短，1fps 重绘成本可忽略。

### F8: subtask-tree running 行显示 elapsed

base 行（L201）status==="running" 且有 startedAt → 追加 dim `(Ns|Nm|Nh Nm)`。本地 `formatElapsed()` 模块函数（<60s→Ns，<1h→Nm，else Nh Nm）。挂死 subtask 现在可见 elapsed 持续增长而 % 冻结。

## 验收

- 两 selfcheck：
  - F7：mock tui 计数 requestRender，1.1s 后 ≥1 次；dispose 后 1.1s 无新增（timer 真清）。
  - F8：running subtask startedAt=65s 前 → 渲染含 "(1m"；completed 行无 elapsed。
- bun test test src + tsc（src/ 零错误）。
- feature branch + PR + CI green。

## 不做

- reconnect 退避计数进 footer（extension.ts 仅收 connected boolean，需 orchestrator 事件加 attempt 字段——独立接线任务，下轮或另立）。
- progress-widget elapsed（SubtaskProgressInfo 无 startedAt 字段，需扩 info 结构，另立）。
- #4 row-based pagination、#5 clipboard（下轮候选）。
