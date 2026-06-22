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

import React, {useState, useEffect, useCallback, useRef, useMemo} from 'react';
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

/** Batch window for incoming stream events (ms).
 *  Events arriving within this window are merged into a single state update
 *  to prevent excessive React re-renders. */
const EVENT_BATCH_MS = 50;

/** Maximum number of dedup keys to retain in seenEvents. */
const MAX_SEEN_EVENTS = 500;

/** Event types that represent subtask status transitions and should be deduped. */
const DEDUP_STATUS_EVENTS = new Set([
  'subtask_assigned',
  'subtask_started',
  'subtask_completed',
  'subtask_failed',
]);

/** Callback type for when a sync_required event is received from the server. */
export type SyncRequiredCallback = (reason: string, skipped: number) => void;

export interface UseTaskEventsReturn {
  /** Current active task (null if no task submitted). */
  task: TaskProto | null;

  /** Subtask list derived from events, mapped to TUI SubtaskItem format. */
  subtasks: SubtaskItem[];

  /** All received events (for ChatLog display). */
  events: TaskEventProto[];

  /** Whether the event stream is active AND there are active (non-terminal) subtasks. */
  isStreaming: boolean;

  /** Update subtask state from a SubmitTaskResponse. */
  setSubtasksFromSubmit: (subtasks: SubtaskProto[], task?: TaskProto) => void;

  /** Update a single subtask's status. */
  updateSubtaskStatus: (subtaskId: string, status: SubtaskStatusType) => void;

  /** Clear all task state. */
  clearTask: () => void;

  /** Explicitly mark streaming as finished (all subtasks reached terminal state). */
  markStreamingFinished: () => void;
}

// ── Event Processing ────────────────────────────────────────

/** Parse depends_on from event data (may be string or string[]). */
function parseDependsOn(val: string | string[] | undefined): string[] {
  if (!val) return [];
  if (Array.isArray(val)) return val;
  return val.split(',').map((s) => s.trim()).filter(Boolean);
}

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
          // Event arrived before submit response — create entry
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
        // Parse modified_files from JSON string if present
        const modifiedFiles = (() => {
          try {
            const raw = event.data?.modified_files;
            if (typeof raw === 'string' && raw.length > 0) return JSON.parse(raw);
          } catch { /* ignore parse errors */ }
          return undefined;
        })();
        const existing = updated.get(event.subtaskId);
        if (existing) {
          updated.set(event.subtaskId, {
            ...existing,
            status: 'completed',
            output: event.data?.output ?? existing.output,
            modifiedFiles: modifiedFiles ?? existing.modifiedFiles,
          });
        } else {
          updated.set(event.subtaskId, {
            id: event.subtaskId,
            index: updated.size,
            description: String(event.data?.description ?? ''),
            status: 'completed',
            dependsOn: parseDependsOn(event.data?.depends_on),
            output: event.data?.output,
            modifiedFiles,
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

const TERMINAL_SUBTASK_STATUSES = new Set<SubtaskStatusType>(['completed', 'failed', 'conflicted']);

function allSubtasksTerminal(map: Map<string, SubtaskItem>): boolean {
  if (map.size === 0) return false;
  return Array.from(map.values()).every((st) => TERMINAL_SUBTASK_STATUSES.has(st.status));
}

export function useTaskEvents(
  client: TaskServiceClient | null,
  connectionState: string,
  activeTaskId?: string | null,
  onSyncRequired?: SyncRequiredCallback,
): UseTaskEventsReturn {
  const [task, setTask] = useState<TaskProto | null>(null);
  const [subtaskMap, setSubtaskMap] = useState<Map<string, SubtaskItem>>(new Map());
  const [events, setEvents] = useState<TaskEventProto[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamingFinished, setStreamingFinished] = useState(false);
  const streamRef = useRef<any>(null);
  const streamRetryCount = useRef(0);
  const streamRetryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Dedup: track seen event keys to skip duplicate status-transition events
  const seenEventsRef = useRef<Set<string>>(new Set());
  const seenEventsOrderRef = useRef<string[]>([]);

  // ponytail: memoize subtasks array to prevent App re-render loop.
  // Array.from(subtaskMap.values()) creates a new array every render,
  // which triggers the streamSubtasks diff in App → SET_SUBTASKS → re-render → flicker.
  const subtasks = useMemo(() => Array.from(subtaskMap.values()), [subtaskMap]);

  // ponytail: auto-detect when all subtasks have reached terminal state.
  // Once detected, set isStreaming=false so the spinner stops.
  // streamingFinished is a one-way latch — once true, it stays true until clearTask().
  const prevAllTerminalRef = useRef(false);
  useEffect(() => {
    if (streamingFinished) return;
    const allTerminal = allSubtasksTerminal(subtaskMap);
    if (allTerminal && !prevAllTerminalRef.current && subtaskMap.size > 0) {
      setStreamingFinished(true);
      setIsStreaming(false);
    }
    prevAllTerminalRef.current = allTerminal;
  }, [subtaskMap, streamingFinished]);

  // ponytail: event batching — buffer incoming stream events and flush
  // at most once every EVENT_BATCH_MS to prevent excessive re-renders.
  const eventBufferRef = useRef<TaskEventProto[]>([]);
  const batchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const flushEventBuffer = useCallback(() => {
    if (batchTimerRef.current) {
      clearTimeout(batchTimerRef.current);
      batchTimerRef.current = null;
    }
    const batch = eventBufferRef.current;
    if (batch.length === 0) return;
    eventBufferRef.current = [];

    // Dedup: filter out duplicate status-transition events
    const seen = seenEventsRef.current;
    const order = seenEventsOrderRef.current;
    const filtered = batch.filter((event) => {
      if (!DEDUP_STATUS_EVENTS.has(event.type)) return true;
      const key = `${event.type}:${event.subtaskId ?? ''}:${event.taskId}`;
      if (seen.has(key)) return false; // duplicate
      seen.add(key);
      order.push(key);
      // Evict oldest entries if set exceeds MAX_SEEN_EVENTS
      if (order.length > MAX_SEEN_EVENTS) {
        const excess = order.length - MAX_SEEN_EVENTS;
        for (let i = 0; i < excess; i++) {
          seen.delete(order[i]);
        }
        order.splice(0, excess);
      }
      return true;
    });

    setEvents((prev) => {
      const next = [...prev, ...filtered];
      return next.length > MAX_EVENTS ? next.slice(next.length - MAX_EVENTS) : next;
    });
    setSubtaskMap((prev) => {
      let updated = prev;
      for (const event of filtered) {
        updated = processEvent(event, updated);
      }
      return updated;
    });
  }, []);

  // Cleanup batch timer on unmount
  useEffect(() => {
    return () => {
      if (batchTimerRef.current) {
        clearTimeout(batchTimerRef.current);
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
    // ponytail: discard stale events from before this stream session started.
    // Server replays historical task events on connect; TUI is stateless per launch.
    const sessionStartMs = Date.now();

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

          // Handle sync_required: server tells us we missed events
          if (taskEvent.type === 'sync_required') {
            const reason = String(taskEvent.data?.reason ?? 'unknown');
            const skipped = Number(taskEvent.data?.skipped ?? 0);
            onSyncRequired?.(reason, skipped);
            return;
          }

          // ponytail: discard stale events from previous sessions.
          // Compare event timestamp to stream connection time.
          const eventMs = new Date(taskEvent.timestamp).getTime();
          if (!isNaN(eventMs) && eventMs < sessionStartMs) return;

          // ponytail: on first real event from stream, set isStreaming=true
          // (but only if not already finished)
          setStreamingFinished((prev) => {
            if (!prev) setIsStreaming(true);
            return prev;
          });

          // ponytail: batch events instead of updating state on each one
          eventBufferRef.current.push(taskEvent);
          if (!batchTimerRef.current) {
            batchTimerRef.current = setTimeout(() => {
              flushEventBuffer();
            }, EVENT_BATCH_MS);
          }
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

        // ponytail: isStreaming is set on first 'data' event, not here
        // — avoids showing streaming before any real data arrives
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
      if (batchTimerRef.current) {
        clearTimeout(batchTimerRef.current);
        batchTimerRef.current = null;
      }
      // Flush any remaining buffered events
      flushEventBuffer();
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
    setStreamingFinished(false);
    prevAllTerminalRef.current = false;
    seenEventsRef.current.clear();
    seenEventsOrderRef.current.length = 0;
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

  /** Explicitly mark streaming as finished (e.g. on task_completed event). */
  const markStreamingFinished = useCallback(() => {
    setStreamingFinished(true);
    setIsStreaming(false);
  }, []);

  return {
    task,
    subtasks,
    events,
    isStreaming,
    setSubtasksFromSubmit,
    updateSubtaskStatus,
    clearTask,
    markStreamingFinished,
  };
}

export default useTaskEvents;
