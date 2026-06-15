/**
 * StatusBar component - single-line status display at the bottom.
 *
 * Shows: connection | Worker | Backend | Mode | Task | Progress | Pane | Help
 * Integrated into the unified border frame (no separate border).
 */
import React from 'react';
import {Box, Text} from 'ink';
import type {ConnectionState} from '../grpc/types.js';
import type {SelectedPane, EventFilter} from '../reducer.js';
import {eventFilterLabel} from '../reducer.js';

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

  return (
    <Box paddingX={1}>
      <Text color={connColor}>{connDot}</Text>
      <Text dimColor>{' '}</Text>
      <Text dimColor>{'Worker:'}</Text>
      <Text> {workerId || 'N/A'} </Text>
      <Text dimColor>{'│'}</Text>
      <Text dimColor>{' Backend:'}</Text>
      <Text color={backendColor}> {backend} </Text>
      {mode && (
        <>
          <Text dimColor>{'│'}</Text>
          <Text dimColor>{' '}</Text>
          <Text dimColor>{mode}</Text>
        </>
      )}
      {activeTaskId && (
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
      {lastError && (
        <>
          <Text dimColor>{'│'}</Text>
          <Text color="red"> {lastError.slice(0, 30)}</Text>
        </>
      )}
      <Text dimColor>{'  '}</Text>
      <Text dimColor>{'(Tab pane  Ctrl+P pause  Ctrl+F filter  Ctrl+R reconnect  Ctrl+Q quit)'}</Text>
    </Box>
  );
};

export default StatusBar;
