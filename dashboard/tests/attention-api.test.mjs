import assert from "node:assert/strict";
import { test } from "node:test";
import {
  fetchAttention,
  formatAge,
  formatCapacity,
  isOffline,
  parseAttentionItems,
  parseCapacity,
  resolveAttention,
  worklistOrder,
} from "../src/lib/attentionApi.ts";

const NOW_MS = 1_700_000_000_000; // 2023-11-14T22:13:20Z

function item(overrides) {
  return {
    id: "att-1",
    case_id: "case-1",
    kind: "waiting_event",
    summary: "waiting on CI",
    since: "2023-11-14T22:10:00Z",
    priority: 10,
    ...overrides,
  };
}

// ── worklistOrder ─────────────────────────────────────────────
// The panel renders one section per kind, so the cross-kind order here is the
// order *inside* a section; the kind precedence is pinned by the render test
// below. What this group pins is the canonical intra-kind order.

test("needs_human comes first regardless of priority", () => {
  const order = worklistOrder([
    parseItem(item({ id: "a", kind: "waiting_event", priority: 250 })),
    parseItem(item({ id: "b", kind: "needs_human", priority: 1 })),
  ]);
  assert.deepEqual(order.map((entry) => entry.id), ["b", "a"]);
});

test("priority descending, then the oldest since, then case_id", () => {
  const order = worklistOrder([
    parseItem(item({ id: "low", priority: 1, case_id: "case-z" })),
    parseItem(item({ id: "high-new", priority: 90, since: "2023-11-14T22:12:00Z" })),
    parseItem(item({ id: "high-old", priority: 90, since: "2023-11-14T21:00:00Z" })),
    parseItem(item({ id: "tie-b", priority: 90, since: "2023-11-14T21:00:00Z", case_id: "case-b" })),
    parseItem(item({ id: "tie-a", priority: 90, since: "2023-11-14T21:00:00Z", case_id: "case-a" })),
  ]);
  // Same priority → oldest `since` first; three rows share 21:00, so the tie
  // falls to `case_id` (case-1 < case-a < case-b), exactly like the Python key.
  assert.deepEqual(order.map((entry) => entry.id), [
    "high-old",
    "tie-a",
    "tie-b",
    "high-new",
    "low",
  ]);
});

test("ordering never mutates the input array", () => {
  const items = [
    parseItem(item({ id: "a", kind: "needs_human" })),
    parseItem(item({ id: "b" })),
  ];
  const snapshot = items.map((entry) => entry.id);
  worklistOrder(items);
  assert.deepEqual(items.map((entry) => entry.id), snapshot);
});

// ── formatCapacity ────────────────────────────────────────────

test("the capacity line names all three facts", () => {
  const capacity = parseCapacity({
    worker_slots_total: 4,
    worker_slots_busy: 2,
    worker_slots_reserved_human: 1,
    waiting_event_cases: 5,
    verifying_cases: 1,
  });
  assert.equal(
    formatCapacity(capacity),
    "2/4 worker slots busy · 1 slot reserved for human decisions · 6 slots saved by quiet waiting",
  );
});

test("the capacity line singularises a one-slot budget", () => {
  const capacity = parseCapacity({ worker_slots_total: 1, worker_slots_busy: 1 });
  assert.equal(
    formatCapacity(capacity),
    "1/1 worker slot busy · 0 slots reserved for human decisions · 0 slots saved by quiet waiting",
  );
});

test("derived capacity fields are recomputed, never copied, and never negative", () => {
  const capacity = parseCapacity({
    worker_slots_total: 4,
    worker_slots_busy: 3,
    worker_slots_reserved_human: 2,
    waiting_event_cases: 2,
    verifying_cases: 3,
    // A stale/lying reply: the counters must not be believed over the parts.
    worker_slots_available: 99,
    slots_saved_by_quiet_waiting: 99,
  });
  assert.equal(capacity.worker_slots_available, 0);
  assert.equal(capacity.slots_saved_by_quiet_waiting, 5);
});

// ── formatAge ─────────────────────────────────────────────────

test("age is rendered from the wire timestamp", () => {
  assert.equal(formatAge(NOW_MS - 30_000, NOW_MS), "just now");
  assert.equal(formatAge(NOW_MS - 5 * 60_000, NOW_MS), "5m ago");
  assert.equal(formatAge(NOW_MS - 150 * 60_000, NOW_MS), "2h 30m ago");
  assert.equal(formatAge(NOW_MS - 3 * 86_400_000, NOW_MS), "3d ago");
});

test("an unusable timestamp says so instead of inventing an age", () => {
  assert.equal(formatAge(0, NOW_MS), "age unknown");
  assert.equal(formatAge(Number.NaN, NOW_MS), "age unknown");
});

// ── payload projection ────────────────────────────────────────

function rawItem(overrides) {
  return { ...item(), ...overrides };
}

/** Parse exactly one fixture; a silent `null` here would hide a projection bug. */
function parseItem(raw) {
  const parsed = parseAttentionItems([raw]);
  assert.equal(parsed.length, 1, `fixture was rejected: ${JSON.stringify(raw)}`);
  return parsed[0];
}

test("RFC 3339 and Unix-seconds timestamps both normalise to milliseconds", () => {
  const iso = parseItem(rawItem({ since: "2023-11-14T22:13:20Z" }));
  const seconds = parseItem(rawItem({ since: 1_700_000_000 }));
  assert.equal(iso.since, NOW_MS);
  assert.equal(seconds.since, NOW_MS);
});

test("an unusable timestamp falls back to the epoch sentinel", () => {
  const parsed = parseItem(rawItem({ since: "not-a-time" }));
  assert.equal(parsed.since, 0);
});

test("entries the UI cannot place are filtered, not thrown", () => {
  const items = parseAttentionItems([
    null,
    "nope",
    {},
    rawItem({ id: undefined }),
    rawItem({ case_id: "   " }),
    rawItem({ kind: "future_kind" }),
    rawItem({ id: "kept", kind: "needs_human" }),
  ]);
  assert.deepEqual(items.map((entry) => entry.id), ["kept"]);
});

test("resolved items are dropped unless explicitly asked for", () => {
  const payload = [
    rawItem({ id: "open" }),
    rawItem({ id: "done", resolved_at: "2023-11-14T22:20:00Z" }),
  ];
  assert.deepEqual(parseAttentionItems(payload).map((entry) => entry.id), ["open"]);
  assert.deepEqual(
    parseAttentionItems(payload, true).map((entry) => entry.id),
    ["open", "done"],
  );
});

test("priority is clamped into the Rust u8 range", () => {
  assert.equal(parseItem(rawItem({ priority: 1000 })).priority, 255);
  assert.equal(parseItem(rawItem({ priority: -4 })).priority, 0);
  assert.equal(parseItem(rawItem({ priority: "junk" })).priority, 0);
});

// ── 503 / ok result mapping (stubbed fetch) ───────────────────

async function withFetch(handler, run) {
  const original = globalThis.fetch;
  globalThis.fetch = handler;
  try {
    return await run();
  } finally {
    globalThis.fetch = original;
  }
}

const OFFLINE_BODY = JSON.stringify({ error: "governance_unavailable" });

test("a 503 is returned as data and reports the governance plane offline", async () => {
  const seen = [];
  const result = await withFetch(
    async (url, init) => {
      seen.push({ url, init });
      return new Response(OFFLINE_BODY, {
        status: 503,
        headers: { "Content-Type": "application/json" },
      });
    },
    () => fetchAttention(),
  );
  assert.equal(result.ok, false);
  assert.equal(result.status, 503);
  assert.equal(result.error, "governance_unavailable");
  assert.equal(isOffline(result), true);
  assert.match(seen[0].url, /^\/api\/attention\?include_resolved=false&limit=\d+$/);
});

test("a transport failure reads as offline too, with an honest status", async () => {
  const result = await withFetch(
    async () => {
      throw new Error("Failed to fetch");
    },
    () => fetchAttention(25),
  );
  assert.equal(result.ok, false);
  assert.equal(result.status, 0);
  assert.equal(result.error, "Failed to fetch");
  assert.equal(isOffline(result), true);
});

test("a 200 maps to a parsed worklist plus capacity", async () => {
  const body = JSON.stringify({
    items: [
      rawItem({ id: "att-2", case_id: "case-ci", kind: "waiting_event", priority: 5 }),
      { broken: true },
    ],
    capacity: { worker_slots_total: 2, worker_slots_busy: 1, waiting_event_cases: 1 },
  });
  const result = await withFetch(
    async () => new Response(body, { status: 200, headers: { "Content-Type": "application/json" } }),
    () => fetchAttention(),
  );
  assert.equal(result.ok, true);
  assert.deepEqual(result.data.items.map((entry) => entry.id), ["att-2"]);
  assert.equal(result.data.capacity.worker_slots_busy, 1);
  assert.equal(result.data.capacity.worker_slots_available, 1);
});

test("a 200 with a junk body is a quiet invalid_payload, not a throw", async () => {
  const result = await withFetch(
    async () => new Response("<html>not json</html>", { status: 200 }),
    () => fetchAttention(),
  );
  assert.equal(result.ok, false);
  assert.equal(result.status, 502);
  assert.equal(result.error, "invalid_payload");
  assert.equal(isOffline(result), false);
});

test("resolve posts the human decision and returns the recorded item", async () => {
  let captured;
  const result = await withFetch(
    async (url, init) => {
      captured = { url, init };
      return new Response(
        JSON.stringify({ item: rawItem({ id: "att-9", kind: "needs_human" }) }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    },
    () => resolveAttention("att-9", { resolution_kind: "human_decision", detail: "approved" }),
  );
  assert.equal(result.ok, true);
  assert.equal(result.data.id, "att-9");
  assert.equal(captured.url, "/api/attention/att-9/resolve");
  assert.equal(captured.init.method, "POST");
  assert.deepEqual(JSON.parse(captured.init.body), {
    resolution_kind: "human_decision",
    detail: "approved",
  });
});

test("resolve on a dead bridge stays quiet as well", async () => {
  const result = await withFetch(
    async () => new Response(OFFLINE_BODY, { status: 503 }),
    () => resolveAttention("att-1", { resolution_kind: "human_decision" }),
  );
  assert.equal(result.ok, false);
  assert.equal(result.status, 503);
  assert.equal(isOffline(result), true);
});

// ── rendered worklist ─────────────────────────────────────────

test("the rendered worklist puts needs_human before every other kind", async () => {
  const { createServer } = await import("vite");
  const React = (await import("react")).default;
  const { renderToStaticMarkup } = await import("react-dom/server");
  // noDiscovery: the SSR assertions never need pre-bundling, and leaving the
  // dependency scan on races `close()` (same reason as the manifest panel test).
  const vite = await createServer({
    server: { middlewareMode: true },
    appType: "custom",
    optimizeDeps: { noDiscovery: true },
  });
  try {
    const { AttentionWorklist } = await vite.ssrLoadModule(
      "/src/components/panels/AttentionPanel.tsx",
    );
    const html = renderToStaticMarkup(
      React.createElement(AttentionWorklist, {
        state: "ready",
        capacity: parseCapacity({ worker_slots_total: 4, worker_slots_busy: 2, waiting_event_cases: 2 }),
        items: [
          parseItem(item({ id: "wait-high", case_id: "case-ci", kind: "waiting_event", priority: 255 })),
          parseItem(item({ id: "human-low", case_id: "case-human", kind: "needs_human", priority: 0 })),
        ],
        onResolve: async () => null,
      }),
    );
    assert.ok(html.indexOf("Needs human") >= 0, html);
    assert.ok(html.indexOf("Needs human") < html.indexOf("Waiting on event"), html);
    assert.ok(html.indexOf("case-human") < html.indexOf("case-ci"), html);
    // The resolve control only exists for a pending human decision.
    assert.match(html, /Why\? \(recorded with the decision\)/);
    assert.equal(html.match(/attention-item/g).length, 2);
  } finally {
    await vite.close();
  }
});

test("the rendered offline and empty states are quiet, not error pages", async () => {
  const { createServer } = await import("vite");
  const React = (await import("react")).default;
  const { renderToStaticMarkup } = await import("react-dom/server");
  const vite = await createServer({
    server: { middlewareMode: true },
    appType: "custom",
    optimizeDeps: { noDiscovery: true },
  });
  try {
    const { AttentionWorklist } = await vite.ssrLoadModule(
      "/src/components/panels/AttentionPanel.tsx",
    );
    const offline = renderToStaticMarkup(
      React.createElement(AttentionWorklist, { state: "offline", items: [], capacity: null }),
    );
    assert.match(offline, /Governance plane offline/);
    assert.ok(!/Error|failed/i.test(offline), offline);

    const empty = renderToStaticMarkup(
      React.createElement(AttentionWorklist, {
        state: "ready",
        items: [],
        capacity: parseCapacity({}),
      }),
    );
    assert.match(empty, /Worklist clear/);
    assert.ok(!/Error|failed/i.test(empty), empty);
  } finally {
    await vite.close();
  }
});
