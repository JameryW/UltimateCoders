import { useEffect, useState } from "react";
import { getStoredToken } from "@/hooks/useAuth";
import {
  compareManifestRecords,
  parseManifestKey,
  manifestHeadline,
  tierCountsLine,
  droppedLine,
} from "@/lib/contextManifests";
import type { ContextManifestRecord } from "@/lib/contextManifests";

interface ContextManifestListProps {
  records: ContextManifestRecord[];
}

/**
 * The compile trail as text. Pure render — no fetch, no effects — so the
 * SSR tests can check what an auditor actually sees.
 */
export function ContextManifestList({ records }: ContextManifestListProps) {
  if (records.length === 0) {
    return (
      <p className="text-xs text-[var(--text-muted)]">
        No compiles recorded for this task.
      </p>
    );
  }
  const ordered = [...records].sort(compareManifestRecords);
  return (
    <div className="space-y-0.5" role="list" aria-label="Context compile trail">
      {ordered.map((record) => {
        const { subtaskId, attempt } = parseManifestKey(record.key);
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
 * surface the HTTP endpoint offers. A task with no compiles shows that,
 * rather than an error -- "nothing recorded" is the fact here.
 */
export function ContextManifestTrail({ taskId }: ContextManifestTrailProps) {
  const [records, setRecords] = useState<ContextManifestRecord[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const token = getStoredToken();
    // F69 — authenticate (the backend gates /dashboard/api/* off localhost).
    fetch(
      `/dashboard/api/context/manifests?task_id=${encodeURIComponent(taskId)}${
        token ? `&token=${encodeURIComponent(token)}` : ""
      }`,
    )
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (cancelled) return;
        if (Array.isArray(data?.manifests)) setRecords(data.manifests);
        else setFailed(true);
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
            ? `${records.length} recorded`
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
