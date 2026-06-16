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
 * Keyboard navigation (v2):
 * - selectedIndex: which subtask is highlighted (Up/Down arrows)
 * - Selected row is shown with inverse highlight and "▸" prefix
 * - Enter toggles subtask detail in the main area
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
  /** IDs of subtasks this one depends on. */
  dependsOn?: string[];
  /** Error summary for failed subtasks. */
  errorSummary?: string;
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
  /** Index of the currently selected subtask for keyboard navigation (-1 = none). */
  selectedIndex?: number;
  /** Whether the selected subtask's detail panel is open. */
  detailOpen?: boolean;
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

interface SubtaskRowProps {
  subtask: SubtaskItem;
  maxWidth: number;
  symbols: SymbolSet;
  isSelected: boolean;
  isFocused: boolean;
}

const SubtaskRow: React.FC<SubtaskRowProps> = ({subtask, maxWidth, symbols, isSelected, isFocused}) => {
  const statusConfig = STATUS_CONFIG[subtask.status] ?? STATUS_CONFIG.pending;
  const icon = symbols[statusConfig.symbolKey];

  // Calculate available width for description: subtract icon + index + padding
  // "◉ 1. " = icon(2) + space(1) + number(~2) + dot(1) + space(1) ≈ 7 display cols
  const descWidth = Math.max(10, maxWidth - 7);
  const truncatedDesc = truncateToWidth(subtask.description, descWidth);

  // Selection indicator: "▸" for selected row when subtask tree is focused
  const selectPrefix = isSelected && isFocused ? '▸ ' : '  ';

  return (
    <Box>
      <Text
        color={isSelected && isFocused ? 'cyan' : statusConfig.color}
        bold={statusConfig.bold || (isSelected && isFocused)}
        dimColor={statusConfig.dim && !(isSelected && isFocused)}
      >
        {`${selectPrefix}${icon} `}
      </Text>
      <Text
        bold={isSelected && isFocused}
        dimColor={subtask.status === 'pending' && !(isSelected && isFocused)}
      >
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

/**
 * Detail panel for the selected subtask — shown below the row when detailOpen.
 * Displays full description, status, worker, dependencies, and error summary.
 */
const SubtaskDetail: React.FC<{subtask: SubtaskItem; maxWidth: number}> = ({subtask, maxWidth}) => {
  // Total indent: outer marginLeft={2} + inner marginLeft={1} = 3 cols
  const contentWidth = maxWidth - 3;
  return (
    <Box flexDirection="column" marginLeft={2}>
      <Text dimColor>{'│'}</Text>
      <Box marginLeft={1}>
        <Text dimColor>{'desc: '}</Text>
        <Text>{truncateToWidth(subtask.description, contentWidth - 6)}</Text>
      </Box>
      <Box marginLeft={1}>
        <Text dimColor>{'status: '}</Text>
        <Text color={STATUS_CONFIG[subtask.status]?.color ?? 'white'} bold={STATUS_CONFIG[subtask.status]?.bold}>
          {subtask.status}
        </Text>
      </Box>
      {subtask.assignedWorker && (
        <Box marginLeft={1}>
          <Text dimColor>{'worker: '}</Text>
          <Text>{truncateToWidth(subtask.assignedWorker, contentWidth - 8)}</Text>
        </Box>
      )}
      {subtask.dependsOn && subtask.dependsOn.length > 0 && (
        <Box marginLeft={1}>
          <Text dimColor>{'deps: '}</Text>
          <Text>{truncateToWidth(subtask.dependsOn.join(', '), contentWidth - 6)}</Text>
        </Box>
      )}
      {subtask.errorSummary && (
        <Box marginLeft={1}>
          <Text dimColor>{'error: '}</Text>
          <Text color="red">{truncateToWidth(subtask.errorSummary, contentWidth - 7)}</Text>
        </Box>
      )}
      <Text dimColor>{'│'}</Text>
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
  selectedIndex = -1,
  detailOpen = false,
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
          {subtasks.map((st, idx) => (
            <React.Fragment key={st.id}>
              <SubtaskRow
                subtask={st}
                maxWidth={maxWidth}
                symbols={symbols}
                isSelected={idx === selectedIndex}
                isFocused={isFocused}
              />
              {detailOpen && idx === selectedIndex && (
                <SubtaskDetail subtask={st} maxWidth={maxWidth} />
              )}
            </React.Fragment>
          ))}
        </Box>
      )}
      {isFocused && subtasks.length > 0 && (
        <Box marginTop={1}>
          <Text dimColor>{'↑↓ navigate · Enter detail · Esc back'}</Text>
        </Box>
      )}
    </Box>
  );
};

export default SubtaskTree;
