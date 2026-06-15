/**
 * LogoHeader component - ASCII art "UC" monogram with version.
 *
 * Renders the same visual as the Textual TUI:
 *   ╔═╗╦ ╦╔═╗╔═╗   UltimateCoders v0.1.0
 *   ║  ╚╦╝║╣ ╚═╗
 *   ╚═╝ ╩ ╚═╝╚═╝
 */
import React from 'react';
import {Box, Text} from 'ink';

const LOGO_LINES = [
  '╔═╗╦ ╦╔═╗╔═╗',
  '║  ╚╦╝║╣ ╚═╗',
  '╚═╝ ╩ ╚═╝╚═╝',
];

function getVersion(): string {
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const pkg = require('../../package.json');
    return pkg.version ?? '0.1.0';
  } catch {
    return '0.1.0';
  }
}

const LogoHeader: React.FC = () => {
  const version = getVersion();

  return (
    <Box flexDirection="row" paddingX={1}>
      <Box flexDirection="column">
        {LOGO_LINES.map((line, i) => (
          <Text key={i} bold color="cyan">
            {line}
          </Text>
        ))}
      </Box>
      <Box flexDirection="column" marginLeft={2} justifyContent="center">
        <Text bold>UltimateCoders</Text>
        <Text dimColor>v{version}</Text>
      </Box>
    </Box>
  );
};

export default LogoHeader;
