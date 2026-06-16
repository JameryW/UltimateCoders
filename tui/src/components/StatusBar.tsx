/**
 * StatusBar component - single-line status display at the bottom.
 *
 * Segment-based layout with width budget:
 *   Priority order: connection > worker > backend > progress > focus > view > retry > help
 *   Each segment has a display width. Segments are appended in priority order
 *   until the terminal width budget is exhausted.
 *
 * Connection states:
 *   ● grpc     — connected (green when streaming, yellow idle)
 *   ○ offline  — disconnected (yellow)
 *   ◌ offline  — connecting (yellow)
 *   ✗ offline  — error / unavailable (yellow, not red — offline is expected)
 *   ✗ offline | retry 3/5 — retrying after error
 *
 * Removed from status bar (moved to ? help / diagnostics):
 *   - mode, Task ID, serverAddr, lastError long text
 *   - Full error messages (only short codes like "retry N/5")
 */
import React from 'react';
import {Box, Text} from 'ink';
import type {ConnectionState} from '../grpc/types.js';
import type {FocusedArea, ActiveMainPane, EventFilter} from '../reducer.js';
import {getStatusBarHelp} from '../keymap.js';

export interface StatusBarProps {
  workerId?: string;
  backend?: string;
  progress?: {completed: number; total: number};
  serverAddr?: string;
  connectionState?: ConnectionState;
  isStreaming?: boolean;
  activeTaskId?: string | null;
  lastError?: string | null;
  mode?: string;
  focusedArea?: FocusedArea;
  activeMainPane?: ActiveMainPane;
  eventFilter?: EventFilter;
  /** Terminal width for responsive layout. */
  terminalWidth?: number;
  /** Current retry attempt count. */
  retryCount?: number;
  /** Timestamp of next scheduled retry. */
  nextRetryAt?: number | null;
}

const FOCUS_LABELS: Record<FocusedArea, string> = {
  input: 'Input',
  chat: 'Chat',
  subtask: 'Subtask',
};

const VIEW_LABELS: Record<ActiveMainPane, string> = {
  chat: 'Chat',
  subtask: 'Subtask',
};

const MAX_RETRY_DISPLAY = 5;

// ── Segment-based width budget ────────────────────────────────

interface Segment {
  /** Unique id for testing/debugging. */
  id: string;
  /** Display width in terminal columns (approximate). */
  width: number;
  /** Render this segment. */
  render: () => React.ReactNode;
}

/**
 * Build ordered segments for the status bar.
 * Returns segments in priority order; caller trims to fit terminalWidth.
 */
export function buildSegments(props: {
  connectionState: ConnectionState;
  isStreaming: boolean;
  workerId: string;
  backend: string;
  progress: {completed: number; total: number};
  focusedArea: FocusedArea;
  activeMainPane: ActiveMainPane;
  retryCount: number;
  focusedAreaHelp: string;
}): Segment[] {
  const {
    connectionState,
    isStreaming,
    workerId,
    backend,
    progress,
    focusedArea,
    activeMainPane,
    retryCount,
    focusedAreaHelp,
  } = props;

  const segments: Segment[] = [];

  // ── 0. Brand logo ───────────────────────────────────────
  // Inspired by Claude Code's ▲ prefix — short, distinctive, always visible
  segments.push({
    id: 'brand',
    width: 6, // "◆ UC " = 1 + 1 + 2 + 1 = 5 + 1 padding
    render: () => (
      <>
        <Text color="magenta">{'◆'}</Text>
        <Text bold color="magenta">{' UC'}</Text>
      </>
    ),
  });

  // ── 1. Connection ───────────────────────────────────────
  const connDot = connectionState === 'connected'
    ? '●'
    : connectionState === 'connecting'
      ? '◌'
      : connectionState === 'error'
        ? '✗'
        : '○';
  // Connected=green when streaming, yellow when idle; all other states=yellow (offline is expected)
  const connColor = connectionState === 'connected'
    ? (isStreaming ? 'green' : 'yellow')
    : 'yellow';

  const connLabel = connectionState === 'connected' ? 'grpc' : 'offline';
  // "● grpc" = 1 + 1 + 4 = 6
  segments.push({
    id: 'connection',
    width: connLabel.length + 2, // dot + space + label
    render: () => (
      <>
        <Text color={connColor}>{connDot}</Text>
        <Text> {connLabel}</Text>
      </>
    ),
  });

  // ── 2. Worker ───────────────────────────────────────────
  const workerText = workerId || 'offline';
  // " | worker" = 3 + workerText.length
  segments.push({
    id: 'worker',
    width: 3 + workerText.length,
    render: () => (
      <>
        <Text dimColor>{' │ '}</Text>
        <Text>{workerText}</Text>
      </>
    ),
  });

  // ── 3. Backend ──────────────────────────────────────────
  const backendColor = backend === 'grpc' ? 'green' : 'yellow';
  // " │ backend" = 3 + backend.length
  segments.push({
    id: 'backend',
    width: 3 + backend.length,
    render: () => (
      <>
        <Text dimColor>{' │ '}</Text>
        <Text color={backendColor}>{backend}</Text>
      </>
    ),
  });

  // ── 4. Progress ─────────────────────────────────────────
  const progressText = `P ${progress.completed}/${progress.total}`;
  // " │ P 2/5" = 3 + progressText.length
  segments.push({
    id: 'progress',
    width: 3 + progressText.length,
    render: () => (
      <>
        <Text dimColor>{' │ '}</Text>
        <Text bold>{progressText}</Text>
      </>
    ),
  });

  // ── 5. Focus ────────────────────────────────────────────
  const focusText = FOCUS_LABELS[focusedArea];
  // " │ F Input" = 3 (separator) + 2 ("F ") + focusText.length
  segments.push({
    id: 'focus',
    width: 3 + 2 + focusText.length,
    render: () => (
      <>
        <Text dimColor>{' │ '}</Text>
        <Text bold color="cyan">{'F '}</Text>
        <Text bold color="cyan">{focusText}</Text>
      </>
    ),
  });

  // ── 6. View ─────────────────────────────────────────────
  const viewText = VIEW_LABELS[activeMainPane];
  // " │ V Chat" = 3 (separator) + 2 ("V ") + viewText.length
  segments.push({
    id: 'view',
    width: 3 + 2 + viewText.length,
    render: () => (
      <>
        <Text dimColor>{' │ '}</Text>
        <Text color="yellow">{'V '}</Text>
        <Text color="yellow">{viewText}</Text>
      </>
    ),
  });

  // ── 7. Retry (only when error + retrying) ────────────────
  if (connectionState === 'error' && retryCount > 0) {
    const retryText = `retry ${retryCount}/${MAX_RETRY_DISPLAY}`;
    segments.push({
      id: 'retry',
      width: 3 + retryText.length,
      render: () => (
        <>
          <Text dimColor>{' │ '}</Text>
          <Text color="yellow">{retryText}</Text>
        </>
      ),
    });
  } else if (connectionState !== 'connected' && connectionState !== 'connecting') {
    // Offline but not retrying: show reconnect hint
    const reconnectText = 'C-R reconnect';
    segments.push({
      id: 'retry',
      width: 3 + reconnectText.length, // " │ C-R reconnect"
      render: () => (
        <>
          <Text dimColor>{' │ '}</Text>
          <Text color="yellow">{'C-R reconnect'}</Text>
        </>
      ),
    });
  }

  // ── 8. Help ─────────────────────────────────────────────
  if (focusedAreaHelp) {
    // "  ? help" or "  S-Tab focus  ? help"
    segments.push({
      id: 'help',
      width: 2 + focusedAreaHelp.length, // 2 spaces + help text
      render: () => (
        <>
          <Text dimColor>{'  '}</Text>
          <Text dimColor>{focusedAreaHelp}</Text>
        </>
      ),
    });
  }

  return segments;
}

/**
 * Select segments that fit within the terminal width budget.
 * Returns segments in display order, trimming from the end if needed.
 */
export function selectSegments(segments: Segment[], budget: number): Segment[] {
  // Reserve 2 cols for padding (paddingX=1 on each side)
  let remaining = budget - 2;
  const result: Segment[] = [];

  for (const seg of segments) {
    if (remaining >= seg.width) {
      result.push(seg);
      remaining -= seg.width;
    } else {
      // Budget exhausted — stop adding segments
      break;
    }
  }

  return result;
}

const StatusBar: React.FC<StatusBarProps> = ({
  workerId = '',
  backend = 'subprocess',
  progress = {completed: 0, total: 0},
  connectionState = 'disconnected',
  isStreaming = false,
  focusedArea = 'input',
  activeMainPane = 'chat',
  terminalWidth = 80,
  retryCount = 0,
  // Props below are accepted for interface compatibility but no longer displayed:
  // serverAddr, activeTaskId, lastError, mode, eventFilter, nextRetryAt
}) => {
  // Help text from keymap (budget-aware)
  const helpText = getStatusBarHelp(focusedArea, terminalWidth);

  // Build and select segments
  const allSegments = buildSegments({
    connectionState,
    isStreaming,
    workerId,
    backend,
    progress,
    focusedArea,
    activeMainPane,
    retryCount,
    focusedAreaHelp: helpText,
  });

  const visibleSegments = selectSegments(allSegments, terminalWidth);

  return (
    <Box paddingX={1}>
      {visibleSegments.map((seg) => (
        <React.Fragment key={seg.id}>{seg.render()}</React.Fragment>
      ))}
    </Box>
  );
};

export default StatusBar;
