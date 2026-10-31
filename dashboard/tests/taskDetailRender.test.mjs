import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const dashboardRoot = join(dirname(fileURLToPath(import.meta.url)), "..");

test("selecting a historical task renders persisted subtask state and result", async () => {
  const vite = await createServer({ server: { middlewareMode: true }, appType: "custom" });
  try {
    const { TasksPanel } = await vite.ssrLoadModule("/src/components/panels/TasksPanel.tsx");
    const task = {
      id: "historical-task",
      description: "Read README heading",
      status: "completed",
      project_id: "repo",
      subtask_count: 1,
      created_at: "2026-09-24T01:00:00Z",
      updated_at: "2026-09-24T02:00:00Z",
      subtasks: [{
        id: "historical-subtask",
        description: "Read the file",
        status: "completed",
        depends_on: [],
        result: "# UltimateCoders",
      }],
    };
    const html = renderToStaticMarkup(React.createElement(TasksPanel, {
      data: { available: true, tasks: [task], total: 1, status_counts: { completed: 1 }, pending_task_count: 0 },
      interactionLog: {},
      onSelectTask: () => {},
      selectedTaskId: task.id,
      onFlush: () => {},
    }));
    assert.match(html, /Task detail: Read README heading/);
    assert.match(html, /# UltimateCoders/);
  } finally {
    await vite.close();
  }
});

test("the operations dashboard opens on the live command-center overview", async () => {
  const vite = await createServer({ server: { middlewareMode: true }, appType: "custom" });
  try {
    const { TerminalDashboard } = await vite.ssrLoadModule("/src/components/terminal/TerminalDashboard.tsx");
    const running = {
      id: "live-task",
      description: "Ship command-center overview",
      status: "in_progress",
      project_id: "repo",
      subtask_count: 2,
      created_at: "2026-09-24T01:00:00Z",
      updated_at: "2026-09-24T02:00:00Z",
      subtasks: [
        {
          id: "swarm-a",
          description: "Wire advisor rail",
          status: "in_progress",
          depends_on: [],
          phase: "executing",
          percent: 42,
          step_status: "started",
        },
        {
          id: "swarm-b",
          description: "Done earlier",
          status: "completed",
          depends_on: [],
          percent: 100,
        },
      ],
    };
    const html = renderToStaticMarkup(React.createElement(TerminalDashboard, {
      connected: true,
      grpcState: "connected",
      grpcExhausted: false,
      dashGrpcState: "connected",
      theme: "dark",
      onToggleTheme: () => {},
      onLogout: () => {},
      onReconnectGrpc: () => {},
      onReconnectDashGrpc: () => {},
      fetchErrors: {},
      grpcStale: false,
      health: {
        available: true,
        status: "healthy",
        version: "0.2.1",
        uptime_seconds: 90,
        components: [{ name: "engine", status: "ok" }, { name: "nats", status: "ok" }],
      },
      workers: {
        available: true,
        workers: [{
          id: "worker-1",
          capabilities: ["code"],
          current_load: 1,
          max_capacity: 4,
          load_percent: 25,
          is_available: true,
          last_heartbeat: "2026-09-24T02:00:00Z",
        }],
        total: 1,
        available_count: 1,
      },
      tasks: { available: true, tasks: [running], total: 1, status_counts: { in_progress: 1 }, pending_task_count: 2 },
      scheduler: { available: false, is_running: false, night_window: null, jobs: [], execution_history: [] },
      eventLog: [{ timestamp: "2026-09-24T02:00:00Z", type: "subtask_started", details: { task_id: running.id, worker_id: "worker-1" } }],
      metrics: null,
      interactionLog: {},
      selectedTask: running,
      selectedTaskId: running.id,
      taskHistoryLoading: false,
      onSelectTask: () => {},
      onPauseTask: async () => ({ success: true }),
      onResumeTask: async () => ({ success: true }),
      onCancelTask: async () => ({ success: true }),
      onRefresh: async () => {},
      onFlush: () => {},
      onTaskCreated: () => {},
      onOptimisticAdd: () => {},
    }));
    assert.match(html, /gateway · lead/);
    assert.match(html, /Ship command-center overview/);
    assert.match(html, /HEALTH advisor/);
    assert.match(html, /◆ engine/);
    assert.match(html, /◇ nats/);
    assert.match(html, /session log/);
    assert.match(html, /workers \[1\/1\]/);
    assert.match(html, /tasks \[1 running\]/);
    assert.match(html, /grpc \[connected\]/);
    assert.match(html, /dispatch · fork layer/);
    assert.match(html, /delegate to workers · swarm/);
    assert.match(html, /◐ running/);
    assert.match(html, />42%</);
    assert.match(html, /review · in flight/);
    assert.doesNotMatch(html, /Opus|Sonnet|Haiku|Claude Code|JEV/);
    assert.doesNotMatch(html, /preview · not live data/);
    assert.doesNotMatch(html, /data-cc-preview="sample"/);
    assert.doesNotMatch(html, />SAMPLE</);
    // Overview hero: left command rail collapsed (mobile quick actions may still exist).
    assert.doesNotMatch(html, /terminal-command-rail/);
    // Completed subtasks must not steal the swarm row when nothing else is running.
    assert.doesNotMatch(html, /swarm-b/);
    // Running subtasks drive the swarm stage + flow particles above it.
    assert.match(html, /data-active-stage="swarm"/);
    assert.match(html, /cc-card swarm(?: cc-swarm-enter)? cc-stage-active/);
    assert.match(html, /cc-flow-connector cc-flow-active[^>]*data-flow-from="lead"/);
    assert.match(html, /cc-flow-connector cc-flow-active[^>]*data-flow-from="dispatch"/);
    assert.doesNotMatch(html, /data-flow-from="swarm"[^>]*cc-flow-active|cc-flow-active[^>]*data-flow-from="swarm"/);
  } finally {
    await vite.close();
  }
});

test("command-center swarm falls back to workers when no running subtasks", async () => {
  const vite = await createServer({ server: { middlewareMode: true }, appType: "custom" });
  try {
    const { TerminalDashboard } = await vite.ssrLoadModule("/src/components/terminal/TerminalDashboard.tsx");
    const idle = {
      id: "idle-task",
      description: "Queued only",
      status: "planning",
      project_id: "repo",
      subtask_count: 1,
      created_at: "2026-09-24T01:00:00Z",
      updated_at: "2026-09-24T02:00:00Z",
      subtasks: [{ id: "done-only", description: "Finished", status: "completed", depends_on: [], percent: 100 }],
    };
    const html = renderToStaticMarkup(React.createElement(TerminalDashboard, {
      connected: true,
      grpcState: "connected",
      grpcExhausted: false,
      dashGrpcState: "connected",
      theme: "dark",
      onToggleTheme: () => {},
      onLogout: () => {},
      onReconnectGrpc: () => {},
      onReconnectDashGrpc: () => {},
      fetchErrors: {},
      grpcStale: false,
      health: { available: true, status: "healthy", components: [{ name: "engine", status: "ok" }] },
      workers: {
        available: true,
        workers: [{
          id: "worker-alpha",
          capabilities: ["code"],
          current_load: 0,
          max_capacity: 2,
          load_percent: 0,
          is_available: true,
          last_heartbeat: "2026-09-24T02:00:00Z",
        }],
        total: 1,
        available_count: 1,
      },
      tasks: { available: true, tasks: [idle], total: 1, status_counts: { planning: 1 }, pending_task_count: 0 },
      scheduler: { available: false, is_running: false, night_window: null, jobs: [], execution_history: [] },
      eventLog: [],
      metrics: null,
      interactionLog: {},
      selectedTask: idle,
      selectedTaskId: idle.id,
      taskHistoryLoading: false,
      onSelectTask: () => {},
      onPauseTask: async () => ({ success: true }),
      onResumeTask: async () => ({ success: true }),
      onCancelTask: async () => ({ success: true }),
      onRefresh: async () => {},
      onFlush: () => {},
      onTaskCreated: () => {},
      onOptimisticAdd: () => {},
    }));
    assert.match(html, /worker-alpha/);
    assert.doesNotMatch(html, /done-only/);
    assert.match(html, /✓ done|◇ idle/);
    // Planning with no running swarm → lead stage (no connector particles yet).
    assert.match(html, /data-active-stage="lead"/);
    assert.match(html, /cc-card lead cc-stage-active/);
    assert.doesNotMatch(html, /cc-flow-active/);
  } finally {
    await vite.close();
  }
});

test("command-center active stage marches: dispatch queue and review terminal", async () => {
  const vite = await createServer({ server: { middlewareMode: true }, appType: "custom" });
  try {
    const mod = await vite.ssrLoadModule("/src/components/terminal/TerminalDashboard.tsx");
    const { TerminalDashboard, deriveActiveStage } = mod;

    assert.equal(deriveActiveStage({
      connected: true,
      lead: { id: "t1", description: "x", status: "planning", project_id: "", subtask_count: 0, created_at: "", updated_at: "", subtasks: [] },
      selectedTask: null,
      pending: 0,
      swarm: [{ running: false }],
    }), "lead");
    assert.equal(deriveActiveStage({
      connected: true,
      lead: { id: "t1", description: "x", status: "in_progress", project_id: "", subtask_count: 1, created_at: "", updated_at: "", subtasks: [] },
      selectedTask: null,
      pending: 3,
      swarm: [{ running: false }],
    }), "dispatch");
    assert.equal(deriveActiveStage({
      connected: true,
      lead: { id: "t1", description: "x", status: "in_progress", project_id: "", subtask_count: 1, created_at: "", updated_at: "" },
      selectedTask: null,
      pending: 3,
      swarm: [{ running: true }],
    }), "swarm");
    assert.equal(deriveActiveStage({
      connected: true,
      lead: { id: "t1", description: "x", status: "completed", project_id: "", subtask_count: 1, created_at: "", updated_at: "" },
      selectedTask: { id: "t1", description: "x", status: "completed", project_id: "", subtask_count: 1, created_at: "", updated_at: "" },
      pending: 0,
      swarm: [],
    }), "review");
    assert.equal(deriveActiveStage({
      connected: false,
      lead: null,
      selectedTask: null,
      pending: 0,
      swarm: [],
    }), "idle");

    const queued = {
      id: "queued-task",
      description: "Waiting on workers",
      status: "in_progress",
      project_id: "repo",
      subtask_count: 1,
      created_at: "2026-09-24T01:00:00Z",
      updated_at: "2026-09-24T02:00:00Z",
      subtasks: [{ id: "pending-sub", description: "Not started", status: "pending", depends_on: [] }],
    };
    const dispatchHtml = renderToStaticMarkup(React.createElement(TerminalDashboard, {
      connected: true,
      grpcState: "connected",
      grpcExhausted: false,
      dashGrpcState: "connected",
      theme: "dark",
      onToggleTheme: () => {},
      onLogout: () => {},
      onReconnectGrpc: () => {},
      onReconnectDashGrpc: () => {},
      fetchErrors: {},
      grpcStale: false,
      health: { available: true, status: "healthy", components: [] },
      workers: { available: true, workers: [], total: 0, available_count: 0 },
      tasks: { available: true, tasks: [queued], total: 1, status_counts: { in_progress: 1 }, pending_task_count: 4 },
      scheduler: { available: false, is_running: false, night_window: null, jobs: [], execution_history: [] },
      eventLog: [],
      metrics: null,
      interactionLog: {},
      selectedTask: queued,
      selectedTaskId: queued.id,
      taskHistoryLoading: false,
      onSelectTask: () => {},
      onPauseTask: async () => ({ success: true }),
      onResumeTask: async () => ({ success: true }),
      onCancelTask: async () => ({ success: true }),
      onRefresh: async () => {},
      onFlush: () => {},
      onTaskCreated: () => {},
      onOptimisticAdd: () => {},
    }));
    assert.match(dispatchHtml, /data-active-stage="dispatch"/);
    assert.match(dispatchHtml, /cc-card dispatch cc-stage-active/);
    assert.match(dispatchHtml, /cc-flow-connector cc-flow-active[^>]*data-flow-from="lead"/);
    assert.doesNotMatch(dispatchHtml, /data-flow-from="dispatch"[^>]*cc-flow-active|cc-flow-active[^>]*data-flow-from="dispatch"/);

    const done = {
      id: "done-task",
      description: "Ready for verify",
      status: "completed",
      project_id: "repo",
      subtask_count: 1,
      created_at: "2026-09-24T01:00:00Z",
      updated_at: "2026-09-24T02:00:00Z",
      subtasks: [{ id: "done-sub", description: "Finished", status: "completed", depends_on: [], percent: 100 }],
    };
    const reviewHtml = renderToStaticMarkup(React.createElement(TerminalDashboard, {
      connected: true,
      grpcState: "connected",
      grpcExhausted: false,
      dashGrpcState: "connected",
      theme: "dark",
      onToggleTheme: () => {},
      onLogout: () => {},
      onReconnectGrpc: () => {},
      onReconnectDashGrpc: () => {},
      fetchErrors: {},
      grpcStale: false,
      health: { available: true, status: "healthy", components: [] },
      workers: { available: true, workers: [], total: 0, available_count: 0 },
      tasks: { available: true, tasks: [done], total: 1, status_counts: { completed: 1 }, pending_task_count: 0 },
      scheduler: { available: false, is_running: false, night_window: null, jobs: [], execution_history: [] },
      eventLog: [],
      metrics: null,
      interactionLog: {},
      selectedTask: done,
      selectedTaskId: done.id,
      taskHistoryLoading: false,
      onSelectTask: () => {},
      onPauseTask: async () => ({ success: true }),
      onResumeTask: async () => ({ success: true }),
      onCancelTask: async () => ({ success: true }),
      onRefresh: async () => {},
      onFlush: () => {},
      onTaskCreated: () => {},
      onOptimisticAdd: () => {},
    }));
    assert.match(reviewHtml, /data-active-stage="review"/);
    assert.match(reviewHtml, /cc-card review cc-stage-active/);
    assert.match(reviewHtml, /cc-flow-active[^>]*data-flow-from="swarm"|data-flow-from="swarm"[^>]*cc-flow-active/);

    // prefers-reduced-motion must kill stage glow + connector particle motion.
    const css = readFileSync(join(dashboardRoot, "src/index.css"), "utf8");
    assert.match(css, /@media \(prefers-reduced-motion:\s*reduce\)[\s\S]*?\.cc-card\.cc-stage-active[\s\S]*?animation:\s*none/);
    assert.match(css, /@media \(prefers-reduced-motion:\s*reduce\)[\s\S]*?\.cc-flow-connector\.cc-flow-active em[\s\S]*?animation:\s*none/);
  } finally {
    await vite.close();
  }
});

function emptyClusterProps(overrides = {}) {
  return {
    connected: false,
    grpcState: "disconnected",
    grpcExhausted: false,
    dashGrpcState: "disconnected",
    theme: "dark",
    onToggleTheme: () => {},
    onLogout: () => {},
    onReconnectGrpc: () => {},
    onReconnectDashGrpc: () => {},
    fetchErrors: {},
    grpcStale: false,
    health: { available: false, status: "unavailable", components: [] },
    workers: { available: false, workers: [], total: 0, available_count: 0 },
    tasks: { available: false, tasks: [], total: 0, status_counts: {}, pending_task_count: 0 },
    scheduler: { available: false, is_running: false, night_window: null, jobs: [], execution_history: [] },
    eventLog: [],
    metrics: null,
    interactionLog: {},
    selectedTask: null,
    selectedTaskId: null,
    taskHistoryLoading: false,
    onSelectTask: () => {},
    onPauseTask: async () => ({ success: true }),
    onResumeTask: async () => ({ success: true }),
    onCancelTask: async () => ({ success: true }),
    onRefresh: async () => {},
    onFlush: () => {},
    onTaskCreated: () => {},
    onOptimisticAdd: () => {},
    ...overrides,
  };
}

test("command-center preview activates offline with empty cluster and drops when live data arrives", async () => {
  const vite = await createServer({ server: { middlewareMode: true }, appType: "custom" });
  try {
    const mod = await vite.ssrLoadModule("/src/components/terminal/TerminalDashboard.tsx");
    const { TerminalDashboard, shouldUseCommandCenterPreview } = mod;

    assert.equal(shouldUseCommandCenterPreview({
      connected: false,
      tasks: { tasks: [], total: 0 },
      workers: { workers: [], total: 0 },
    }), true);
    assert.equal(shouldUseCommandCenterPreview({
      connected: true,
      tasks: { tasks: [], total: 0 },
      workers: { workers: [], total: 0 },
    }), false);
    assert.equal(shouldUseCommandCenterPreview({
      connected: false,
      tasks: { tasks: [{ id: "t1" }], total: 1 },
      workers: { workers: [], total: 0 },
    }), false);
    assert.equal(shouldUseCommandCenterPreview({
      connected: false,
      tasks: { tasks: [], total: 0 },
      workers: { workers: [{ id: "w1" }], total: 1 },
    }), false);

    const previewHtml = renderToStaticMarkup(React.createElement(TerminalDashboard, emptyClusterProps()));
    assert.match(previewHtml, /data-cc-preview="sample"/);
    assert.match(previewHtml, /data-testid="cc-preview-banner"/);
    assert.match(previewHtml, />SAMPLE</);
    assert.match(previewHtml, /preview · not live data — waiting for gateway/);
    assert.match(previewHtml, /Ship command-center live board/);
    assert.match(previewHtml, /preview-a|preview-b|preview-c/);
    assert.match(previewHtml, /◐ running/);
    assert.match(previewHtml, /✓ done/);
    assert.match(previewHtml, /cc-flow-connector cc-flow-active/);
    assert.match(previewHtml, /data-active-stage="swarm"/);
    assert.doesNotMatch(previewHtml, /terminal-command-rail/);
    assert.doesNotMatch(previewHtml, /Opus|Sonnet|Haiku|Claude Code|JEV/);
    // Connected idle tip must not leak into SAMPLE preview.
    assert.doesNotMatch(previewHtml, /data-testid="cc-idle-tip"/);
    assert.doesNotMatch(previewHtml, /» submit a task to see live flow/);

    // Offline + residual totals must not fake a sample board.
    assert.equal(shouldUseCommandCenterPreview({
      connected: false,
      tasks: { tasks: [], total: 2 },
      workers: { workers: [], total: 0 },
    }), false);

    const liveHtml = renderToStaticMarkup(React.createElement(TerminalDashboard, emptyClusterProps({
      connected: true,
      grpcState: "connected",
      dashGrpcState: "connected",
      health: { available: true, status: "healthy", components: [{ name: "engine", status: "ok" }] },
      workers: {
        available: true,
        workers: [{
          id: "worker-live",
          capabilities: ["code"],
          current_load: 0,
          max_capacity: 2,
          load_percent: 0,
          is_available: true,
          last_heartbeat: "2026-10-31T12:00:00Z",
        }],
        total: 1,
        available_count: 1,
      },
    })));
    assert.doesNotMatch(liveHtml, /preview · not live data/);
    assert.doesNotMatch(liveHtml, />SAMPLE</);
    assert.doesNotMatch(liveHtml, /data-cc-preview="sample"/);
    assert.doesNotMatch(liveHtml, /Ship command-center live board/);
    assert.match(liveHtml, /data-cc-preview="live"/);
    assert.match(liveHtml, /data-testid="cc-idle-tip"/);
    assert.match(liveHtml, /» submit a task to see live flow/);
    assert.match(liveHtml, /data-testid="cc-idle-tip-log"/);
    assert.match(liveHtml, /cc-swarm-enter/);
  } finally {
    await vite.close();
  }
});

test("command-center merges interactionLog into session feed and diffs pill flashes", async () => {
  const vite = await createServer({ server: { middlewareMode: true }, appType: "custom" });
  try {
    const mod = await vite.ssrLoadModule("/src/components/terminal/TerminalDashboard.tsx");
    const {
      TerminalDashboard,
      mergeSessionLogEvents,
      sessionLogEventKey,
      changedPillKeys,
      isCommandCenterConnectedIdle,
      COMMAND_CENTER_IDLE_TIP,
    } = mod;

    const merged = mergeSessionLogEvents(
      [{ timestamp: "2026-10-31T12:00:10Z", type: "task_submitted", details: { task_id: "t1" } }],
      [
        {
          timestamp: "2026-10-31T12:00:20Z",
          type: "tool_call",
          task_id: "t1",
          subtask_id: "s1",
          data: { tool: "read" },
        },
        {
          timestamp: "2026-10-31T12:00:05Z",
          type: "subtask_started",
          task_id: "t1",
          data: { worker_id: "w1" },
        },
      ],
      8,
    );
    assert.deepEqual(merged.map((event) => event.type), ["tool_call", "task_submitted", "subtask_started"]);
    assert.equal(merged[0].details.tool, "read");
    assert.equal(merged[0].details.subtask_id, "s1");

    // Duplicate timestamp+type+task+subtask collapses to one row.
    const deduped = mergeSessionLogEvents(
      [{ timestamp: "2026-10-31T12:00:20Z", type: "tool_call", details: { task_id: "t1", subtask_id: "s1", tool: "read" } }],
      [{ timestamp: "2026-10-31T12:00:20Z", type: "tool_call", task_id: "t1", subtask_id: "s1", data: { tool: "read" } }],
      8,
    );
    assert.equal(deduped.length, 1);

    // Prepending a newer event must keep older row keys stable (no index suffix).
    const older = { timestamp: "2026-10-31T12:00:10Z", type: "task_submitted", details: { task_id: "t1" } };
    const newer = { timestamp: "2026-10-31T12:00:30Z", type: "tool_result", details: { task_id: "t1", subtask_id: "s1" } };
    const beforeKeys = [older].map((event) => sessionLogEventKey(event));
    const afterKeys = [newer, older].map((event) => sessionLogEventKey(event));
    assert.equal(afterKeys[1], beforeKeys[0]);
    assert.doesNotMatch(beforeKeys[0], /\|0$/);

    assert.deepEqual(changedPillKeys(null, {
      workers: "1/1", tasks: "0", queue: "0", grpc: "connected", dash: "connected", events: "0", backend: "online",
    }), []);
    assert.deepEqual(changedPillKeys(
      { workers: "1/1", tasks: "0", queue: "0", grpc: "connected", dash: "connected", events: "0", backend: "online" },
      { workers: "2/2", tasks: "1", queue: "0", grpc: "connected", dash: "connected", events: "3", backend: "online" },
    ), ["workers", "tasks", "events"]);

    assert.equal(isCommandCenterConnectedIdle({ connected: true, previewMode: false, stage: "idle" }), true);
    assert.equal(isCommandCenterConnectedIdle({ connected: true, previewMode: false, stage: "swarm" }), false);
    assert.equal(isCommandCenterConnectedIdle({ connected: false, previewMode: true, stage: "idle" }), false);
    assert.equal(COMMAND_CENTER_IDLE_TIP, "» submit a task to see live flow");

    const running = {
      id: "merge-task",
      description: "Merge session feed",
      status: "in_progress",
      project_id: "repo",
      subtask_count: 1,
      created_at: "2026-09-24T01:00:00Z",
      updated_at: "2026-09-24T02:00:00Z",
      subtasks: [{
        id: "merge-sub",
        description: "Work",
        status: "in_progress",
        depends_on: [],
        percent: 55,
        step_status: "started",
      }],
    };
    const html = renderToStaticMarkup(React.createElement(TerminalDashboard, {
      connected: true,
      grpcState: "connected",
      grpcExhausted: false,
      dashGrpcState: "connected",
      theme: "dark",
      onToggleTheme: () => {},
      onLogout: () => {},
      onReconnectGrpc: () => {},
      onReconnectDashGrpc: () => {},
      fetchErrors: {},
      grpcStale: false,
      health: { available: true, status: "healthy", version: "0.2.1", components: [] },
      workers: { available: true, workers: [], total: 0, available_count: 0 },
      tasks: { available: true, tasks: [running], total: 1, status_counts: { in_progress: 1 }, pending_task_count: 0 },
      scheduler: { available: false, is_running: false, night_window: null, jobs: [], execution_history: [] },
      eventLog: [{ timestamp: "2026-10-31T12:00:00Z", type: "task_submitted", details: { task_id: running.id } }],
      metrics: null,
      interactionLog: {
        [running.id]: [{
          timestamp: "2026-10-31T12:00:30Z",
          type: "tool_call",
          task_id: running.id,
          subtask_id: "merge-sub",
          data: { tool: "edit" },
        }],
      },
      selectedTask: running,
      selectedTaskId: running.id,
      taskHistoryLoading: false,
      onSelectTask: () => {},
      onPauseTask: async () => ({ success: true }),
      onResumeTask: async () => ({ success: true }),
      onCancelTask: async () => ({ success: true }),
      onRefresh: async () => {},
      onFlush: () => {},
      onTaskCreated: () => {},
      onOptimisticAdd: () => {},
    }));
    assert.match(html, /tool call →/);
    assert.match(html, /tool=edit/);
    assert.match(html, /task submitted →/);
    assert.match(html, /cc-card swarm cc-swarm-enter/);
    assert.doesNotMatch(html, /data-testid="cc-idle-tip"/);

    const css = readFileSync(join(dashboardRoot, "src/index.css"), "utf8");
    assert.match(css, /@keyframes cc-session-row-enter/);
    assert.match(css, /@keyframes cc-pill-flash/);
    assert.match(css, /@keyframes cc-swarm-enter/);
    assert.match(css, /@media \(prefers-reduced-motion:\s*reduce\)[\s\S]*?\.cc-session-row\.cc-session-row-enter[\s\S]*?animation:\s*none/);
    assert.match(css, /@media \(prefers-reduced-motion:\s*reduce\)[\s\S]*?\.cc-pill\.cc-pill-flash[\s\S]*?animation:\s*none/);
    assert.match(css, /@media \(prefers-reduced-motion:\s*reduce\)[\s\S]*?\.cc-card\.swarm\.cc-swarm-enter[\s\S]*?animation:\s*none/);
  } finally {
    await vite.close();
  }
});

test("command-center autofocus helper, stage chip, and first-visit dark preference", async () => {
  const vite = await createServer({ server: { middlewareMode: true }, appType: "custom" });
  try {
    const mod = await vite.ssrLoadModule("/src/components/terminal/TerminalDashboard.tsx");
    const {
      TerminalDashboard,
      pickAutofocusTaskId,
      shouldApplyCommandCenterDefaultDark,
    } = mod;

    const active = {
      id: "active-autofocus",
      description: "Wire autofocus",
      status: "in_progress",
      project_id: "repo",
      subtask_count: 1,
      created_at: "2026-09-24T01:00:00Z",
      updated_at: "2026-09-24T02:00:00Z",
      subtasks: [{
        id: "active-sub",
        description: "Running",
        status: "in_progress",
        depends_on: [],
        percent: 40,
        step_status: "started",
      }],
    };
    const idle = {
      id: "idle-other",
      description: "Completed earlier",
      status: "completed",
      project_id: "repo",
      subtask_count: 0,
      created_at: "2026-09-24T00:00:00Z",
      updated_at: "2026-09-24T00:30:00Z",
    };
    const tasks = { tasks: [idle, active] };

    assert.equal(pickAutofocusTaskId(tasks, null), "active-autofocus");
    assert.equal(pickAutofocusTaskId(tasks, undefined), "active-autofocus");
    assert.equal(pickAutofocusTaskId(tasks, "user-picked"), null);
    assert.equal(pickAutofocusTaskId(tasks, "active-autofocus"), null);
    assert.equal(pickAutofocusTaskId({ tasks: [idle] }, null), null);
    assert.equal(pickAutofocusTaskId({ tasks: [] }, null), null);

    assert.equal(shouldApplyCommandCenterDefaultDark("light", () => null), true);
    assert.equal(shouldApplyCommandCenterDefaultDark("dark", () => null), false);
    assert.equal(shouldApplyCommandCenterDefaultDark("light", () => "light"), false);
    assert.equal(shouldApplyCommandCenterDefaultDark("light", () => "dark"), false);
    assert.equal(shouldApplyCommandCenterDefaultDark("dark", () => "light"), false);

    let selected = null;
    const html = renderToStaticMarkup(React.createElement(TerminalDashboard, {
      connected: true,
      grpcState: "connected",
      grpcExhausted: false,
      dashGrpcState: "connected",
      theme: "dark",
      onToggleTheme: () => {},
      onLogout: () => {},
      onReconnectGrpc: () => {},
      onReconnectDashGrpc: () => {},
      fetchErrors: {},
      grpcStale: false,
      health: { available: true, status: "healthy", version: "0.2.1", components: [] },
      workers: { available: true, workers: [], total: 0, available_count: 0 },
      tasks: { available: true, tasks: [active], total: 1, status_counts: { in_progress: 1 }, pending_task_count: 0 },
      scheduler: { available: false, is_running: false, night_window: null, jobs: [], execution_history: [] },
      eventLog: [],
      metrics: null,
      interactionLog: {},
      selectedTask: active,
      selectedTaskId: active.id,
      taskHistoryLoading: false,
      onSelectTask: (id) => { selected = id; },
      onPauseTask: async () => ({ success: true }),
      onResumeTask: async () => ({ success: true }),
      onCancelTask: async () => ({ success: true }),
      onRefresh: async () => {},
      onFlush: () => {},
      onTaskCreated: () => {},
      onOptimisticAdd: () => {},
    }));
    assert.match(html, /data-testid="cc-stage-chip"/);
    assert.match(html, /data-stage="swarm"/);
    assert.match(html, /stage · swarm/);
    assert.match(html, /cc-stage-chip swarm/);
    // SSR does not run effects — autofocus is covered by the pure helper above.
    assert.equal(selected, null);

    const source = readFileSync(join(dashboardRoot, "src/components/terminal/TerminalDashboard.tsx"), "utf8");
    assert.match(source, /pickAutofocusTaskId/);
    assert.match(source, /shouldApplyCommandCenterDefaultDark/);
    assert.match(source, /uc_dashboard_theme/);
    assert.match(source, /Prefer dark CC chrome on first visit only/);
    assert.doesNotMatch(source, /Opus|Sonnet|Haiku|Claude Code|JEV/);

    const css = readFileSync(join(dashboardRoot, "src/index.css"), "utf8");
    assert.match(css, /\.cc-stage-chip\.swarm/);
    assert.match(css, /\.cc-legend-row/);
    assert.match(css, /@media \(prefers-reduced-motion:\s*reduce\)[\s\S]*?\.cc-stage-chip\.swarm[\s\S]*?box-shadow:\s*none/);
  } finally {
    await vite.close();
  }
});
