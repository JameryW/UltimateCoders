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

// ── Word Boundary Helpers ────────────────────────────────────

/** Whether a grapheme is whitespace. */
function isWhitespace(g: string): boolean {
  return /^\s$/.test(g);
}

/**
 * Whether a grapheme is a CJK ideograph (Han, Hiragana, Katakana, etc.).
 * Each CJK character is its own "word" for navigation purposes.
 */
function isCjk(g: string): boolean {
  const cp = g.codePointAt(0)!;
  return (
    (cp >= 0x4e00 && cp <= 0x9fff)   // CJK Unified Ideographs
    || (cp >= 0x3040 && cp <= 0x30ff) // Hiragana + Katakana
    || (cp >= 0xac00 && cp <= 0xd7af) // Hangul Syllables
    || (cp >= 0x3400 && cp <= 0x4dbf) // CJK Extension A
    || (cp >= 0xf900 && cp <= 0xfaff) // CJK Compatibility Ideographs
  );
}

/** Categorize a grapheme for word-boundary purposes. */
type CharClass = 'space' | 'cjk' | 'word';
function charClass(g: string): CharClass {
  if (isWhitespace(g)) return 'space';
  if (isCjk(g)) return 'cjk';
  return 'word';
}

/**
 * Find the grapheme index of the previous word boundary.
 * Skips whitespace, then stops at the boundary between different char classes.
 * Each CJK character is its own word.
 */
export function wordBoundaryBackward(value: string, cursorGI: number): number {
  if (cursorGI <= 0) return 0;
  const graphemes = splitter.splitGraphemes(value);
  let i = cursorGI - 1;
  // Skip whitespace
  while (i > 0 && isWhitespace(graphemes[i])) i--;
  if (i < 0) return 0;
  // Find boundary: stop when char class changes
  const startClass = charClass(graphemes[i]);
  if (startClass === 'cjk') return i; // CJK: each char is a word
  while (i > 0 && charClass(graphemes[i - 1]) === startClass) i--;
  return i;
}

/**
 * Find the grapheme index of the next word boundary.
 * Skips current run of same char class, then whitespace.
 * Each CJK character is its own word.
 */
export function wordBoundaryForward(value: string, cursorGI: number): number {
  const graphemes = splitter.splitGraphemes(value);
  const max = graphemes.length;
  if (cursorGI >= max) return max;
  let i = cursorGI;
  // If on a CJK char, skip just this one
  if (i < max && charClass(graphemes[i]) === 'cjk') return i + 1;
  // Skip current run of same char class
  const startClass = charClass(graphemes[i]);
  while (i < max && charClass(graphemes[i]) === startClass) i++;
  // Skip whitespace
  while (i < max && isWhitespace(graphemes[i])) i++;
  return i;
}
