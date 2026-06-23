/**
 * StatusBar component - single-line status display at the bottom.
 *
 * Segment-based layout with width budget:
 *   Priority order: brand > connection > worker > backend > progress > focus > retry > help
 *   Each segment has a display width. Segments are appended in priority order
 *   until the terminal width budget is exhausted.
 *
 * Worker segment now shows multi-worker summary derived from subtasks:
 *   Connected: "3/5 active" (N workers with active subtasks / M unique workers)
 *   Offline: "offline"
 *   Click/Enter on worker segment toggles expanded worker detail list.
 */
import React, {useState, useEffect} from 'react';
import {Box, Text} from 'ink';
import type {ConnectionState} from '../grpc/types.js';
import type {FocusedArea} from '../reducer.js';
import {getStatusBarHelp} from '../keymap.js';
import {getSymbols} from '../symbols.js';
import {MAX_RETRY_DISPLAY} from '../statusbar-utils.js';

/** Per-worker summary derived from subtasks. */
export interface WorkerSummaryEntry {
  workerId: string;
  activeSubtaskCount: number;
}

/** Summary of all workers, derived from SubtaskItem[].assignedWorker. */
export interface WorkerSummary {
  /** Workers with at least one in_progress/assigned subtask. */
  activeCount: number;
  /** Total unique workers seen across all subtasks. */
  totalCount: number;
  /** Per-worker detail for expanded view. */
  entries: WorkerSummaryEntry[];
}

export interface StatusBarProps {
  /** Legacy: single worker id. Ignored if workerSummary is provided. */
  workerId?: string;
  /** Multi-worker summary derived from subtasks. */
  workerSummary?: WorkerSummary;
  /** Whether worker detail is expanded (controlled by parent reducer). */
  workersExpanded?: boolean;
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
  /** Notification flash (auto-dismisses after 3s). */
  notification?: {text: string; color: string; timestamp: number} | null;
  /** Hint rotation index for cycling StatusBar shortcuts. */
  hintRotationIndex?: number;
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
  segments.push({
    id: 'worker',
    width: 3 + workerId.length,
    render: () => (
      <>
        <Text dimColor>{' │ '}</Text>
        <Text>{workerId}</Text>
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
  workerSummary,
  workersExpanded = false,
  backend = 'subprocess',
  progress = {completed: 0, total: 0},
  connectionState = 'disconnected',
  isStreaming = false,
  focusedArea = 'input',
  terminalWidth = 80,
  retryCount = 0,
  nextRetryAt = null,
  notification,
  hintRotationIndex = 0,
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

  const helpText = getStatusBarHelp(focusedArea, terminalWidth, hintRotationIndex);
  const S = getSymbols();

  // ── Derive worker display text ──────────────────────────
  // ponytail: if workerSummary provided, use multi-worker format; else fall back to legacy workerId
  let effectiveWorkerText: string;
  if (workerSummary && connectionState === 'connected') {
    const {activeCount, totalCount} = workerSummary;
    effectiveWorkerText = totalCount > 0
      ? `${activeCount}/${totalCount} active`
      : '0 workers';
  } else {
    effectiveWorkerText = workerId || 'offline';
  }

  const allSegments = buildSegments({
    connectionState,
    isStreaming,
    workerId: effectiveWorkerText,
    backend,
    progress,
    focusedArea,
    retryCount,
    nextRetryAt,
    focusedAreaHelp: helpText,
    brandChar: S.brand,
  });

  const visibleSegments = selectSegments(allSegments, terminalWidth);
  const workerSegVisible = visibleSegments.some(s => s.id === 'worker');

  return (
    <Box flexDirection="column">
      {notification && (
        <Box paddingX={1}>
          <Text color={notification.color} bold>{`⚠ ${notification.text}`}</Text>
        </Box>
      )}
      <Box paddingX={1}>
        {visibleSegments.map((seg) => (
          <React.Fragment key={seg.id}>{seg.render()}</React.Fragment>
        ))}
      </Box>
      {/* ponytail: expanded worker detail — bar chart showing load per worker */}
      {workersExpanded && workerSummary && workerSummary.entries.length > 0 && workerSegVisible && (
        <Box paddingX={1} marginTop={0}>
          <Text dimColor>{'  '}</Text>
          {workerSummary.entries.map((w, i) => {
            const maxCount = Math.max(...workerSummary!.entries.map(e => e.activeSubtaskCount), 1);
            const barWidth = 4;
            const filled = Math.round((w.activeSubtaskCount / maxCount) * barWidth);
            const empty = barWidth - filled;
            const bar = S.barFilled.repeat(filled) + S.barEmpty.repeat(empty);
            return (
              <React.Fragment key={w.workerId}>
                {i > 0 && <Text dimColor>{'  '}</Text>}
                <Text dimColor>{w.workerId.length > 5 ? w.workerId.slice(0, 5) : w.workerId}</Text>
                <Text color={w.activeSubtaskCount > 0 ? 'cyan' : 'gray'}>{` ${bar}`}</Text>
                <Text dimColor>{` ${w.activeSubtaskCount}`}</Text>
              </React.Fragment>
            );
          })}
        </Box>
      )}
    </Box>
  );
};

export default StatusBar;
