/**
 * LogoBanner — ASCII art brand banner for the TUI header.
 *
 * Displays a large "UC" logo in magenta, inspired by Claude Code's
 * startup banner. Fits in 80+ col terminals; hidden on narrow terminals.
 *
 * ASCII art variants by symbolMode:
 *   unicode: █ block style
 *   ascii:   # hash style
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

// ── ASCII Art Logos ─────────────────────────────────────────

const UC_UNICODE = [
  '▄▄▄▄▄▄ ▄▄   ▄▄',
  '█   █  █ █ █  █',
  '█▄▄▄█  █  ▀█  █',
  '█   █  █   ▀ █',
  '█   █  █    ▄▄█',
];

const UC_ASCII = [
  '####### ##   ##',
  '#   # # # # # #',
  '# ### # #  ## #',
  '#   # # #   # #',
  '#   # # #   ###',
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
      <Box marginTop={0}>
        <Text color="magenta">{brandChar}</Text>
        <Text bold color="magenta"> UltimateCoders</Text>
        {version && <Text dimColor>{` v${version}`}</Text>}
      </Box>
    </Box>
  );
}

export default LogoBanner;
