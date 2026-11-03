import assert from "node:assert/strict";
import { test } from "node:test";
import {
  countAttention,
  countPendingApprovals,
  emptyGovernanceCounts,
  formatGovernanceCounts,
  mergeGovernanceCounts,
  shouldPollCounts,
} from "../src/lib/governanceCounts.ts";

function attentionItem(id) {
  return {
    id,
    case_id: "case-1",
    kind: "needs_human",
    summary: "needs a decision",
    since: Date.parse("2026-01-01T00:00:00Z"),
    priority: 100,
  };
}

function approval(overrides = {}) {
  return {
    id: "apr-0001",
    tool: "scale_workers",
    requested_by_kind: "agent",
    requested_by_id: "executor",
    state: "pending",
    requested_at: Date.parse("2026-01-01T00:00:00Z"),
    expires_at: Date.parse("2026-01-01T00:15:00Z"),
    ...overrides,
  };
}

test("unresolved attention items are counted, junk is not", () => {
  assert.equal(countAttention({ items: [attentionItem("a"), attentionItem("b")] }), 2);
  assert.equal(countAttention({ items: [] }), 0);
  assert.equal(countAttention(null), 0);
  assert.equal(countAttention(undefined), 0);
  // The snapshot already excludes resolved items; a malformed entry is not a person's task.
  assert.equal(countAttention({ items: [{ nope: true }, attentionItem("a")] }), 1);
});

test("only pending approvals are counted", () => {
  const pending = approval({ id: "apr-1" });
  const approved = approval({ id: "apr-2", state: "approved" });
  const rejected = approval({ id: "apr-3", state: "rejected" });
  const used = approval({ id: "apr-4", state: "used" });
  const expired = approval({ id: "apr-5", state: "expired" });

  assert.equal(countPendingApprovals([pending, approved, rejected, used, expired]), 1);
  assert.equal(countPendingApprovals([]), 0);
  assert.equal(countPendingApprovals(null), 0);
});

test("an approved permit is not the human's task", () => {
  // `isUsable` means "approved and unexpired" — that is the agent's licence to
  // spend, so it must not inflate the footer's number of things to decide.
  assert.equal(countPendingApprovals([approval({ state: "approved", expires_at: Date.now() + 60_000 })]), 0);
});

test("merge sums parts and treats unknown as unknown", () => {
  assert.deepEqual(
    mergeGovernanceCounts({ attention: 2, approvals: 1 }, { attention: 1, approvals: 0 }),
    { attention: 3, approvals: 1 },
  );
  assert.deepEqual(mergeGovernanceCounts(emptyGovernanceCounts()), { attention: 0, approvals: 0 });
  // One unknown part poisons the total: a partial sum would understate the queue.
  assert.equal(mergeGovernanceCounts({ attention: 2, approvals: 0 }, null), null);
});

test("an empty or unknown queue renders as nothing at all", () => {
  assert.equal(formatGovernanceCounts(null), "");
  assert.equal(formatGovernanceCounts(undefined), "");
  assert.equal(formatGovernanceCounts(emptyGovernanceCounts()), "");
  assert.equal(formatGovernanceCounts({ attention: 0, approvals: 2 }), "审批 2");
  assert.equal(formatGovernanceCounts({ attention: 2, approvals: 0 }), "待办 2");
  assert.equal(formatGovernanceCounts({ attention: 2, approvals: 1 }), "待办 2 · 审批 1");
});

test("counts are polled only while the backend is reachable", () => {
  assert.equal(shouldPollCounts(true), true);
  assert.equal(shouldPollCounts(false), false);
});
