# OMP Orchestrator Phase 3 — Checkpoint/Resume Enhancement + PRD Gap Closure

## Goal

Fix the `resumeFromWave` persistence bug and add task-level checkpoint snapshots to the omp Orchestrator, aligning with Python 侧's auto-checkpoint-after-each-subtask pattern while keeping the ponytail approach.

## What I already know

### Bug: `resumeFromWave` lost on persist/restore

- `TaskState.resumeFromWave` is **dropped** by `toPersisted()` — never written to JSON
- `fromPersisted()` heuristically sets it to `0` for paused/failed tasks
- **Result**: paused task always resumes from wave 0 instead of the wave it was paused at
- This is a data-loss bug, not a missing feature

### Python 侧 checkpoint pattern

- **Orchestrator-level**: `checkpoint_task()` auto-called after each subtask result, stores `task.to_dict()` to engine memory
- **Worker-level**: `_save_checkpoint()` / `_load_checkpoint()` per-subtask, skips re-execution if checkpoint exists
- **Snapshot ID**: `snap-{task_id}-{timestamp}` format
- **Two-tier**: engine native checkpoint → engine memory fallback

### omp 侧 current state

- `SubtaskResult` already has `modifiedFiles`, `recentToolCalls`, `stderrTail`, `retryCount` — good
- `TaskStore` persists full task JSON per wave — but `resumeFromWave` is lost
- No snapshot mechanism — just incremental overwrites
- No auto-checkpoint trigger — `persist()` is called manually at key points

## Assumptions (temporary)

- Snapshot should be lightweight (JSON copy, not git workspace snapshot)
- omp 侧不需要 Worker-level per-subtask checkpoint（subprocess 不可中断，重跑整个 subtask 即可）
- `resumeFromWave` fix is straightforward — just persist it

## Open Questions

1. Should we keep task-level snapshots (history of N checkpoints) or just latest-wins?
2. Should checkpoint include git diff/workspace state, or just task metadata?

## Requirements

1. **Fix `resumeFromWave` persistence** — add to `PersistedTask`, round-trip correctly
2. **Add task-level checkpoint** — auto-save snapshot after each wave completes (matching Python 侧 pattern)
3. **Restore from checkpoint** — on startup `restore()`, load latest snapshot and resume from correct wave
4. **PRD gap closure** — update the alignment table: retry = ✅, checkpoint = ✅, resume = ✅

## Acceptance Criteria

* [ ] `resumeFromWave` persists correctly — pause at wave N, restart process, resume from wave N
* [ ] Auto-checkpoint after each wave — snapshot saved to `.uc/checkpoints/{taskId}.snap.json`
* [ ] Restore loads latest checkpoint on startup
* [ ] `toPersisted()` / `fromPersisted()` round-trip test passes
* [ ] PRD alignment table updated: retry ✅, checkpoint ✅, resume ✅

## Definition of Done

* Unit tests for persist/restore round-trip
* Unit test for checkpoint save/load
* `resumeFromWave` no longer lost on persist
* Lint/typecheck green

## Out of Scope

* Worker-level per-subtask checkpoint (omp subprocess 不可中断)
* Git workspace snapshot (YAGNI — task metadata sufficient)
* Checkpoint history / N-snapshot retention (latest-wins is enough)
* OOM protection / dynamic capacity (deferred from previous task)

## Technical Notes

### Key files

* `packages/uc-orchestrator/src/orchestrator/orchestrator.ts` — TaskState, persist/restore, executeWaves
* `packages/uc-orchestrator/src/orchestrator/task-store.ts` — PersistedTask, save/load
* `packages/uc-orchestrator/src/orchestrator/scheduler.test.ts` — existing tests

### Python 侧 reference

* `python/ultimate_coders/agent/orchestrator.py` — checkpoint_task(), recover_task()
* `python/ultimate_coders/agent/worker.py` — _save_checkpoint(), _load_checkpoint()

### Bug details

`toPersisted()` (line 861) does NOT include `resumeFromWave`.
`fromPersisted()` (line 888) sets `resumeFromWave: 0` heuristically — always resumes from wave 0.

### Checkpoint design (latest-wins)

```
.uc/
  tasks/
    uc-1-xxx.json          # current task state (overwrite each persist)
  checkpoints/
    uc-1-xxx.snap.json     # latest wave-completed snapshot
```

Snapshot = copy of task state at wave boundary (after all subtasks in a wave complete, before next wave starts). On restore, if checkpoint exists and task was paused/failed, use checkpoint's `resumeFromWave` value.
