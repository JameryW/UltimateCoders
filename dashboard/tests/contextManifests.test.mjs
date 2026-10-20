import assert from "node:assert/strict";
import { test } from "node:test";
import {
  appendManifestPage,
  manifestOrderKey,
  manifestHeadline,
  manifestTrailQuery,
  manifestRecordedAt,
  manifestCountLine,
  tierCountsLine,
  droppedLine,
  parseManifestKey,
} from "../src/lib/contextManifests.ts";

const record = (key, summary, recorded_at) => ({ key, summary, recorded_at });

test("the order key keeps a subtask boundary between s1 and s10", () => {
  // Parsed per attempt, not sorted per string: the backend's task scan is
  // pinned Python-side; this pins the helper the panel parses keys with.
  assert.deepEqual(manifestOrderKey("t1:s1:1"), ["t1:s1", 1]);
  assert.deepEqual(manifestOrderKey("t1:s10:0"), ["t1:s10", 0]);
  assert.notEqual(manifestOrderKey("t1:s1:1")[0], manifestOrderKey("t1:s10:0")[0]);
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

test("the panel asks for the newest window, not the default key order", () => {
  // The endpoint's default is the OLDEST key window (#751): asking for it
  // hides exactly the attempts an audit is about once the trail truncates.
  // `order=recent` is the real time window.
  assert.match(manifestTrailQuery("t1"), /[?&]task_id=t1(&|$)/);
  assert.match(manifestTrailQuery("t1"), /[?&]order=recent(&|$)/);
  assert.ok(!manifestTrailQuery("t1").includes("order=key"));
  // Key order stays available on request, and ids are still encoded.
  assert.match(manifestTrailQuery("task 10", "key"), /task_id=task%2010(&|$)/);
  assert.match(manifestTrailQuery("task 10", "key"), /[?&]order=key(&|$)/);
});

test("the compile time is rendered in UTC", () => {
  // Deterministic on every machine: epoch seconds -> HH:MM:SSZ. Local time
  // would make the rendered trail depend on the viewer's time zone.
  assert.equal(manifestRecordedAt(1700000000), "22:13:20Z");
  assert.equal(manifestRecordedAt(0), "00:00:00Z");
});

test("the compile time degrades without inventing one", () => {
  // A missing or non-finite timestamp is not a time; the row omits it
  // rather than printing "Invalid Date"/"NaN" (the count line's "of N"
  // rule, one field over).
  assert.equal(manifestRecordedAt(undefined), "");
  assert.equal(manifestRecordedAt(Number.NaN), "");
  assert.equal(manifestRecordedAt(Number.POSITIVE_INFINITY), "");
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

test("drop reasons are attributed to the tier that lost the item", () => {
  // The surviving-side counts already name tiers ("P2 evidence 3"); the drop
  // line used to aggregate reasons only, hiding which tier was starved even
  // though the record carried it.
  assert.equal(droppedLine([
    { tier: "P2 evidence", reason: "budget" },
    { tier: "P2 evidence", reason: "budget" },
    { tier: "P4 memory", reason: "budget" },
  ]), "P2 evidence: budget (2) · P4 memory: budget");
  assert.equal(droppedLine([]), "");
});

test("drop lines are ordered by tier, not by the record's drop order", () => {
  assert.equal(droppedLine([
    { tier: "P5 background", reason: "ttl" },
    { tier: "P0 policy", reason: "duplicate" },
    { tier: "P3 trace", reason: "budget" },
  ]), "P0 policy: duplicate · P3 trace: budget · P5 background: ttl");
});

test("pre-vocabulary and tierless drops degrade without inventing a title", () => {
  // Manifests written before the vocabulary fix store the raw enum number:
  // render "P3", never a title the record did not carry. No tier at all
  // keeps the old reason-only form.
  assert.equal(droppedLine([
    { tier: 3, reason: "ttl" },
    { tier: 3, reason: "ttl" },
    { reason: "budget" },
  ]), "P3: ttl (2) · budget");
});

test("tierless drops keep the reason-only form and counts", () => {
  assert.equal(droppedLine([
    { reason: "budget" }, { reason: "budget" }, { reason: "ttl" }, {},
  ]), "budget (2) · ttl · unspecified");
});

test("the count line never reports a truncated window as the whole trail", () => {
  // THE honesty pin: shown is the window size, total the scan count.
  // Rendering `shown` as "N recorded" understates the trail exactly when
  // it was truncated -- the case the line exists to distinguish.
  assert.equal(manifestCountLine(3, 3), "3 recorded");
  assert.equal(manifestCountLine(1, 3), "1 of 3 recorded");
  assert.equal(manifestCountLine(0, 0), "0 recorded");
});

test("the count line degrades without inventing an 'of N' claim", () => {
  // No total from the backend (older wire): the shown count stands alone.
  // An implementation that interpolates `total` unconditionally renders
  // "2 of undefined recorded" here -- this pin and the truncation pin
  // above redden on different mutations, so neither is decorative.
  assert.equal(manifestCountLine(2, undefined), "2 recorded");
});

test("the trail query carries the paging cursor for the next page", () => {
  // Without the cursor the endpoint can only ever serve the newest window;
  // the cursor is the previous page's last row key and must survive URL
  // encoding (keys contain colons, which are legal but must not be lost).
  assert.match(
    manifestTrailQuery("t1", "recent", "t1:s1:3"),
    /[?&]before=t1%3As1%3A3(&|$)/,
  );
  assert.ok(!manifestTrailQuery("t1", "recent").includes("before="));
});

test("appending a page keeps one row per key", () => {
  // The endpoint's cursor should not overlap pages, but a record re-stamped
  // between requests can be served twice; the panel must show one row per
  // compile, not one per response.
  const merged = appendManifestPage(
    { records: [record("t1:b:0", {})] },
    { manifests: [record("t1:b:0", {}), record("t1:a:0", {})] },
  );
  assert.deepEqual(merged.records.map((r) => r.key), ["t1:b:0", "t1:a:0"]);
});

test("appending a page updates or clears the next cursor", () => {
  const more = appendManifestPage(
    { records: [] },
    { manifests: [record("t1:s1:1", {})], next_cursor: "t1:s1:1" },
  );
  assert.equal(more.nextCursor, "t1:s1:1");
  // null on the wire means "trail exhausted": no cursor is invented.
  const end = appendManifestPage(more, {
    manifests: [record("t1:s1:0", {})],
    next_cursor: null,
  });
  assert.equal(end.nextCursor, undefined);
});

test("a page without a total keeps the loaded total", () => {
  const merged = appendManifestPage(
    { records: [], total: 9 },
    { manifests: [record("t1:s1:0", {})] },
  );
  assert.equal(merged.total, 9);
  // A page that does report a total refreshes it.
  const refreshed = appendManifestPage(merged, { manifests: [], total: 11 });
  assert.equal(refreshed.total, 11);
});

test("the rendered trail preserves the backend's newest-first order", async () => {
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
    // Reverse key order with descending times: the endpoint's window order
    // is authoritative, so re-sorting here would silently turn the
    // newest-first window back into key order.
    const html = renderToStaticMarkup(React.createElement(ContextManifestList, {
      records: [
        record("t1:s9:0", {
          budget_tokens: 10, total_tokens: 4,
          dropped: [{ reason: "budget" }],
          prompt: "SECRET PROMPT CONTENT",
        }, 1700000000),
        record("t1:s1:2", { budget_tokens: 10, total_tokens: 6, risk: "high" },
          1699999000),
        record("t1:s1:10", { budget_tokens: 10, total_tokens: 8 }, 1699998000),
      ],
    }));
    const first = html.indexOf("s9 · attempt 0");
    const second = html.indexOf("s1 · attempt 2");
    const third = html.indexOf("s1 · attempt 10");
    assert.ok(first >= 0, html);
    assert.ok(second > first && third > second, html);
    assert.match(html, /22:13:20Z/);
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
