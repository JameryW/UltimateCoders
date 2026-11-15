import { useEffect, useState } from "react";
import { getStoredToken } from "@/hooks/useAuth";
import {
  knowledgeCountsLine,
  knowledgeEmpty,
  knowledgeGateLine,
  knowledgeOutcomeLine,
  knowledgeOverviewQuery,
  knowledgeStageLine,
} from "@/lib/knowledge";
import type { KnowledgeOverview } from "@/lib/knowledge";

interface KnowledgeOverviewTextProps {
  overview: KnowledgeOverview;
}

/**
 * The overview as text. Pure render -- no fetch, no effects -- so the SSR
 * tests can assert exactly what an auditor sees. Counts only: the payload
 * never carries claim statements, diffs or page bodies, so neither does this.
 */
export function KnowledgeOverviewText({ overview }: KnowledgeOverviewTextProps) {
  if (knowledgeEmpty(overview)) {
    return (
      <p className="text-xs text-[var(--text-muted)]">
        No knowledge records captured for this task. Evidence capture starts at
        knowledge stage `shadow`; recovery views and graph sharing are gated
        stages.
      </p>
    );
  }
  return (
    <div className="space-y-0.5" role="list" aria-label="Knowledge overview">
      <p className="text-[10px] text-[var(--text-muted)]">{knowledgeStageLine(overview)}</p>
      <p className="text-[10px] text-[var(--text-primary)]">{knowledgeCountsLine(overview)}</p>
      <p className="text-[10px] text-[var(--text-muted)]">{knowledgeOutcomeLine(overview)}</p>
      <p className="text-[10px] text-[var(--text-muted)]">{knowledgeGateLine(overview)}</p>
    </div>
  );
}

interface KnowledgeOverviewPanelProps {
  /** Task whose attempt partition this panel reads. */
  taskId: string;
}

/**
 * Task-scoped knowledge read face: attempt outcomes, evidence/claim/page
 * counts and the Epistemic Gate's allowed/denied split with its top denial
 * reasons (`GET /dashboard/api/knowledge`). Read-only; a failed fetch says so
 * instead of inventing counts.
 */
export function KnowledgeOverviewPanel({ taskId }: KnowledgeOverviewPanelProps) {
  const [overview, setOverview] = useState<KnowledgeOverview | null>(null);
  const [failedFor, setFailedFor] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const token = getStoredToken();
    fetch(knowledgeOverviewQuery(taskId, { token }))
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (cancelled) return;
        if (data && typeof data === "object" && !Array.isArray(data)) {
          setOverview(data as KnowledgeOverview);
          setFailedFor(null);
        } else {
          setFailedFor(taskId);
        }
      })
      .catch(() => {
        if (!cancelled) setFailedFor(taskId);
      });
    return () => {
      cancelled = true;
    };
  }, [taskId]);

  const failed = failedFor === taskId;
  const loaded = !failed && overview !== null;

  return (
    <div className="mb-2">
      <p className="text-xs text-[var(--text-secondary)] mb-1">
        Knowledge overview{" "}
        {loaded ? (
          <span className="text-[10px] text-[var(--text-muted)]">counts only</span>
        ) : null}
      </p>
      {failed ? (
        <p className="text-xs text-[var(--text-muted)]">
          Knowledge overview unavailable.
        </p>
      ) : loaded ? (
        <KnowledgeOverviewText overview={overview as KnowledgeOverview} />
      ) : (
        <p className="text-xs text-[var(--text-muted)]">Loading knowledge overview…</p>
      )}
    </div>
  );
}
