/**
 * Governance counters for the shell's status line: "待办 2 · 审批 1".
 *
 * Why this exists rather than leaving the counts inside the two panels: a pending
 * approval and an unanswered `needs_human` are the only governance facts that
 * require a *human*, and a person should not have to open a tab to discover them.
 * Nothing pops up, nothing steals focus — the number appears in the footer, and
 * the existing key bar already says which key opens the worklist.
 *
 * Two rules keep it honest:
 *
 * * `null` means **unknown** (the bridge is unreachable or the payload was
 *   malformed) and renders as nothing at all. Showing `0` when the gateway is
 *   down would be a lie in the one place a human looks for "is anything waiting
 *   for me?".
 * * Only things a human can act on are counted: attention items that are not
 *   resolved, and approvals still `pending` a decision. An *approved* permit is
 *   deliberately not counted — it is the agent's to spend, and counting it would
 *   tell a person to act on work that is already unblocked.
 */

import { type Approval, isPending } from "./approvalApi.ts";
import { type AttentionItem, type AttentionSnapshot } from "./attentionApi.ts";

export interface GovernanceCounts {
  attention: number;
  approvals: number;
}

export function emptyGovernanceCounts(): GovernanceCounts {
  return { attention: 0, approvals: 0 };
}

/** Unresolved attention items. The snapshot already excludes resolved ones. */
export function countAttention(snapshot: AttentionSnapshot | null | undefined): number {
  if (!snapshot || !Array.isArray(snapshot.items)) return 0;
  return snapshot.items.filter((item: AttentionItem) => Boolean(item.id)).length;
}

/**
 * Approvals still waiting for a human decision.
 *
 * Not `isUsable`: that means "approved and unexpired", which is the agent's
 * permission to spend, not a person's task.
 */
export function countPendingApprovals(
  approvals: readonly Approval[] | null | undefined,
): number {
  if (!Array.isArray(approvals)) return 0;
  return approvals.filter((approval) => isPending(approval)).length;
}

export function mergeGovernanceCounts(
  ...parts: readonly (GovernanceCounts | null)[]
): GovernanceCounts | null {
  if (parts.some((part) => part === null)) return null;
  const total = emptyGovernanceCounts();
  for (const part of parts) {
    if (!part) continue;
    total.attention += part.attention;
    total.approvals += part.approvals;
  }
  return total;
}

/**
 * The footer text. `null` (unknown) and "nothing waiting" both render as "", so the
 * status line does not grow a `0 · 0` that means nothing to a reader.
 */
export function formatGovernanceCounts(
  counts: GovernanceCounts | null | undefined,
): string {
  if (!counts) return "";
  const parts: string[] = [];
  if (counts.attention > 0) parts.push(`待办 ${counts.attention}`);
  if (counts.approvals > 0) parts.push(`审批 ${counts.approvals}`);
  return parts.join(" · ");
}

/** True when the counters should be polled at all (offline dashboards stay quiet). */
export function shouldPollCounts(connected: boolean): boolean {
  return connected;
}
