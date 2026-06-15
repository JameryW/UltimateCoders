/**
 * Event formatters — pure functions that convert TaskEventProto to ChatMessage.
 *
 * Moved out of App.tsx render path to eliminate render-side setState.
 * Called from useEffect when events array changes.
 */
import type {TaskEventProto} from './grpc/types.js';
import type {ChatMessage} from './components/ChatLog.js';
import {createSystemMessage} from './components/ChatLog.js';

/**
 * Convert a single TaskEventProto to a ChatMessage for the ChatLog.
 * Returns null for event types that should not appear in the log.
 */
export function formatTaskEvent(event: TaskEventProto): ChatMessage | null {
  let text = '';
  let color: string | undefined;
  let bold: boolean | undefined;
  let dim: boolean | undefined;

  switch (event.type) {
    case 'task_submitted':
      text = `Task submitted: ${event.taskId.slice(0, 8)}...`;
      color = 'cyan';
      bold = true;
      break;

    case 'subtask_assigned':
      text = `Subtask assigned: ${(event.subtaskId ?? '').slice(-6)} → ${event.data?.worker_id ?? 'unknown'}`;
      color = 'blue';
      break;

    case 'subtask_started':
      text = `Subtask started: ${(event.subtaskId ?? '').slice(-6)}`;
      color = 'cyan';
      break;

    case 'subtask_completed':
      text = `Subtask completed: ${(event.subtaskId ?? '').slice(-6)}`;
      color = 'green';
      break;

    case 'subtask_failed':
      text = `Subtask failed: ${(event.subtaskId ?? '').slice(-6)} — ${event.data?.error ?? 'unknown'}`;
      color = 'red';
      bold = true;
      break;

    case 'tool_call':
      text = `Tool call: ${event.data?.tool_name ?? 'unknown'}`;
      dim = true;
      break;

    case 'tool_result':
      text = `Tool result: ${event.data?.success === 'true' ? '✓' : '✗'}`;
      dim = true;
      break;

    case 'task_completed':
      text = `Task completed: ${event.taskId.slice(0, 8)}...`;
      color = 'green';
      bold = true;
      break;

    case 'task_failed':
      text = `Task failed: ${event.taskId.slice(0, 8)}... — ${event.data?.error ?? 'unknown'}`;
      color = 'red';
      bold = true;
      break;

    default:
      text = `${event.type}: ${event.taskId.slice(0, 8)}...`;
      dim = true;
      break;
  }

  if (!text) return null;

  return {...createSystemMessage(text, {color, bold, dim}), eventType: event.type};
}

/**
 * Convert an array of TaskEventProto to ChatMessages.
 * Filters out null results (events that shouldn't appear in log).
 */
export function formatTaskEvents(events: TaskEventProto[]): ChatMessage[] {
  return events.map(formatTaskEvent).filter((m): m is ChatMessage => m !== null);
}
