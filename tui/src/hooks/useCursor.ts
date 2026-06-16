/**
 * Custom useCursor hook for IME/CJK support.
 *
 * Ink 5.x does not include a built-in useCursor hook.
 * This implementation provides minimal cursor management:
 * - Hides the real terminal cursor on mount (we use inline fake cursor)
 * - Restores the real terminal cursor on unmount
 * - setCursorPosition is a no-op (CjkTextInput renders its own cursor)
 *
 * Design decision: We hide the real terminal cursor because CjkTextInput
 * renders an inverse-video cursor indicator inline, which is more reliable
 * than trying to position the real cursor (which fights with Ink's render
 * cycle). Having two visible cursors (real + fake) is confusing.
 *
 * For IME composition: most terminals position the candidate window near
 * the last text output position, which is close enough to our inline cursor.
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
  /**
   * No-op. Kept for API compatibility with TaskInput.
   * CjkTextInput renders its own inline cursor indicator.
   */
  setCursorPosition: (pos: CursorPosition) => void;
  /**
   * Show the real terminal cursor.
   * Only used on unmount to restore normal terminal behavior.
   */
  showCursor: () => void;
  /** Hide the real terminal cursor. */
  hideCursor: () => void;
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

  // No-op: we use CjkTextInput's inline cursor indicator instead of
  // positioning the real terminal cursor (which fights with Ink's render).
  const setCursorPosition = useCallback(
    (_pos: CursorPosition) => {
      // Intentionally empty — CjkTextInput handles cursor display.
    },
    [],
  );

  // Hide real cursor on mount (we use fake cursor), restore on unmount
  useEffect(() => {
    hideCursor();
    return () => {
      showCursor();
    };
  }, [showCursor, hideCursor]);

  return {setCursorPosition, showCursor, hideCursor};
}

export default useCursor;
