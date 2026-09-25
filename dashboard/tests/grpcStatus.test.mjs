import assert from "node:assert/strict";
import { test } from "node:test";
import { normalizeGrpcStatus } from "../src/lib/grpcStatus.ts";

test("Gateway statuses match Dashboard filters and progress counts", () => {
  assert.equal(normalizeGrpcStatus("Completed"), "completed");
  assert.equal(normalizeGrpcStatus("InProgress"), "in_progress");
  assert.equal(normalizeGrpcStatus("in_progress"), "in_progress");
});
