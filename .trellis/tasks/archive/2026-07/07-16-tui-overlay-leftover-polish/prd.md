# PRD: TUI Overlay Leftover Polish

## 背景

`/loop` 第二轮 TUI 优化。上轮 `tui-overlay-interaction-polish`（archived，6 PR #284-#288/#290）覆盖交互/错误/retry/live-step。本轮回挖遗留边角。

涉及文件：
- `packages/uc-orchestrator/src/ui/subtask-tree-overlay.ts`
- `packages/uc-orchestrator/src/ui/task-list-overlay.ts`

## 候选

### S10 — subtask-tree expand detail 行 width-aware [价值中等]
subtask-tree expand（Enter 展开）detail 行（L199-222）拼 `parts.join(" · ")`：formatErrorForDisplay(width-12) + review + retry×N + dispatchMode 单行。error 占 width-12 后拼其他 tag 必超 width，compositor 截断右侧（retry/mode/review 丢）。同 S9（progress-widget live-step）模式但未修。

**改**：复用 S9 的 greedy 思路或简化——error 单独一行（不拼），review/retry/mode 拼第二短行按 width 截断。需 selfcheck 断言 expand 行不超 width + error 完整（error 是诊断关键，不该被 tag 挤掉）。

### S11 — overlay 间跳转焦点链审查 [调研，可能无 bug]
subtask-tree `d` → task-list detail（initialDetailTaskId）→ Esc 回 list。#285 已让 detail 可 c/p/r。审查：跳转后 cursor 位置、detail scroll 重置、返回后焦点是否合理。可能无真实缺口——若审查发现 bug 才做。

## 验收

- selfcheck + tsc 通过
- feature branch + PR（git-workflow-pr-only memory）
- PR 后查 CI（pr-ci-check-workflow memory）

## 不做

- 不动 vendor/oh-my-pi
- 不改 Rust 层
