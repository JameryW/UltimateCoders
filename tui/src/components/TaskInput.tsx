/**
 * TaskInput component - text input with CJK/IME support.
 *
 * Uses ink-text-input for the input field and a custom useCursor hook
 * to position the real terminal cursor for IME composition.
 * This is the critical component that fixes CJK input in iTerm2.
 *
 * Key difference from Textual's Input:
 * - Ink preserves the real terminal cursor (useCursor positions it)
 * - Input arrives as UTF-8 strings, not raw bytes
 * - IME composition window appears at the real cursor position
 */
import React, {useState, useCallback} from 'react';
import {Box, Text} from 'ink';
import TextInput from 'ink-text-input';
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
      // Position the real terminal cursor for IME composition.
      // The cursor position is based on the prompt prefix length
      // ("> " = 2 chars) plus the input value length.
      // For CJK characters, each character occupies 2 cell widths,
      // but Ink's internal rendering handles the visual width.
      // The x offset here is the visual column position.
      setCursorPosition({x: 4 + newValue.length, y: 0});
      showCursor();
    },
    [setCursorPosition, showCursor],
  );

  return (
    <Box
      borderStyle="single"
      borderColor={isFocused ? 'cyan' : 'gray'}
      paddingX={1}
      marginX={1}
    >
      <Text color="cyan" bold>
        {'> '}
      </Text>
      <TextInput
        value={value}
        onChange={handleChange}
        onSubmit={handleSubmit}
        placeholder="type task description and press Enter..."
        focus={isFocused}
      />
    </Box>
  );
};

export default TaskInput;
