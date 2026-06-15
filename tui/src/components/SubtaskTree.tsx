/**
 * SubtaskTree component - right panel showing subtask list with status icons.
 *
 * Status icons:
 * - Pending: hourglass (dim)
 * - In progress: spinner (cyan)
 * - Completed: checkmark (green)
 * - Failed: cross (red)
 *
 * Border title shows "Subtasks [X/Y Z%]"
 */
import React from 'react';
import {Box, Text} from 'ink';

export type SubtaskStatusType = 'pending' | 'assigned' | 'in_progress' | 'completed' | 'failed' | 'conflicted';

export interface SubtaskItem {
  id: string;
  index: number;
  description: string;
  status: SubtaskStatusType;
}

export interface SubtaskTreeProps {
  subtasks: SubtaskItem[];
  taskDescription?: string;
  progress?: {completed: number; total: number};
}

const STATUS_ICONS: Record<SubtaskStatusType, {icon: string; color?: string; bold?: boolean; dim?: boolean}> = {
  pending: {icon: '⏳', dim: true},
  assigned: {icon: '⏳', dim: true},
  in_progress: {icon: '🔄', color: 'cyan', bold: true},
  completed: {icon: '✅', color: 'green', bold: true},
  failed: {icon: '❌', color: 'red', bold: true},
  conflicted: {icon: '⚠️', color: 'yellow', bold: true},
};

function getProgressText(completed: number, total: number): string {
  const pct = total > 0 ? Math.round((100 * completed) / total) : 0;
  return `${completed}/${total} ${pct}%`;
}

const SubtaskRow: React.FC<{subtask: SubtaskItem}> = ({subtask}) => {
  const statusInfo = STATUS_ICONS[subtask.status] ?? STATUS_ICONS.pending;

  return (
    <Box>
      <Text
        color={statusInfo.color}
        bold={statusInfo.bold}
        dimColor={statusInfo.dim}
      >
        {`${statusInfo.icon} `}
      </Text>
      <Text dimColor={subtask.status === 'pending'}>
        {`${subtask.index}. ${subtask.description}`}
      </Text>
    </Box>
  );
};

const SubtaskTree: React.FC<SubtaskTreeProps> = ({
  subtasks,
  taskDescription = 'No task',
  progress = {completed: 0, total: 0},
}) => {
  const titleSuffix = getProgressText(progress.completed, progress.total);

  return (
    <Box
      flexDirection="column"
      flexGrow={1}
      borderStyle="single"
      borderColor="gray"
      paddingX={1}
    >
      <Box marginBottom={1}>
        <Text bold>{`Subtasks [${titleSuffix}]`}</Text>
      </Box>
      {subtasks.length === 0 ? (
        <Text dimColor>No subtasks yet.</Text>
      ) : (
        <Box flexDirection="column">
          {subtasks.map((st) => (
            <SubtaskRow key={st.id} subtask={st} />
          ))}
        </Box>
      )}
    </Box>
  );
};

export default SubtaskTree;
