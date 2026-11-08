import assert from "node:assert/strict";
import { test } from "node:test";
import {
  appendDecisionPage,
  decisionCountLine,
  decisionDigestLabel,
  decisionHeadline,
  decisionOrderKey,
  decisionRecordedAt,
  decisionRouteLine,
  decisionTrailQuery,
  decisionsFromPayload,
  parseDecisionKey,
  workspaceLine,
} from "../src/lib/decisions.ts";

const record = (key, terminal = {}, extra = {}) => ({ key, terminal, ...extra });

test("the order key keeps a subtask boundary between s1 and s10", () => {
  assert.deepEqual(decisionOrderKey("t1:s1:1"), ["t1:s1", 1]);
  assert.deepEqual(decisionOrderKey("t1:s10:0"), ["t1:s10", 0]);
  assert.notEqual(decisionOrderKey("t1:s1:1")[0], decisionOrderKey("t1:s10:0")[0]);
});

test("a non-numeric attempt tail sorts like attempt 0", () => {
  assert.deepEqual(decisionOrderKey("t1:s1:oops"), ["t1:s1", 0]);
  assert.equal(parseDecisionKey("t1:s1:oops").attempt, 0);
});

test("parse splits on the last two colons so task ids may contain one", () => {
  assert.deepEqual(parseDecisionKey("task-10:s1:3"), {
    taskId: "task-10",
    subtaskId: "s1",
    attempt: 3,
  });
  assert.deepEqual(parseDecisionKey("task:10:s1:3"), {
    taskId: "task:10",
    subtaskId: "s1",
    attempt: 3,
  });
});

test("the trail query asks for the recent window and encodes the id", () => {
  assert.match(decisionTrailQuery("t1"), /[?&]task_id=t1(&|$)/);
  assert.match(decisionTrailQuery("t1"), /[?&]limit=100(&|$)/);
  assert.match(decisionTrailQuery("t1"), /[?&]order=recent(&|$)/);
  assert.match(decisionTrailQuery("task 10"), /task_id=task%2010(&|$)/);
  assert.match(decisionTrailQuery("t1", { limit: 5 }), /[?&]limit=5(&|$)/);
  assert.match(
    decisionTrailQuery("t1", { before: "t1:s1:9" }),
    /[?&]before=t1%3As1%3A9(&|$)/,
  );
  assert.ok(!decisionTrailQuery("t1").includes("before="));
});

test("paging appends without repeating a row and follows the cursor", () => {
  const first = appendDecisionPage(
    { records: [], total: 0 },
    {
      decisions: [record("t1:s1:2"), record("t1:s1:1")],
      total: 3,
      next_cursor: "t1:s1:1",
    },
  );
  assert.equal(first.total, 3);
  assert.equal(first.nextCursor, "t1:s1:1");

  const second = appendDecisionPage(first, {
    decisions: [record("t1:s1:1"), record("t1:s1:0")],
    total: 3,
    next_cursor: null,
  });
  assert.deepEqual(
    second.records.map((row) => row.key),
    ["t1:s1:2", "t1:s1:1", "t1:s1:0"],
  );
  assert.equal(second.nextCursor, undefined, "a short page is the end");
});

test("the terminal time is rendered in UTC", () => {
  assert.equal(decisionRecordedAt(1700000000), "22:13:20Z");
  assert.equal(decisionRecordedAt(0), "00:00:00Z");
});

test("the terminal time degrades without inventing one", () => {
  assert.equal(decisionRecordedAt(undefined), "");
  assert.equal(decisionRecordedAt(-1), "");
  assert.equal(decisionRecordedAt(Number.NaN), "");
});

test("a verdict is read, never guessed", () => {
  assert.equal(decisionHeadline(record("t:s:0", { success: true })), "succeeded");
  assert.equal(decisionHeadline(record("t:s:0", { success: false })), "failed");
  assert.equal(
    decisionHeadline(record("t:s:0", { success: false, error_class: "timeout" })),
    "failed (timeout)",
  );
  assert.equal(decisionHeadline(record("t:s:0", {})), "no verdict");
  assert.equal(decisionHeadline({ key: "t:s:0" }), "no verdict");
});

test("the route line names only the fields the route actually carries", () => {
  assert.equal(
    decisionRouteLine({ stage: "implementation", risk: "medium", model: "gpt-test" }),
    "implementation · medium · gpt-test",
  );
  assert.equal(decisionRouteLine({ stage: "implementation" }), "implementation");
  assert.equal(decisionRouteLine({}), "");
  assert.equal(decisionRouteLine(undefined), "");
});

test("the digest label is short and never the summary", () => {
  assert.equal(decisionDigestLabel("0123456789abcdef"), "ctx 0123456789ab");
  assert.equal(decisionDigestLabel(undefined), "");
});

test("the workspace line renders P7 evidence, capped with +N", () => {
  const workspace = {
    files: [
      { path: "src/a.py", change_type: "modified" },
      { path: "src/b.py", change_type: "created" },
      { path: "tests/c.py", change_type: "deleted" },
      { path: "docs/d.md", change_type: "modified" },
    ],
    file_count: 4,
    tool_calls: ["Edit", "Bash"],
    truncated: false,
  };
  const line = workspaceLine(workspace);
  assert.match(line, /^4 files \(src\/a\.py, src\/b\.py, tests\/c\.py, \+1\)/);
  assert.match(line, /tools: Edit, Bash/);
  assert.ok(!line.includes("truncated"), line);
});

test("the workspace line says truncated when the record was clipped", () => {
  const line = workspaceLine({
    files: [{ path: "a.py" }],
    file_count: 60,
    tool_calls: [],
    truncated: true,
  });
  assert.match(line, /^60 files \(a\.py\)/);
  assert.match(line, /truncated/);
});

test("the workspace line is empty when no effects were recorded", () => {
  assert.equal(workspaceLine(undefined), "");
  assert.equal(workspaceLine({ files: [], tool_calls: [] }), "0 files");
  assert.equal(workspaceLine({ files: [{ path: "" }], file_count: 0 }), "0 files");
});

test("the payload keeps the endpoint's counts and invents none", () => {
  const parsed = decisionsFromPayload({
    decisions: [record("t:s:0"), record("t:s:1")],
    count: 2,
    total: 9,
  });
  assert.equal(parsed.records.length, 2);
  assert.equal(parsed.count, 2);
  assert.equal(parsed.total, 9);

  const bare = decisionsFromPayload({ decisions: [record("t:s:0")] });
  assert.equal(bare.count, 1);
  assert.equal(bare.total, 1);
});

test("the count line never claims the window is the whole trail", () => {
  assert.equal(decisionCountLine(3, 3), "3 attempts");
  assert.equal(decisionCountLine(1, 1), "1 attempt");
  assert.equal(decisionCountLine(3, 12), "3 of 12 attempts");
});

async function renderList(props) {
  const { createServer } = await import("vite");
  const React = (await import("react")).default;
  const { renderToStaticMarkup } = await import("react-dom/server");
  const vite = await createServer({
    server: { middlewareMode: true },
    appType: "custom",
    optimizeDeps: { noDiscovery: true },
  });
  try {
    const { DecisionRecordList } = await vite.ssrLoadModule(
      "/src/components/panels/DecisionRecordPanel.tsx",
    );
    return renderToStaticMarkup(React.createElement(DecisionRecordList, props));
  } finally {
    await vite.close();
  }
}

test("an auditor sees the verdict, the route and what the attempt changed", async () => {
  const html = await renderList({
    records: [
      record(
        "t1:s1:2",
        {
          success: true,
          workspace: {
            files: [
              { path: "src/app.py", change_type: "modified" },
              { path: "tests/test_app.py", change_type: "created" },
            ],
            file_count: 2,
            tool_calls: ["Edit", "Bash"],
            truncated: false,
          },
        },
        { route: { stage: "implementation", risk: "medium" }, context_digest: "0123456789abcdef" },
      ),
    ],
  });
  assert.match(html, /s1 · attempt 2/);
  assert.match(html, /succeeded/);
  assert.match(html, /implementation · medium/);
  assert.match(html, /ctx 0123456789ab/);
  assert.match(html, /2 files \(src\/app\.py, tests\/test_app\.py\)/);
  assert.match(html, /tools: Edit, Bash/);
});

test("an empty trail says so instead of pretending", async () => {
  const html = await renderList({ records: [] });
  assert.match(html, /No dispatch attempts recorded for this task\./);
});
