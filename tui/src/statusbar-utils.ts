/**
 * Pure functions extracted from StatusBar component for testability.
 *
 * Connection indicator mapping, detail string generation, and layout mode.
 */

import type {ConnectionState} from './grpc/types.js';

// ── Connection Indicator ─────────────────────────────────────

export interface ConnectionIndicator {
  dot: string;
  color: string;
}

export const MAX_RETRY_DISPLAY = 5;

/**
 * Map connection state + streaming status to a dot character and color.
 */
export function getConnectionIndicator(
  connectionState: ConnectionState,
  isStreaming: boolean,
): ConnectionIndicator {
  switch (connectionState) {
    case 'connected':
      return isStreaming
        ? {dot: '●', color: 'green'}
        : {dot: '○', color: 'yellow'};
    case 'connecting':
      return {dot: '◌', color: 'yellow'};
    case 'error':
      return {dot: '✗', color: 'red'};
    case 'disconnected':
      return {dot: '○', color: 'red'};
  }
}

/**
 * Compute the connection detail string for error/offline states.
 *
 * @param now - Current timestamp (Date.now()). Injected for testability.
 */
export function getConnDetail(
  connectionState: ConnectionState,
  serverAddr: string,
  retryCount: number,
  nextRetryAt: number | null,
  now: number,
): string {
  if (connectionState === 'error') {
    const retrySecondsLeft = nextRetryAt
      ? Math.max(0, Math.ceil((nextRetryAt - now) / 1000))
      : 0;
    const retryPart = retryCount > 0
      ? ` retry ${retryCount}/${MAX_RETRY_DISPLAY}`
      : '';
    const secondsPart = retrySecondsLeft > 0
      ? ` in ${retrySecondsLeft}s`
      : '';
    return ` ${serverAddr}${retryPart}${secondsPart}`;
  }
  if (connectionState !== 'connected') {
    return ` ${serverAddr}`;
  }
  return '';
}

// ── Layout Mode ──────────────────────────────────────────────

export type LayoutMode = 'narrow' | 'medium' | 'wide';

/**
 * Determine layout mode based on terminal width.
 */
export function getLayoutMode(terminalWidth: number): LayoutMode {
  if (terminalWidth < 80) return 'narrow';
  if (terminalWidth < 100) return 'medium';
  return 'wide';
}
