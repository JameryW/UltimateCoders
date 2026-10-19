import assert from "node:assert/strict";
import { test } from "node:test";
import {
  compareManifestRecords,
  parseManifestKey,
  manifestOrderKey,
  manifestHeadline,
  tierCountsLine,
  droppedLine,
} from "../src/lib/contextManifests.ts";

const record = (key, summary) => ({ key, summary });

test("attempt order is numeric, not lexicographic", () => {
  // Plain string order would put ":10" before ":2".
  const keys = ["t1:s1:10", "t1:s1:2", "t1:s1:0"].sort((a, b) =>
    compareManifestRecords(record(a), record(b)));
  assert.deepEqual(keys, ["t1:s1:0", "t1:s1:2", "t1:s1:10"]);
});

test("the order key keeps a subtask boundary between s1 and s10", () => {
  const keys = ["t1:s10:0", "t1:s1:1", "t1:s1:0"].sort((a, b) =>
    compareManifestRecords(record(a), record(b)));
  assert.deepEqual(keys, ["t1:s1:0", "t1:s1:1", "t1:s10:0"]);
});

test("a non-numeric attempt tail sorts like attempt 0", () => {
  assert.deepEqual(manifestOrderKey("t1:s1:oops"), ["t1:s1", 0]);
  assert.equal(parseManifestKey("t1:s1:oops").attempt, 0);
});

test("parse splits on the last two colons so task ids may contain one", () => {
  assert.deepEqual(parseManifestKey("task-10:s1:3"),
    { taskId: "task-10", subtaskId: "s1", attempt: 3 });
  // A split on every colon would mangle this id into task / 10 / s1:3;
  // the last-two-colons rule keeps the task id intact.
  assert.deepEqual(parseManifestKey("task:10:s1:3"),
    { taskId: "task:10", subtaskId: "s1", attempt: 3 });
});

test("the headline reports cost, reuse, risk and drops", () => {
  assert.equal(manifestHeadline({
    budget_tokens: 2000,
    total_tokens: 1200,
    stable_tokens: 800,
    risk: "high",
    over_budget: false,
    dropped: [{ reason: "budget" }, { reason: "budget" }, { reason: "ttl" }],
  }), "1200/2000 tok · stable 800 · risk high · 3 dropped");
  assert.equal(manifestHeadline({ budget_tokens: 10, total_tokens: 40, over_budget: true }),
    "40/10 tok · over budget");
  assert.equal(manifestHeadline(undefined), "no summary recorded");
});

test("the headline never renders fields it does not know", () => {
  const line = manifestHeadline({
    budget_tokens: 100, total_tokens: 50,
    prompt: "SECRET PROMPT CONTENT", tool_log: "SECRET TRACE",
  });
  assert.equal(line, "50/100 tok");
  assert.ok(!line.includes("SECRET"));
});

test("tier counts come out in tier order and skip empty tiers", () => {
  assert.equal(tierCountsLine({
    "P1 state": 2,
    "P0 policy": 1,
    "P3 trace": 0,
    "P2 evidence": 4,
  }), "P0 policy 1 · P1 state 2 · P2 evidence 4");
  assert.equal(tierCountsLine(undefined), "");
});

test("drop reasons are aggregated with their counts", () => {
  assert.equal(droppedLine([
    { reason: "budget" }, { reason: "budget" }, { reason: "ttl" }, {},
  ]), "budget (2) · ttl · unspecified");
  assert.equal(droppedLine([]), "");
});

test("the rendered trail shows attempts in order and no prompt content", async () => {
  const { createServer } = await import("vite");
  const React = (await import("react")).default;
  const { renderToStaticMarkup } = await import("react-dom/server");
  // noDiscovery: the SSR tests never need pre-bundling, and leaving the
  // dependency scan on races `close()` -- a fast test closes the server
  // while the scan is still resolving entries, which logs "Request is
  // outdated" noise and made the run look flaky.
  const vite = await createServer({
    server: { middlewareMode: true },
    appType: "custom",
    optimizeDeps: { noDiscovery: true },
  });
  try {
    const { ContextManifestList } = await vite.ssrLoadModule(
      "/src/components/panels/ContextManifestPanel.tsx");
    const html = renderToStaticMarkup(React.createElement(ContextManifestList, {
      records: [
        record("t1:s1:10", {
          budget_tokens: 10, total_tokens: 4,
          dropped: [{ reason: "budget" }],
          prompt: "SECRET PROMPT CONTENT",
        }),
        record("t1:s1:2", { budget_tokens: 10, total_tokens: 6, risk: "high" }),
      ],
    }));
    assert.ok(html.indexOf("attempt 2") < html.indexOf("attempt 10"),
      "attempt 2 must render before attempt 10");
    assert.match(html, /6\/10 tok · risk high/);
    assert.match(html, /4\/10 tok/);
    assert.match(html, /budget/);
    assert.ok(!html.includes("SECRET"));
  } finally {
    await vite.close();
  }
});

test("an empty trail says nothing was recorded, not an error", async () => {
  const { createServer } = await import("vite");
  const React = (await import("react")).default;
  const { renderToStaticMarkup } = await import("react-dom/server");
  const vite = await createServer({
    server: { middlewareMode: true },
    appType: "custom",
    optimizeDeps: { noDiscovery: true },
  });
  try {
    const { ContextManifestList } = await vite.ssrLoadModule(
      "/src/components/panels/ContextManifestPanel.tsx");
    const html = renderToStaticMarkup(
      React.createElement(ContextManifestList, { records: [] }));
    assert.match(html, /No compiles recorded/);
    assert.ok(!/error|unavailable/i.test(html));
  } finally {
    await vite.close();
  }
});
