# PRD: Progress widget shows blocked-subtask rows

## Problem

The always-visible progress widget (packages/uc-orchestrator/src/ui/progress-widget.ts)
shows running subtask rows + a failed summary, but a task stalled on unmet
dependencies shows only the bar-row marker "·N⏳" (#488). When 0 subtasks are
running and N are blocked, the widget shows nothing below the bar — the user
cannot see WHICH subtasks are stalled without opening the subtask-tree overlay.

## Goal

When a task has blocked subtasks (pending + unmet deps), render one dim line
listing the blocked subtask IDs, capped to width with "…" — mirroring the
existing failed-summary line ("⚠ N failed: …").

- Placement: after the running rows section, before the failed summary.
- Only when blocked > 0 (no noise on healthy tasks).
- Narrow/tiny width: clamp id budget to 0, never throw, never overflow width.
- Existing bar-row "·N⏳" marker unchanged.

## Non-goals

- No per-subtask deps detail in the widget (deps live in the tree overlay).
- No interaction (the widget is display-only by design).

## Out of scope

- Other overlay surfaces (already show blocked info).
