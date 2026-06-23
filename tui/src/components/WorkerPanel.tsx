/**
 * WorkerPanel — shows per-worker activity when workersExpanded is true.
 * Derived entirely from subtasks (no gRPC dependency).
 *
 * Each row: Worker-1 [████░░] 2/3 done  running: #3 "写测试"
 */
import React from 'react';
import {Box, Text} from 'ink';
import type {SubtaskItem} from './SubtaskTree.js';
import {getSymbols, type SymbolSet} from '../symbols.js';
import {truncateToWidth} from '../truncate.js';

export interface WorkerEntry {
  workerId: string;
  active: SubtaskItem[];
  completed: number;
  failed: number;
  total: number;
}

/** Derive worker summary from subtasks. */
export function deriveWorkerEntries(subtasks: SubtaskItem[]): WorkerEntry[] {
  const map = new Map<string, WorkerEntry>();
  for (const st of subtasks) {
    if (!st.assignedWorker) continue;
    const entry = map.get(st.assignedWorker) ?? {
      workerId: st.assignedWorker,
      active: [],
      completed: 0,
      failed: 0,
      total: 0,
    };
    entry.total++;
    if (st.status === 'in_progress') entry.active.push(st);
    if (st.status === 'completed') entry.completed++;
    if (st.status === 'failed') entry.failed++;
    map.set(st.assignedWorker, entry);
  }
  return Array.from(map.values()).sort((a, b) => b.active.length - a.active.length);
}

export interface WorkerPanelProps {
  subtasks: SubtaskItem[];
  maxWidth?: number;
  symbols?: SymbolSet;
}

const WorkerPanel: React.FC<WorkerPanelProps> = ({subtasks, maxWidth = 40, symbols: symbolsProp}) => {
  const S = symbolsProp ?? getSymbols();
  const entries = deriveWorkerEntries(subtasks);

  if (entries.length === 0) {
    return <Text dimColor>{'  No worker activity.'}</Text>;
  }

  const maxTotal = Math.max(...entries.map(e => e.total), 1);
  const barWidth = 4;

  return (
    <Box flexDirection="column" paddingX={1} marginTop={0}>
      {entries.map((w) => {
        const filled = Math.round((w.total / maxTotal) * barWidth);
        const done = Math.round((w.completed / Math.max(w.total, 1)) * filled);
        const run = filled - done;
        const empty = barWidth - filled;
        const doneCount = Math.max(0, done);
        const runCount = Math.max(0, run);
        const emptyCount = Math.max(0, empty);
        const activeLabel = w.active.length > 0
          ? ` running: #${w.active[0]!.index} "${truncateToWidth(w.active[0]!.description, 16)}"`
          : '';
        const workerLabel = truncateToWidth(w.workerId, 8);
        return (
          <Box key={w.workerId}>
            <Text dimColor>{`  ${workerLabel} [`}</Text>
            <Text>{S.barFilled.repeat(doneCount)}</Text>
            {runCount > 0 && <Text color="cyan">{S.barHalf.repeat(runCount)}</Text>}
            <Text>{S.barEmpty.repeat(emptyCount)}</Text>
            <Text>{'] '}</Text>
            <Text dimColor>{`${w.completed}/${w.total}`}</Text>
            {activeLabel && <Text color="cyan">{activeLabel}</Text>}
          </Box>
        );
      })}
    </Box>
  );
};

export default WorkerPanel;
