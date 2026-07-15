# fix(tui): feedback when retrying a non-failed subtask

## Problem
In the Ctrl+T SubtaskTree overlay, pressing `r`/`R` on a subtask that is NOT
`failed` does nothing — silently. `handleInput` (subtask-tree-overlay.ts:181-186)
guards `if (item && item.subtask.status === "failed" && this.opts.onRetry)` and
falls through with no feedback when the status is completed/running/pending/
reviewing. The help line advertises `R retry`, so a user pressing it on a
non-failed row gets a dead key with no explanation.

## Root cause
The retry branch only acts on `failed` subtasks; every other status hits the
implicit else with no render update and no message. The overlay has no toast
channel (its `tui` arg is the TUI, not `ctx.ui`, so `notify()` isn't available),
so feedback must be rendered in-overlay.

## Scope (MVP)
- Add a transient in-overlay message field to `SubtaskTreeComponent`, e.g.
  `private flashMsg: string | null = null`.
- In the `r`/`R` branch: when the cursor's subtask is NOT failed (or onRetry is
  absent), set `flashMsg` to a dim hint naming the actual status, e.g.
  `only failed subtasks can be retried (this is <status>)`. When it IS failed
  and onRetry fires, clear `flashMsg`.
- In `render(width)`: if `flashMsg` is set, emit it on its own dim line (after
  the list / footer). Truncate to width.
- Clear `flashMsg` on the next keypress that isn't `r`/`R` (so any navigation
  or enter/esc dismisses it). Esc already closes the overlay; other keys clear
  the flash then proceed.
- Keep the existing retry-wiring selfcheck assertions green: `r` on a failed
  subtask still invokes `onRetry` with `(taskId, subtaskId)`; `r` on completed
  still does NOT invoke `onRetry`. Add ONE new assertion: `r` on a completed
  subtask sets a flashMsg that appears in the rendered output (a line containing
  "only failed" or the status name).

## Out of scope
- Auto-expiring the flash by timer (TUI render is event-driven; clear-on-key
  is simpler and sufficient). 
- Changing which statuses are retryable (failed-only is the existing
  `resumeTask` contract — task-scoped, resets all failed subtasks).
- Per-subtask retry (needs new orchestrator API).
