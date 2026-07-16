# feat: per-subtask retry (reset and redispatch single failed subtask)

## Goal

Allow retrying a SINGLE failed subtask without resetting all failed subtasks in
the task. Today `resumeTask` is task-scoped — it resets every failed subtask
to pending and rebuilds waves. The Ctrl+T subtask-tree overlay's `r` key already
passes `(taskId, subtaskId)` to `onRetry`, but extension.ts calls the task-scoped
`resumeTask`. Users want to retry just the one subtask they're inspecting,
preserving the failure state of others (e.g. to investigate them separately).

## What I already know (from repo inspection)

- `UCOrchestrator.resumeTask(taskId, ctx)` (orchestrator.ts:991) — resets ALL
  failed subtasks to pending, rebuilds waves from pending+running, executeWaves.
  Guard: `controlState !== "paused" && status !== "failed"` → false.
- `executeWaves(task, waves, ctx)` runs waves locally in the orchestrator; the
  gRPC bridge's `resumeTask(taskId)` (grpc-bridge.ts:480) only syncs task STATE
  to the gateway — execution is local. So per-subtask retry can be local-only.
- **No `retrySubtask` RPC exists** in proto (engine.proto) or grpc-bridge.
  resumeTask is the only resume-shaped RPC, and it's task-scoped.
- TUI `onRetry(taskId, subtaskId)` already passes subtaskId (subtask-tree
  overlay handleInput r/R). extension.ts:226 currently calls
  `orchestrator.resumeTask(taskId, ctx)` — ignores subtaskId.
- `cascadeCancel` (orchestrator.ts:~1048) cancels downstream subtasks depending
  on a cancelled one. So a failed subtask's downstream may be `cancelled`.

## Assumptions (temporary)

- Per-subtask retry is orchestrator-local (no new gRPC RPC needed); optionally
  sync state via the existing `resumeTask` bridge call (task-scoped sync is fine
  — it just pushes the updated task snapshot).
- The target subtask's dependencies are completed (the common case: a subtask
  failed mid-task, its deps already ran). If deps aren't completed, retry should
  refuse with a clear message (not auto-reset upstream).

## Open Questions

- ~~Q1: downstream cancelled subtasks~~ → **Decision: Approach B** — reset X AND cascade-un-cancel X's downstream (all subtasks cancelled because X failed) back to pending, rebuild waves containing X + downstream, execute. One retry recovers the whole failed chain.
- ~~Q2: other failed subtasks~~ → **Decision (self-answered, mirrors resumeTask):** subtasks NOT in X's downstream stay in their current state (failed/cancelled). Only X + its downstream are reset to pending and re-dispatched. Task goes in_progress during execution; final status determined by executeWaves (failed if any failed remain, else completed).
- ~~Q3: gRPC sync~~ → **Decision (self-answered):** reuse `bridge.resumeTask(taskId)` for state sync (task-scoped sync is correct — it just pushes the updated snapshot). No new RPC.

## Requirements (evolving)

- New `retrySubtask(taskId, subtaskId, ctx)` on UCOrchestrator.
- TUI `r`/`R` on a failed subtask calls `retrySubtask` (not `resumeTask`).
- `/uc resume` stays task-scoped (unchanged) — distinct from per-subtask retry.

## Acceptance Criteria (evolving)

- [ ] `retrySubtask` resets ONLY the target failed subtask to pending (+retryCount=0).
- [ ] Rebuilds waves containing the target (deps satisfied) and executes.
- [ ] Refuses (clear message) if target deps not completed.
- [ ] Other failed subtasks stay failed.
- [ ] TUI r/R routes to retrySubtask; toast success/refuse.
- [ ] selfcheck + orchestrator tests cover reset-only + deps-unsatisfied refuse.

## Out of Scope (explicit)

- New gRPC retrySubtask RPC (local-only; sync via existing resumeTask bridge).
- Auto-resetting upstream failed deps.
- Per-step retry (step-level retry already exists via retry_count).

## Technical Approach

**`retrySubtask(taskId, subtaskId, ctx)` on UCOrchestrator** (mirrors resumeTask shape):

1. Guard: task exists; target subtask status === "failed" (else return false with toast).
2. Reset target X → pending (error/result undefined, retryCount=0).
3. **Reverse cascade-un-cancel:** iteratively find cancelled subtasks whose ALL
   dependsOn are now (completed OR in the reset-pending set) → set pending
   (retryCount=0). Repeat to fixed point. (This recovers X's downstream that was
   cascade-cancelled solely because X failed. A downstream depending on ANOTHER
   still-failed subtask is NOT recovered — its deps aren't satisfied.)
4. Refuse if X's own deps aren't all completed (can't re-dispatch) → toast.
5. Rebuild waves from pending+running (includes X + recovered downstream),
   buildDAG + splitWavesByFileOverlap, executeWaves.
6. task.controlState="running", status="in_progress"; sync via bridge.resumeTask
   (task-scoped state sync — no new RPC).
7. Other failed/cancelled subtasks NOT in X's recovered set stay as-is.

**TUI:** extension.ts onRetry changes `resumeTask(taskId)` → `retrySubtask(taskId, subtaskId)`. subtask-tree overlay r/R unchanged (already passes subtaskId). `/uc resume` stays task-scoped (untouched).

## Decision (ADR-lite)

**Context:** r/R retry was task-scoped (reset ALL failed). Users inspecting one failed subtask want to retry just it + recover its downstream, not wipe other failures.
**Decision:** Approach B — retrySubtask resets target + reverse-cascade-un-cancels its downstream. Local-only (no new gRPC RPC; sync via existing resumeTask bridge). Other failed subtasks preserved.
**Consequences:** More complex than Approach A (reverse cascade), but matches user intent (recover the failed chain). Edge: downstream depending on another failed subtask stays cancelled (correct — deps unsatisfied).

## Implementation Plan (small PRs)

- **PR1:** `retrySubtask` on UCOrchestrator + unit tests (reset-only, deps-unsatisfied refuse, reverse-cascade-un-cancel, other-failed-preserved). No TUI change yet.
- **PR2:** extension.ts onRetry → retrySubtask; toast success/refuse. subtask-tree selfcheck unchanged (r/R wiring already locked).
