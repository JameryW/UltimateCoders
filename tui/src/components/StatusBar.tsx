/**
 * StatusBar component - single-line status display at the bottom.
 *
 * Shows: connection | Worker | Backend | Mode | Task | Progress | Pane | Help
 * Integrated into the unified border frame (no separate border).
 *
 * Responsive: omits less-critical fields on narrow terminals to avoid overflow.
 * At <100 cols, the help text and mode are hidden.
 * At <80 cols, only connection + worker + progress + pane are shown.
 */
import React from 'react';
import {Box, Text} from 'ink';
import type {ConnectionState} from '../grpc/types.js';
import type {SelectedPane, EventFilter} from '../reducer.js';

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
  selectedPane?: SelectedPane;
  eventFilter?: EventFilter;
  /** Terminal width for responsive layout. */
  terminalWidth?: number;
}

const PANE_LABELS: Record<SelectedPane, string> = {
  input: 'Input',
  chat: 'Chat',
  subtask: 'Subtask',
};

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
  selectedPane = 'input',
  eventFilter = 'all',
  terminalWidth = 80,
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
      <Text dimColor>{' Pane:'}</Text>
      <Text bold color="cyan"> {PANE_LABELS[selectedPane]}</Text>
      {!isNarrow && lastError && (
        <>
          <Text dimColor>{'│'}</Text>
          <Text color="red"> {lastError.slice(0, 30)}</Text>
        </>
      )}
      {!isMedium && !isNarrow && (
        <>
          <Text dimColor>{'  '}</Text>
          <Text dimColor>{'(Tab pane  Ctrl+P pause  Ctrl+F filter  Ctrl+R reconnect  Ctrl+Q quit)'}</Text>
        </>
      )}
    </Box>
  );
};

export default StatusBar;
