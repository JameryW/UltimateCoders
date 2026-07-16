# feat: tui overlay quick actions (cancel/pause/resume + jump-to-failed)

## Goal

Three TUI overlay interaction gaps, all in one task:

1. **task-list overlay quick actions** — `c`/`p`/`r` keys on cursor task do cancel/pause/resume (currently only `/uc cancel|pause|resume <id>` command, split from the overlay the user is inspecting).
2. **subtask-tree overlay → task-list detail jump** — from a subtask row, jump to that subtask's parent task in the task-list detail view.
3. **progress-widget → jump to failed subtask** — a shortcut opens the subtask-tree overlay with the cursor on the first failed subtask (fastest path to the retry key).

## What I already know (from repo inspection)

- **Overlay pattern**: `ctx.ui.custom(factory, { overlay: true })`. Factory `(tui, theme, keybindings, done) => Component & { dispose? }`. `custom<T>` returns `Promise<T>`, resolves on `done(value)`. See vendor `types.ts:121`.
- **Both overlays** (`subtask-tree-overlay.ts`, `task-list-overlay.ts`) already have `handleInput(data)` matching raw xterm bytes + vim-style `g`/`G`/`j`/`k`. Search/filter mode via `/` already works on both. `r`/`R` retry already wired in subtask-tree → `onRetry(taskId, subtaskId)`.
- **Overlay opts are closures** — extension.ts:220 passes `onRetry`, `onClose`, `tasks`, `getTask`. Adding `onAction`/`onJump` callbacks is the same pattern; the closure holds `ctx` so it can call `ctx.ui.custom` to open a new overlay.
- **Orchestrator actions exist**: `cancelTask`, `pauseTask`, `resumeTask` (task-scoped), `retrySubtask` (per-subtask, PR #282 merged 98ae5d33). All return `Promise<boolean>` + already `ctx.ui.notify` toast on the action.
- **Overlay nesting**: calling `ctx.ui.custom(newOverlay)` from within an overlay's handler — the new overlay stacks. Must `done()` current overlay first (close) then open new, so they don't stack. The `onClose`/`onAction` closure is the seam.
- **progress-widget** (`progress-widget.ts`) is a NON-interactive Component (only `render`/`invalidate`/`dispose`, no `handleInput`). So gap3 must be a **global shortcut** (like Ctrl+T), not a widget key. Widget renders the failed-subtask summary already; the shortcut opens the tree on the first failed.
- **Shortcuts registered**: Ctrl+T (subtask tree), Ctrl+Shift+T (task list). Ctrl+Shift+F (jump to failed) fits the pattern; `KeyId` is opaque string.
- **detailLines carry ANSI** (task-list detail) — overlay compositor truncates ANSI-safely; emit full lines, don't raw-slice (existing `ponytail:` note in task-list-overlay renderDetail).

## Assumptions (temporary)

- gap1 `c`/`p`/`r` act on the task under the cursor in LIST mode (not detail mode — detail is read-only scroll).
- `c` (cancel) is destructive/irreversible — needs a confirm step. `p`/`r` (pause/resume) are reversible — no confirm.
- gap2 jump target: from subtask-tree row → open task-list detail for that subtask's parent taskId. Close subtask-tree first.
- gap3: Ctrl+Shift+F opens subtask-tree with cursor pre-set to first `failed` subtask (skip completed/running/pending). If no failed subtask, toast "no failed subtasks".

## Open Questions

* ~~Q1 (UX): cancel confirm flow~~ → **Decision: in-overlay double-tap.** First `c` sets `flashMsg` "press c again to cancel task X"; second `c` executes, any other key clears. Reuses existing flashMsg pattern (mirrors retry dead-key feedback). Overlay stays open; no stacking.

## Requirements

- task-list overlay LIST mode: `c` cancel / `p` pause / `r` resume → `onAction(taskId, "cancel"|"pause"|"resume")`.
  - `c` double-tap confirm (first sets flashMsg, second executes); `p`/`r` act immediately.
  - refuse + toast if action invalid for task state (e.g. cancel a completed/cancelled task; pause a non-running one).
- subtask-tree overlay: `d` (jump to parent task Detail) → `onJumpToTask(taskId)` → close tree, open task-list detail for that task.
- global shortcut **Ctrl+Shift+F** → open subtask-tree with cursor pre-set to first `failed` subtask; no failed → toast "no failed subtasks".
- hint lines on both overlays list new keys (`c/p/r` / `d`; Ctrl+Shift+F documented in extension header).
- toasts on every action (success + refuse).
- selfcheck covers: new key routing, cursor-on-failed pre-set, jump closes-then-opens, double-tap confirm state.

## Acceptance Criteria

- [ ] task-list `c` double-tap cancels cursor task; `p` pauses; `r` resumes.
- [ ] cancel/ pause/resume on a task in the wrong state toasts refusal (orchestrator returns false).
- [ ] subtask-tree `d` opens task-list detail for the subtask's parent task (tree closed first).
- [ ] Ctrl+Shift+F opens subtask-tree with cursor on first failed subtask; no-failed → toast.
- [ ] hint lines list new keys (`c/p/r` in task-list, `d` in subtask-tree).
- [ ] selfcheck + overlay selfcheck green; CI green (3 bun test jobs).

## Definition of Done

- Tests/selfcheck added for new keys + jump + cursor-pre-set + double-tap.
- typecheck + bun test green.
- PR per plan.

## Technical Approach

- **gap1 (task-list actions)**: add `onAction?: (taskId, action) => void` to `TaskListOptions`. In `handleInput` LIST mode (after the search-mode block, before/after nav): `c` → double-tap confirm via new `pendingCancel` field (first `c` sets it + flashMsg, second `c` with pending set → `onAction(id,"cancel")` + clear, any other key clears pending); `p` → `onAction(id,"pause")`; `r` → `onAction(id,"resume")`. extension.ts closure calls `orchestrator.{cancelTask,pauseTask,resumeTask}` (already toast internally) — but those need the task to be in the right state; rely on their boolean return for the refuse toast.
- **gap2 (subtask-tree jump)**: add `onJumpToTask?: (taskId) => void` to `SubtaskTreeOptions`. `d` key → `onJumpToTask(taskId)` then `this.done()`. extension.ts closure: `done()` already closes; in the closure call `ctx.ui.custom(createTaskListOverlay({...getTask...}))` and immediately call its detail open — simpler: open task-list overlay, then the user presses Enter. OR pass an `initialDetailTaskId` to TaskListOptions so it opens straight into detail mode. Use `initialDetailTaskId` (cleanest — one keypress lands in detail).
- **gap3 (jump to failed)**: add `Ctrl+Shift+F` shortcut. Reuse `createSubtaskTreeOverlay` with new `cursorOnFailed?: boolean` option → constructor finds first `failed` subtask, sets `cursorIdx` to it + clamps scroll. No failed → `ctx.ui.notify` + return (don't open overlay).

## Decision (ADR-lite)

**Context**: 3 overlay interaction gaps; user wants all. cancel irreversible needs confirm; overlay interactivity vs nesting; widget is render-only.
**Decision**: (1) cancel via in-overlay double-tap (reuse flashMsg, no stacking). (2) jump `d` opens task-list detail via `initialDetailTaskId` (close tree first). (3) jump-to-failed is a global shortcut Ctrl+Shift+F (widget can't take keys), subtask-tree gets `cursorOnFailed` init.
**Consequences**: 2 new overlay opts (`onAction`, `onJumpToTask`/`initialDetailTaskId`, `cursorOnFailed`) — small surface, mirrors existing `onRetry`. Double-tap window is implicit (no timeout — cleared on any non-`c` key, matches retry flashMsg semantics). Same flashMsg clearing rule as retry (`r`/`R` excluded from clear).

## Out of Scope (explicit)

- Subtask-level cancel/pause (only task-scoped, mirrors `/uc` commands).
- Multi-select (batch cancel/pause) — one cursor, one task at a time.
- progress-widget interactivity (stays render-only).
- Confirm timeout on double-tap (cleared by next key instead).

## Technical Notes

- Files: `ui/task-list-overlay.ts`, `ui/subtask-tree-overlay.ts`, `extension.ts`. progress-widget untouched.
- Overlay seam: inject `onAction`/`onJumpToTask` callbacks (closures holding `ctx`); `initialDetailTaskId`/`cursorOnFailed` init opts.
- gap3: new `pi.registerShortcut("ctrl+shift+f")` handler; reuse `createSubtaskTreeOverlay` with `cursorOnFailed`.
- Keys (no conflicts): task-list adds `c`/`p`/`r` (used: g/G/j/k/q). subtask-tree adds `d` (used: g/G/j/k/q/r/R).
