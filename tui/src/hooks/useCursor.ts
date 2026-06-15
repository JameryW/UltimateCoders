/**
 * Custom useCursor hook for IME/CJK support.
 *
 * Ink 5.x does not include a built-in useCursor hook.
 * This implementation provides the same functionality:
 * - Shows the real terminal cursor (not Ink's virtual one)
 * - Positions the cursor at the specified location for IME composition
 *
 * The key insight from the Ink research: IME composition windows
 * appear at the OS-level terminal cursor position. Ink normally
 * hides the real cursor and draws its own output. For CJK/IME
 * input to work, we must:
 * 1. Show the real cursor using ANSI escape \x1B[?25h
 * 2. Position it at the input field location
 *
 * Cursor positioning strategy:
 * - y=0 means the input is at the bottom of the TUI frame
 *   (2 rows from the terminal bottom: input + status bar)
 * - y>0 means the input is offset further up (e.g., multi-line input)
 * - x is the display column within the input field
 *
 * This hook uses useStdout to write ANSI escape sequences directly.
 */
import {useCallback, useEffect, useRef} from 'react';
import {useStdout} from 'ink';

export interface CursorPosition {
  /** Display column (0-based). */
  x: number;
  /**
   * Vertical offset from the bottom of the terminal.
   * 0 = input is at the second-to-last row (above status bar).
   * Positive = further up.
   */
  y: number;
}

export interface UseCursorReturn {
  setCursorPosition: (pos: CursorPosition) => void;
  showCursor: () => void;
  hideCursor: () => void;
}

/** Rows from terminal bottom for the status bar + border. */
const BOTTOM_RESERVED = 2;

export function useCursor(): UseCursorReturn {
  const {stdout} = useStdout();
  const positionRef = useRef<CursorPosition>({x: 0, y: 0});

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

  const setCursorPosition = useCallback(
    (pos: CursorPosition) => {
      positionRef.current = pos;
      if (stdout) {
        // Position cursor: ESC [ row ; col H (1-based)
        // Row calculation: count from terminal bottom, accounting for
        // status bar and border lines.
        // pos.y=0 → input line (2nd from bottom)
        // pos.y=1 → one line above input (multi-line)
        const row = (stdout.rows || 24) - BOTTOM_RESERVED - pos.y;
        const col = pos.x + 1; // 1-based column
        stdout.write(`\x1B[${row};${col}H`);
        showCursor();
      }
    },
    [stdout, showCursor],
  );

  // Show cursor on mount, hide on unmount
  useEffect(() => {
    showCursor();
    return () => {
      hideCursor();
    };
  }, [showCursor, hideCursor]);

  return {setCursorPosition, showCursor, hideCursor};
}

export default useCursor;
