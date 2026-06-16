/**
 * Custom useCursor hook for IME/CJK support.
 *
 * Ink 5.x does not include a built-in useCursor hook.
 * This implementation provides minimal cursor management:
 * - Hides the real terminal cursor on mount
 * - Shows it again on unmount
 * - Provides setCursorPosition for IME (but relies on Ink's
 *   natural cursor position rather than fighting it with ANSI)
 *
 * Design decision: We do NOT write ANSI cursor-positioning sequences
 * (\x1B[row;colH) here because Ink manages the terminal cursor during
 * its own render cycle. Writing ANSI sequences that fight with Ink's
 * cursor management causes the sequences to be rendered as text or
 * the cursor to jump to wrong positions.
 *
 * Instead, CjkTextInput renders an inverse-video cursor indicator
 * inline, and the real terminal cursor is hidden while the TUI is
 * active to avoid showing two cursors at different positions.
 */
import {useCallback, useEffect} from 'react';
import {useStdout} from 'ink';

export interface CursorPosition {
  /** Display column (0-based). */
  x: number;
  /**
   * Vertical offset from the bottom of the terminal.
   * 0 = input line, positive = further up.
   */
  y: number;
}

export interface UseCursorReturn {
  /** Show the terminal cursor. */
  showCursor: () => void;
  /** Hide the terminal cursor. */
  hideCursor: () => void;
  /**
   * Request cursor position for IME.
   * Currently a no-op for positioning — Ink manages cursor location.
   * Kept for API compatibility with TaskInput.
   */
  setCursorPosition: (pos: CursorPosition) => void;
}

export function useCursor(): UseCursorReturn {
  const {stdout} = useStdout();

  const showCursor = useCallback(() => {
    if (stdout) {
      stdout.write('\x1B[?25h');
    }
  }, [stdout]);

  const hideCursor = useCallback(() => {
    if (stdout) {
      stdout.write('\x1B[?25l');
    }
  }, [stdout]);

  // No-op: CjkTextInput renders the visible cursor inline. We keep the
  // API for compatibility but don't write ANSI positioning sequences.
  const setCursorPosition = useCallback(
    (_pos: CursorPosition) => {
    },
    [],
  );

  // Hide the real terminal cursor while the TUI is active. The input
  // component renders its own inline cursor and we restore the terminal
  // cursor before exiting.
  useEffect(() => {
    hideCursor();
    return () => {
      showCursor();
    };
  }, [showCursor, hideCursor]);

  return {setCursorPosition, showCursor, hideCursor};
}

export default useCursor;
