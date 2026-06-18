/**
 * useTaskEvents hook - subscribes to WatchTask server-streaming RPC.
 *
 * Maintains React state for:
 * - Current task (from the most recent task_submitted event)
 * - Subtask list (updated from subtask_assigned/started/completed/failed events)
 * - Event log (all received TaskEvent messages)
 *
 * Re-subscribes automatically on reconnect.
 */

import {useState, useEffect, useCallback, useRef} from 'react';
import type {
  TaskEventProto,
  SubtaskProto,
  TaskProto,
  SubtaskStatusType,
} from '../grpc/types.js';
import {mapSubtaskStatus} from '../grpc/types.js';
import type {SubtaskItem} from '../components/SubtaskTree.js';
import type {TaskServiceClient} from '../grpc/client.js';

// ── Types ───────────────────────────────────────────────────

/** Maximum events to keep in memory. Matches reducer's MAX_MESSAGES. */
const MAX_EVENTS = 2000;

/** Maximum stream resubscribe retries before giving up. */
const MAX_STREAM_RETRIES = 3;

/** Delay before retrying stream subscription (ms). */
const STREAM_RETRY_DELAY = 5000;

export interface UseTaskEventsReturn {
  /** Current active task (null if no task submitted). */
  task: TaskProto | null;

  /** Subtask list derived from events, mapped to TUI SubtaskItem format. */
  subtasks: SubtaskItem[];

  /** All received events (for ChatLog display). */
  events: TaskEventProto[];

  /** Whether the event stream is active. */
  isStreaming: boolean;

  /** Update subtask state from a SubmitTaskResponse. */
  setSubtasksFromSubmit: (subtasks: SubtaskProto[], task?: TaskProto) => void;

  /** Update a single subtask's status. */
  updateSubtaskStatus: (subtaskId: string, status: SubtaskStatusType) => void;

  /** Clear all task state. */
  clearTask: () => void;
}

// ── Event Processing ────────────────────────────────────────

/** Process a TaskEvent and update subtask state accordingly. */
export function processEvent(
  event: TaskEventProto,
  currentSubtasks: Map<string, SubtaskItem>,
): Map<string, SubtaskItem> {
  const updated = new Map(currentSubtasks);

  switch (event.type) {
    case 'task_submitted': {
      // New task submitted - clear previous subtasks
      // Subtasks will arrive via subsequent events or from the submit response
      break;
    }
    case 'subtask_assigned': {
      if (event.subtaskId) {
        const existing = updated.get(event.subtaskId);
        if (existing) {
          updated.set(event.subtaskId, {
            ...existing,
            status: 'assigned',
            assignedWorker: event.data?.worker_id,
          });
        } else {
          updated.set(event.subtaskId, {
            id: event.subtaskId,
            index: updated.size,
            description: String(event.data?.description ?? ''),
            status: 'assigned',
            assignedWorker: event.data?.worker_id,
            dependsOn: parseDependsOn(event.data?.depends_on),
          });
        }
      }
      break;
    }
    case 'subtask_started': {
      if (event.subtaskId) {
        const existing = updated.get(event.subtaskId);
        if (existing) {
          updated.set(event.subtaskId, {
            ...existing,
            status: 'in_progress',
            assignedWorker: event.data?.worker_id ?? existing.assignedWorker,
          });
        } else {
          updated.set(event.subtaskId, {
            id: event.subtaskId,
            index: updated.size,
            description: String(event.data?.description ?? ''),
            status: 'in_progress',
            assignedWorker: event.data?.worker_id,
            dependsOn: parseDependsOn(event.data?.depends_on),
          });
        }
      }
      break;
    }
    case 'subtask_completed': {
      if (event.subtaskId) {
        const existing = updated.get(event.subtaskId);
        if (existing) {
          updated.set(event.subtaskId, {
            ...existing,
            status: 'completed',
          });
        } else {
          updated.set(event.subtaskId, {
            id: event.subtaskId,
            index: updated.size,
            description: String(event.data?.description ?? ''),
            status: 'completed',
            dependsOn: parseDependsOn(event.data?.depends_on),
          });
        }
      }
      break;
    }
    case 'subtask_failed': {
      if (event.subtaskId) {
        const existing = updated.get(event.subtaskId);
        if (existing) {
          updated.set(event.subtaskId, {
            ...existing,
            status: 'failed',
            errorSummary: event.data?.error_summary ?? event.data?.error ?? undefined,
          });
        } else {
          updated.set(event.subtaskId, {
            id: event.subtaskId,
            index: updated.size,
            description: String(event.data?.description ?? ''),
            status: 'failed',
            errorSummary: event.data?.error_summary ?? event.data?.error ?? undefined,
            dependsOn: parseDependsOn(event.data?.depends_on),
          });
        }
      }
      break;
    }
    default:
      // Other event types (tool_call, tool_result, file_modified, etc.)
      // don't affect subtask status but are stored in events for ChatLog
      break;
  }

  return updated;
}

/** Convert proto SubtaskProto[] to SubtaskItem[] for TUI display. */
export function protoSubtasksToItems(subtasks: SubtaskProto[]): SubtaskItem[] {
  return subtasks.map((st, idx) => ({
    id: st.id,
    index: idx + 1,
    description: st.description,
    status: mapSubtaskStatus(st.status),
    assignedWorker: st.assignedWorker,
    dependsOn: st.dependsOn,
  }));
}

// ── Hook Implementation ─────────────────────────────────────

export function useTaskEvents(
  client: TaskServiceClient | null,
  connectionState: string,
  activeTaskId?: string | null,
): UseTaskEventsReturn {
  const [task, setTask] = useState<TaskProto | null>(null);
  const [subtaskMap, setSubtaskMap] = useState<Map<string, SubtaskItem>>(new Map());
  const [events, setEvents] = useState<TaskEventProto[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const streamRef = useRef<any>(null);
  const streamRetryCount = useRef(0);
  const streamRetryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Convert the subtask map to an array for display
  const subtasks = Array.from(subtaskMap.values());

  // Cleanup retry timer on unmount
  useEffect(() => {
    return () => {
      if (streamRetryTimerRef.current) {
        clearTimeout(streamRetryTimerRef.current);
      }
    };
  }, []);

  // Subscribe to WatchTask stream when client becomes available
  // ponytail: always watch ALL tasks (taskId='') to avoid stream rebuilds
  // when activeTaskId changes. Individual event processing filters by
  // activeTaskId where needed. Stream rebuilds only on connect/disconnect.
  useEffect(() => {
    if (!client || connectionState !== 'connected') {
      setIsStreaming(false);
      if (streamRef.current) {
        streamRef.current.cancel();
        streamRef.current = null;
      }
      streamRetryCount.current = 0;
      if (streamRetryTimerRef.current) {
        clearTimeout(streamRetryTimerRef.current);
        streamRetryTimerRef.current = null;
      }
      return;
    }

    let cancelled = false;

    const subscribe = () => {
      if (cancelled) return;
      try {
        const stream = client.watchTask({taskId: ''}); // watch all tasks
        streamRef.current = stream;

        stream.on('data', (event: any) => {
          if (cancelled) return;
          const taskEvent: TaskEventProto = {
            timestamp: event.timestamp ?? new Date().toISOString(),
            type: event.type ?? 'unknown',
            taskId: event.taskId ?? '',
            subtaskId: event.subtaskId ?? undefined,
            data: event.data ?? {},
          };
          setEvents((prev) => {
            const next = [...prev, taskEvent];
            return next.length > MAX_EVENTS ? next.slice(next.length - MAX_EVENTS) : next;
          });
          setSubtaskMap((prev) => processEvent(taskEvent, prev));
        });

        stream.on('end', () => {
          if (!cancelled) {
            setIsStreaming(false);
            // ponytail: retry with backoff instead of giving up
            if (streamRetryCount.current < MAX_STREAM_RETRIES) {
              streamRetryCount.current++;
              streamRetryTimerRef.current = setTimeout(() => {
                streamRetryTimerRef.current = null;
                subscribe();
              }, STREAM_RETRY_DELAY);
            } else {
              console.warn('[useTaskEvents] Stream retry exhausted — manual reconnect needed (Ctrl+R)');
            }
          }
        });

        stream.on('error', (_err: any) => {
          if (!cancelled) {
            setIsStreaming(false);
            if (streamRetryCount.current < MAX_STREAM_RETRIES) {
              streamRetryCount.current++;
              streamRetryTimerRef.current = setTimeout(() => {
                streamRetryTimerRef.current = null;
                subscribe();
              }, STREAM_RETRY_DELAY);
            } else {
              console.warn('[useTaskEvents] Stream retry exhausted after error — manual reconnect needed (Ctrl+R)');
            }
          }
        });

        setIsStreaming(true);
        streamRetryCount.current = 0;
      } catch (_err) {
        if (!cancelled) setIsStreaming(false);
      }
    };

    subscribe();

    return () => {
      cancelled = true;
      if (streamRef.current) {
        streamRef.current.cancel();
        streamRef.current = null;
      }
      if (streamRetryTimerRef.current) {
        clearTimeout(streamRetryTimerRef.current);
        streamRetryTimerRef.current = null;
      }
    };
  }, [client, connectionState]); // ponytail: removed activeTaskId — always watch all tasks

  /** Update subtask state from a SubmitTaskResponse. */
  const setSubtasksFromSubmit = useCallback(
    (protoSubtasks: SubtaskProto[], taskProto?: TaskProto) => {
      const items = protoSubtasksToItems(protoSubtasks);
      const newMap = new Map<string, SubtaskItem>();
      for (const item of items) {
        newMap.set(item.id, item);
      }
      setSubtaskMap(newMap);

      if (taskProto) {
        setTask(taskProto);
      }
    },
    [],
  );

  /** Clear all task state. */
  const clearTask = useCallback(() => {
    setTask(null);
    setSubtaskMap(new Map());
    setEvents([]);
  }, []);

  /** Update a single subtask's status by ID. */
  const updateSubtaskStatus = useCallback(
    (subtaskId: string, status: SubtaskStatusType) => {
      setSubtaskMap((prev) => {
        const updated = new Map(prev);
        const existing = updated.get(subtaskId);
        if (existing) {
          updated.set(subtaskId, {...existing, status});
        }
        return updated;
      });
    },
    [],
  );

  return {
    task,
    subtasks,
    events,
    isStreaming,
    setSubtasksFromSubmit,
    updateSubtaskStatus,
    clearTask,
  };
}

export default useTaskEvents;
