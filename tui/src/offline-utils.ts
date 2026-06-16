/**
 * Pure functions extracted from App component's offline simulation.
 *
 * Description decomposition and subtask description cleaning.
 */

/**
 * Decompose a task description into subtask lines.
 *
 * Splits by newlines, trims, filters empty lines.
 * If only one non-empty line results, duplicates it so there
 * are always at least 2 "subtasks" for the offline simulation.
 */
export function decomposeDescription(description: string): string[] {
  const lines = description
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l.length > 0);

  if (lines.length <= 1) {
    lines.push(description.trim());
  }
  return lines;
}

/**
 * Strip leading number prefixes from subtask descriptions.
 *
 * "1. Fix bug" → "Fix bug"
 * "2) Write tests" → "Write tests"
 * "10. Deploy" → "Deploy"
 * "No number" → "No number" (unchanged)
 */
export function cleanSubtaskDescription(line: string): string {
  const cleaned = line.replace(/^\d+[\.\)]\s*/, '').trim();
  return cleaned || line;
}
