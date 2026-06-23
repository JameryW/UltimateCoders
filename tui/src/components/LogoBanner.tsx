/**
 * LogoBanner — Brand banner for the TUI header.
 *
 * Responsive:
 *   >=80 cols: pixel-game-style "UC" logo (5 lines) + version line = 6 lines
 *   60-79 cols: compact single-line brand + version = 1 line
 *   <60 cols: hidden = 0 lines
 *
 * Export getLogoHeight(terminalWidth) so App can compute ChatLog visibleLines.
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
  /** Whether to show welcome hint (auto-dismisses via parent timer). */
  welcomeVisible?: boolean;
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

/** Logo height in terminal rows, given terminal width. */
export function getLogoHeight(terminalWidth: number): number {
  if (terminalWidth < 60) return 0;
  if (terminalWidth < 80) return 1;
  return 6; // 5 logo lines + 1 version line
}

export function LogoBanner({
  terminalWidth = 80,
  brandChar = '◆',
  version = '',
  welcomeVisible = false,
}: LogoBannerProps): React.ReactNode | null {
  // Hide on very narrow terminals
  if (terminalWidth < 60) return null;

  // Compact on narrow terminals (60-79 cols)
  if (terminalWidth < 80) {
    return (
      <Box paddingX={1}>
        <Text color="magenta" bold>{brandChar}</Text>
        <Text bold color="magenta">{' UC'}</Text>
        {version && <Text dimColor>{` v${version}`}</Text>}
      </Box>
    );
  }

  // Full pixel-game logo (>=80 cols)
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
      {welcomeVisible && (
        <Box marginTop={0}>
          <Text dimColor>{'  按 ? 查看快捷键 │ Shift+Tab 切换焦点 │ Ctrl+P 命令面板'}</Text>
        </Box>
      )}
    </Box>
  );
}

export default LogoBanner;
