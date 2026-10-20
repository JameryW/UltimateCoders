import { useEffect, useState } from "react";
import { getStoredToken } from "@/hooks/useAuth";
import {
  appendManifestPage,
  parseManifestKey,
  manifestHeadline,
  manifestRecordedAt,
  manifestTrailQuery,
  manifestCountLine,
  tierCountsLine,
  droppedLine,
} from "@/lib/contextManifests";
import type {
  ContextManifestRecord,
  ManifestTrailState,
} from "@/lib/contextManifests";

interface ContextManifestListProps {
  records: ContextManifestRecord[];
}

/**
 * The compile trail as text. Pure render — no fetch, no effects — so the
 * SSR tests can check what an auditor actually sees. Rows are rendered in
 * the order the endpoint returned (newest first): the backend owns the
 * ordering, and re-sorting here would undo its time window.
 */
export function ContextManifestList({ records }: ContextManifestListProps) {
  if (records.length === 0) {
    return (
      <p className="text-xs text-[var(--text-muted)]">
        No compiles recorded for this task.
      </p>
    );
  }
  return (
    <div className="space-y-0.5" role="list" aria-label="Context compile trail">
      {records.map((record) => {
        const { subtaskId, attempt } = parseManifestKey(record.key);
        const at = manifestRecordedAt(record.recorded_at);
        const tiers = tierCountsLine(record.summary?.items_by_tier);
        const dropped = droppedLine(record.summary?.dropped);
        return (
          <div
            key={record.key}
            role="listitem"
            className="border-l-2 border-l-cyan-500/40 pl-2 py-0.5"
          >
            <div className="flex items-center gap-1.5">
              <span className="font-mono text-[10px] text-[var(--text-muted)]">
                {at ? `${at} · ` : ""}
                {subtaskId} · attempt {attempt}
              </span>
              <span className="text-[10px] text-[var(--text-primary)] truncate">
                {manifestHeadline(record.summary)}
              </span>
            </div>
            {(tiers || dropped) && (
              <div
                className="text-[10px] text-[var(--text-muted)] truncate"
                title={`${tiers}${dropped ? ` | ${dropped}` : ""}`}
              >
                {tiers}
                {dropped ? ` | ${dropped}` : ""}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

interface ContextManifestTrailProps {
  taskId: string;
}

/**
 * Task-scoped compile trail: one request for the whole task, the same
 * surface the HTTP endpoint offers. It asks for the endpoint's `order=recent`
 * window (newest first) on purpose: the default key order is the *oldest*
 * window, so a truncated trail would hide exactly the attempts an audit is
 * about. A task with no compiles shows that, rather than an error --
 * "nothing recorded" is the fact here.
 *
 * When the first page reports a `next_cursor` the count line has told the
 * operator that more compiles exist; "load more" fetches the page after that
 * row key and appends it (deduped) instead of leaving the remainder
 * unreachable. A failed page keeps the rows already loaded and offers a
 * retry -- the loaded trail is not discarded because one request failed.
 */
export function ContextManifestTrail({ taskId }: ContextManifestTrailProps) {
  const [loaded, setLoaded] = useState<{
    taskId: string;
    trail: ManifestTrailState;
  } | null>(null);
  const [failedFor, setFailedFor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreFailed, setMoreFailed] = useState(false);

  // Derived, so a task change cannot show the previous task's rows: the
  // loaded state is only valid for the task it was fetched for. (Resetting
  // in the effect body would be synchronous setState there, and the old
  // state would flash before the reset.)
  const trail = loaded?.taskId === taskId ? loaded.trail : null;
  const failed = failedFor === taskId;

  useEffect(() => {
    let cancelled = false;
    const token = getStoredToken();
    // F69 — authenticate (the backend gates /dashboard/api/* off localhost).
    fetch(
      `${manifestTrailQuery(taskId)}${
        token ? `&token=${encodeURIComponent(token)}` : ""
      }`,
    )
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (cancelled) return;
        if (Array.isArray(data?.manifests)) {
          // One merge path for both pages: total is the scan count (the
          // window may be smaller) and next_cursor is the paging anchor.
          setLoaded({
            taskId,
            trail: appendManifestPage({ records: [] }, data),
          });
        } else setFailedFor(taskId);
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
    setLoadingMore(true);
    setMoreFailed(false);
    fetch(
      `${manifestTrailQuery(taskId, "recent", cursor)}${
        token ? `&token=${encodeURIComponent(token)}` : ""
      }`,
    )
      .then((res) => (res.ok ? res.json() : null))
      .then((page) => {
        if (!page || !Array.isArray(page.manifests)) {
          setMoreFailed(true);
          return;
        }
        setLoaded((current) =>
          current && current.taskId === taskId
            ? {
                taskId,
                trail: appendManifestPage(current.trail, page),
              }
            : current,
        );
      })
      .catch(() => setMoreFailed(true))
      .finally(() => setLoadingMore(false));
  };

  return (
    <div className="mb-2">
      <p className="text-xs text-[var(--text-secondary)] mb-1">
        Context compiles{" "}
        <span className="text-[var(--text-muted)]">
          {trail
            ? `${manifestCountLine(trail.records.length, trail.total)} · newest first`
            : failed
              ? "unavailable"
              : "loading…"}
        </span>
      </p>
      {failed ? (
        <p className="text-xs text-red-400/80">Manifest store unavailable</p>
      ) : trail === null ? null : (
        <>
          <ContextManifestList records={trail.records} />
          {trail.nextCursor ? (
            <button
              type="button"
              onClick={loadMore}
              disabled={loadingMore}
              className="mt-1 text-[10px] text-[var(--text-muted)] underline"
            >
              {loadingMore
                ? "loading…"
                : moreFailed
                  ? "load more failed — retry"
                  : "load more"}
            </button>
          ) : null}
        </>
      )}
    </div>
  );
}
