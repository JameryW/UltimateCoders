import React, {useState, useEffect, useRef} from 'react';
import {Box, Text} from 'ink';

// ponytail: spinner chars — standard braille pattern, no extra deps
const SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];

export interface StatusIndicatorProps {
  /** Whether a task is being submitted. */
  isSubmitting: boolean;
  /** Whether the event stream is active. */
  isStreaming: boolean;
  /** Timestamp (ms) when the current task started. */
  startedAt: number | null;
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
}) => {
  const [frame, setFrame] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const active = isSubmitting || isStreaming;

  useEffect(() => {
    if (!active) {
      if (timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
      return;
    }

    // Tick spinner + elapsed every 100ms
    timerRef.current = setInterval(() => {
      setFrame((f) => (f + 1) % SPINNER_FRAMES.length);
      if (startedAt) {
        setElapsed(Date.now() - startedAt);
      }
    }, 100);

    return () => {
      if (timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [active, startedAt]);

  if (!active) return null;

  const spinner = SPINNER_FRAMES[frame];
  const elapsedStr = startedAt ? ` ${formatElapsed(elapsed)}` : '';
  const label = isSubmitting ? 'Working' : 'Streaming';
  const cancelHint = isSubmitting ? '  Esc cancel' : '';

  return (
    <Box paddingX={1}>
      <Text color="cyan">{spinner}</Text>
      <Text color="cyan">{` ${label}...${elapsedStr}`}</Text>
      {cancelHint && <Text dimColor>{cancelHint}</Text>}
    </Box>
  );
};

export default StatusIndicator;
