import { useEffect, useState } from "react";
import { getStoredToken } from "@/hooks/useAuth";
import {
  appendDecisionPage,
  decisionCountLine,
  decisionDigestLabel,
  decisionHeadline,
  decisionRecordedAt,
  decisionRouteLine,
  decisionTrailQuery,
  parseDecisionKey,
  workspaceLine,
} from "@/lib/decisions";
import type { DecisionRecord, DecisionTrailState } from "@/lib/decisions";

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
  trail: DecisionTrailState;
}

/**
 * Task-scoped attempt trail. The endpoint requires `task_id`; it answers the
 * `order=recent` time window (newest first) and pages with `before` — the
 * previous page's `next_cursor` — so a long trail is reachable instead of
 * truncated. The panel appends pages deduped by key and keeps the rows it
 * already holds when a page fails.
 *
 * A task with no attempts shows that, rather than an error: "nothing
 * recorded" is the fact here. A failed first request keeps no stale rows: the
 * loaded state is only valid for the task it was fetched for.
 */
export function DecisionRecordTrail({ taskId }: DecisionRecordTrailProps) {
  const [loaded, setLoaded] = useState<LoadedTrail | null>(null);
  const [failedFor, setFailedFor] = useState<string | null>(null);
  const [moreState, setMoreState] = useState<{
    taskId: string;
    busy: boolean;
    failed: boolean;
  } | null>(null);

  const trail = loaded?.taskId === taskId ? loaded.trail : null;
  const failed = failedFor === taskId;
  const more = moreState?.taskId === taskId ? moreState : null;
  const loadingMore = more?.busy ?? false;
  const moreFailed = more?.failed ?? false;

  useEffect(() => {
    let cancelled = false;
    const token = getStoredToken();
    fetch(`${decisionTrailQuery(taskId)}${token ? `&token=${encodeURIComponent(token)}` : ""}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (cancelled) return;
        if (data && Array.isArray(data.decisions)) {
          setLoaded({ taskId, trail: appendDecisionPage({ records: [], total: 0 }, data) });
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

  const loadMore = () => {
    const cursor = trail?.nextCursor;
    if (!cursor) return;
    const token = getStoredToken();
    setMoreState({ taskId, busy: true, failed: false });
    fetch(
      `${decisionTrailQuery(taskId, { before: cursor })}${
        token ? `&token=${encodeURIComponent(token)}` : ""
      }`,
    )
      .then((res) => (res.ok ? res.json() : null))
      .then((page) => {
        if (!page || !Array.isArray(page.decisions)) {
          setMoreState({ taskId, busy: false, failed: true });
          return;
        }
        setLoaded((current) =>
          current && current.taskId === taskId
            ? { taskId, trail: appendDecisionPage(current.trail, page) }
            : current,
        );
        setMoreState({ taskId, busy: false, failed: false });
      })
      .catch(() => setMoreState({ taskId, busy: false, failed: true }));
  };

  return (
    <div className="mb-2">
      <p className="text-xs text-[var(--text-secondary)] mb-1">
        Decision records{" "}
        {trail ? (
          <span className="text-[10px] text-[var(--text-muted)]">
            {decisionCountLine(trail.records.length, trail.total)}
          </span>
        ) : null}
      </p>
      {failed ? (
        <p className="text-xs text-[var(--text-muted)]">
          Decision records unavailable.
        </p>
      ) : trail ? (
        <>
          <DecisionRecordList records={trail.records} />
          {trail.nextCursor ? (
            <button
              type="button"
              onClick={loadMore}
              disabled={loadingMore}
              className="mt-1 text-[10px] text-[var(--text-muted)] underline disabled:opacity-50"
            >
              {loadingMore ? "Loading…" : moreFailed ? "Retry older attempts" : "Load older attempts"}
            </button>
          ) : null}
        </>
      ) : (
        <p className="text-xs text-[var(--text-muted)]">Loading…</p>
      )}
    </div>
  );
}
