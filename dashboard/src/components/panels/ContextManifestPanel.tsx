import { useEffect, useState } from "react";
import { getStoredToken } from "@/hooks/useAuth";
import {
  parseManifestKey,
  manifestHeadline,
  manifestRecordedAt,
  manifestTrailQuery,
  manifestCountLine,
  tierCountsLine,
  droppedLine,
} from "@/lib/contextManifests";
import type { ContextManifestRecord } from "@/lib/contextManifests";

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
 */
export function ContextManifestTrail({ taskId }: ContextManifestTrailProps) {
  const [records, setRecords] = useState<ContextManifestRecord[] | null>(null);
  const [total, setTotal] = useState<number | undefined>(undefined);
  const [failed, setFailed] = useState(false);

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
          setRecords(data.manifests);
          // `total` is the scan count; the window may be smaller. Absent
          // (older backend) leaves it undefined and the label degrades.
          setTotal(
            typeof data.total === "number" && Number.isFinite(data.total)
              ? data.total
              : undefined,
          );
        } else setFailed(true);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [taskId]);

  return (
    <div className="mb-2">
      <p className="text-xs text-[var(--text-secondary)] mb-1">
        Context compiles{" "}
        <span className="text-[var(--text-muted)]">
          {records
            ? `${manifestCountLine(records.length, total)} · newest first`
            : failed
              ? "unavailable"
              : "loading…"}
        </span>
      </p>
      {failed ? (
        <p className="text-xs text-red-400/80">Manifest store unavailable</p>
      ) : records === null ? null : (
        <ContextManifestList records={records} />
      )}
    </div>
  );
}
