/**
 * CJK-aware text input component for Ink.
 *
 * Replaces ink-text-input which uses JS string length (code units) for
 * cursor positioning. CJK characters occupy 2 terminal columns each but
 * have length 1, causing misaligned cursor, broken IME composition, and
 * incorrect arrow/backspace behavior.
 *
 * This component:
 * - Uses `string-width` for all display width calculations
 * - Uses `GraphemeSplitter` for proper grapheme cluster handling
 * - Moves cursor by grapheme index (1 step = 1 grapheme, regardless of width)
 * - Deletes whole grapheme clusters on backspace/delete
 * - Positions the real terminal cursor correctly for IME via onCursorMove
 *
 * Extended shortcuts:
 * - Ctrl+J: insert newline (multi-line task editing)
 * - Ctrl+U: clear entire input
 * - Ctrl+K: delete from cursor to end of line
 * - Up/Down: delegate to parent for input history navigation
 */
import React, {useState, useEffect, useRef} from 'react';
import {Text, useInput} from 'ink';
import GraphemeSplitter from 'grapheme-splitter';
import stringWidth from 'string-width';
import {
  inverseChar,
  dimText,
  insertAtCursor,
  deleteBackward,
  deleteToEnd,
  renderInputWithCursor,
  cursorDisplayCol,
} from '../cjk-input-utils.js';

const splitter = new GraphemeSplitter();

export interface CjkTextInputProps {
  /** Current input value (controlled). */
  readonly value: string;
  /** Called when the value changes. */
  readonly onChange: (value: string) => void;
  /** Called when Enter is pressed. */
  readonly onSubmit?: (value: string) => void;
  /** Placeholder text when value is empty. */
  readonly placeholder?: string;
  /** Whether the input is focused and receives key events. */
  readonly focus?: boolean;
  /** Whether to show the cursor indicator. */
  readonly showCursor?: boolean;
  /** Callback to position the real terminal cursor (for IME). Receives display column. */
  readonly onCursorMove?: (displayCol: number) => void;
  /** Callback for Up/Down history navigation. */
  readonly onHistoryNav?: (direction: 'up' | 'down') => void;
}

/**
 * CJK-aware text input component.
 *
 * Maintains cursor position as a grapheme index internally, but converts
 * to display width for rendering and real cursor positioning.
 *
 * Uses a ref for cursor index so useInput callbacks always see the latest
 * value without stale closure issues.
 */
const CjkTextInput: React.FC<CjkTextInputProps> = ({
  value,
  onChange,
  onSubmit,
  placeholder = '',
  focus = true,
  showCursor = true,
  onCursorMove,
  onHistoryNav,
}) => {
  // Cursor position as grapheme index (0 = before first, N = after last).
  // Kept in both state (for re-render) and ref (for non-stale useInput callbacks).
  const [cursorGI, setCursorGI] = useState(() => splitter.countGraphemes(value));
  const cursorRef = useRef(cursorGI);

  // Track previous value to detect external changes (e.g. clear on submit)
  const prevValueRef = useRef(value);

  // Keep ref in sync with state
  useEffect(() => {
    cursorRef.current = cursorGI;
  }, [cursorGI]);

  // When value changes externally, reset cursor to end
  useEffect(() => {
    if (value !== prevValueRef.current) {
      const end = splitter.countGraphemes(value);
      setCursorGI(end);
      cursorRef.current = end;
      prevValueRef.current = value;
    }
  }, [value]);

  // Notify parent of cursor display position
  useEffect(() => {
    if (onCursorMove) {
      onCursorMove(cursorDisplayCol(value, cursorGI));
    }
  }, [cursorGI, value, onCursorMove]);

  // Clamp cursor when value shrinks
  useEffect(() => {
    const maxIndex = splitter.countGraphemes(value);
    if (cursorGI > maxIndex) {
      setCursorGI(maxIndex);
      cursorRef.current = maxIndex;
    }
  }, [value, cursorGI]);

  useInput(
    (input, key) => {
      // Shift+Tab: do NOT handle locally — let it bubble to App's useInput
      // which dispatches CYCLE_FOCUS. Calling onShiftTab here would cause
      // a double dispatch since both useInput handlers fire on the same keypress.
      if (key.shift && key.tab) {
        return;
      }

      // Tab: insert spaces for indentation
      if (key.tab) {
        const {nextValue, nextCursorGI} = insertAtCursor(value, cursorRef.current, '  ');
        setCursorGI(nextCursorGI);
        cursorRef.current = nextCursorGI;
        prevValueRef.current = nextValue;
        onChange(nextValue);
        return;
      }

      // Ctrl+C is handled by the parent App, not by the input
      if (key.ctrl && input === 'c') {
        return;
      }

      // Ctrl+R is handled by the parent App for reconnect
      if (key.ctrl && input === 'r') {
        return;
      }

      // Ctrl+P is handled by the parent App for pause/resume
      if (key.ctrl && input === 'p') {
        return;
      }

      // Ctrl+Q is handled by the parent App for quit
      if (key.ctrl && input === 'q') {
        return;
      }

      // ── Ctrl+J: insert newline (multi-line task) ───────
      if (key.ctrl && input === 'j') {
        const {nextValue, nextCursorGI} = insertAtCursor(value, cursorRef.current, '\n');
        setCursorGI(nextCursorGI);
        cursorRef.current = nextCursorGI;
        prevValueRef.current = nextValue;
        onChange(nextValue);
        return;
      }

      // ── Ctrl+U: clear entire input ─────────────────────
      if (key.ctrl && input === 'u') {
        setCursorGI(0);
        cursorRef.current = 0;
        prevValueRef.current = '';
        onChange('');
        return;
      }

      // ── Ctrl+K: delete from cursor to end ──────────────
      if (key.ctrl && input === 'k') {
        const {nextValue, nextCursorGI} = deleteToEnd(value, cursorRef.current);
        prevValueRef.current = nextValue;
        // cursor stays at same position (nextCursorGI === cursorRef.current)
        onChange(nextValue);
        return;
      }

      if (key.return) {
        if (onSubmit) {
          onSubmit(value);
        }
        return;
      }

      // ── Up/Down: delegate to parent for history nav ────
      if (key.upArrow) {
        if (onHistoryNav) {
          onHistoryNav('up');
          return;
        }
        // Without history nav, ignore
        return;
      }

      if (key.downArrow) {
        if (onHistoryNav) {
          onHistoryNav('down');
          return;
        }
        return;
      }

      if (key.leftArrow) {
        if (showCursor && cursorRef.current > 0) {
          const next = cursorRef.current - 1;
          setCursorGI(next);
          cursorRef.current = next;
        }
        return;
      }

      if (key.rightArrow) {
        if (showCursor) {
          const maxIndex = splitter.countGraphemes(value);
          const next = Math.min(cursorRef.current + 1, maxIndex);
          setCursorGI(next);
          cursorRef.current = next;
        }
        return;
      }

      // Home (Ctrl+A): move cursor to start
      if (key.ctrl && input === 'a') {
        setCursorGI(0);
        cursorRef.current = 0;
        return;
      }

      // End (Ctrl+E): move cursor to end
      if (key.ctrl && input === 'e') {
        const end = splitter.countGraphemes(value);
        setCursorGI(end);
        cursorRef.current = end;
        return;
      }

      if (key.backspace || key.delete) {
        if (value.length === 0) return;
        const result = deleteBackward(value, cursorRef.current);
        if (result) {
          setCursorGI(result.nextCursorGI);
          cursorRef.current = result.nextCursorGI;
          prevValueRef.current = result.nextValue;
          onChange(result.nextValue);
        }
        return;
      }

      // Printable input: insert at cursor position
      if (input.length > 0 && !key.ctrl && !key.meta) {
        const {nextValue, nextCursorGI} = insertAtCursor(value, cursorRef.current, input);
        setCursorGI(nextCursorGI);
        cursorRef.current = nextCursorGI;
        prevValueRef.current = nextValue;
        onChange(nextValue);
      }
    },
    {isActive: focus},
  );

  // ── Rendering ──────────────────────────────────────────────

  const rendered = renderInputWithCursor(value, cursorGI, showCursor, focus, placeholder);

  return <Text>{rendered}</Text>;
};

export default CjkTextInput;
