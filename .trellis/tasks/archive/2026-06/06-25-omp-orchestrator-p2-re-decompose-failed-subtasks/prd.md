# OMP Orchestrator P2 — Re-decompose Failed Subtasks

## Goal

When all subtasks are done but some have permanently failed, attempt to re-decompose the failed ones into smaller, more specific subtasks. This prevents a single overly-broad subtask from dooming the entire task.

## What I already know

### Python 侧 pattern

- `_try_redecompose_failed()` called when: all subtasks done + some failed + no prior re-decomposition
- Builds context from completed subtasks + failed descriptions → calls decomposer again
- New subtasks depend on all completed subtasks, `retry_count = max_retries` (prevent infinite loops)
- Re-decomposition attempted only once per task
- On success: task status → IN_PROGRESS, event published. On failure: task → FAILED

### omp 侧 current behavior

- Failed subtasks with exhausted retries → entire task marked `failed`
- No attempt to break failed work into smaller pieces
- `resumeTask()` resets failed subtasks to pending and retries, but doesn't re-decompose

## Requirements

1. **Re-decompose on all-failed** — when executeWaves completes with failed subtasks, attempt re-decomposition before marking task as failed
2. **One-shot guard** — only attempt re-decomposition once per task (prevent infinite loops)
3. **Context injection** — feed completed subtask results + failed subtask errors to decomposer
4. **New subtask wiring** — new subtasks depend on all completed subtasks, inherit `retryCount = maxRetries`
5. **Re-execute** — after re-decomposition, continue wave execution with new subtasks

## Acceptance Criteria

* [ ] When task completes with failed subtasks, `tryRedecompose()` is called before marking task failed
* [ ] Re-decomposition only attempted once per task (tracked via `redecomposed` flag)
* [ ] New subtasks depend on all completed subtasks
* [ ] New subtasks have `retryCount = maxRetries` (no further retries)
* [ ] On re-decompose success, task continues execution with new subtasks
* [ ] On re-decompose failure, task marked as failed with original error

## Out of Scope

* Multiple re-decomposition attempts (one-shot only)
* Adaptive decomposition strategies (shrink scope, fallback tool)
* LLM-assisted conflict resolution

## Technical Approach

Add `tryRedecompose()` method to UCOrchestrator. Called from `executeWaves()` when task would otherwise be marked `failed`. Uses existing `decompose()` method with a re-decomposition prompt. Adds `redecomposed?: boolean` to TaskState.

## Technical Notes

### Key files

* `packages/uc-orchestrator/src/orchestrator/orchestrator.ts` — add tryRedecompose(), wire into executeWaves()

### Python 侧 reference

* `python/ultimate_coders/agent/orchestrator.py` lines 1803-1888 — _try_redecompose_failed()
