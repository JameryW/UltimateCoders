/**
 * TaskListOverlay — interactive task list with Up/Down navigation and Enter to switch.
 *
 * Rendered as a full-screen overlay (like SubtaskTree overlay).
 * Purely visual; keyboard handling is in App.tsx.
 */
import React from 'react';
import {Box, Text} from 'ink';
import type {TaskProto} from '../grpc/types.js';
import {mapTaskStatus} from '../grpc/types.js';
import {formatRelativeTime} from '../task-list-utils.js';
import {truncateToWidth} from '../truncate.js';

interface TaskListOverlayProps {
  tasks: TaskProto[];
  selectedIndex: number;
  maxWidth: number;
}

const STATUS_ICON: Record<string, string> = {
  created: '○',
  planning: '◌',
  pending: '○',
  in_progress: '◉',
  completed: '●',
  failed: '✗',
  paused: '‖',
  conflicted: '✗',
};

const STATUS_COLOR: Record<string, string | undefined> = {
  created: undefined,
  planning: undefined,
  pending: undefined,
  in_progress: 'cyan',
  completed: 'green',
  failed: 'red',
  paused: 'yellow',
  conflicted: 'red',
};

function getIcon(status: string): string {
  return STATUS_ICON[status] ?? '○';
}

function getColor(status: string): string | undefined {
  return STATUS_COLOR[status] ?? undefined;
}

export const TaskListOverlay: React.FC<TaskListOverlayProps> = ({tasks, selectedIndex, maxWidth}) => {
  if (tasks.length === 0) {
    return <Text dimColor>{'No tasks found.'}</Text>;
  }

  const descWidth = maxWidth - 26;

  return (
    <Box flexDirection="column">
      {tasks.map((task, i) => {
        const status = mapTaskStatus(task.status);
        const icon = getIcon(status);
        const color = getColor(status);
        const isSelected = i === selectedIndex;
        const desc = truncateToWidth(task.description || '(no description)', descWidth);
        const shortId = task.id.slice(0, 8);
        const time = formatRelativeTime(task.updatedAt || task.createdAt);

        return (
          <Box key={task.id}>
            {isSelected && <Text color="yellow">{'▸'}</Text>}
            {!isSelected && <Text>{' '}</Text>}
            <Text color={color}>{`${icon} `}</Text>
            <Text bold={isSelected}>{shortId}</Text>
            <Text>{'  '}</Text>
            <Text color={color}>{`${status.padEnd(12)}`}</Text>
            <Text bold={isSelected}>{desc}</Text>
            <Text dimColor>{` ${time}`}</Text>
          </Box>
        );
      })}
    </Box>
  );
};
