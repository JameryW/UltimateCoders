import assert from "node:assert/strict";
import { test } from "node:test";
import { TERMINAL_VIEWS, hashForView, viewFromHash } from "../src/lib/terminalView.ts";

test("dashboard hash maps to a view and back", () => {
  assert.equal(viewFromHash("#/dashboard"), "overview");
  assert.equal(viewFromHash("/dashboard"), "overview");
  assert.equal(viewFromHash("#/dashboard/tasks"), "tasks");
  assert.equal(viewFromHash("#/dashboard/inference"), "inference");
  assert.equal(viewFromHash("#/dashboard/tasks/"), "tasks");
  assert.equal(viewFromHash("#/dashboard/effects"), "effects");

  assert.equal(hashForView("overview"), "#/dashboard");
  assert.equal(hashForView("tasks"), "#/dashboard/tasks");
  assert.equal(hashForView("inference"), "#/dashboard/inference");
  assert.equal(hashForView("effects"), "#/dashboard/effects");
});

test("every registered view is addressable", () => {
  for (const view of TERMINAL_VIEWS) {
    assert.equal(viewFromHash(hashForView(view)), view, view);
  }
  assert.ok(TERMINAL_VIEWS.includes("effects"), "the receipt queue view is registered");
});

test("non-dashboard or unknown hashes are rejected", () => {
  assert.equal(viewFromHash("#"), null);
  assert.equal(viewFromHash("#/"), null);
  assert.equal(viewFromHash("#/other"), null);
  assert.equal(viewFromHash("#/dashboard/unknown"), null);
  assert.equal(viewFromHash(""), null);
});
