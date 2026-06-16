/**
 * Pure functions extracted from CjkTextInput for testability.
 *
 * These functions handle grapheme-aware text editing operations:
 * - Insertion at cursor position
 * - Backward deletion (backspace)
 * - Delete-to-end (Ctrl+K)
 * - Cursor rendering with inverse-video indicator
 * - ANSI escape helpers for cursor display
 */

import GraphemeSplitter from 'grapheme-splitter';
import stringWidth from 'string-width';

const splitter = new GraphemeSplitter();

// ── ANSI Escape Helpers ──────────────────────────────────────

/** Apply inverse video to a character (or grapheme cluster). */
export function inverseChar(char: string): string {
  return `\x1B[7m${char}\x1B[27m`;
}

/** Apply dim styling to text. */
export function dimText(text: string): string {
  return `\x1B[2m${text}\x1B[22m`;
}

// ── Grapheme Editing Operations ──────────────────────────────

export interface EditResult {
  nextValue: string;
  nextCursorGI: number;
}

/** Insert input string at the given grapheme index. */
export function insertAtCursor(
  value: string,
  cursorGI: number,
  input: string,
): EditResult {
  const graphemes = splitter.splitGraphemes(value);
  const inputGraphemes = splitter.splitGraphemes(input);
  const nextValue = [
    ...graphemes.slice(0, cursorGI),
    ...inputGraphemes,
    ...graphemes.slice(cursorGI),
  ].join('');
  return {nextValue, nextCursorGI: cursorGI + inputGraphemes.length};
}

/**
 * Delete the grapheme before the cursor (backward delete).
 * Returns null if there is nothing to delete (cursor at position 0).
 */
export function deleteBackward(
  value: string,
  cursorGI: number,
): EditResult | null {
  if (cursorGI <= 0) return null;
  const graphemes = splitter.splitGraphemes(value);
  const nextValue = [
    ...graphemes.slice(0, cursorGI - 1),
    ...graphemes.slice(cursorGI),
  ].join('');
  return {nextValue, nextCursorGI: cursorGI - 1};
}

/**
 * Delete all graphemes from cursor position to end of value (Ctrl+K).
 */
export function deleteToEnd(value: string, cursorGI: number): EditResult {
  const graphemes = splitter.splitGraphemes(value);
  const nextValue = graphemes.slice(0, cursorGI).join('');
  // cursor stays at same position
  return {nextValue, nextCursorGI: cursorGI};
}

// ── Cursor Rendering ─────────────────────────────────────────

/**
 * Build the rendered text string with a visible cursor indicator.
 *
 * The cursor is shown as inverse-video on the grapheme at the cursor
 * position. When the cursor is at the end, an inverse-video space block
 * is appended.
 */
export function renderInputWithCursor(
  value: string,
  cursorGI: number,
  showCursor: boolean,
  focus: boolean,
  placeholder: string,
): string {
  const graphemes = splitter.splitGraphemes(value);
  const hasValue = value.length > 0;

  if (!hasValue && placeholder) {
    if (showCursor && focus) {
      const placeholderGraphemes = splitter.splitGraphemes(placeholder);
      const firstGrapheme = placeholderGraphemes[0] ?? ' ';
      const rest = placeholderGraphemes.slice(1).join('');
      return inverseChar(firstGrapheme) + dimText(rest);
    }
    return dimText(placeholder);
  }

  if (hasValue) {
    if (showCursor && focus) {
      let rendered = '';
      for (let i = 0; i < graphemes.length; i++) {
        if (i === cursorGI) {
          rendered += inverseChar(graphemes[i]);
        } else {
          rendered += graphemes[i];
        }
      }
      if (cursorGI >= graphemes.length) {
        rendered += inverseChar(' ');
      }
      return rendered;
    }
    return value;
  }

  if (showCursor && focus) {
    return inverseChar(' ');
  }

  return '';
}

/**
 * Compute the display column (terminal width) for a cursor position.
 * Returns the number of terminal columns occupied by the graphemes
 * before the cursor.
 */
export function cursorDisplayCol(value: string, cursorGI: number): number {
  const graphemes = splitter.splitGraphemes(value);
  const textBeforeCursor = graphemes.slice(0, cursorGI).join('');
  return stringWidth(textBeforeCursor);
}
