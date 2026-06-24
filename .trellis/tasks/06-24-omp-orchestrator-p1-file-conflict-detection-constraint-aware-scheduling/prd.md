# OMP Orchestrator P1 — File Conflict Detection + Constraint-Aware Scheduling

## Goal

Enable safe parallel subtask execution by using `SubtaskDef.files` to detect file conflicts and prevent overlapping edits. Without this, two omp workers can silently clobber each other's changes when placed in the same wave.

## What I already know

### Current state

- `SubtaskDef.files` exists in scheduler.ts but is **never used for scheduling** — only surfaced in supervisor review prompt
- `buildDAG()` forms waves purely by dependency graph, ignoring file overlap
- omp has no conflict detection — parallel subtasks modifying the same file will race
- Python 侧 has full `ConflictDetector` (intent-based locking) + `ConflictResolver` (4-tier: auto-merge, LLM-assisted, reassign, human)

### Python 侧 reference

- `ConflictDetector`: `declare_intent()`, `check_conflict()`, `remove_intent()`, `get_intents()`
- `EditIntent`: worker_id, file_path, edit_type, line-range regions
- Used in `_select_next_subtask()` to skip file-blocked subtasks
- Worker declares/releases intents around sandbox execution

## Requirements

1. **File-aware wave building** — `buildDAG()` or a post-processing step splits waves so that subtasks with overlapping `files` are never in the same wave
2. **Conflict detection at execution** — before running a subtask, check if any running subtask has declared intent on the same files; if so, defer
3. **Intent declaration/release** — track which files each running subtask is modifying; release on completion/failure

## Acceptance Criteria

* [ ] `buildDAG()` (or post-process) separates subtasks with overlapping `files` into different waves
* [ ] Running subtask declares file intents before execution
* [ ] Conflicting subtasks are deferred (not placed in same parallel batch)
* [ ] File intents released on subtask completion/failure/cancellation
* [ ] Unit tests: overlapping files → separate waves; non-overlapping → same wave; partial overlap → separate
* [ ] Existing DAG tests still pass (files=[] treated as no constraint)

## Definition of Done

* Unit tests for file-aware scheduling
* Existing 27 tests still pass
* Lint/typecheck green

## Out of Scope

* 4-tier conflict resolution (auto-merge, LLM-assisted, reassign, human) — just prevent, don't resolve
* Line-range granular conflict (file-level is sufficient for MVP)
* WorkerInfo / worker registry / load balancing (separate task)
* Rate limiting / circuit breaker (P2, separate task)

## Technical Approach

### File-aware wave building

Add a post-processing step after `buildDAG()` that further splits waves where subtasks have overlapping files:

```
buildDAG(subtasks) → waves
for each wave:
  split into sub-waves where no two subtasks share a file
```

Algorithm: within a wave, group subtasks by file overlap. Subtasks with no shared files can stay parallel; those sharing files must be sequential. This is a graph coloring problem — subtasks sharing files get an edge, then each color class becomes a sub-wave.

ponytail: greedy coloring is O(V²) and sufficient — we rarely have >10 subtasks per wave.

### Intent tracking

Add a `FileIntentTracker` class:
- `declare(subtaskId, files: string[])` — mark files as owned by subtask
- `release(subtaskId)` — remove all intents for subtask
- `isConflicting(files: string[]): string[]` — return list of conflicting subtask IDs
- `getOwnedFiles(): Map<string, string[]>` — for debugging/status

Used in `executeWave()`: before picking next subtask from queue, check `isConflicting(def.files)`. If conflicting, skip and try next; if all remaining conflict, wait for a running subtask to complete.

## Decision (ADR-lite)

**Context**: Parallel subtasks can silently clobber each other's file edits. Need to prevent concurrent writes to the same file.
**Decision**: File-aware wave splitting (static, at DAG build time) + runtime intent tracking (dynamic, at execution time). Static splitting handles the common case; runtime tracking handles edge cases where actual modified files differ from declared files.
**Consequences**: Subtasks may execute more sequentially than strictly necessary (over-conservative). Can be relaxed later with line-range granular tracking.

## Technical Notes

### Key files to modify

* `packages/uc-orchestrator/src/orchestrator/scheduler.ts` — add `splitWavesByFileOverlap()`
* `packages/uc-orchestrator/src/orchestrator/orchestrator.ts` — add `FileIntentTracker`, use in `executeWave()`
* `packages/uc-orchestrator/src/orchestrator/scheduler.test.ts` — add file-aware scheduling tests

### Python 侧 reference

* `python/ultimate_coders/agent/conflict.py` — ConflictDetector, ConflictResolver, EditIntent
* `python/ultimate_coders/agent/orchestrator.py` — _select_next_subtask() conflict check
