/**
 * LogoBanner — Pixel-game-style brand banner for the TUI header.
 *
 * Displays a double-line box "UC" logo in magenta, like a retro game HUD.
 * Inspired by pixel art aesthetics + Claude Code's startup banner.
 *
 * Unicode variant: ╔╗╚╝ double-line box characters
 * ASCII variant:  # hash block characters
 *
 * Hidden on narrow terminals (<80 cols).
 */
import React from 'react';
import {Box, Text} from 'ink';

export interface LogoBannerProps {
  /** Terminal width for responsive display. */
  terminalWidth?: number;
  /** Brand symbol from getSymbols().brand */
  brandChar: string;
  /** Version string. */
  version?: string;
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
}: LogoBannerProps): React.ReactNode | null {
  // Hide logo on narrow terminals (<80 cols)
  if (terminalWidth < 80) return null;

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
