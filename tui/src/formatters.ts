/**
 * Event formatters — pure functions that convert TaskEventProto to ChatMessage.
 *
 * Tool events (tool_call, tool_result, file_modified) use markdown formatting
 * for better readability when rendered by ChatLog's markdown renderer.
 */
import type {TaskEventProto} from './grpc/types.js';
import type {ChatMessage} from './components/ChatLog.js';
import {createSystemMessage} from './components/ChatLog.js';

/**
 * Format tool event data as markdown for better terminal display.
 * Returns empty string for non-tool events.
 */
function formatToolData(eventType: string, data: Record<string, string>): string {
  switch (eventType) {
    case 'tool_call': {
      const toolName = data.tool_name ?? 'unknown';
      const args = data.args ?? data.query ?? '';
      if (!args) return `Tool call: **${toolName}**`;
      const shortArgs = args.length > 200 ? args.slice(0, 197) + '...' : args;
      return `Tool call: **${toolName}**\n\`\`\`\n${shortArgs}\n\`\`\``;
    }
    case 'tool_result': {
      const success = data.success === 'true';
      const result = data.result ?? data.output ?? '';
      const icon = success ? '✓' : '✗';
      if (!result) return `Tool result: ${icon}`;
      const shortResult = result.length > 300 ? result.slice(0, 297) + '...' : result;
      return `Tool result: ${icon}\n\`\`\`\n${shortResult}\n\`\`\``;
    }
    case 'file_modified': {
      const filePath = data.file_path ?? data.path ?? 'unknown';
      const diff = data.diff ?? '';
      if (!diff) return `File modified: \`${filePath}\``;
      const shortDiff = diff.length > 500 ? diff.slice(0, 497) + '...' : diff;
      return `File modified: \`${filePath}\`\n\`\`\`diff\n${shortDiff}\n\`\`\``;
    }
    default:
      return '';
  }
}

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

    case 'tool_call': {
      const md = formatToolData('tool_call', event.data ?? {});
      text = md || `Tool call: ${event.data?.tool_name ?? 'unknown'}`;
      dim = true;
      break;
    }

    case 'tool_result': {
      const md = formatToolData('tool_result', event.data ?? {});
      text = md || `Tool result: ${event.data?.success === 'true' ? '✓' : '✗'}`;
      dim = true;
      break;
    }

    case 'file_modified': {
      const md = formatToolData('file_modified', event.data ?? {});
      text = md || `File modified: ${event.data?.file_path ?? event.data?.path ?? 'unknown'}`;
      dim = true;
      break;
    }

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
