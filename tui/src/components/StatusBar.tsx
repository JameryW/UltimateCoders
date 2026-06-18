/**
 * StatusBar component - single-line status display at the bottom.
 *
 * Segment-based layout with width budget:
 *   Priority order: brand > connection > worker > backend > progress > focus > retry > help
 *   Each segment has a display width. Segments are appended in priority order
 *   until the terminal width budget is exhausted.
 *
 * Connection states:
 *   ● grpc     — connected (green when streaming, yellow idle)
 *   ○ offline  — disconnected (yellow)
 *   ◌ offline  — connecting (yellow)
 *   ✗ offline  — error / unavailable (yellow, not red — offline is expected)
 *   ✗ offline | retry 3/5 — retrying after error
 */
import React, {useState, useEffect} from 'react';
import {Box, Text} from 'ink';
import type {ConnectionState} from '../grpc/types.js';
import type {FocusedArea} from '../reducer.js';
import {getStatusBarHelp} from '../keymap.js';
import {getSymbols} from '../symbols.js';
import {MAX_RETRY_DISPLAY} from '../statusbar-utils.js';

export interface StatusBarProps {
  workerId?: string;
  backend?: string;
  progress?: {completed: number; total: number};
  connectionState?: ConnectionState;
  isStreaming?: boolean;
  focusedArea?: FocusedArea;
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
};

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
  retryCount: number;
  nextRetryAt: number | null;
  focusedAreaHelp: string;
  /** Brand symbol from getSymbols().brand (◆ or *). */
  brandChar: string;
}): Segment[] {
  const {
    connectionState,
    isStreaming,
    workerId,
    backend,
    progress,
    focusedArea,
    retryCount,
    nextRetryAt,
    focusedAreaHelp,
    brandChar,
  } = props;

  const segments: Segment[] = [];

  // ── 0. Brand logo ───────────────────────────────────────
  const brandLabel = 'UC';
  segments.push({
    id: 'brand',
    width: brandChar.length + 1 + brandLabel.length,
    render: () => (
      <>
        <Text color="magenta">{brandChar}</Text>
        <Text bold color="magenta">{` ${brandLabel}`}</Text>
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
  const connColor = connectionState === 'connected'
    ? (isStreaming ? 'green' : 'yellow')
    : 'yellow';

  const connLabel = connectionState === 'connected' ? 'grpc' : 'offline';
  segments.push({
    id: 'connection',
    width: connLabel.length + 2,
    render: () => (
      <>
        <Text color={connColor}>{connDot}</Text>
        <Text> {connLabel}</Text>
      </>
    ),
  });

  // ── 2. Worker ───────────────────────────────────────────
  const workerText = workerId || 'offline';
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

  // ── 6. Retry (only when error + retrying) ────────────────
  if (connectionState === 'error' && retryCount > 0) {
    const retrySecondsLeft = nextRetryAt
      ? Math.max(0, Math.ceil((nextRetryAt - Date.now()) / 1000))
      : 0;
    const retryText = retrySecondsLeft > 0
      ? `retry ${retryCount}/${MAX_RETRY_DISPLAY} in ${retrySecondsLeft}s`
      : `retry ${retryCount}/${MAX_RETRY_DISPLAY}`;
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
    const reconnectText = 'C-R reconnect';
    segments.push({
      id: 'retry',
      width: 3 + reconnectText.length,
      render: () => (
        <>
          <Text dimColor>{' │ '}</Text>
          <Text color="yellow">{'C-R reconnect'}</Text>
        </>
      ),
    });
  }

  // ── 7. Help ─────────────────────────────────────────────
  if (focusedAreaHelp) {
    segments.push({
      id: 'help',
      width: 2 + focusedAreaHelp.length,
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
 * Progressive collapse tiers:
 *   >100 cols: full info
 *   80-100 cols: remove help
 *   60-80 cols: only brand + connection + progress + focus
 *   <60 cols: only brand + connection + progress
 */
export function selectSegments(segments: Segment[], budget: number): Segment[] {
  let remaining = budget - 2;
  const result: Segment[] = [];

  const skipIds = new Set<string>();
  if (budget < 60) {
    skipIds.add('worker');
    skipIds.add('backend');
    skipIds.add('focus');
    skipIds.add('retry');
    skipIds.add('help');
  } else if (budget < 80) {
    skipIds.add('worker');
    skipIds.add('backend');
    skipIds.add('help');
  } else if (budget < 100) {
    skipIds.add('help');
  }

  for (const seg of segments) {
    if (skipIds.has(seg.id)) continue;
    if (remaining >= seg.width) {
      result.push(seg);
      remaining -= seg.width;
    } else {
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
  terminalWidth = 80,
  retryCount = 0,
  nextRetryAt = null,
}) => {
  const isRetrying = connectionState === 'error' && retryCount > 0;
  // Drive 1-second re-renders when retrying, so countdown updates visually
  // ponytail: ink 5 lacks useAnimation; use setInterval + useState tick instead
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!isRetrying) return;
    const id = setInterval(() => setTick(t => t + 1), 1000);
    return () => clearInterval(id);
  }, [isRetrying]);

  const helpText = getStatusBarHelp(focusedArea, terminalWidth);
  const S = getSymbols();

  const allSegments = buildSegments({
    connectionState,
    isStreaming,
    workerId,
    backend,
    progress,
    focusedArea,
    retryCount,
    nextRetryAt,
    focusedAreaHelp: helpText,
    brandChar: S.brand,
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
