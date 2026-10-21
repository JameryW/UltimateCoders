import { useEffect, useState } from "react";
import { getStoredToken } from "@/hooks/useAuth";
import {
  appendManifestPage,
  parseManifestKey,
  manifestHeadline,
  manifestRecordedAt,
  manifestTrailQuery,
  manifestCountLine,
  manifestScopeLabel,
  tierCountsLine,
  droppedLine,
} from "@/lib/contextManifests";
import type {
  ContextManifestRecord,
  ManifestTrailState,
} from "@/lib/contextManifests";

interface ContextManifestListProps {
  records: ContextManifestRecord[];
  /** When set, each row's subtask id becomes a scope selector. */
  onSelectSubtask?: (subtaskId: string) => void;
  /** Empty-state text; the scoped trail names its scope. */
  emptyLabel?: string;
}

/**
 * The compile trail as text. Pure render — no fetch, no effects — so the
 * SSR tests can check what an auditor actually sees. Rows are rendered in
 * the order the endpoint returned (newest first): the backend owns the
 * ordering, and re-sorting here would undo its time window.
 *
 * `onSelectSubtask` makes the row's subtask id a button: the endpoint
 * already scopes by `subtask_id`, so a long task-wide trail can be narrowed
 * without a separate view.
 */
export function ContextManifestList({
  records,
  onSelectSubtask,
  emptyLabel,
}: ContextManifestListProps) {
  if (records.length === 0) {
    return (
      <p className="text-xs text-[var(--text-muted)]">
        {emptyLabel ?? "No compiles recorded for this task."}
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
        const meta = `${at ? `${at} · ` : ""}${subtaskId} · attempt ${attempt}`;
        return (
          <div
            key={record.key}
            role="listitem"
            className="border-l-2 border-l-cyan-500/40 pl-2 py-0.5"
          >
            <div className="flex items-center gap-1.5">
              {onSelectSubtask ? (
                <button
                  type="button"
                  onClick={() => onSelectSubtask(subtaskId)}
                  title={`Show only subtask ${subtaskId}`}
                  className="font-mono text-[10px] text-[var(--text-muted)] underline"
                >
                  {meta}
                </button>
              ) : (
                <span className="font-mono text-[10px] text-[var(--text-muted)]">
                  {meta}
                </span>
              )}
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

interface LoadedTrail {
  taskId: string;
  /** null = task-wide; a subtask id = scoped trail. */
  subtaskId: string | null;
  trail: ManifestTrailState;
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
 *
 * Rows' subtask ids scope the trail to one subtask (the endpoint accepts
 * `subtask_id`); paging and the count line stay inside that scope, and
 * "all subtasks" returns to the task-wide window. The scope is part of the
 * loaded state's identity, so a task or scope switch never shows the
 * previous window while the new one is in flight.
 */
export function ContextManifestTrail({ taskId }: ContextManifestTrailProps) {
  const [scope, setScope] = useState<{
    taskId: string;
    subtaskId: string;
  } | null>(null);
  const [loaded, setLoaded] = useState<LoadedTrail | null>(null);
  const [failedFor, setFailedFor] = useState<{
    taskId: string;
    subtaskId: string | null;
  } | null>(null);
  const [moreState, setMoreState] = useState<{
    taskId: string;
    subtaskId: string | null;
    busy: boolean;
    failed: boolean;
  } | null>(null);

  // Derived, so a task change cannot show the previous task's rows (and a
  // scope switch cannot show the task-wide rows): the loaded state is only
  // valid for the identity it was fetched for. (Resetting in the effect body
  // would be synchronous setState there, and stale state would flash.)
  const activeScope = scope?.taskId === taskId ? scope.subtaskId : null;
  const trail =
    loaded?.taskId === taskId && loaded.subtaskId === activeScope
      ? loaded.trail
      : null;
  const failed =
    failedFor?.taskId === taskId &&
    failedFor?.subtaskId === activeScope;
  const more =
    moreState?.taskId === taskId && moreState?.subtaskId === activeScope
      ? moreState
      : null;
  const loadingMore = more?.busy ?? false;
  const moreFailed = more?.failed ?? false;

  useEffect(() => {
    let cancelled = false;
    const token = getStoredToken();
    // F69 — authenticate (the backend gates /dashboard/api/* off localhost).
    fetch(
      `${manifestTrailQuery(
        taskId,
        "recent",
        undefined,
        activeScope ?? undefined,
      )}${token ? `&token=${encodeURIComponent(token)}` : ""}`,
    )
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (cancelled) return;
        if (Array.isArray(data?.manifests)) {
          // One merge path for both pages: total is the scan count (the
          // window may be smaller) and next_cursor is the paging anchor.
          setLoaded({
            taskId,
            subtaskId: activeScope,
            trail: appendManifestPage({ records: [] }, data),
          });
        } else setFailedFor({ taskId, subtaskId: activeScope });
      })
      .catch(() => {
        if (!cancelled) setFailedFor({ taskId, subtaskId: activeScope });
      });
    return () => {
      cancelled = true;
    };
  }, [taskId, activeScope]);

  const loadMore = () => {
    const cursor = trail?.nextCursor;
    if (!cursor) return;
    const token = getStoredToken();
    const identity = { taskId, subtaskId: activeScope };
    setMoreState({ ...identity, busy: true, failed: false });
    fetch(
      `${manifestTrailQuery(
        taskId,
        "recent",
        cursor,
        activeScope ?? undefined,
      )}${token ? `&token=${encodeURIComponent(token)}` : ""}`,
    )
      .then((res) => (res.ok ? res.json() : null))
      .then((page) => {
        if (!page || !Array.isArray(page.manifests)) {
          setMoreState({ ...identity, busy: false, failed: true });
          return;
        }
        setLoaded((current) =>
          current &&
          current.taskId === taskId &&
          current.subtaskId === activeScope
            ? {
                taskId,
                subtaskId: activeScope,
                trail: appendManifestPage(current.trail, page),
              }
            : current,
        );
        setMoreState({ ...identity, busy: false, failed: false });
      })
      .catch(() => setMoreState({ ...identity, busy: false, failed: true }));
  };

  const scopeLabel = manifestScopeLabel(activeScope ?? undefined);

  return (
    <div className="mb-2">
      <p className="text-xs text-[var(--text-secondary)] mb-1">
        Context compiles{" "}
        <span className="text-[var(--text-muted)]">
          {trail
            ? `${scopeLabel}${manifestCountLine(trail.records.length, trail.total)} · newest first`
            : failed
              ? "unavailable"
              : "loading…"}
        </span>
        {activeScope ? (
          <button
            type="button"
            onClick={() => setScope(null)}
            className="ml-1.5 text-[10px] text-[var(--text-muted)] underline"
          >
            all subtasks
          </button>
        ) : null}
      </p>
      {failed ? (
        <p className="text-xs text-red-400/80">Manifest store unavailable</p>
      ) : trail === null ? null : (
        <>
          <ContextManifestList
            records={trail.records}
            onSelectSubtask={
              activeScope
                ? undefined
                : (subtaskId) => setScope({ taskId, subtaskId })
            }
            emptyLabel={
              activeScope
                ? "No compiles recorded for this subtask."
                : undefined
            }
          />
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
