import { useEffect, useState } from "react";
import { APPROVAL_PAGE_LIMIT, fetchApprovals } from "@/lib/approvalApi";
import { ATTENTION_PAGE_LIMIT, fetchAttention } from "@/lib/attentionApi";
import {
  countAttention,
  countPendingApprovals,
  shouldPollCounts,
  type GovernanceCounts,
} from "@/lib/governanceCounts";

/** 20s: a human queue does not move faster than people do, and this runs forever. */
export const DEFAULT_COUNT_INTERVAL_MS = 20_000;

/** Never poll faster than this, whatever a caller asks for. */
const MIN_INTERVAL_MS = 5_000;

export interface UseGovernanceCountsOptions {
  /** Poll only while the backend is reachable; an offline shell shows nothing. */
  connected?: boolean;
  intervalMs?: number;
}

/**
 * The two numbers a person must not have to go looking for.
 *
 * Returns `null` when either read failed — the footer then shows nothing rather
 * than a confident `0`. Both calls are cheap list reads against the local bridge,
 * and the panels keep their own fetch loops; this hook never mutates anything.
 */
export function useGovernanceCounts(
  options: UseGovernanceCountsOptions = {},
): GovernanceCounts | null {
  const { connected = true, intervalMs = DEFAULT_COUNT_INTERVAL_MS } = options;
  const [counts, setCounts] = useState<GovernanceCounts | null>(null);

  useEffect(() => {
    // No synchronous setState here: while disconnected the derived return value
    // below is already `null`, so the effect can simply not run.
    if (!shouldPollCounts(connected)) return;
    let cancelled = false;

    const poll = async (): Promise<void> => {
      const [attention, approvals] = await Promise.all([
        fetchAttention(ATTENTION_PAGE_LIMIT),
        fetchApprovals("pending", APPROVAL_PAGE_LIMIT),
      ]);
      if (cancelled) return;
      if (!attention.ok || !approvals.ok) {
        setCounts(null);
        return;
      }
      setCounts({
        attention: countAttention(attention.data),
        approvals: countPendingApprovals(approvals.data),
      });
    };

    void poll();
    const timer = window.setInterval(
      () => void poll(),
      Math.max(MIN_INTERVAL_MS, intervalMs),
    );
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [connected, intervalMs]);

  // Derived, not stored: a stale count from before a disconnect must not leak
  // back into the footer when the shell comes back up mid-poll.
  return shouldPollCounts(connected) ? counts : null;
}
