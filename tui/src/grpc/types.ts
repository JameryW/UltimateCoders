/**
 * TypeScript interfaces matching the TaskService proto messages.
 *
 * These types correspond to the messages defined in engine.proto
 * and are used by the gRPC client hooks.
 */

// ── Task Service Messages ───────────────────────────────────

export interface SubmitTaskRequest {
  description: string;
  projectId: string;
}

export interface SubmitTaskResponse {
  success: boolean;
  taskId: string;
  status: string;
  subtaskCount: number;
  subtasks: SubtaskProto[];
  error?: string;
}

export interface GetTaskRequest {
  taskId: string;
}

export interface GetTaskResponse {
  available: boolean;
  task?: TaskProto;
}

export interface ListTasksRequest {}

export interface ListTasksResponse {
  available: boolean;
  tasks: TaskProto[];
  total: number;
  statusCounts: Record<string, number>;
}

export interface PauseTaskRequest {
  taskId: string;
}

export interface PauseTaskResponse {
  success: boolean;
  taskId: string;
  status: string;
  error?: string;
}

export interface ResumeTaskRequest {
  taskId: string;
}

export interface ResumeTaskResponse {
  success: boolean;
  taskId: string;
  status: string;
  error?: string;
}

export interface WatchTaskRequest {
  taskId: string; // empty = watch all tasks
}

export interface TaskEventProto {
  timestamp: string;
  type: string;
  taskId: string;
  subtaskId?: string;
  data: Record<string, string>;
}

// ── Proto Entity Types ──────────────────────────────────────

export interface TaskProto {
  id: string;
  description: string;
  status: string;
  projectId: string;
  subtaskCount: number;
  createdAt: number;
  updatedAt: number;
  subtasks: SubtaskProto[];
}

export interface SubtaskProto {
  id: string;
  description: string;
  status: string;
  dependsOn: string[];
  assignedWorker?: string;
}

// ── Connection State ────────────────────────────────────────

export type ConnectionState = 'disconnected' | 'connecting' | 'connected' | 'error';

// ── TUI-specific Types ──────────────────────────────────────

/** Subtask status as used in the TUI SubtaskTree component. */
export type SubtaskStatusType = 'pending' | 'assigned' | 'in_progress' | 'completed' | 'failed' | 'conflicted';

/** Map proto SubtaskStatus string to TUI SubtaskStatusType. */
export function mapSubtaskStatus(protoStatus: string): SubtaskStatusType {
  switch (protoStatus) {
    case 'Pending': return 'pending';
    case 'Assigned': return 'assigned';
    case 'InProgress': return 'in_progress';
    case 'Completed': return 'completed';
    case 'Failed': return 'failed';
    case 'Conflicted': return 'conflicted';
    default: return 'pending';
  }
}

/** Map proto TaskStatus string to a display-friendly form. */
export function mapTaskStatus(protoStatus: string): string {
  return protoStatus; // Proto strings are already display-friendly (Created, InProgress, etc.)
}
