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
    assert.match(html, /terminal-task-row[^"]*is-selected|is-selected[^"]*terminal-task-row/);
  } finally {
    await vite.close();
  }
});

test("tasks command-center: list chip, live strip, swarm rows, session log roles", async () => {
  const vite = await createServer({ server: { middlewareMode: true }, appType: "custom" });
  try {
    const progress = await vite.ssrLoadModule("/src/lib/taskProgress.ts");
    const {
      aggregateSubtaskProgress,
      taskListProgressChip,
      sessionRoleForEventType,
    } = progress;

    assert.equal(sessionRoleForEventType("task_submitted"), "lead");
    assert.equal(sessionRoleForEventType("subtask_started"), "swarm");
    assert.equal(sessionRoleForEventType("tool_call"), "dispatch");
    assert.equal(sessionRoleForEventType("llm_request"), "advisor");

    const subtasks = [
      {
        id: "swarm-done",
        description: "Done piece",
        status: "completed",
        depends_on: [],
        percent: 100,
        step_status: "completed",
      },
      {
        id: "swarm-run",
        description: "Wire session log",
        status: "in_progress",
        depends_on: [],
        percent: 42,
        phase: "executing",
        step_agent: "codex",
        step_status: "started",
        assigned_worker: "worker-alpha",
      },
      {
        id: "swarm-idle",
        description: "Pending piece",
        status: "pending",
        depends_on: [],
      },
    ];
    const agg = aggregateSubtaskProgress(subtasks);
    assert.equal(agg.completed, 1);
    assert.equal(agg.total, 3);
    assert.equal(agg.percent, 71); // mean of 100 + 42
    assert.equal(agg.phase, "executing");
    assert.equal(agg.stepAgent, "codex");
    assert.equal(agg.hasRunning, true);

    const chip = taskListProgressChip({ status: "in_progress", subtasks });
    assert.deepEqual(chip, { percent: 71, phase: "executing" });
    assert.equal(taskListProgressChip({ status: "completed", subtasks }), null);

    const { TasksPanel } = await vite.ssrLoadModule("/src/components/panels/TasksPanel.tsx");
    const { TaskDetail } = await vite.ssrLoadModule("/src/components/panels/TaskDetail.tsx");
    const { InteractionLog } = await vite.ssrLoadModule("/src/components/panels/InteractionLog.tsx");
    const { TerminalDashboard } = await vite.ssrLoadModule("/src/components/terminal/TerminalDashboard.tsx");

    const running = {
      id: "cc-task",
      description: "Polish tasks command center",
      status: "in_progress",
      project_id: "repo",
      subtask_count: 3,
      created_at: "2026-09-24T01:00:00Z",
      updated_at: "2026-09-24T02:00:00Z",
      subtasks,
    };

    const listHtml = renderToStaticMarkup(React.createElement(TasksPanel, {
      data: { available: true, tasks: [running], total: 1, status_counts: { in_progress: 1 }, pending_task_count: 0 },
      interactionLog: {},
      onSelectTask: () => {},
      selectedTaskId: running.id,
      showInlineDetail: false,
      onPauseTask: () => {},
      onCancelTask: () => {},
    }));
    assert.match(listHtml, /data-testid="terminal-task-row"/);
    assert.match(listHtml, /terminal-task-row[^"]*\bis-selected\b|\bis-selected\b[^"]*terminal-task-row/);
    assert.match(listHtml, /data-testid="terminal-task-progress-chip"/);
    assert.match(listHtml, /71% · executing/);
    assert.doesNotMatch(listHtml, /border-l-blue-500/);

    const events = [
      {
        timestamp: "2026-10-31T12:00:10Z",
        type: "task_submitted",
        task_id: running.id,
        data: { description: running.description },
      },
      {
        timestamp: "2026-10-31T12:00:20Z",
        type: "subtask_started",
        task_id: running.id,
        subtask_id: "swarm-run",
        data: { description: "Wire session log", worker_id: "worker-alpha" },
      },
      {
        timestamp: "2026-10-31T12:00:30Z",
        type: "tool_call",
        task_id: running.id,
        subtask_id: "swarm-run",
        data: { tool: "read", input_summary: "README.md" },
      },
      {
        timestamp: "2026-10-31T12:00:40Z",
        type: "llm_request",
        task_id: running.id,
        subtask_id: "swarm-run",
        data: { model: "gateway-llm" },
      },
    ];

    const detailHtml = renderToStaticMarkup(React.createElement(TaskDetail, {
      task: running,
      interactionLog: events,
      variant: "terminal",
    }));
    assert.match(detailHtml, /data-testid="terminal-swarm-list"/);
    assert.match(detailHtml, /data-testid="terminal-swarm-row"/);
    assert.match(detailHtml, /◐ running/);
    assert.match(detailHtml, /✓ done/);
    assert.match(detailHtml, />42%</);
    assert.match(detailHtml, /executing/);
    assert.match(detailHtml, /codex/);
    assert.match(detailHtml, /worker-a/);
    assert.match(detailHtml, /swarm · subtasks/);
    assert.doesNotMatch(detailHtml, /Timeline:/);
    assert.doesNotMatch(detailHtml, /🔧|▶️|📤|✅|❌/);
    assert.doesNotMatch(detailHtml, /Opus|Sonnet|Haiku|JEV/);
    assert.match(detailHtml, /cc-session-row cc-session-role-lead/);
    assert.match(detailHtml, /cc-session-row cc-session-role-swarm/);
    assert.match(detailHtml, /cc-session-row cc-session-role-dispatch/);
    assert.match(detailHtml, /cc-session-row cc-session-role-advisor/);
    assert.match(detailHtml, /data-session-role="lead"/);
    assert.match(detailHtml, /data-session-role="swarm"/);
    assert.match(detailHtml, /data-session-role="dispatch"/);
    assert.match(detailHtml, /data-session-role="advisor"/);

    // Default variant still keeps the emoji timeline for non-terminal surfaces.
    const legacyHtml = renderToStaticMarkup(React.createElement(TaskDetail, {
      task: running,
      interactionLog: events,
      variant: "default",
    }));
    assert.match(legacyHtml, /Timeline:/);
    assert.doesNotMatch(legacyHtml, /data-testid="terminal-swarm-list"/);

    const logHtml = renderToStaticMarkup(React.createElement(InteractionLog, { events, variant: "terminal" }));
    assert.match(logHtml, /cc-session-role-lead/);
    assert.match(logHtml, /cc-session-role-swarm/);
    assert.match(logHtml, /cc-session-role-dispatch/);
    assert.match(logHtml, /cc-session-role-advisor/);
    assert.doesNotMatch(logHtml, /🔧|▶️|●/);
    // Identity-stable keys (timestamp|type|task|subtask) — no trailing index suffix.
    const logSource = readFileSync(join(dashboardRoot, "src/components/panels/InteractionLog.tsx"), "utf8");
    assert.match(logSource, /interactionEventKey/);
    assert.doesNotMatch(logSource, /key=\{`\$\{ev\.timestamp\}-\$\{ev\.type\}-\$\{rowIndex\}`\}/);

    const dashHtml = renderToStaticMarkup(React.createElement(TerminalDashboard, {
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
      eventLog: [],
      metrics: null,
      interactionLog: { [running.id]: events },
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
      initialView: "tasks",
    }));
    assert.match(dashHtml, /data-testid="terminal-task-live-strip"/);
    assert.match(dashHtml, /terminal-mini-bar blue/);
    assert.match(dashHtml, /1\/3/);
    assert.match(dashHtml, /data-testid="terminal-task-live-phase"/);
    assert.match(dashHtml, /executing · codex/);
    assert.match(dashHtml, /data-testid="terminal-swarm-list"/);
    assert.match(dashHtml, /◐ running/);
    assert.match(dashHtml, /terminal-task-row/);
    assert.match(dashHtml, /data-testid="terminal-task-progress-chip"/);
    assert.doesNotMatch(dashHtml, /Timeline:/);
    assert.doesNotMatch(dashHtml, /border-l-blue-500/);
    assert.doesNotMatch(dashHtml, /Opus|Sonnet|Haiku|Claude Code|JEV/);

    const css = readFileSync(join(dashboardRoot, "src/index.css"), "utf8");
    assert.match(css, /\.terminal-task-layout \.status-in_progress[\s\S]*?--cc-swarm/);
    assert.match(css, /\.terminal-task-layout \.status-completed[\s\S]*?--cc-dispatch/);
    assert.match(css, /\.terminal-task-layout \.status-failed[\s\S]*?--terminal-red/);
    assert.match(css, /\.terminal-task-layout \.status-paused[\s\S]*?--cc-lead/);
    assert.match(css, /\.btn-action-warn[\s\S]*?--cc-lead/);
    assert.match(css, /\.terminal-task-row\.is-selected/);
    assert.match(css, /\.terminal-task-live-strip/);
    assert.match(css, /\.terminal-swarm-marker\.running/);
    assert.match(css, /@media \(prefers-reduced-motion:\s*reduce\)[\s\S]*?\.terminal-swarm-marker\.running[\s\S]*?animation:\s*none/);
    assert.match(css, /\.cc-session-role-lead[\s\S]*?--cc-lead/);
    assert.match(css, /\.cc-session-role-swarm[\s\S]*?--cc-swarm/);
  } finally {
    await vite.close();
  }
});

test("workers command-center: neon rows, mini-bar load, assignment swarm strip", async () => {
  const vite = await createServer({ server: { middlewareMode: true }, appType: "custom" });
  try {
    const { WorkersPanel, workerRowNeon, workerLoadTone } = await vite.ssrLoadModule(
      "/src/components/panels/WorkersPanel.tsx",
    );
    const { TerminalDashboard } = await vite.ssrLoadModule("/src/components/terminal/TerminalDashboard.tsx");

    assert.equal(workerRowNeon({ is_available: false, load_percent: 0, heartbeat_stale: false }), "offline");
    assert.equal(workerRowNeon({ is_available: true, load_percent: 0, heartbeat_stale: true }), "stale");
    assert.equal(workerRowNeon({ is_available: true, load_percent: 55, heartbeat_stale: false }), "loaded");
    assert.equal(workerRowNeon({ is_available: true, load_percent: 0, heartbeat_stale: false }), "online");
    assert.equal(workerLoadTone(100), "red");
    assert.equal(workerLoadTone(80), "orange");
    assert.equal(workerLoadTone(55), "blue");
    assert.equal(workerLoadTone(10), "green");

    const workers = {
      available: true,
      available_count: 2,
      total: 3,
      workers: [
        {
          id: "worker-alpha",
          capabilities: ["code", "test"],
          current_load: 1,
          max_capacity: 2,
          load_percent: 55,
          last_heartbeat: "2026-10-31T12:00:00Z",
          heartbeat_age_seconds: 3,
          heartbeat_stale: false,
          is_available: true,
          metadata: JSON.stringify({ hostname: "box-a" }),
        },
        {
          id: "worker-offline",
          capabilities: ["code"],
          current_load: 0,
          max_capacity: 2,
          load_percent: 0,
          last_heartbeat: "2026-10-31T11:00:00Z",
          heartbeat_age_seconds: 3600,
          heartbeat_stale: true,
          is_available: false,
        },
        {
          id: "worker-stale",
          capabilities: [],
          current_load: 0,
          max_capacity: 2,
          load_percent: 0,
          last_heartbeat: "2026-10-31T11:50:00Z",
          heartbeat_age_seconds: 120,
          heartbeat_stale: true,
          is_available: true,
        },
      ],
    };

    const tasks = {
      available: true,
      total: 1,
      status_counts: { in_progress: 1 },
      pending_task_count: 0,
      tasks: [{
        id: "parent-task",
        description: "Polish workers command center",
        status: "in_progress",
        project_id: "repo",
        subtask_count: 2,
        created_at: "2026-10-31T12:00:00Z",
        updated_at: "2026-10-31T12:01:00Z",
        subtasks: [
          {
            id: "assigned-run",
            description: "Wire worker neon",
            status: "in_progress",
            depends_on: [],
            assigned_worker: "worker-alpha",
            percent: 42,
            phase: "executing",
            step_agent: "codex",
            step_status: "started",
          },
          {
            id: "assigned-done",
            description: "Caps chips",
            status: "completed",
            depends_on: [],
            assigned_worker: "worker-alpha",
            percent: 100,
            step_status: "completed",
          },
        ],
      }],
    };

    const html = renderToStaticMarkup(React.createElement(WorkersPanel, {
      workers,
      tasks,
      variant: "terminal",
      onJumpTask: () => {},
    }));
    assert.match(html, /data-testid="terminal-worker-row"/);
    assert.match(html, /data-neon="loaded"/);
    assert.match(html, /data-neon="offline"/);
    assert.match(html, /data-neon="stale"/);
    assert.match(html, /terminal-worker-row[^"]*\bis-loaded\b|\bis-loaded\b[^"]*terminal-worker-row/);
    assert.match(html, /terminal-mini-bar blue/);
    assert.match(html, /data-testid="terminal-worker-load"/);
    assert.match(html, /1\/2/);
    assert.match(html, /55%/);
    assert.match(html, /data-testid="terminal-worker-assignments"/);
    assert.match(html, /data-testid="terminal-swarm-row"/);
    assert.match(html, /◐ running/);
    assert.match(html, /✓ done/);
    assert.match(html, /executing/);
    assert.match(html, /codex/);
    assert.match(html, /terminal-worker-cap/);
    assert.match(html, />code</);
    assert.match(html, />test</);
    assert.doesNotMatch(html, /border-l-green-500/);
    assert.doesNotMatch(html, /border-l-blue-500/);
    assert.doesNotMatch(html, /load-bar-fill|class="load-bar/);
    assert.doesNotMatch(html, /Opus|Sonnet|Haiku|JEV/);

    const legacyHtml = renderToStaticMarkup(React.createElement(WorkersPanel, {
      workers,
      tasks,
      variant: "default",
    }));
    assert.match(legacyHtml, /border-l-green-500|border-l-red-500/);
    assert.doesNotMatch(legacyHtml, /data-testid="terminal-worker-row"/);

    const dashHtml = renderToStaticMarkup(React.createElement(TerminalDashboard, {
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
      workers,
      tasks,
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
      initialView: "workers",
    }));
    assert.match(dashHtml, /terminal-workers-layout/);
    assert.match(dashHtml, /data-testid="terminal-worker-row"/);
    assert.match(dashHtml, /data-testid="terminal-swarm-row"/);
    assert.match(dashHtml, /terminal-mini-bar/);
    assert.doesNotMatch(dashHtml, /border-l-green-500/);
    assert.doesNotMatch(dashHtml, /Opus|Sonnet|Haiku|Claude Code|JEV/);

    const css = readFileSync(join(dashboardRoot, "src/index.css"), "utf8");
    assert.match(css, /\.terminal-workers-layout/);
    assert.match(css, /\.terminal-worker-row\.is-loaded[\s\S]*?--cc-swarm|\.terminal-worker-row\.is-online[\s\S]*?--cc-swarm/);
    assert.match(css, /\.terminal-worker-row\.is-offline[\s\S]*?--terminal-red/);
    assert.match(css, /\.terminal-worker-row\.is-stale[\s\S]*?--cc-lead/);
    assert.match(css, /\.terminal-worker-row\.is-expanded/);
    assert.match(css, /\.terminal-mini-bar\.red i[\s\S]*?--terminal-red/);
    assert.match(css, /@media \(prefers-reduced-motion:\s*reduce\)[\s\S]*?\.terminal-worker-row[\s\S]*?transition:\s*none/);
  } finally {
    await vite.close();
  }
});

test("logs command-center: session rows, terminal chips, shell layout", async () => {
  const vite = await createServer({ server: { middlewareMode: true }, appType: "custom" });
  try {
    const { EventLogPanel } = await vite.ssrLoadModule("/src/components/panels/EventLogPanel.tsx");
    const { TerminalDashboard } = await vite.ssrLoadModule("/src/components/terminal/TerminalDashboard.tsx");
    const { sessionRoleForEventType } = await vite.ssrLoadModule("/src/lib/taskProgress.ts");

    assert.equal(sessionRoleForEventType("task_submitted"), "lead");
    assert.equal(sessionRoleForEventType("subtask_progress"), "swarm");
    assert.equal(sessionRoleForEventType("tool_call"), "dispatch");
    assert.equal(sessionRoleForEventType("llm_request"), "advisor");

    const events = [
      {
        timestamp: "2026-10-31T12:04:18Z",
        type: "task_submitted",
        details: { task_id: "task-alpha", description: "Ship logs CC polish" },
      },
      {
        timestamp: "2026-10-31T12:04:20Z",
        type: "subtask_progress",
        details: { task_id: "task-alpha", subtask_id: "sub-1", percent: 42, phase: "executing", step_agent: "codex" },
      },
      {
        timestamp: "2026-10-31T12:04:22Z",
        type: "tool_call",
        details: { task_id: "task-alpha", tool: "read_file" },
      },
      {
        timestamp: "2026-10-31T12:04:24Z",
        type: "llm_request",
        details: { task_id: "task-alpha", model: "local" },
      },
      {
        timestamp: "2026-10-31T12:04:26Z",
        type: "subtask_failed",
        details: { task_id: "task-alpha", error: "boom" },
      },
    ];

    const html = renderToStaticMarkup(React.createElement(EventLogPanel, {
      events,
      variant: "terminal",
      onSelectTask: () => {},
    }));
    assert.match(html, /data-testid="terminal-logs-panel"/);
    assert.match(html, /cc-session-log-head|terminal-logs-head/);
    assert.match(html, /cc-session-row cc-session-role-lead|cc-session-role-lead/);
    assert.match(html, /cc-session-role-swarm/);
    assert.match(html, /cc-session-role-dispatch/);
    assert.match(html, /cc-session-role-advisor/);
    assert.match(html, /data-session-role="lead"/);
    assert.match(html, /data-session-role="swarm"/);
    assert.match(html, /data-testid="terminal-log-row"/);
    assert.match(html, /terminal-log-chip/);
    assert.match(html, /data-testid="terminal-log-chip-errors"/);
    assert.match(html, /data-testid="terminal-log-chip-type"/);
    assert.match(html, /data-testid="terminal-log-chip-range"/);
    assert.match(html, /data-testid="terminal-log-chip-export"/);
    assert.match(html, /data-testid="terminal-logs-search"/);
    assert.match(html, /data-testid="terminal-logs-stream"/);
    assert.match(html, /terminal-logs-task-jump/);
    assert.doesNotMatch(html, /border-l-green-500/);
    assert.doesNotMatch(html, /border-l-blue-500/);
    assert.doesNotMatch(html, /bg-blue-500|bg-blue-600/);
    assert.doesNotMatch(html, /evt-submitted|evt-completed|evt-failed/);
    assert.doesNotMatch(html, /Opus|Sonnet|Haiku|JEV/);

    const emptyHtml = renderToStaticMarkup(React.createElement(EventLogPanel, {
      events: [],
      variant: "terminal",
      stale: true,
    }));
    assert.match(emptyHtml, /data-testid="terminal-logs-empty"/);
    assert.match(emptyHtml, /waiting for live events/);
    assert.match(emptyHtml, /data-testid="terminal-logs-stale"/);
    assert.match(emptyHtml, /is-stale|data-stale="true"/);

    const legacyHtml = renderToStaticMarkup(React.createElement(EventLogPanel, {
      events,
      variant: "default",
    }));
    assert.match(legacyHtml, /Event Log/);
    assert.doesNotMatch(legacyHtml, /data-testid="terminal-logs-panel"/);
    assert.match(legacyHtml, /evt-submitted|text-blue-500/);

    const workers = { available: true, available_count: 0, total: 0, workers: [] };
    const tasks = {
      available: true,
      total: 0,
      status_counts: {},
      pending_task_count: 0,
      tasks: [],
    };
    const dashHtml = renderToStaticMarkup(React.createElement(TerminalDashboard, {
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
      workers,
      tasks,
      scheduler: { available: false, is_running: false, night_window: null, jobs: [], execution_history: [] },
      eventLog: events,
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
      initialView: "logs",
    }));
    assert.match(dashHtml, /terminal-logs-layout/);
    assert.match(dashHtml, /data-testid="terminal-logs-panel"/);
    assert.match(dashHtml, /cc-session-role-lead|data-session-role="lead"/);
    assert.match(dashHtml, /terminal-log-chip/);
    assert.doesNotMatch(dashHtml, /border-l-green-500/);
    assert.doesNotMatch(dashHtml, /bg-blue-500|bg-blue-600/);
    assert.doesNotMatch(dashHtml, /Opus|Sonnet|Haiku|Claude Code|JEV/);

    const css = readFileSync(join(dashboardRoot, "src/index.css"), "utf8");
    assert.match(css, /\.terminal-logs-layout/);
    assert.match(css, /\.terminal-log-chip/);
    assert.match(css, /\.terminal-log-chip\.is-active\.error[\s\S]*?--terminal-red/);
    assert.match(css, /\.terminal-logs-stream/);
    assert.match(css, /\.terminal-logs-latest/);
    assert.match(css, /\.terminal-logs-panel \.cc-session-role-lead[\s\S]*?--cc-lead/);
    assert.match(css, /\.terminal-logs-panel \.cc-session-role-swarm[\s\S]*?--cc-swarm/);
    assert.match(css, /@media \(prefers-reduced-motion:\s*reduce\)[\s\S]*?\.terminal-logs-latest[\s\S]*?transition:\s*none/);
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
    assert.match(html, /<button[^>]*data-testid="cc-stage-chip"/);
    // Lead present → elapsed renders HH:MM:SS (wall clock vs updated_at/created_at).
    assert.match(html, /data-testid="cc-lead-elapsed"[^>]*>elapsed \d{2,}:\d{2}:\d{2}</);
    assert.match(html, /elapsed \d+:\d{2}:\d{2}/);
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

test("command-center stage nav, elapsed helper, and live handoff", async () => {
  const vite = await createServer({ server: { middlewareMode: true }, appType: "custom" });
  try {
    const mod = await vite.ssrLoadModule("/src/components/terminal/TerminalDashboard.tsx");
    const {
      TerminalDashboard,
      formatElapsed,
      viewForCommandCenterStage,
      COMMAND_CENTER_LIVE_HANDOFF,
    } = mod;

    assert.equal(formatElapsed(null, Date.parse("2026-10-31T12:00:00Z")), "—");
    assert.equal(formatElapsed(undefined, Date.parse("2026-10-31T12:00:00Z")), "—");
    assert.equal(formatElapsed("not-a-date", Date.parse("2026-10-31T12:00:00Z")), "—");
    assert.equal(
      formatElapsed("2026-10-31T12:00:00Z", Date.parse("2026-10-31T12:00:00Z")),
      "00:00:00",
    );
    assert.equal(
      formatElapsed("2026-10-31T11:47:56Z", Date.parse("2026-10-31T12:00:00Z")),
      "00:12:04",
    );
    // Negative delta clamps to zero.
    assert.equal(
      formatElapsed("2026-10-31T13:00:00Z", Date.parse("2026-10-31T12:00:00Z")),
      "00:00:00",
    );

    assert.equal(viewForCommandCenterStage("lead"), "tasks");
    assert.equal(viewForCommandCenterStage("review"), "tasks");
    assert.equal(viewForCommandCenterStage("dispatch"), "workers");
    assert.equal(viewForCommandCenterStage("swarm", false), "workers");
    assert.equal(viewForCommandCenterStage("swarm", true), "tasks");
    assert.equal(viewForCommandCenterStage("idle"), null);

    assert.equal(COMMAND_CENTER_LIVE_HANDOFF, "live · gateway connected");

    const idleHtml = renderToStaticMarkup(React.createElement(TerminalDashboard, {
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
      tasks: { available: true, tasks: [], total: 0, status_counts: {}, pending_task_count: 0 },
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
    }));
    assert.match(idleHtml, /data-testid="cc-lead-elapsed"[^>]*>elapsed —</);
    assert.match(idleHtml, /data-stage="idle"/);
    assert.match(idleHtml, /<button[^>]*disabled[^>]*data-testid="cc-stage-chip"|<button[^>]*data-testid="cc-stage-chip"[^>]*disabled/);
    // SSR: live handoff only appears after preview→live effect; absent on first paint.
    assert.doesNotMatch(idleHtml, /data-testid="cc-live-banner"/);
    assert.doesNotMatch(idleHtml, /data-testid="cc-preview-banner"/);

    const source = readFileSync(join(dashboardRoot, "src/components/terminal/TerminalDashboard.tsx"), "utf8");
    assert.match(source, /viewForCommandCenterStage/);
    assert.match(source, /formatElapsed/);
    assert.match(source, /COMMAND_CENTER_LIVE_HANDOFF/);
    assert.match(source, /cc-live-banner/);
    assert.match(source, /showLiveHandoff/);
    // Handoff edge must survive Strict Mode cleanup (clear ref only after window).
    assert.match(source, /wasPreviewRef\.current = false/);

    const css = readFileSync(join(dashboardRoot, "src/index.css"), "utf8");
    assert.match(css, /\.cc-live-banner/);
    assert.match(css, /@keyframes cc-live-banner-enter/);
    assert.match(css, /@media \(prefers-reduced-motion:\s*reduce\)[\s\S]*?\.cc-live-banner[\s\S]*?animation:\s*none/);
  } finally {
    await vite.close();
  }
});
