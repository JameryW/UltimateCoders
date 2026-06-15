/**
 * StatusBar component - single-line status display at the bottom.
 *
 * Shows: connection | Worker | Backend | Mode | Task | Progress | Focus | Help
 * Integrated into the unified border frame (no separate border).
 *
 * Focus model (v2):
 *   focusedArea: which area has keyboard focus (shown as "Focus:")
 *   activeMainPane: which pane is in the main area (shown as "View:")
 *   Help text comes from keymap.ts — always accurate.
 *
 * Responsive: omits less-critical fields on narrow terminals to avoid overflow.
 * At <100 cols, the help text and mode are hidden.
 * At <80 cols, only connection + worker + progress + focus are shown.
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

const StatusBar: React.FC<StatusBarProps> = ({
  workerId = '',
  backend = 'subprocess',
  progress = {completed: 0, total: 0},
  serverAddr = '',
  connectionState = 'disconnected',
  isStreaming = false,
  activeTaskId = null,
  lastError = null,
  mode = '',
  focusedArea = 'input',
  activeMainPane = 'chat',
  eventFilter = 'all',
  terminalWidth = 80,
  retryCount = 0,
  nextRetryAt = null,
}) => {
  const progressText = `${progress.completed}/${progress.total}`;
  const backendColor = backend === 'grpc' ? 'green' : backend === 'disconnected' ? 'red' : 'yellow';

  // Connection indicator
  const connDot = connectionState === 'connected'
    ? (isStreaming ? '●' : '○')
    : connectionState === 'connecting'
      ? '◌'
      : connectionState === 'error'
        ? '✗'
        : '○';
  const connColor = connectionState === 'connected'
    ? (isStreaming ? 'green' : 'yellow')
    : connectionState === 'error'
      ? 'red'
      : connectionState === 'connecting'
        ? 'yellow'
        : 'red';

  // Connection detail for error/offline
  const retrySecondsLeft = nextRetryAt ? Math.max(0, Math.ceil((nextRetryAt - Date.now()) / 1000)) : 0;
  const connDetail = connectionState === 'error'
    ? ` ${serverAddr}${retryCount > 0 ? ` retry ${retryCount}/${MAX_RETRY_DISPLAY}` : ''}${retrySecondsLeft > 0 ? ` in ${retrySecondsLeft}s` : ''}`
    : connectionState !== 'connected'
      ? ` ${serverAddr}`
      : '';

  // Help text from keymap
  const helpText = getStatusBarHelp(focusedArea, terminalWidth);

  // Responsive: show fewer fields on narrow terminals
  const isNarrow = terminalWidth < 80;
  const isMedium = terminalWidth >= 80 && terminalWidth < 100;

  return (
    <Box paddingX={1}>
      <Text color={connColor}>{connDot}</Text>
      <Text dimColor>{' '}</Text>
      <Text dimColor>{'Worker:'}</Text>
      <Text> {workerId || 'N/A'} </Text>
      {!isNarrow && (
        <>
          <Text dimColor>{'│'}</Text>
          <Text dimColor>{' Backend:'}</Text>
          <Text color={backendColor}> {backend} </Text>
        </>
      )}
      {!isNarrow && mode && (
        <>
          <Text dimColor>{'│'}</Text>
          <Text dimColor>{' '}</Text>
          <Text dimColor>{mode}</Text>
        </>
      )}
      {connDetail && !isNarrow && (
        <Text dimColor>{connDetail}</Text>
      )}
      {!isNarrow && activeTaskId && (
        <>
          <Text dimColor>{'│'}</Text>
          <Text dimColor>{' Task:'}</Text>
          <Text> {activeTaskId.slice(0, 8)}</Text>
        </>
      )}
      <Text dimColor>{'│'}</Text>
      <Text dimColor>{' Progress:'}</Text>
      <Text bold> {progressText}</Text>
      <Text dimColor>{'│'}</Text>
      <Text dimColor>{' Focus:'}</Text>
      <Text bold color="cyan"> {FOCUS_LABELS[focusedArea]}</Text>
      {!isNarrow && (
        <>
          <Text dimColor>{'│'}</Text>
          <Text dimColor>{' View:'}</Text>
          <Text color="yellow"> {VIEW_LABELS[activeMainPane]}</Text>
        </>
      )}
      {!isNarrow && lastError && (
        <>
          <Text dimColor>{'│'}</Text>
          <Text color="red"> {lastError.slice(0, 30)}</Text>
        </>
      )}
      {!isMedium && !isNarrow && (
        <>
          <Text dimColor>{'  '}</Text>
          <Text dimColor>({helpText})</Text>
        </>
      )}
    </Box>
  );
};

export default StatusBar;
