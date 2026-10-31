import assert from "node:assert/strict";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

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
    assert.match(html, /◇ engine/);
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
    // Completed subtasks must not steal the swarm row when nothing else is running.
    assert.doesNotMatch(html, /swarm-b/);
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
  } finally {
    await vite.close();
  }
});
