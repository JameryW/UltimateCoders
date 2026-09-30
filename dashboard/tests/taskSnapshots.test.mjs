import assert from "node:assert/strict";
import { test } from "node:test";
import { ignoresLateStart, normalizeTaskSnapshot, restoreCancelledTasks, retryParentState, snapshotParentState, terminalEventStatus } from "../src/lib/taskSnapshots.ts";

const task = {
  id: "real-task", description: "Calculator fix", project_id: "verification",
  subtask_count: 3, status: "completed",
  created_at: "2026-09-30T08:18:42.000Z", updated_at: "2026-09-30T08:19:18.210Z",
};

test("SSE Unix seconds produce the same task dates and statuses as gRPC", () => {
  const snapshot = normalizeTaskSnapshot({
    available: true, tasks: [{ ...task, status: "Completed", created_at: 1790756322, updated_at: 1790756358 }],
    total: 1, status_counts: { Completed: 1 }, pending_task_count: 0,
  });
  assert.equal(snapshot.tasks[0].created_at, task.created_at);
  assert.equal(snapshot.tasks[0].updated_at, "2026-09-30T08:19:18.000Z");
  assert.deepEqual(snapshot.status_counts, { completed: 1 });
});

test("late active snapshots cannot undo completed, failed or cancelled tasks", () => {
  for (const status of ["completed", "failed", "cancelled"]) {
    assert.deepEqual(snapshotParentState({ ...task, status }, {
      ...task, status: "in_progress", updated_at: "2026-09-30T08:19:19.000Z",
    }), { status, updated_at: task.updated_at });
  }
  assert.equal(snapshotParentState({ ...task, status: "paused" }, {
    ...task, status: "in_progress", updated_at: "2026-09-30T08:19:17.000Z",
  }).status, "paused");
  assert.equal(snapshotParentState({ ...task, status: "in_progress" }, task).status, "completed");
});

test("late starts preserve terminal parents and nodes while explicit retry remains allowed", () => {
  for (const status of ["completed", "failed", "cancelled"]) {
    assert.equal(ignoresLateStart(status, "subtask_started"), true);
    assert.equal(ignoresLateStart(status, "subtask_assigned"), true);
    assert.equal(ignoresLateStart(status, "subtask_retrying"), false);
  }
  assert.equal(ignoresLateStart("retrying", "subtask_started"), false);
});

test("fresh explicit retry reopens a failed parent for start and active snapshots", () => {
  const timestamp = "2026-09-30T08:19:19.000Z";
  const retried = { ...task, ...retryParentState({ ...task, status: "failed" }, timestamp) };
  assert.equal(retried.status, "in_progress");
  assert.equal(ignoresLateStart(retried.status, "subtask_started"), false);
  assert.equal(snapshotParentState(retried, { ...retried, status: "in_progress" }).status, "in_progress");
  assert.equal(retryParentState({ ...task, status: "failed" }, task.created_at).status, "failed");
  assert.equal(retryParentState({ ...task, status: "paused" }, timestamp).status, "paused");
  const cancelled = { ...task, status: terminalEventStatus("task_cancelled") };
  assert.equal(retryParentState(cancelled, timestamp).status, "cancelled");
  assert.equal(snapshotParentState(cancelled, { ...task, status: "failed" }).status, "cancelled");
});

test("late worker and parent failures cannot erase cancellation before a retry", () => {
  const timestamp = "2026-09-30T08:19:19.000Z";
  for (const eventType of ["subtask_failed", "task_failed"]) {
    const cancelled = { ...task, status: terminalEventStatus("task_cancelled") };
    const lateFailure = { ...cancelled, status: terminalEventStatus(eventType, cancelled.status), updated_at: timestamp };
    assert.equal(lateFailure.status, "cancelled");
    assert.equal(retryParentState(lateFailure, "2026-09-30T08:19:20.000Z").status, "cancelled");
    assert.equal(terminalEventStatus(eventType, "in_progress"), "failed");
  }
});

test("initial history restores cancellation from legacy Failed snapshots after reload", () => {
  const restored = restoreCancelledTasks({
    available: true, total: 2, pending_task_count: 0, status_counts: { failed: 2 },
    tasks: [{ ...task, status: "failed" }, { ...task, id: "actual-failure", status: "failed" }],
  }, [{ type: "task_cancelled", timestamp: task.updated_at, details: { task_id: task.id } }]);
  assert.equal(restored.tasks[0].status, "cancelled");
  assert.equal(restored.tasks[1].status, "failed");
  assert.deepEqual(restored.status_counts, { cancelled: 1, failed: 1 });
  assert.equal(snapshotParentState(restored.tasks[0], { ...task, status: "failed" }).status, "cancelled");
});
