import React from 'react';
import {Box, Text, useAnimation} from 'ink';

// spinner chars — standard braille pattern at 80ms
const SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];

export interface StatusIndicatorProps {
  /** Whether a task is being submitted. */
  isSubmitting: boolean;
  /** Whether the event stream is active. */
  isStreaming: boolean;
  /** Timestamp (ms) when the current task started. */
  startedAt: number | null;
  /** Subtask progress (completed/total). */
  progress?: {completed: number; total: number};
}

/** Format elapsed ms into compact string: (5s) / (1m 30s) / (2h 15m 00s) */
export function formatElapsed(ms: number): string {
  const totalSec = Math.floor(ms / 1000);
  if (totalSec < 60) return `(${totalSec}s)`;
  const sec = totalSec % 60;
  const totalMin = Math.floor(totalSec / 60);
  if (totalMin < 60) return `(${totalMin}m ${String(sec).padStart(2, '0')}s)`;
  const min = totalMin % 60;
  const hr = Math.floor(totalMin / 60);
  return `(${hr}h ${String(min).padStart(2, '0')}m ${String(sec).padStart(2, '0')}s)`;
}

const StatusIndicator: React.FC<StatusIndicatorProps> = ({
  isSubmitting,
  isStreaming,
  startedAt,
  progress,
}) => {
  const active = isSubmitting || isStreaming;

  // useAnimation shares a single timer across all animated components
  const {frame, time} = useAnimation({interval: 80, isActive: active});

  if (!active) return null;

  const spinner = SPINNER_FRAMES[frame % SPINNER_FRAMES.length];
  const elapsed = startedAt ? time : 0;
  const elapsedStr = startedAt ? ` ${formatElapsed(elapsed)}` : '';
  const label = isSubmitting ? 'Working' : 'Streaming';
  const cancelHint = isSubmitting ? '  Esc cancel' : '';

  // Subtask progress bar
  let progressStr = '';
  if (progress && progress.total > 0) {
    const pct = progress.completed / progress.total;
    const filled = Math.round(pct * 10);
    const bar = '█'.repeat(filled) + '░'.repeat(10 - filled);
    progressStr = ` [${bar}] ${progress.completed}/${progress.total}`;
  }

  return (
    <Box paddingX={1}>
      <Text color="cyan">{spinner}</Text>
      <Text color="cyan">{` ${label}...${elapsedStr}`}</Text>
      {progressStr && <Text color="cyan">{progressStr}</Text>}
      {cancelHint && <Text dimColor>{cancelHint}</Text>}
    </Box>
  );
};

export default StatusIndicator;
