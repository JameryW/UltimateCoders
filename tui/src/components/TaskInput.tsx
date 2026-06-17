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
 * - Shift+Tab: cycle focus to next area
 *
 * Visual indicators:
 * - Multi-line: shows Ln/Col + submit hint
 * - History browsing: shows "history N/M"
 * - Submitting: shows "submitting..." and disables Enter
 *
 * No border — the parent App component provides the unified outer frame.
 * The custom useCursor hook hides the real terminal cursor (CjkTextInput
 * renders its own inline inverse-video cursor indicator).
 */
import React, {useState, useCallback, useEffect, useRef} from 'react';
import {Box, Text} from 'ink';
import CjkTextInput from './CjkTextInput.js';
import useCursor from '../hooks/useCursor.js';
import type {SlashCommand} from '../commands.js';

export interface TaskInputProps {
  onSubmit: (value: string) => void;
  isFocused?: boolean;
  isSubmitting?: boolean;
  inputHistory?: string[];
  historyIndex?: number;
  onHistoryIndexChange?: (index: number) => void;
  isOffline?: boolean;
  commandSuggestions?: SlashCommand[] | null;
  /** Called when the input value changes (for slash command detection). */
  onValueChange?: (value: string) => void;
}

const TaskInput: React.FC<TaskInputProps> = ({
  onSubmit,
  isFocused = true,
  isSubmitting = false,
  inputHistory = [],
  historyIndex = -1,
  onHistoryIndexChange,
  isOffline = false,
  commandSuggestions,
  onValueChange,
}) => {
  const [value, setValue] = useState('');
  const [savedDraft, setSavedDraft] = useState('');
  const [showEmptyHint, setShowEmptyHint] = useState(false);
  const emptyHintTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useCursor(); // manages real terminal cursor visibility (hides it)

  // Cleanup empty hint timer on unmount
  useEffect(() => {
    return () => {
      if (emptyHintTimer.current) clearTimeout(emptyHintTimer.current);
    };
  }, []);

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
      if (isSubmitting) return;
      const trimmed = submittedValue.trim();
      if (trimmed.length === 0) {
        // Show empty hint for 2 seconds
        setShowEmptyHint(true);
        if (emptyHintTimer.current) clearTimeout(emptyHintTimer.current);
        emptyHintTimer.current = setTimeout(() => setShowEmptyHint(false), 2000);
        return;
      }
      onSubmit(trimmed);
      setValue('');
      setSavedDraft('');
    },
    [onSubmit, isSubmitting],
  );

  const handleChange = useCallback(
    (newValue: string) => {
      setValue(newValue);
      onValueChange?.(newValue);
    },
    [onValueChange],
  );

  // Called by CjkTextInput whenever the cursor moves (including arrow keys)
  // Note: setCursorPosition is a no-op — CjkTextInput renders its own
  // inline cursor indicator. This callback is kept for future use if
  // we ever implement real ANSI cursor positioning.
  const handleCursorMove = useCallback(
    (_displayCol: number) => {
      // No-op: CjkTextInput handles cursor display via inline inverse video.
      // Previously tried: setCursorPosition({x: 3 + displayCol, y: 0}) + showCursor()
      // but that caused dual-cursor because setCursorPosition was a no-op
      // and the real cursor didn't align with the fake one.
    },
    [],
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

  // Calculate line/col for multi-line display
  const getLineCol = (): {line: number; col: number} => {
    if (!isMultiline) return {line: 1, col: value.length + 1};
    const lines = value.split('\n');
    return {line: lines.length, col: (lines[lines.length - 1]?.length ?? 0) + 1};
  };

  const {line, col} = getLineCol();

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
        placeholder={isSubmitting ? 'submitting...' : isOffline ? 'offline demo: type task description...' : 'type task description and press Enter...'}
        focus={isFocused}
      />
      {isSubmitting && <Text color="yellow">{' [submitting...]'}</Text>}
      {showEmptyHint && !isSubmitting && (
        <Text dimColor>{' ↵ Enter a task description'}</Text>
      )}
      {isMultiline && !isSubmitting && (
        <Text dimColor>{` Ln${line}:Col${col} │ Enter submit · Ctrl+J newline`}</Text>
      )}
      {historyIndex >= 0 && !isSubmitting && (
        <Text dimColor>{` history ${historyIndex + 1}/${inputHistory.length}`}</Text>
      )}
      {commandSuggestions && commandSuggestions.length > 0 && !isSubmitting && (
        <Text dimColor>{` ${commandSuggestions.map((c) => `/${c.name}`).join(' ')}`}</Text>
      )}
    </Box>
  );
};

export default TaskInput;
