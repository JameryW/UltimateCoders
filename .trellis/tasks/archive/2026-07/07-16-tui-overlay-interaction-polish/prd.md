# PRD: TUI Overlay Interaction Polish

## 背景

`/loop` 驱动的 TUI 交互体验持续优化。多轮推进，每轮一个最小 PR。

涉及文件：
- `packages/uc-orchestrator/src/ui/task-list-overlay.ts`
- `packages/uc-orchestrator/src/ui/subtask-tree-overlay.ts`
- `packages/uc-orchestrator/src/extension.ts` (L220-314 overlay 接线)

最近合并基础：#282 (per-subtask retry) + #283 (overlay quick actions: c/p/r + d jump + Ctrl+Shift+F)。

## 候选 subtask（按价值/成本排序）

### S1 — p/r 成功无反馈 [本轮]
`c` 有双击确认 + flashMsg，但 `p`(pause) `r`(resume) 成功静默执行。`onAction` 回调（extension.ts L229-239）只在失败时 `notify`，成功无任何提示。`c` 双击成功（task-list-overlay L344）也无确认。

**改**：
- `p` 成功后 `flashMsg = "paused ${id.slice(0,8)}"`
- `r` 成功后 `flashMsg = "resumed ${id.slice(0,8)}"`
- `c` 双击成功补 `flashMsg = "cancelled ${id.slice(0,8)}"`
- 失败已有 `notify`（extension.ts），overlay 内可选补 flashMsg 命名实际状态

### S2 — detail mode 缺 q 退出 [本轮]
list mode 有 `q`（L302），detail mode 只 Esc（L233）。交互不一致。

**改**：detail 分支加 `data === "q"` 同 Esc 退出回 list。

### S3 — `/` 在 detail mode 无反馈
注释说 no-op（L246），但按 `/` 无任何提示。改：detail 下 `/` 设 flashMsg "filter N/A in detail"。

### S4 — detail mode 快捷操作 [价值高，改动大]
detail 只能 scroll，不能直接 cancel/pause/resume 当前 task。需 detail 拿 current task id + 接 onAction。下下轮。

## 验收

- 每个 subtask：`cd packages/uc-orchestrator && bun test src/ui/*.selfcheck.ts` 通过
- 走 feature branch + PR（见 git-workflow-pr-only memory）
- PR 后查 CI（见 pr-ci-check-workflow memory）

## 已完成

- S1+S2 → PR #284 merged（6b3f25e3）：p/r/c 成功 flashMsg + detail `q` back
- S3+S4 → PR #285 merged（4ce1d03e）：detail c/p/r single-tap + detail `/` flashMsg
- S5+S6 → PR #286 merged（b7f115ea）：hintLine 窄屏精简 + dead-key flashMsg
- S7+S8 → PR #287 merged（36dc6942）：retryCount copy（本地）+ progress-widget `retried N×`
- 远程 proto → PR #288 merged（bbb4a233）：SubtaskProto retry_count field 15 全链路 + executeSubtaskWithRetry clobber guard

## 下轮候选

### S9 — live step 行 width-aware 截断（progress-widget L106-143）[价值中等，compositor 兜底]
progress-widget running subtask 的 live step 行（L130 `parts.join(" ")`）拼接 agentTag+pctTag+stepTag+phaseText+statusTag+parallelTag 后**不按 width 截断整行**。compositor ANSI-truncate 兜底（不崩），但窄屏右侧 parallelTag/statusTag 丢。

另：L117 phaseText budget `width-16` 独立计算却拼进整行——phase 占 width-16 再加其他 tag 必超，budget 与拼接不一致。phase 截断后拼 statusTag 视觉断裂。

**改**：算整行 plain-text 宽度，超 width 按优先级砍（先砍 phaseText，保 agent/pct 核心）。或 phaseText budget 改 `width - 其他 tag plain 宽度和`。需 selfcheck 断言窄屏整行不超 width + 核心 tag 在。

数据源（extension.ts L156-179 progressBySubtask 填）+ 清理（L150 terminal 清）已正确，无数据层问题。

## 不做

- 不动 vendor/oh-my-pi 上游 pi-tui 核心
- 不加新 overlay 文件
- 不改 Rust 层
