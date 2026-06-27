# OMP UI Display Dispatch Mode

## Goal

在 SubtaskTreeOverlay 的展开详情区域显示 subtask 的 dispatch_mode，让用户能看到哪个 subtask 是强制远程执行的。

## Requirements

* `SubtaskResult` interface (orchestrator.ts) 添加 `dispatchMode?: string` 字段
* SubtaskTreeOverlay 展开详情区域加一行 `Mode: remote / prefer_remote / local`
* 仅在 dispatchMode 非 PreferRemote（默认值）或明确设置了值时才显示
* 主题色用 dim（和 Retries 行一致）

## Acceptance Criteria

* [ ] SubtaskResult 有 dispatchMode 字段
* [ ] SubtaskTreeOverlay 展开详情显示 dispatch mode 行
* [ ] PreferRemote (默认) 可选显示或隐藏

## Out of Scope

* Dashboard web UI 展示
* dispatch_mode 编辑交互
* Worker capability matching

## Technical Notes

* `packages/uc-orchestrator/src/orchestrator/orchestrator.ts` — SubtaskResult interface
* `packages/uc-orchestrator/src/ui/subtask-tree-overlay.ts` — render() method, expanded detail section
* 已有模式：retryCount、error、review 行在 expanded 区展示
