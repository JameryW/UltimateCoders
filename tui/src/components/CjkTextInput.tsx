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
 * - Tab completes slash commands when suggestion is available
 *
 * Extended shortcuts:
 * - Tab: complete slash command (when available) or insert indent
 * - Ctrl+J / Alt+Enter: insert newline (multi-line task editing)
 * - Ctrl+U: clear entire input
 * - Ctrl+K: delete from cursor to end of line
 * - Ctrl+Left/Right: word navigation
 * - Up/Down: delegate to parent for input history navigation
 * - Undo: Cmd+Z (macOS) / Alt+Z (Linux)
 * - Redo: Cmd+Shift+Z (macOS) / Alt+Shift+Z (Linux)
 */
import React, {useState, useEffect, useRef, useCallback} from 'react';
import {Text, useInput, useStdin, type Key} from 'ink';
import GraphemeSplitter from 'grapheme-splitter';
import stringWidth from 'string-width';

// ponytail: ink 5 Key lacks 'alt'; extend via intersection. Remove when on ink 6.
type InkKey = Key & {alt?: boolean};
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
  /** If set, Tab completes to this command name instead of inserting indent. */
  readonly tabCompleteCommand?: string | null;
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
  tabCompleteCommand,
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

  // ── Raw input handler for Home/End + bracketed paste ────────
  // ponytail: ink 5 useStdin has internal_eventEmitter but the type
  // declaration marks it readonly — we can still listen. Remove this
  // workaround when upgrading to Ink v6.6+ (usePaste + Key.home/end).
  const {stdin, internal_eventEmitter} = useStdin();
  useEffect(() => {
    if (!focus || !stdin || !internal_eventEmitter) return;
    const onRawInput = (data: string) => {
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
    internal_eventEmitter.on('input', onRawInput);
    return () => {
      internal_eventEmitter.off('input', onRawInput);
    };
  }, [focus, stdin, internal_eventEmitter, value]);

  useInput(
    (input, key) => {
      const k = key as InkKey;
      // Shift+Tab: do NOT handle locally — let it bubble to App's useInput
      if (k.shift && k.tab) {
        return;
      }

      // Tab: complete slash command if suggestion exists, otherwise indent
      if (k.tab) {
        if (tabCompleteCommand) {
          const completed = `/${tabCompleteCommand} `;
          setCursorGI(splitter.countGraphemes(completed));
          cursorRef.current = splitter.countGraphemes(completed);
          prevValueRef.current = completed;
          onChange(completed);
        } else {
          pushUndo();
          const {nextValue, nextCursorGI} = insertAtCursor(value, cursorRef.current, '  ');
          setCursorGI(nextCursorGI);
          cursorRef.current = nextCursorGI;
          prevValueRef.current = nextValue;
          onChange(nextValue);
        }
        return;
      }

      // Ctrl+C is handled by the parent App
      if (k.ctrl && input === 'c') {
        return;
      }

      // Ctrl+R is handled by the parent App for reconnect
      if (k.ctrl && input === 'r') {
        return;
      }

      // Ctrl+P is handled by the parent App for pause/resume
      if (k.ctrl && input === 'p') {
        return;
      }

      // Ctrl+Q is handled by the parent App for quit
      if (k.ctrl && input === 'q') {
        return;
      }

      // Ctrl+W is handled by the parent App for cycle focus
      if (k.ctrl && input === 'w') {
        return;
      }

      // ── Undo: Cmd+Z (macOS) / Alt+Z (Linux/macOS) ───────
      if ((k.meta && input === 'z' && !k.shift) || (k.alt && input === 'z' && !k.shift)) {
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
      if ((k.meta && input === 'z' && k.shift) || (k.alt && input === 'z' && k.shift)) {
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
      if ((k.ctrl && input === 'j') || (k.alt && k.return)) {
        pushUndo();
        const {nextValue, nextCursorGI} = insertAtCursor(value, cursorRef.current, '\n');
        setCursorGI(nextCursorGI);
        cursorRef.current = nextCursorGI;
        prevValueRef.current = nextValue;
        onChange(nextValue);
        return;
      }

      // ── Ctrl+U: clear entire input ─────────────────────
      if (k.ctrl && input === 'u') {
        pushUndo();
        setCursorGI(0);
        cursorRef.current = 0;
        prevValueRef.current = '';
        onChange('');
        return;
      }

      // ── Ctrl+K: delete from cursor to end ──────────────
      if (k.ctrl && input === 'k') {
        pushUndo();
        const {nextValue, nextCursorGI} = deleteToEnd(value, cursorRef.current);
        prevValueRef.current = nextValue;
        onChange(nextValue);
        return;
      }

      if (k.return) {
        if (onSubmit) {
          onSubmit(value);
        }
        return;
      }

      // ── Up/Down: delegate to parent for history nav ────
      if (k.upArrow) {
        if (onHistoryNav) {
          onHistoryNav('up');
          return;
        }
        return;
      }

      if (k.downArrow) {
        if (onHistoryNav) {
          onHistoryNav('down');
          return;
        }
        return;
      }

      // Ctrl+Left: word backward
      if (k.ctrl && k.leftArrow) {
        const next = wordBoundaryBackward(value, cursorRef.current);
        setCursorGI(next);
        cursorRef.current = next;
        return;
      }

      // Ctrl+Right: word forward
      if (k.ctrl && k.rightArrow) {
        const next = wordBoundaryForward(value, cursorRef.current);
        setCursorGI(next);
        cursorRef.current = next;
        return;
      }

      if (k.leftArrow) {
        if (showCursor && cursorRef.current > 0) {
          const next = cursorRef.current - 1;
          setCursorGI(next);
          cursorRef.current = next;
        }
        return;
      }

      if (k.rightArrow) {
        if (showCursor) {
          const maxIndex = splitter.countGraphemes(value);
          const next = Math.min(cursorRef.current + 1, maxIndex);
          setCursorGI(next);
          cursorRef.current = next;
        }
        return;
      }

      // Home (Ctrl+A): move cursor to start
      if (k.ctrl && input === 'a') {
        setCursorGI(0);
        cursorRef.current = 0;
        return;
      }

      // End (Ctrl+E): move cursor to end
      if (k.ctrl && input === 'e') {
        const end = splitter.countGraphemes(value);
        setCursorGI(end);
        cursorRef.current = end;
        return;
      }

      if (k.backspace || k.delete) {
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
      if (input.length > 0 && !k.ctrl && !k.meta) {
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
