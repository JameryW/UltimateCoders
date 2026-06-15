/**
 * SubtaskTree component - right panel showing subtask list with status icons.
 *
 * No border — the parent App component provides the unified outer frame.
 * Separated from ChatLog by a vertical divider rendered by App.
 *
 * Status icons:
 * - Pending: ○ (dim)
 * - Assigned: ◌ (dim)
 * - In progress: ◉ (cyan, bold)
 * - Completed: ● (green, bold)
 * - Failed: ✗ (red, bold) — shows error summary
 * - Conflicted: ⚠ (yellow, bold)
 *
 * Enhanced: shows assignedWorker, highlights running items, truncates
 * descriptions with string-width for terminal width safety.
 */
import React from 'react';
import {Box, Text} from 'ink';
import stringWidth from 'string-width';
import GraphemeSplitter from 'grapheme-splitter';
import type {SymbolSet} from '../symbols.js';
import {getSymbols} from '../symbols.js';

const splitter = new GraphemeSplitter();

export type SubtaskStatusType = 'pending' | 'assigned' | 'in_progress' | 'completed' | 'failed' | 'conflicted';

export interface SubtaskItem {
  id: string;
  index: number;
  description: string;
  status: SubtaskStatusType;
  assignedWorker?: string;
}

export interface SubtaskTreeProps {
  subtasks: SubtaskItem[];
  taskDescription?: string;
  progress?: {completed: number; total: number};
  isFocused?: boolean;
  /** Available width for description truncation. */
  maxWidth?: number;
  /** Symbol set for rendering (unicode or ascii). */
  symbols?: SymbolSet;
}

// Map status to symbol field key
const STATUS_CONFIG: Record<SubtaskStatusType, {
  symbolKey: 'pending' | 'assigned' | 'inProgress' | 'completed' | 'failed' | 'conflicted';
  color?: string;
  bold?: boolean;
  dim?: boolean;
}> = {
  pending: {symbolKey: 'pending', dim: true},
  assigned: {symbolKey: 'assigned', dim: true},
  in_progress: {symbolKey: 'inProgress', color: 'cyan', bold: true},
  completed: {symbolKey: 'completed', color: 'green', bold: true},
  failed: {symbolKey: 'failed', color: 'red', bold: true},
  conflicted: {symbolKey: 'conflicted', color: 'yellow', bold: true},
};

function getProgressText(completed: number, total: number): string {
  const pct = total > 0 ? Math.round((100 * completed) / total) : 0;
  return `${completed}/${total} ${pct}%`;
}

/**
 * Truncate a string to fit within maxDisplayWidth terminal columns.
 * Uses string-width for CJK-aware measurement and GraphemeSplitter
 * for safe grapheme-boundary truncation (avoids splitting combining
 * characters, emoji ZWJ sequences, etc.).
 */
function truncateToWidth(text: string, maxDisplayWidth: number): string {
  if (stringWidth(text) <= maxDisplayWidth) return text;
  // Remove graphemes from end until width fits (leave room for "…")
  const ellipsisWidth = 1;
  const graphemes = splitter.splitGraphemes(text);
  let width = stringWidth(text);
  let end = graphemes.length;
  while (width > maxDisplayWidth - ellipsisWidth && end > 0) {
    end--;
    width -= stringWidth(graphemes[end]);
  }
  return graphemes.slice(0, end).join('') + '…';
}

const SubtaskRow: React.FC<{subtask: SubtaskItem; maxWidth: number; symbols: SymbolSet}> = ({subtask, maxWidth, symbols}) => {
  const statusConfig = STATUS_CONFIG[subtask.status] ?? STATUS_CONFIG.pending;
  const icon = symbols[statusConfig.symbolKey];

  // Calculate available width for description: subtract icon + index + padding
  // "◉ 1. " = icon(2) + space(1) + number(~2) + dot(1) + space(1) ≈ 7 display cols
  const descWidth = Math.max(10, maxWidth - 7);
  const truncatedDesc = truncateToWidth(subtask.description, descWidth);

  return (
    <Box>
      <Text
        color={statusConfig.color}
        bold={statusConfig.bold}
        dimColor={statusConfig.dim}
      >
        {`${icon} `}
      </Text>
      <Text dimColor={subtask.status === 'pending'}>
        {`${subtask.index}. ${truncatedDesc}`}
      </Text>
      {subtask.assignedWorker && subtask.status === 'in_progress' && (
        <Text dimColor>{` →${truncateToWidth(subtask.assignedWorker, 8)}`}</Text>
      )}
      {subtask.status === 'failed' && (
        <Text color="red">{` ✗`}</Text>
      )}
    </Box>
  );
};

const SubtaskTree: React.FC<SubtaskTreeProps> = ({
  subtasks,
  taskDescription = 'No task',
  progress = {completed: 0, total: 0},
  isFocused = false,
  maxWidth = 40,
  symbols: symbolsProp,
}) => {
  const symbols = symbolsProp ?? getSymbols();
  const titleSuffix = getProgressText(progress.completed, progress.total);

  // Truncate task description for header
  const truncatedTaskDesc = truncateToWidth(taskDescription, maxWidth - 10);

  return (
    <Box flexDirection="column" flexGrow={1} paddingX={1}>
      <Box marginBottom={1}>
        <Text bold color="cyan">{`Subtasks`}</Text>
        <Text dimColor>{` [${titleSuffix}]`}</Text>
        {isFocused && <Text dimColor>{' [focused]'}</Text>}
      </Box>
      {taskDescription !== 'No task' && (
        <Box marginBottom={1}>
          <Text dimColor>{`Task: `}</Text>
          <Text>{truncatedTaskDesc}</Text>
        </Box>
      )}
      {subtasks.length === 0 ? (
        <Text dimColor>No subtasks yet.</Text>
      ) : (
        <Box flexDirection="column">
          {subtasks.map((st) => (
            <SubtaskRow key={st.id} subtask={st} maxWidth={maxWidth} symbols={symbols} />
          ))}
        </Box>
      )}
    </Box>
  );
};

export default SubtaskTree;
