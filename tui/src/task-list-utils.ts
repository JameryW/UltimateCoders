/**
 * Task list formatting — convert TaskProto[] to display-friendly strings.
 *
 * Used by /tasks command to render a task list in ChatLog.
 * Pure functions, no React dependency.
 */
import type {TaskProto} from './grpc/types.js';
import {mapTaskStatus} from './grpc/types.js';

// ── Status Icons ──────────────────────────────────────────────

const STATUS_ICONS: Record<string, string> = {
  Created: '○',
  Planning: '◌',
  Pending: '○',
  Queued: '◌',
  InProgress: '◉',
  Running: '◉',
  Completed: '●',
  Failed: '✗',
  Paused: '‖',
  Cancelled: '✗',
};

function getStatusIcon(status: string): string {
  return STATUS_ICONS[status] ?? '○';
}

function getStatusColor(status: string): string | undefined {
  switch (status) {
    case 'Completed': return 'green';
    case 'InProgress': case 'Running': return 'cyan';
    case 'Failed': case 'Cancelled': return 'red';
    case 'Paused': return 'yellow';
    default: return undefined;
  }
}

// ── Time Formatting ───────────────────────────────────────────

/** Format a timestamp (ms since epoch) as a relative time string. */
export function formatRelativeTime(timestampMs: number): string {
  const now = Date.now();
  const diff = now - timestampMs;
  if (diff < 0) return 'now';
  if (diff < 60_000) return 'just now';
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
  return `${Math.floor(diff / 86_400_000)}d ago`;
}

// ── Task List Formatting ──────────────────────────────────────

export interface TaskListRow {
  id: string;
  shortId: string;
  status: string;
  statusIcon: string;
  statusColor: string | undefined;
  description: string;
  subtaskCount: number;
  relativeTime: string;
}

/**
 * Convert TaskProto[] to display rows.
 * Pure function for easy testing.
 */
export function formatTaskList(tasks: TaskProto[]): TaskListRow[] {
  return tasks.map((t) => ({
    id: t.id,
    shortId: t.id.slice(0, 8),
    status: mapTaskStatus(t.status),
    statusIcon: getStatusIcon(t.status),
    statusColor: getStatusColor(t.status),
    description: t.description.length > 40 ? t.description.slice(0, 37) + '...' : t.description,
    subtaskCount: t.subtaskCount,
    relativeTime: formatRelativeTime(t.updatedAt || t.createdAt),
  }));
}

/**
 * Build a text summary of the task list for ChatLog display.
 * Returns an array of lines.
 */
export function buildTaskListText(tasks: TaskProto[]): string[] {
  if (tasks.length === 0) return ['No tasks found.'];

  const header = `📋 Tasks (${tasks.length} total):`;
  const rows = formatTaskList(tasks).map((r) =>
    `  ${r.statusIcon} ${r.shortId}  ${r.status.padEnd(12)}${r.description.padEnd(42)} ${r.relativeTime}`
  );
  return [header, ...rows];
}
