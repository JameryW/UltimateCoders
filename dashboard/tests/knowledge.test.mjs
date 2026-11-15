import assert from "node:assert/strict";
import { test } from "node:test";
import {
  knowledgeCountsLine,
  knowledgeEmpty,
  knowledgeGateLine,
  knowledgeOutcomeLine,
  knowledgeOverviewQuery,
  knowledgeStageLine,
} from "../src/lib/knowledge.ts";

const overview = (extra = {}) => ({
  schema: "knowledge-overview/1",
  task_id: "t1",
  stage: "graph",
  truncated: false,
  attempts: { total: 3, by_outcome: { completed: 2, failed: 1 } },
  evidence: { total: 5, verified: 4 },
  claims: { total: 2, promoted: 1, by_status: { candidate: 1, promoted: 1 } },
  gate: { allowed: 3, denied: 2, other: 0, reasons: { graph_scope_missing: 2 } },
  pages: { total: 1, active: 1, locked: 0 },
  ...extra,
});

test("the query encodes the task id and appends the stored token", () => {
  assert.equal(
    knowledgeOverviewQuery("task/1"),
    "/dashboard/api/knowledge?task_id=task%2F1",
  );
  assert.equal(
    knowledgeOverviewQuery("t1", { limit: 50, token: "a b" }),
    "/dashboard/api/knowledge?task_id=t1&limit=50&token=a%20b",
  );
});

test("stage line says the stage and flags a truncated sample", () => {
  assert.equal(knowledgeStageLine(overview()), "stage graph");
  assert.equal(knowledgeStageLine(overview({ truncated: true })), "stage graph · truncated sample");
  assert.equal(knowledgeStageLine(overview({ stage: "" })), "stage unknown");
});

test("counts line is the four aggregates in a fixed order", () => {
  assert.equal(
    knowledgeCountsLine(overview()),
    "attempts 3 · evidence 5 · claims 2 · pages 1",
  );
  assert.equal(
    knowledgeCountsLine(overview({ attempts: {}, pages: undefined })),
    "attempts 0 · evidence 5 · claims 2 · pages 0",
  );
});

test("gate line reports allowed/denied, other, and top reasons by count", () => {
  assert.equal(
    knowledgeGateLine(overview()),
    "gate allowed 3 · denied 2 · reasons: graph_scope_missing×2",
  );
  const busy = overview({
    gate: {
      allowed: 1,
      denied: 4,
      other: 1,
      reasons: { validation_missing: 1, revision_mismatch: 3, attempt_stale: 2 },
    },
  });
  assert.equal(
    knowledgeGateLine(busy),
    "gate allowed 1 · denied 4 · other 1 · reasons: revision_mismatch×3, attempt_stale×2, validation_missing×1",
  );
});

test("a gate with no denials still says allowed and never invents reasons", () => {
  assert.equal(knowledgeGateLine(overview({ gate: { allowed: 2 } })), "gate allowed 2 · denied 0");
});

test("outcome line sorts by count and says when nothing was recorded", () => {
  assert.equal(knowledgeOutcomeLine(overview()), "outcomes completed 2 · failed 1");
  assert.equal(
    knowledgeOutcomeLine(overview({ attempts: {} })),
    "outcomes none recorded",
  );
});

test("an all-zero payload is empty; one captured record is not", () => {
  const zero = { attempts: {}, evidence: {}, claims: {}, pages: {} };
  assert.equal(knowledgeEmpty(zero), true);
  assert.equal(knowledgeEmpty(overview()), false);
  assert.equal(knowledgeEmpty(overview({ attempts: { total: 0 }, evidence: { total: 1 } })), false);
});
