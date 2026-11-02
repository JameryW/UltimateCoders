import { useCallback, useEffect, useMemo, useState } from "react";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { shortId, truncate } from "@/lib/utils";
import { isOffline } from "@/lib/attentionApi";
import {
  approvalOrder,
  decideApproval,
  fetchApprovals,
  formatExpiry,
  isPending,
  type Approval,
} from "@/lib/approvalApi";

/** How often the queue is re-read while the panel is mounted. */
const POLL_INTERVAL_MS = 30_000;

export interface ApprovalPanelProps {
  /** Render without the Card wrapper (for a view that already provides one). */
  embedded?: boolean;
  className?: string;
}

/**
 * The human side of the approval loop: a queue of human-tier tool requests
 * (scale a worker pool, run a container) with an approve/reject decision each.
 *
 * Self-fetching: the gateway's approval state is not part of the gRPC dashboard
 * snapshot, so the panel owns its poll loop. Two honesty rules, inherited from
 * the attention worklist:
 *
 * - A `503 governance_unavailable` is one quiet offline note, never a thrown
 *   render and never a toast per poll.
 * - A dead bridge **clears** the rows instead of greying them: an approval may
 *   have been decided elsewhere, and acting on a stale queue is worse than
 *   showing none.
 *
 * The decision is sent as a `user` principal by contract; a `401` means the
 * deployment wants `UC_DASHBOARD_TOKEN` and this browser has none, which is
 * reported in place rather than looking like an empty queue.
 */
export function ApprovalPanel({ embedded = false, className }: ApprovalPanelProps) {
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [state, setState] = useState<"loading" | "ready" | "offline">("loading");
  const [failure, setFailure] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [refreshToken, setRefreshToken] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const result = await fetchApprovals();
      if (cancelled) return;
      if (result.ok) {
        setApprovals(result.data);
        setState("ready");
        setFailure(null);
        return;
      }
      const offline = isOffline(result);
      setState(offline ? "offline" : "ready");
      setFailure(offline ? null : `${result.error} (HTTP ${result.status})`);
      setApprovals([]);
    };
    void load();
    const timer = window.setInterval(() => void load(), POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [refreshToken]);

  const decide = useCallback(
    async (approval: Approval, approve: boolean, note: string) => {
      setPendingId(approval.id);
      const result = await decideApproval(approval.id, approve, note);
      setPendingId(null);
      if (!result.ok) {
        if (isOffline(result)) {
          setState("offline");
          setApprovals([]);
          setNotice("Governance plane offline — the decision was not recorded.");
          return;
        }
        if (result.status === 401) {
          setNotice(
            "This deployment requires a dashboard token; sign in again before deciding.",
          );
          return;
        }
        setNotice(`Could not record the decision: ${result.error} (HTTP ${result.status}).`);
        return;
      }
      setApprovals((current) =>
        current.map((entry) => (entry.id === approval.id ? result.data : entry)),
      );
      setNotice(
        approve
          ? `Approved ${approval.tool} — the permit is single-use.`
          : `Rejected ${approval.tool}.`,
      );
    },
    [],
  );

  const ordered = useMemo(() => approvalOrder(approvals), [approvals]);
  const waiting = ordered.filter(isPending);
  const decided = ordered.filter((entry) => !isPending(entry));

  const rows = (waiting.length > 0 ? waiting : []) as Approval[];

  const content = (
    <div className="p-3" data-testid="approval-panel">
      {notice && (
        <p className="mb-2 text-xs text-[var(--text-muted)]" data-testid="approval-notice">
          {notice}
        </p>
      )}
      {failure && (
        <p className="mb-2 text-xs text-[var(--terminal-red)]" data-testid="approval-failure">
          {failure}
        </p>
      )}
      {state === "loading" && (
        <p className="text-xs text-[var(--text-muted)]">Loading approvals…</p>
      )}
      {state === "offline" && (
        <p className="text-xs text-[var(--text-muted)]" data-testid="approval-offline">
          Governance plane offline — the queue is unavailable.
        </p>
      )}
      {state === "ready" && rows.length === 0 && (
        <EmptyState
          icon="shield"
          title="No approvals waiting"
          description="Human-tier actions (scaling workers, running containers) show up here for a decision."
        />
      )}
      {rows.map((approval) => (
        <ApprovalRow
          key={approval.id}
          approval={approval}
          busy={pendingId === approval.id}
          onDecide={decide}
        />
      ))}
      {decided.length > 0 && (
        <section className="mt-3 border-t border-[var(--border)] pt-2" data-testid="approval-decided">
          <h3 className="mb-1 text-[10px] uppercase tracking-wide text-[var(--text-muted)]">
            Already decided
          </h3>
          <ul className="space-y-1">
            {decided.map((approval) => (
              <li key={approval.id} className="text-xs text-[var(--text-muted)]">
                <span className="font-mono">{approval.tool}</span> · {approval.state} ·{" "}
                {formatExpiry(approval)}
                {approval.decided_by_id ? ` · by ${approval.decided_by_id}` : ""}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );

  const header = (
    <CardHeader>
      <CardTitle>Approvals</CardTitle>
      <div className="flex items-center gap-2">
        {state === "offline" && <Badge variant="unavailable">Offline</Badge>}
        <Badge>{waiting.length > 0 ? `${waiting.length} waiting` : approvals.length}</Badge>
        <button
          type="button"
          onClick={() => setRefreshToken((token) => token + 1)}
          aria-label="Refresh approvals"
          title="Refresh approvals"
          className="text-xs text-[var(--text-muted)] hover:text-[var(--text-primary)] cursor-pointer rounded px-1 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[var(--cc-swarm)]"
        >
          ↻
        </button>
      </div>
    </CardHeader>
  );

  if (embedded) {
    return (
      <div className={className}>
        {header}
        {content}
      </div>
    );
  }
  return (
    <Card className={className}>
      {header}
      {content}
    </Card>
  );
}

interface ApprovalRowProps {
  approval: Approval;
  busy: boolean;
  onDecide: (approval: Approval, approve: boolean, note: string) => void;
}

function ApprovalRow({ approval, busy, onDecide }: ApprovalRowProps) {
  const [note, setNote] = useState("");
  const inputId = `approval-note-${approval.id.replace(/[^A-Za-z0-9_-]/g, "-")}`;
  return (
    <article
      className="mb-2 rounded border-l-2 border-[var(--cc-lead)] bg-[var(--surface-2)] p-2 last:mb-0"
      data-testid="approval-row"
    >
      <header className="flex items-baseline justify-between gap-2">
        <h3 className="text-xs font-semibold text-[var(--text-primary)]">
          {approval.tool}
          {approval.case_id ? (
            <span className="ml-1 font-mono text-[10px] text-[var(--text-muted)]">
              {shortId(approval.case_id)}
            </span>
          ) : null}
        </h3>
        <span className="text-[10px] text-[var(--text-muted)]" data-testid="approval-expiry">
          {formatExpiry(approval)}
        </span>
      </header>
      <p className="mt-1 text-[11px] text-[var(--text-muted)]">
        requested by {approval.requested_by_kind}:{approval.requested_by_id}
        {approval.reason ? ` · ${truncate(approval.reason, 120)}` : ""}
      </p>
      {approval.action_digest && (
        <p className="mt-1 font-mono text-[10px] text-[var(--text-muted)]">
          action {truncate(approval.action_digest, 64)}
        </p>
      )}
      <div className="mt-2 flex items-center gap-2">
        <label htmlFor={inputId} className="sr-only">
          Decision note
        </label>
        <input
          id={inputId}
          value={note}
          onChange={(event) => setNote(event.target.value)}
          placeholder="note (recorded in the audit)"
          className="min-w-0 flex-1 rounded border border-[var(--border)] bg-[var(--surface-1)] px-2 py-1 text-[11px] text-[var(--text-primary)]"
        />
        <button
          type="button"
          disabled={busy}
          onClick={() => onDecide(approval, true, note)}
          className="rounded bg-[var(--cc-lead)] px-2 py-1 text-[11px] font-semibold text-[var(--surface-1)] disabled:opacity-50"
        >
          Approve
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => onDecide(approval, false, note)}
          className="rounded border border-[var(--border)] px-2 py-1 text-[11px] text-[var(--text-muted)] disabled:opacity-50"
        >
          Reject
        </button>
      </div>
    </article>
  );
}
