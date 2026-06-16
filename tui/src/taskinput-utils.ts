/**
 * Pure functions extracted from TaskInput component for testability.
 *
 * Line/column calculation, history navigation, and submit validation.
 */

// ── Line/Column Calculation ──────────────────────────────────

export interface LineCol {
  line: number;
  col: number;
}

/**
 * Calculate line number and column for multi-line input display.
 *
 * For single-line input: returns line=1, col=string.length+1.
 * For multi-line input: splits on \n, returns last line's length+1.
 */
export function getLineCol(value: string): LineCol {
  if (!value.includes('\n')) return {line: 1, col: value.length + 1};
  const lines = value.split('\n');
  return {
    line: lines.length,
    col: (lines[lines.length - 1]?.length ?? 0) + 1,
  };
}

// ── Submit Validation ────────────────────────────────────────

/**
 * Validate and trim a submitted value.
 *
 * Returns the trimmed string if non-empty, or null if the value
 * is empty/whitespace-only (indicating an invalid submission).
 */
export function validateAndTrimSubmit(value: string): string | null {
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

// ── History Navigation ───────────────────────────────────────

export interface HistoryNavResult {
  nextIndex: number;
  /** Value to display after navigation. */
  nextValue: string;
  /** Draft to save (only set when first entering history). */
  draftToSave: string | null;
}

/**
 * Compute the next history navigation state.
 *
 * @param direction - 'up' or 'down'
 * @param historyIndex - Current history index (-1 = not browsing)
 * @param historyLength - Number of history entries
 * @param currentValue - Current input value
 * @param savedDraft - Previously saved draft
 */
export function navigateHistory(
  direction: 'up' | 'down',
  historyIndex: number,
  historyLength: number,
  currentValue: string,
  savedDraft: string,
): HistoryNavResult {
  if (historyLength === 0) {
    return {nextIndex: historyIndex, nextValue: currentValue, draftToSave: null};
  }

  if (direction === 'up') {
    const next = historyIndex < 0
      ? 0
      : Math.min(historyIndex + 1, historyLength - 1);
    const draftToSave = historyIndex < 0 ? currentValue : null;
    return {nextIndex: next, nextValue: '', draftToSave};
  }

  // direction === 'down'
  if (historyIndex <= 0) {
    // Exit history, restore draft
    return {nextIndex: -1, nextValue: savedDraft, draftToSave: null};
  }
  return {nextIndex: historyIndex - 1, nextValue: '', draftToSave: null};
}
