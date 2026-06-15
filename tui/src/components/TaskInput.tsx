/**
 * TaskInput component - text input with CJK/IME support.
 *
 * Uses CjkTextInput (replacing ink-text-input) for proper CJK-width-aware
 * cursor positioning and grapheme-cluster-based editing.
 *
 * The custom useCursor hook positions the real terminal cursor for IME
 * composition. This is the critical component that fixes CJK input in iTerm2.
 *
 * Key difference from Textual's Input:
 * - Ink preserves the real terminal cursor (useCursor positions it)
 * - Input arrives as UTF-8 strings, not raw bytes
 * - IME composition window appears at the real cursor position
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
      // Cursor positioning is handled by onCursorMove callback from CjkTextInput,
      // which correctly computes the display column based on the cursor's grapheme
      // position (not just the end of the string). This avoids a one-frame flicker
      // where handleChange would position the cursor at the end before onCursorMove
      // corrects it to the actual position (e.g. after backspace/delete in the middle).
    },
    [],
  );

  // Called by CjkTextInput whenever the cursor moves (including arrow keys)
  const handleCursorMove = useCallback(
    (displayCol: number) => {
      // x offset: 1 (marginX) + 1 (left border │) + 1 (paddingX) + 2 ("> " prefix) = 5 display columns
      setCursorPosition({x: 5 + displayCol, y: 0});
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
