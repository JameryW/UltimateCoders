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

## 不做

- 不动 vendor/oh-my-pi 上游 pi-tui 核心
- 不加新 overlay 文件
- 不改 Rust 层
