import assert from "node:assert/strict";
import { test } from "node:test";
import {
  LETTER_VIEWS,
  TERMINAL_VIEWS,
  hashForView,
  viewFromHash,
  viewKeyHint,
} from "../src/lib/terminalView.ts";

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

test("the numeric key hint is derived from the keys, not hardcoded", () => {
  // The dashboard hardcoded "1–7" twice and "1–9" once; all three went stale the
  // moment a view was added (receipts landed on `0`).
  assert.equal(viewKeyHint(["1", "2", "3"]), "1–3");
  assert.equal(viewKeyHint(["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]), "0–9");
  assert.equal(viewKeyHint(["2", "1", "3"]), "1–3", "input order must not matter");
  assert.equal(viewKeyHint(["1", "2", "4"]), "1·2·4", "a gap is shown, not smoothed over");
  assert.equal(viewKeyHint(["1", "1", "2"]), "1–2", "duplicates collapse");
  assert.equal(viewKeyHint([]), "");
});

test("the shortcut table only names registered views, with unique letters", () => {
  const letters = Object.keys(LETTER_VIEWS);
  assert.equal(new Set(letters.map((letter) => letter.toLowerCase())).size, letters.length);

  for (const [letter, view] of Object.entries(LETTER_VIEWS)) {
    assert.ok(
      TERMINAL_VIEWS.includes(view),
      `letter ${letter} points at ${view}, which is not a registered view`,
    );
  }
});

test("non-dashboard or unknown hashes are rejected", () => {
  assert.equal(viewFromHash("#"), null);
  assert.equal(viewFromHash("#/"), null);
  assert.equal(viewFromHash("#/other"), null);
  assert.equal(viewFromHash("#/dashboard/unknown"), null);
  assert.equal(viewFromHash(""), null);
});
