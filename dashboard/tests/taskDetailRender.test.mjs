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

test("the operations dashboard opens with visible task history and selected logs", async () => {
  const vite = await createServer({ server: { middlewareMode: true }, appType: "custom" });
  try {
    const { TerminalDashboard } = await vite.ssrLoadModule("/src/components/terminal/TerminalDashboard.tsx");
    const task = {
      id: "historical-task",
      description: "Read README heading",
      status: "completed",
      project_id: "repo",
      subtask_count: 1,
      created_at: "2026-09-24T01:00:00Z",
      updated_at: "2026-09-24T02:00:00Z",
      subtasks: [{ id: "historical-subtask", description: "Read the file", status: "completed", depends_on: [], result: "# UltimateCoders" }],
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
      health: { available: true, status: "healthy" },
      workers: { available: true, workers: [], total: 0 },
      tasks: { available: true, tasks: [task], total: 1, status_counts: { completed: 1 }, pending_task_count: 0 },
      scheduler: { available: false, jobs: [] },
      eventLog: [],
      metrics: null,
      interactionLog: { "historical-task": [{ timestamp: "2026-09-24T02:00:00Z", type: "subtask_completed", task_id: task.id, subtask_id: "historical-subtask", data: { summary: "# UltimateCoders" } }] },
      selectedTask: task,
      selectedTaskId: task.id,
      onSelectTask: () => {},
      onPauseTask: async () => ({ success: true }),
      onResumeTask: async () => ({ success: true }),
      onCancelTask: async () => ({ success: true }),
      onRefresh: async () => {},
      onFlush: () => {},
      onTaskCreated: () => {},
      onOptimisticAdd: () => {},
    }));
    assert.match(html, /Read README heading/);
    assert.match(html, /Task detail: Read README heading/);
    assert.match(html, /Interaction Log/);
  } finally {
    await vite.close();
  }
});
