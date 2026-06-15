/**
 * TaskInput component - text input with CJK/IME support.
 *
 * Uses CjkTextInput (replacing ink-text-input) for proper CJK-width-aware
 * cursor positioning and grapheme-cluster-based editing.
 *
 * Extended shortcuts:
 * - Ctrl+J: insert newline (multi-line task)
 * - Ctrl+U: clear input
 * - Ctrl+K: delete to end of line
 * - Up/Down: browse input history
 *
 * No border — the parent App component provides the unified outer frame.
 * The custom useCursor hook positions the real terminal cursor for IME
 * composition.
 */
import React, {useState, useCallback, useEffect} from 'react';
import {Box, Text} from 'ink';
import CjkTextInput from './CjkTextInput.js';
import useCursor from '../hooks/useCursor.js';

export interface TaskInputProps {
  onSubmit: (value: string) => void;
  isFocused?: boolean;
  /** Previously submitted task descriptions (most recent first). */
  inputHistory?: string[];
  /** Current index into inputHistory (-1 = not browsing). */
  historyIndex?: number;
  /** Called when the history index changes (Up/Down navigation). */
  onHistoryIndexChange?: (index: number) => void;
}

const TaskInput: React.FC<TaskInputProps> = ({
  onSubmit,
  isFocused = true,
  inputHistory = [],
  historyIndex = -1,
  onHistoryIndexChange,
}) => {
  const [value, setValue] = useState('');
  const [savedDraft, setSavedDraft] = useState('');
  const {setCursorPosition, showCursor} = useCursor();

  // Sync from history index: when user navigates history, update value
  useEffect(() => {
    if (historyIndex >= 0 && historyIndex < inputHistory.length) {
      // Save current draft before navigating
      if (historyIndex === 0 && savedDraft === '' && value !== '') {
        setSavedDraft(value);
      }
      setValue(inputHistory[historyIndex]);
    } else if (historyIndex === -1) {
      // Restore draft when exiting history
      setValue(savedDraft);
      setSavedDraft('');
    }
  }, [historyIndex, inputHistory]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleSubmit = useCallback(
    (submittedValue: string) => {
      const trimmed = submittedValue.trim();
      if (trimmed.length === 0) {
        return;
      }
      onSubmit(trimmed);
      setValue('');
      setSavedDraft('');
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

  // Handle history navigation via Up/Down
  const handleHistoryNav = useCallback(
    (direction: 'up' | 'down') => {
      if (inputHistory.length === 0) return;

      if (direction === 'up') {
        const next = historyIndex < 0 ? 0 : Math.min(historyIndex + 1, inputHistory.length - 1);
        // Save draft when first entering history
        if (historyIndex < 0) {
          setSavedDraft(value);
        }
        onHistoryIndexChange?.(next);
      } else {
        if (historyIndex <= 0) {
          // Exit history, restore draft
          onHistoryIndexChange?.(-1);
        } else {
          onHistoryIndexChange?.(historyIndex - 1);
        }
      }
    },
    [historyIndex, inputHistory.length, value, onHistoryIndexChange],
  );

  // Detect multi-line value for visual indicator
  const isMultiline = value.includes('\n');

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
        onHistoryNav={handleHistoryNav}
        placeholder="type task description and press Enter..."
        focus={isFocused}
      />
      {isMultiline && <Text dimColor>{' [multi-line]'}</Text>}
    </Box>
  );
};

export default TaskInput;
