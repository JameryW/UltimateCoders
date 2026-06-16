/**
 * LogoBanner — Brand banner for the TUI header.
 *
 * Full mode: pixel-game-style "UC" logo (5 lines) + version line.
 * Compact mode: single-line brand + version (for space-efficient layout).
 *
 * Hidden on narrow terminals (<60 cols).
 */
import React, {useState, useEffect} from 'react';
import {Box, Text} from 'ink';

export interface LogoBannerProps {
  /** Terminal width for responsive display. */
  terminalWidth?: number;
  /** Brand symbol from getSymbols().brand */
  brandChar: string;
  /** Version string. */
  version?: string;
  /** Show compact single-line banner instead of full logo. */
  compact?: boolean;
}

// ── Pixel-game-style UC Logos ──────────────────────────────

const UC_UNICODE = [
  '╔═══╗╔════╗',
  '║╔══╝╚═══╗║',
  '║║       ║║',
  '║╚══╗╔═══╝║',
  '╚═══╝╚════╝',
];

const UC_ASCII = [
  '####  #####',
  '## ## ## ##',
  '##      ###',
  '## ## ## ##',
  ' ####  ####',
];

export function LogoBanner({
  terminalWidth = 80,
  brandChar = '◆',
  version = '',
  compact = false,
}: LogoBannerProps): React.ReactNode | null {
  // Hide on very narrow terminals
  if (terminalWidth < 60) return null;

  // Compact mode: single-line brand
  if (compact) {
    return (
      <Box paddingX={1}>
        <Text color="magenta" bold>{brandChar}</Text>
        <Text bold color="magenta">{' UC'}</Text>
        {version && <Text dimColor>{` v${version}`}</Text>}
      </Box>
    );
  }

  // Full mode: hide on narrow terminals (<80 cols)
  if (terminalWidth < 80) {
    return (
      <Box paddingX={1}>
        <Text color="magenta" bold>{brandChar}</Text>
        <Text bold color="magenta">{' UC'}</Text>
        {version && <Text dimColor>{` v${version}`}</Text>}
      </Box>
    );
  }

  const isUnicode = brandChar === '◆';
  const lines = isUnicode ? UC_UNICODE : UC_ASCII;

  return (
    <Box flexDirection="column" paddingX={1}>
      {lines.map((line, i) => (
        <Text key={i} color="magenta" bold>{line}</Text>
      ))}
      <Box>
        <Text color="yellow">{'⚡'}</Text>
        <Text bold color="magenta">{' UltimateCoders'}</Text>
        {version && <Text dimColor>{` v${version}`}</Text>}
      </Box>
    </Box>
  );
}

export default LogoBanner;
