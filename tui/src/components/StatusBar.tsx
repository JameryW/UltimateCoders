/**
 * StatusBar component - single-line status display at the bottom.
 *
 * Shows: connection dot | Worker | Backend | Progress
 * Integrated into the unified border frame (no separate border).
 */
import React from 'react';
import {Box, Text} from 'ink';

export interface StatusBarProps {
  workerId?: string;
  backend?: string;
  progress?: {completed: number; total: number};
}

const StatusBar: React.FC<StatusBarProps> = ({
  workerId = '',
  backend = 'subprocess',
  progress = {completed: 0, total: 0},
}) => {
  const progressText = `${progress.completed}/${progress.total}`;
  const backendColor = backend === 'grpc' ? 'green' : backend === 'disconnected' ? 'red' : 'yellow';

  return (
    <Box paddingX={1}>
      <Text dimColor>{'Worker:'}</Text>
      <Text> {workerId || 'N/A'} </Text>
      <Text dimColor>{'│'}</Text>
      <Text dimColor>{' Backend:'}</Text>
      <Text color={backendColor}> {backend} </Text>
      <Text dimColor>{'│'}</Text>
      <Text dimColor>{' Progress:'}</Text>
      <Text bold> {progressText}</Text>
      <Text dimColor>{'  '}</Text>
      <Text dimColor>{'(Ctrl+R reconnect  Ctrl+Q quit)'}</Text>
    </Box>
  );
};

export default StatusBar;
