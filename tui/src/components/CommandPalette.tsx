/**
 * CommandPalette — Ctrl+P overlay for searching and executing slash commands.
 * Reuses CjkTextInput for search, displays filtered COMMANDS list.
 */
import React, {useState, useCallback} from 'react';
import {Box, Text, useInput, useApp} from 'ink';
import {COMMANDS, type SlashCommand} from '../commands.js';
import CjkTextInput from './CjkTextInput.js';

export interface CommandPaletteProps {
  query: string;
  selectedIndex: number;
  onSelect: (command: SlashCommand, args: string) => void;
  onQueryChange: (query: string) => void;
  onSelectIndex: (index: number) => void;
  onClose: () => void;
}

const CommandPalette: React.FC<CommandPaletteProps> = ({
  query,
  selectedIndex,
  onSelect,
  onQueryChange,
  onSelectIndex,
  onClose,
}) => {
  const filtered = query
    ? COMMANDS.filter((c) => c.name.includes(query.toLowerCase()) || c.description.toLowerCase().includes(query.toLowerCase()))
    : COMMANDS;

  const safeSelected = Math.min(selectedIndex, filtered.length - 1);

  useInput(useCallback((input: string, key: {upArrow?: boolean; downArrow?: boolean; return?: boolean; escape?: boolean}) => {
    if (key.escape) {
      onClose();
      return;
    }
    if (key.upArrow) {
      onSelectIndex(Math.max(0, safeSelected - 1));
      return;
    }
    if (key.downArrow) {
      onSelectIndex(Math.min(filtered.length - 1, safeSelected + 1));
      return;
    }
    if (key.return && filtered.length > 0) {
      onSelect(filtered[safeSelected]!, '');
      onClose();
      return;
    }
  }, [safeSelected, filtered, onSelect, onClose, onSelectIndex]));

  return (
    <Box flexDirection="column" borderStyle="round" borderColor="yellow" padding={1}>
      <Box marginBottom={1}>
        <Text bold color="yellow">{'Command Palette'}</Text>
        <Text dimColor>{' (type to search · ↑↓ navigate · Enter execute · Esc close)'}</Text>
      </Box>
      <Box marginBottom={1}>
        <Text dimColor>{'/'}</Text>
        <CjkTextInput
          value={query}
          onChange={onQueryChange}
          focus={true}
          placeholder="search commands..."
          showCursor={true}
        />
      </Box>
      {filtered.length === 0 && (
        <Text dimColor>{'  No matching commands'}</Text>
      )}
      {filtered.map((cmd, i) => (
        <Box key={cmd.name}>
          <Text color={i === safeSelected ? 'yellow' : 'white'} bold={i === safeSelected}>
            {i === safeSelected ? '▸ ' : '  '}
          </Text>
          <Text color={i === safeSelected ? 'yellow' : 'cyan'}>{`/${cmd.name}`}</Text>
          <Text dimColor>{`  ${cmd.description}`}</Text>
        </Box>
      ))}
    </Box>
  );
};

export default CommandPalette;
