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
import React, {useState, useCallback, useRef} from 'react';
import {Box, Text} from 'ink';
import stringWidth from 'string-width';
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
  // Track the display-width offset of cursor within the input field
  const cursorDisplayOffsetRef = useRef(0);

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
      // After a change, the cursor is at the end of the input.
      // Use stringWidth (not .length) for display-width-based column.
      const displayWidth = stringWidth(newValue);
      cursorDisplayOffsetRef.current = displayWidth;
      // Position the real terminal cursor for IME composition.
      // x offset: 2 chars for "> " border prefix + 2 for border padding = 4 display columns
      setCursorPosition({x: 4 + displayWidth, y: 0});
      showCursor();
    },
    [setCursorPosition, showCursor],
  );

  // Called by CjkTextInput whenever the cursor moves (including arrow keys)
  const handleCursorMove = useCallback(
    (displayCol: number) => {
      cursorDisplayOffsetRef.current = displayCol;
      setCursorPosition({x: 4 + displayCol, y: 0});
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
