# Render step_status (retrying/skipped) in TUI + Dashboard

## Background

PRs #245-#249 added new `step_status` values ("retrying", "skipped") +
parallel_group to the workflow step events. The worker emits these via
`subtask_progress` events, and both TUI (`progress-widget.ts`) and dashboard
(`TaskDetail.tsx`) receive the data — but neither renders the new status
distinctly. The TUI stores `progressBySubtask` (with stepStatus/stepAgent/
phase/percent) but its `render()` doesn't use it at all (comment says
"PR4 renders this" but it doesn't). The dashboard renders step_agent/phase/
percent/step_index but not step_status. Users can't see when a step is
retrying or was skipped — a visibility gap for the new orchestration features.

## What I already know (verified this session)

- **TUI** `packages/uc-orchestrator/src/ui/progress-widget.ts`:
  - `SubtaskProgressInfo` interface (line ~57): `phase`, `percent`, `stepIndex?`, `stepTotal?`, `stepAgent?`, `stepStatus?`, `stepSummary?`.
  - `ProgressWidgetState.progressBySubtask?: Map<string, SubtaskProgressInfo>` (line ~49).
  - `render(width)` (line 82-136): renders task header + wave progress + running subtasks + failed summary. **Does NOT use `progressBySubtask` at all** — the step info is stored but never displayed.
- **Dashboard** `dashboard/src/components/panels/TaskDetail.tsx`:
  - `SubtaskProgress` component (line ~152): renders `step_agent` badge + `phase` label + percent bar + `[step_index+1/step_total]`. **Does NOT render `step_status`** — retrying/skipped not visible.
- **Data flow**: `useDashboard.ts:504` reads `step_status` from event data; `:518` stores it on the subtask summary. So the data reaches both UIs; it's just not rendered.
- `SubtaskSummary.step_status` type exists (`types/dashboard.ts:63`).

## The gap

1. **TUI**: `progressBySubtask` entirely unused in render — no step phase/agent/status shown for running subtasks.
2. **Dashboard**: `step_status` not rendered — retrying/skipped invisible.

## Decisions (locked)

- **D1 (TUI)**: In `progress-widget.ts` `render()`, for each running subtask
  that has an entry in `progressBySubtask`, render an indented step line
  showing: step agent badge + phase + (if stepStatus) a status tag. Map
  stepStatus to a color: "retrying" → warn (yellow), "skipped" → dim,
  "started"/"completed" → accent, "failed" → error. Keep it compact (one
  line per running subtask's current step). Match the existing theme.fg()
  style. Don't overflow width.
- **D2 (Dashboard)**: In `TaskDetail.tsx` `SubtaskProgress`, add a
  `step_status` badge (small pill) when `st.step_status` is present and
  is one of retrying/skipped/failed (skip for started/completed — those
  are implied by the phase). Color: retrying → yellow, skipped → gray,
  failed → red. Place it next to the step_agent badge.
- **D3**: No data-layer changes — `step_status` already flows to both UIs.
  This is pure rendering.
- **Out of scope**: parallel_group visualization (which steps are parallel);
  TUI layout overhaul; dashboard step timeline.

## Acceptance Criteria

- [ ] TUI `render()` uses `progressBySubtask` to show a step line (agent +
      phase + status tag) for running subtasks that have progress info.
- [ ] Dashboard `SubtaskProgress` shows a `step_status` badge for
      retrying/skipped/failed.
- [ ] `bun run check` (OMP tsc) green for progress-widget.ts (no new errors).
- [ ] `npx tsc --noEmit` (dashboard) green.
- [ ] Existing rendering unchanged when step_status/progressBySubtask absent
      (backward compat — old events without step info render as before).

## Technical Approach

1. **TUI** `progress-widget.ts`:
   - In `render()`, after the running-subtasks loop (or within it), look up
     `s.progressBySubtask?.get(st.id)` for each running subtask `st`.
   - If progress info exists, push an indented line:
     `    {agent badge} {phase} [{status tag}]`
   - status tag color via a small helper mapping stepStatus → theme color.
   - Truncate phase to fit width.
2. **Dashboard** `TaskDetail.tsx`:
   - In `SubtaskProgress`, after the step_agent badge, add:
     `{["retrying","skipped","failed"].includes(st.step_status) && <span className={pillClass}>{st.step_status}</span>}`
   - pillClass: retrying → yellow, skipped → gray, failed → red (match
     existing badge styles like the step_agent cyan badge).
3. Verify tsc both packages.

## Risk

- **TUI width overflow**: step line could overflow narrow terminals. Mitigation:
  truncate phase, keep one line, use dim color for less-important parts.
- **Backward compat**: old events without step_status/progressBySubtask must
  render as before. Both changes are conditional on the field being present.
- **No runtime test**: can't easily run the TUI/dashboard in CI. Rely on tsc
  + careful conditional rendering.
