# Visualize Parallel Steps (Multiple Concurrent Step Progress) in TUI

## Background

PR #249 added read-only parallel step groups (consecutive same
`parallel_group` steps run via `asyncio.gather`). PR #251 rendered
`step_status` in the TUI/dashboard. But `progressBySubtask` is
`Map<subtaskId, SubtaskProgressInfo>` (single entry per subtask) — when
N steps run concurrently, their interleaved `subtask_progress` events
overwrite each other. Only the last-received step is visible. Users can't
see that steps are running in parallel, nor each concurrent step's status.

Research (`research/parallel-step-event-flow.md`) confirmed:
- `step_index` IS carried in every layer but useless under single-entry Map.
- `parallel_group` is used in worker logic but **NOT emitted in event data**
  — consumers can't distinguish sequential from parallel.
- No proto change needed (event `data` is `map<string,string>`).
- Rust `AgentEventType::SubtaskProgress` needs `parallel_group` field +
  conversion for the Rust-side WatchTask path.

## Decisions (locked)

- **D1 (worker emit)**: `_emit_step_event` calls pass `parallel_group`
  (from `step.parallel_group`) into the event data dict. Add a
  `parallel_group` key (empty string for sequential steps — present
  always so consumers can reliably read it).
- **D2 (Rust event)**: `AgentEventType::SubtaskProgress` (events.rs) add
  `parallel_group: Option<String>` field + conversion in conversions.rs
  (insert into data HashMap). This covers the Rust-side WatchTask path.
- **D3 (TUI ingest)**: `orchestrator.ts` extract `parallel_group` from
  `ev.data` → re-emit. `extension.ts` store `parallelGroup` on
  `SubtaskProgressInfo`. Keep `progressBySubtask` as
  `Map<string, SubtaskProgressInfo>` (single entry) BUT add
  `parallelGroup?: string` + `parallelStepCount?: number` fields.
  The worker emits `parallel_step_count` (total steps in the group) so
  the TUI can show "N parallel" without storing all entries.
- **D4 (TUI render — Approach C hybrid)**: In `render()`, after the step
  line, if `parallelGroup` is non-empty AND `parallelStepCount > 1`,
  append a `⟳ N parallel` indicator (warn color). Single step (no group
  or count==1) renders as today (full detail). This avoids the
  multi-entry Map refactor while signaling parallelism.
- **D5 (worker emit parallel_step_count)**: When emitting events for a
  parallel group step, include `parallel_step_count` = the group size
  (number of steps sharing this `parallel_group`). Sequential steps emit
  `parallel_step_count` = 1 (or omit — consumer treats absent as 1).
- **D6 (dashboard)**: `SubtaskSummary` add `parallel_group?: string` +
  `parallel_step_count?: number`. `mergeProgressEvent` upsert them.
  `SubtaskProgress` component: if `parallel_group` && count > 1, show a
  "⟳ N parallel" badge next to the step_status badge.
- **Out of scope**: full per-step multi-entry visualization (Approach A —
  too invasive for now); the hybrid indicator (Approach C) is the
  minimal viable parallelism signal. Per-step detail in parallel groups
  is a future enhancement.

## Acceptance Criteria

- [ ] `parallel_group` + `parallel_step_count` in worker event data dict.
- [ ] Rust `AgentEventType::SubtaskProgress` carries `parallel_group` +
      conversion inserts it into the data map.
- [ ] TUI `SubtaskProgressInfo` has `parallelGroup` + `parallelStepCount`;
      `render()` shows `⟳ N parallel` when group non-empty + count > 1.
- [ ] Dashboard `SubtaskSummary` + `SubtaskProgress` show the indicator.
- [ ] Sequential steps (no parallel_group) render unchanged (backward compat).
- [ ] cargo check/test, ruff, pytest, OMP tsc, dashboard tsc green.

## Technical Approach

1. **worker.py `_emit_step_event`**: add `parallel_group` + `parallel_step_count`
   params; pass `step.parallel_group` + group size from `_run_single_step`/
   `_execute_steps`. For sequential steps, parallel_group="" + count=1.
2. **events.rs `SubtaskProgress`**: add `parallel_group: Option<String>`.
3. **conversions.rs**: insert `parallel_group` into data HashMap when present.
4. **orchestrator.ts**: extract `parallel_group` + `parallel_step_count` from
   `ev.data`; re-emit in the `subtask_progress` payload.
5. **events.ts**: add `parallelGroup?` + `parallelStepCount?` to the
   `subtask_progress` event interface.
6. **extension.ts**: set `parallelGroup` + `parallelStepCount` on
   `SubtaskProgressInfo`.
7. **progress-widget.ts**: add fields to `SubtaskProgressInfo`; in `render()`
   append `⟳ N parallel` (warn) when `parallelGroup` && count > 1.
8. **dashboard types.ts**: add `parallel_group?` + `parallel_step_count?` to
   `SubtaskSummary`.
9. **useDashboard.ts mergeProgressEvent**: upsert the two fields.
10. **TaskDetail.tsx SubtaskProgress**: show `⟳ N parallel` badge when
    `parallel_group` && count > 1.
11. Verify all layers.

## Risk

- **Rust enum change**: adding a field to `AgentEventType::SubtaskProgress`
  requires updating all match arms that construct/destructure it. Grep for
  `SubtaskProgress {` in `crates/` to find all sites.
- **Event data key consistency**: use `parallel_group` (snake) in the data
  dict consistently (Python + Rust + TS extraction). The TS event interface
  uses camelCase (`parallelGroup`) — match the existing stepIndex/stepStatus
  camelCase pattern in the TS layer, snake in the data dict.
- **No runtime parallel test**: hard to trigger real parallel steps in CI.
  Rely on tsc + unit tests for the indicator logic.
