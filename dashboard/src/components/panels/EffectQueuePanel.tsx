import { useEffect, useState } from "react";
import { getStoredToken } from "@/hooks/useAuth";
import {
  EFFECT_STALE_STATUS,
  EFFECT_STATUSES,
  appendEffectPage,
  effectCountLine,
  effectHeadline,
  effectOwnerLine,
  effectRequestLine,
  effectsFromPayload,
  effectsQuery,
} from "@/lib/effects";
import type { EffectQueueState, EffectReceipt } from "@/lib/effects";

interface EffectQueueListProps {
  rows: EffectReceipt[];
  emptyLabel?: string;
}

/**
 * The receipt queue as text. Pure render — no fetch, no effects — so the SSR tests
 * can check what an operator actually sees: what state it is in, who holds it, and
 * the scope it was recorded against. Read-only by decision (D23): there is no
 * reconcile button here.
 */
export function EffectQueueList({ rows, emptyLabel }: EffectQueueListProps) {
  if (rows.length === 0) {
    return (
      <p className="text-xs text-[var(--text-muted)]">
        {emptyLabel ?? "No receipts in this state."}
      </p>
    );
  }
  return (
    <div className="space-y-0.5" role="list" aria-label="Effect receipt queue">
      {rows.map((row) => {
        const scope = effectRequestLine(row.request);
        const owner = effectOwnerLine(row);
        return (
          <div
            key={row.effect_id}
            role="listitem"
            className="border-l-2 border-l-amber-500/40 pl-2 py-0.5"
          >
            <div className="flex items-center gap-1.5">
              <span className="font-mono text-[10px] text-[var(--text-muted)]">
                {row.kind} · {effectHeadline(row)}
              </span>
              <span className="text-[10px] text-[var(--text-primary)] truncate">
                {row.reason || row.status}
              </span>
            </div>
            {owner && (
              <div className="text-[10px] text-[var(--text-muted)] truncate" title={owner}>
                {owner}
              </div>
            )}
            {scope && (
              <div className="text-[10px] text-[var(--text-muted)] truncate" title={scope}>
                {scope}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

interface LoadedQueue {
  key: string;
  queue: EffectQueueState;
}

/**
 * Read-only receipt queue (D23 / D24). One status at a time, because that is what
 * the endpoint answers; `stale` is only offered for `RECONCILING`, where an expired
 * lease means the reconciler died and someone may take the record over. Pages append
 * deduped by effect id, and a failed page keeps the rows already held.
 */
export function EffectQueue() {
  const [status, setStatus] = useState<string>("UNKNOWN");
  const [stale, setStale] = useState(false);
  const [loaded, setLoaded] = useState<LoadedQueue | null>(null);
  const [failedFor, setFailedFor] = useState<string | null>(null);
  const [more, setMore] = useState<{ key: string; busy: boolean; failed: boolean } | null>(null);

  const key = `${status}:${stale ? "stale" : "all"}`;
  const queue = loaded?.key === key ? loaded.queue : null;
  const failed = failedFor === key;
  const moreState = more?.key === key ? more : null;
  const loadingMore = moreState?.busy ?? false;
  const moreFailed = moreState?.failed ?? false;

  useEffect(() => {
    let cancelled = false;
    const token = getStoredToken();
    fetch(`${effectsQuery({ status, stale })}${token ? `&token=${encodeURIComponent(token)}` : ""}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (cancelled) return;
        if (data && Array.isArray(data.effects)) {
          setLoaded({ key, queue: effectsFromPayload(data) });
          setFailedFor(null);
        } else {
          setFailedFor(key);
        }
      })
      .catch(() => {
        if (!cancelled) setFailedFor(key);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- key encodes status+stale
  }, [key]);

  const loadMore = () => {
    const cursor = queue?.nextAfter;
    if (!cursor) return;
    const token = getStoredToken();
    setMore({ key, busy: true, failed: false });
    fetch(
      `${effectsQuery({ status, stale, after: cursor })}${
        token ? `&token=${encodeURIComponent(token)}` : ""
      }`,
    )
      .then((res) => (res.ok ? res.json() : null))
      .then((page) => {
        if (!page || !Array.isArray(page.effects)) {
          setMore({ key, busy: false, failed: true });
          return;
        }
        setLoaded((current) =>
          current && current.key === key
            ? { key, queue: appendEffectPage(current.queue, page) }
            : current,
        );
        setMore({ key, busy: false, failed: false });
      })
      .catch(() => setMore({ key, busy: false, failed: true }));
  };

  return (
    <div className="mb-2">
      <p className="text-xs text-[var(--text-secondary)] mb-1 flex items-center gap-1.5">
        Effect receipts
        <select
          aria-label="Receipt status"
          value={status}
          onChange={(event) => setStatus(event.target.value)}
          className="text-[10px] bg-transparent text-[var(--text-muted)]"
        >
          {EFFECT_STATUSES.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
        {status === EFFECT_STALE_STATUS ? (
          <label className="text-[10px] text-[var(--text-muted)]">
            <input
              type="checkbox"
              checked={stale}
              onChange={(event) => setStale(event.target.checked)}
              className="mr-1"
            />
            lease expired
          </label>
        ) : null}
        {queue ? (
          <span className="text-[10px] text-[var(--text-muted)]">
            {effectCountLine(queue.rows.length, queue.total, status)}
          </span>
        ) : null}
      </p>
      {failed ? (
        <p className="text-xs text-[var(--text-muted)]">Effect receipts unavailable.</p>
      ) : queue ? (
        <>
          <EffectQueueList rows={queue.rows} />
          {queue.nextAfter ? (
            <button
              type="button"
              onClick={loadMore}
              disabled={loadingMore}
              className="mt-1 text-[10px] text-[var(--text-muted)] underline disabled:opacity-50"
            >
              {loadingMore ? "Loading…" : moreFailed ? "Retry older receipts" : "Load older receipts"}
            </button>
          ) : null}
        </>
      ) : (
        <p className="text-xs text-[var(--text-muted)]">Loading…</p>
      )}
    </div>
  );
}
