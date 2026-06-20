import React, {useState, useEffect, useRef} from 'react';
import {Box, Text} from 'ink';

// spinner chars — standard braille pattern
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

// ponytail: single state object avoids two useState per tick.
// 200ms interval instead of 80ms — Ink redraws the entire terminal on each state
// change, so 5Hz is a better tradeoff between smoothness and flicker.
interface TickState { frame: number; elapsed: number }
const INIT_TICK: TickState = {frame: 0, elapsed: 0};

const StatusIndicator: React.FC<StatusIndicatorProps> = ({
  isSubmitting,
  isStreaming,
  startedAt,
  progress,
}) => {
  const active = isSubmitting || isStreaming;
  const [tick, setTick] = useState<TickState>(INIT_TICK);

  useEffect(() => {
    if (!active) { setTick(INIT_TICK); return; }
    const start = Date.now();
    const id = setInterval(() => {
      setTick(prev => ({frame: prev.frame + 1, elapsed: Date.now() - start}));
    }, 200);
    return () => clearInterval(id);
  }, [active]);

  if (!active) return null;

  const spinner = SPINNER_FRAMES[tick.frame % SPINNER_FRAMES.length];
  const elapsedStr = startedAt ? ` ${formatElapsed(tick.elapsed)}` : '';
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
