/**
 * StatusBar component - single-line status display at the bottom.
 *
 * Shows: Worker ID | Backend | Progress (X/Y)
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

  return (
    <Box>
      <Text backgroundColor="blue" color="white">
        {' Worker: '}
      </Text>
      <Text backgroundColor="blue" color="white" bold>
        {workerId || 'N/A'}
      </Text>
      <Text backgroundColor="blue" color="white">
        {' | Backend: '}
      </Text>
      <Text backgroundColor="blue" color="white" bold>
        {backend}
      </Text>
      <Text backgroundColor="blue" color="white">
        {' | Progress: '}
      </Text>
      <Text backgroundColor="blue" color="white" bold>
        {progressText}
      </Text>
      <Text backgroundColor="blue" color="white">
        {' '}
      </Text>
    </Box>
  );
};

export default StatusBar;
