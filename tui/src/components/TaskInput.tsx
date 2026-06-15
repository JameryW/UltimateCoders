/**
 * TaskInput component - text input with CJK/IME support.
 *
 * Uses CjkTextInput (replacing ink-text-input) for proper CJK-width-aware
 * cursor positioning and grapheme-cluster-based editing.
 *
 * No border — the parent App component provides the unified outer frame.
 * The custom useCursor hook positions the real terminal cursor for IME
 * composition.
 */
import React, {useState, useCallback} from 'react';
import {Box, Text} from 'ink';
import CjkTextInput from './CjkTextInput.js';
import useCursor from '../hooks/useCursor.js';

export interface TaskInputProps {
  onSubmit: (value: string) => void;
  isFocused?: boolean;
}

const TaskInput: React.FC<TaskInputProps> = ({
  onSubmit,
  isFocused = true,
}) => {
  const [value, setValue] = useState('');
  const {setCursorPosition, showCursor} = useCursor();

  const handleSubmit = useCallback(
    (submittedValue: string) => {
      const trimmed = submittedValue.trim();
      if (trimmed.length === 0) {
        return;
      }
      onSubmit(trimmed);
      setValue('');
    },
    [onSubmit],
  );

  const handleChange = useCallback(
    (newValue: string) => {
      setValue(newValue);
    },
    [],
  );

  // Called by CjkTextInput whenever the cursor moves (including arrow keys)
  const handleCursorMove = useCallback(
    (displayCol: number) => {
      // x offset: 1 (paddingX) + 2 ("> " prefix) = 3 display columns
      setCursorPosition({x: 3 + displayCol, y: 0});
      showCursor();
    },
    [setCursorPosition, showCursor],
  );

  return (
    <Box paddingX={1}>
      <Text color="cyan" bold>
        {'> '}
      </Text>
      <CjkTextInput
        value={value}
        onChange={handleChange}
        onSubmit={handleSubmit}
        onCursorMove={handleCursorMove}
        placeholder="type task description and press Enter..."
        focus={isFocused}
      />
    </Box>
  );
};

export default TaskInput;
