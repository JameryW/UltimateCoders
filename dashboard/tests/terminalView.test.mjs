import assert from "node:assert/strict";
import { test } from "node:test";
import { hashForView, viewFromHash } from "../src/lib/terminalView.ts";

test("dashboard hash maps to a view and back", () => {
  assert.equal(viewFromHash("#/dashboard"), "overview");
  assert.equal(viewFromHash("/dashboard"), "overview");
  assert.equal(viewFromHash("#/dashboard/tasks"), "tasks");
  assert.equal(viewFromHash("#/dashboard/inference"), "inference");
  assert.equal(viewFromHash("#/dashboard/tasks/"), "tasks");

  assert.equal(hashForView("overview"), "#/dashboard");
  assert.equal(hashForView("tasks"), "#/dashboard/tasks");
  assert.equal(hashForView("inference"), "#/dashboard/inference");
});

test("non-dashboard or unknown hashes are rejected", () => {
  assert.equal(viewFromHash("#"), null);
  assert.equal(viewFromHash("#/"), null);
  assert.equal(viewFromHash("#/other"), null);
  assert.equal(viewFromHash("#/dashboard/unknown"), null);
  assert.equal(viewFromHash(""), null);
});
