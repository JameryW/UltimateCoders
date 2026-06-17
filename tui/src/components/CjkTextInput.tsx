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
 * - Supports undo/redo with platform-aware keybindings
 * - Handles bracketed paste mode
 *
 * Extended shortcuts:
 * - Ctrl+J / Alt+Enter: insert newline (multi-line task editing)
 * - Ctrl+U: clear entire input
 * - Ctrl+K: delete from cursor to end of line
 * - Ctrl+Left/Right: word navigation
 * - Up/Down: delegate to parent for input history navigation
 * - Undo: Cmd+Z (macOS) / Alt+Z (Linux)
 * - Redo: Cmd+Shift+Z (macOS) / Alt+Shift+Z (Linux)
 */
import React, {useState, useEffect, useRef, useCallback} from 'react';
import {Text, useInput, useStdin} from 'ink';
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
  wordBoundaryBackward,
  wordBoundaryForward,
} from '../cjk-input-utils.js';

const splitter = new GraphemeSplitter();

// ── Undo/Redo Types ─────────────────────────────────────────

interface Snapshot {
  value: string;
  cursorGI: number;
}

const UNDO_LIMIT = 50;

// ── Component ───────────────────────────────────────────────

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
  const [cursorGI, setCursorGI] = useState(() => splitter.countGraphemes(value));
  const cursorRef = useRef(cursorGI);

  // Track previous value to detect external changes (e.g. clear on submit)
  const prevValueRef = useRef(value);

  // ── Undo/Redo stacks ──────────────────────────────────────
  const undoStackRef = useRef<Snapshot[]>([]);
  const redoStackRef = useRef<Snapshot[]>([]);

  /** Push current state to undo stack before a mutation. */
  const pushUndo = useCallback(() => {
    undoStackRef.current.push({value, cursorGI: cursorRef.current});
    if (undoStackRef.current.length > UNDO_LIMIT) {
      undoStackRef.current.shift();
    }
    // Any new mutation clears redo
    redoStackRef.current = [];
  }, [value]);

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

  // ── Paste handler (bracketed paste mode) ─────────────────
  // ponytail: usePaste is Ink 6+ only; handle paste via raw stdin for now.
  // When upgrading to Ink 6+, replace with usePaste hook for proper bracketed paste.
  useEffect(() => {
    if (!focus || !rawStdin || !rawEmitter) return;
    const onRawInput = (data: string) => {
      // Bracketed paste: ESC[200~ ... ESC[201~
      // For now, detect Home/End sequences and skip paste detection
      if (data === '\x1b[H' || data === '\x1b[1~') {
        setCursorGI(0);
        cursorRef.current = 0;
      } else if (data === '\x1b[F' || data === '\x1b[4~') {
        const end = splitter.countGraphemes(value);
        setCursorGI(end);
        cursorRef.current = end;
      }
      // TODO: add bracketed paste detection when needed
    };
    rawEmitter.on('input', onRawInput);
    return () => {
      rawEmitter.off('input', onRawInput);
    };
  }, [focus, rawStdin, rawEmitter, value, pushUndo]);

  useInput(
    (input, key) => {
      // Shift+Tab: do NOT handle locally — let it bubble to App's useInput
      if (key.shift && key.tab) {
        return;
      }

      // Tab: insert spaces for indentation
      if (key.tab) {
        pushUndo();
        const {nextValue, nextCursorGI} = insertAtCursor(value, cursorRef.current, '  ');
        setCursorGI(nextCursorGI);
        cursorRef.current = nextCursorGI;
        prevValueRef.current = nextValue;
        onChange(nextValue);
        return;
      }

      // Ctrl+C is handled by the parent App
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

      // ── Undo: Cmd+Z (macOS) / Alt+Z (Linux/macOS) ───────
      if ((key.meta && input === 'z' && !key.shift) || (key.alt && input === 'z' && !key.shift)) {
        const snapshot = undoStackRef.current.pop();
        if (snapshot) {
          redoStackRef.current.push({value, cursorGI: cursorRef.current});
          setCursorGI(snapshot.cursorGI);
          cursorRef.current = snapshot.cursorGI;
          prevValueRef.current = snapshot.value;
          onChange(snapshot.value);
        }
        return;
      }

      // ── Redo: Cmd+Shift+Z (macOS) / Alt+Shift+Z (Linux/macOS) ──
      if ((key.meta && input === 'z' && key.shift) || (key.alt && input === 'z' && key.shift)) {
        const snapshot = redoStackRef.current.pop();
        if (snapshot) {
          undoStackRef.current.push({value, cursorGI: cursorRef.current});
          setCursorGI(snapshot.cursorGI);
          cursorRef.current = snapshot.cursorGI;
          prevValueRef.current = snapshot.value;
          onChange(snapshot.value);
        }
        return;
      }

      // ── Ctrl+J / Alt+Enter: insert newline (multi-line task) ──
      if ((key.ctrl && input === 'j') || (key.alt && key.return)) {
        pushUndo();
        const {nextValue, nextCursorGI} = insertAtCursor(value, cursorRef.current, '\n');
        setCursorGI(nextCursorGI);
        cursorRef.current = nextCursorGI;
        prevValueRef.current = nextValue;
        onChange(nextValue);
        return;
      }

      // ── Ctrl+U: clear entire input ─────────────────────
      if (key.ctrl && input === 'u') {
        pushUndo();
        setCursorGI(0);
        cursorRef.current = 0;
        prevValueRef.current = '';
        onChange('');
        return;
      }

      // ── Ctrl+K: delete from cursor to end ──────────────
      if (key.ctrl && input === 'k') {
        pushUndo();
        const {nextValue, nextCursorGI} = deleteToEnd(value, cursorRef.current);
        prevValueRef.current = nextValue;
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
        return;
      }

      if (key.downArrow) {
        if (onHistoryNav) {
          onHistoryNav('down');
          return;
        }
        return;
      }

      // Ctrl+Left: word backward
      if (key.ctrl && key.leftArrow) {
        const next = wordBoundaryBackward(value, cursorRef.current);
        setCursorGI(next);
        cursorRef.current = next;
        return;
      }

      // Ctrl+Right: word forward
      if (key.ctrl && key.rightArrow) {
        const next = wordBoundaryForward(value, cursorRef.current);
        setCursorGI(next);
        cursorRef.current = next;
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
        pushUndo();
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
        pushUndo();
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
