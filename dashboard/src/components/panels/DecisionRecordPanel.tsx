import { useEffect, useState } from "react";
import { getStoredToken } from "@/hooks/useAuth";
import {
  decisionCountLine,
  decisionDigestLabel,
  decisionHeadline,
  decisionRecordedAt,
  decisionRouteLine,
  decisionTrailQuery,
  decisionsFromPayload,
  parseDecisionKey,
  workspaceLine,
} from "@/lib/decisions";
import type { DecisionRecord } from "@/lib/decisions";

interface DecisionRecordListProps {
  records: DecisionRecord[];
  emptyLabel?: string;
}

/**
 * The attempt trail as text. Pure render — no fetch, no effects — so the SSR
 * tests can check what an auditor actually sees. Rows keep the order the
 * endpoint returned: the backend owns its key window, and re-sorting here
 * would invent a second order.
 *
 * Each row shows the terminal verdict, the route the attempt took, a short
 * context pointer, and the local effects P7 recorded (changed paths + tool
 * names). None of those fields can carry prompt content or a diff.
 */
export function DecisionRecordList({ records, emptyLabel }: DecisionRecordListProps) {
  if (records.length === 0) {
    return (
      <p className="text-xs text-[var(--text-muted)]">
        {emptyLabel ?? "No dispatch attempts recorded for this task."}
      </p>
    );
  }
  return (
    <div className="space-y-0.5" role="list" aria-label="Decision record trail">
      {records.map((record) => {
        const { subtaskId, attempt } = parseDecisionKey(record.key);
        const at = decisionRecordedAt(record.terminal_at ?? record.recorded_at);
        const meta = `${at ? `${at} · ` : ""}${subtaskId || "?"} · attempt ${attempt}`;
        const route = decisionRouteLine(record.route);
        const digest = decisionDigestLabel(record.context_digest);
        const effects = workspaceLine(record.terminal?.workspace);
        const details = [route, digest].filter(Boolean).join(" · ");
        return (
          <div
            key={record.key}
            role="listitem"
            className="border-l-2 border-l-violet-500/40 pl-2 py-0.5"
          >
            <div className="flex items-center gap-1.5">
              <span className="font-mono text-[10px] text-[var(--text-muted)]">{meta}</span>
              <span className="text-[10px] text-[var(--text-primary)] truncate">
                {decisionHeadline(record)}
              </span>
            </div>
            {details && (
              <div
                className="text-[10px] text-[var(--text-muted)] truncate"
                title={details}
              >
                {details}
              </div>
            )}
            {effects && (
              <div
                className="text-[10px] text-[var(--text-muted)] truncate"
                title={effects}
              >
                {effects}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

interface DecisionRecordTrailProps {
  taskId: string;
}

interface LoadedTrail {
  taskId: string;
  records: DecisionRecord[];
  count: number;
  total: number;
}

/**
 * Task-scoped attempt trail. The endpoint requires `task_id` and takes a
 * `limit`; there is no cursor on this route, so the panel asks for one bounded
 * window and says so when `total` is larger than what it holds — it never
 * claims the window is the whole trail.
 *
 * A task with no attempts shows that, rather than an error: "nothing
 * recorded" is the fact here. A failed request keeps no stale rows: the
 * loaded state is only valid for the task it was fetched for.
 */
export function DecisionRecordTrail({ taskId }: DecisionRecordTrailProps) {
  const [loaded, setLoaded] = useState<LoadedTrail | null>(null);
  const [failedFor, setFailedFor] = useState<string | null>(null);

  const trail = loaded?.taskId === taskId ? loaded : null;
  const failed = failedFor === taskId;

  useEffect(() => {
    let cancelled = false;
    const token = getStoredToken();
    fetch(`${decisionTrailQuery(taskId)}${token ? `&token=${encodeURIComponent(token)}` : ""}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (cancelled) return;
        if (data && Array.isArray(data.decisions)) {
          const { records, count, total } = decisionsFromPayload(data);
          setLoaded({ taskId, records, count, total });
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

  return (
    <div className="mb-2">
      <p className="text-xs text-[var(--text-secondary)] mb-1">
        Decision records{" "}
        {trail ? (
          <span className="text-[10px] text-[var(--text-muted)]">
            {decisionCountLine(trail.count, trail.total)}
          </span>
        ) : null}
      </p>
      {failed ? (
        <p className="text-xs text-[var(--text-muted)]">
          Decision records unavailable.
        </p>
      ) : trail ? (
        <DecisionRecordList records={trail.records} />
      ) : (
        <p className="text-xs text-[var(--text-muted)]">Loading…</p>
      )}
    </div>
  );
}
