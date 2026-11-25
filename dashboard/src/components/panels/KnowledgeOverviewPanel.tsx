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
      <p role="listitem" className="text-[10px] text-[var(--text-muted)]">{knowledgeStageLine(overview)}</p>
      <p role="listitem" className="text-[10px] text-[var(--text-primary)]">{knowledgeCountsLine(overview)}</p>
      <p role="listitem" className="text-[10px] text-[var(--text-muted)]">{knowledgeOutcomeLine(overview)}</p>
      <p role="listitem" className="text-[10px] text-[var(--text-muted)]">{knowledgeGateLine(overview)}</p>
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
  const [loadedState, setLoadedState] = useState<{ taskId: string; overview: KnowledgeOverview } | null>(null);
  const [failedFor, setFailedFor] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const token = getStoredToken();
    const headers: Record<string, string> = { Accept: "application/json" };
    if (token) headers.Authorization = `Bearer ${token}`;

    fetch(knowledgeOverviewQuery(taskId), { headers, signal: controller.signal })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (controller.signal.aborted) return;
        if (data && typeof data === "object" && !Array.isArray(data)) {
          setLoadedState({ taskId, overview: data as KnowledgeOverview });
          setFailedFor(null);
        } else {
          setFailedFor(taskId);
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) setFailedFor(taskId);
      });
    return () => {
      controller.abort();
    };
  }, [taskId]);

  const failed = failedFor === taskId;
  const overview = !failed && loadedState?.taskId === taskId ? loadedState.overview : null;
  const loaded = overview !== null;

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
        <KnowledgeOverviewText overview={overview} />
      ) : (
        <p className="text-xs text-[var(--text-muted)]">Loading knowledge overview…</p>
      )}
    </div>
  );
}
