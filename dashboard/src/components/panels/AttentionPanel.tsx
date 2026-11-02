import { useCallback, useEffect, useMemo, useState } from "react";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { cn, shortId, truncate } from "@/lib/utils";
import {
  fetchAttention,
  formatAge,
  formatCapacity,
  isOffline,
  resolveAttention,
  worklistOrder,
  type AttentionItem,
  type AttentionKind,
  type Capacity,
} from "@/lib/attentionApi";

/** How often the worklist is re-read while the panel is mounted. */
const POLL_INTERVAL_MS = 30_000;

interface AttentionSection {
  kind: AttentionKind;
  title: string;
  hint: string;
  /** Tone dot / rail: the tokens are re-declared for the light theme. */
  dot: string;
  border: string;
}

/**
 * Display order = the worklist's own precedence: a pending human decision is
 * the only thing that blocks a person, so it is always first.
 */
const SECTIONS: readonly AttentionSection[] = [
  {
    kind: "needs_human",
    title: "Needs human",
    hint: "Decisions only an operator can make; each reserves one human slot.",
    dot: "bg-[var(--cc-lead)]",
    border: "border-l-[var(--cc-lead)]",
  },
  {
    kind: "waiting_event",
    title: "Waiting on event",
    hint: "Parked on CI, a webhook or a timer — holds no worker slot.",
    dot: "bg-[var(--cc-swarm)]",
    border: "border-l-[var(--cc-swarm)]",
  },
  {
    kind: "blocked",
    title: "Blocked",
    hint: "Cannot proceed; reported here, but paged to nobody.",
    dot: "bg-[var(--terminal-red)]",
    border: "border-l-[var(--terminal-red)]",
  },
  {
    kind: "verifying",
    title: "Verifying",
    hint: "Result recorded; verification still running.",
    dot: "bg-[var(--cc-dispatch)]",
    border: "border-l-[var(--cc-dispatch)]",
  },
];

function sectionFor(kind: AttentionKind): AttentionSection {
  return SECTIONS.find((section) => section.kind === kind) ?? SECTIONS[0]!;
}

function metaLine(item: AttentionItem): string {
  const parts = [
    `case ${shortId(item.case_id)}`,
    item.task_id ? `task ${shortId(item.task_id)}` : null,
    formatAge(item.since),
    `priority ${item.priority}`,
  ].filter((part): part is string => part !== null);
  return parts.join(" · ");
}

/**
 * Capacity strip: busy/total, the human reserve, and the slots a parked case is
 * not costing. Rendered as a description list so each number keeps its label
 * for assistive technology; `formatCapacity` carries the same three facts as a
 * single sentence in the hover/accessible description.
 */
function CapacityStrip({ capacity }: { capacity: Capacity }) {
  return (
    <dl
      className="flex flex-wrap items-baseline gap-x-4 gap-y-1 mb-3 text-xs"
      title={formatCapacity(capacity)}
      data-testid="attention-capacity"
    >
      <div className="flex items-baseline gap-1">
        <dt className="text-[var(--text-muted)]">worker slots</dt>
        <dd className="font-mono text-[var(--text-primary)]">
          {capacity.worker_slots_busy}/{capacity.worker_slots_total}
        </dd>
      </div>
      <div className="flex items-baseline gap-1">
        <dt className="text-[var(--text-muted)]">reserved for human</dt>
        <dd className="font-mono text-[var(--text-primary)]">
          {capacity.worker_slots_reserved_human}
        </dd>
      </div>
      <div className="flex items-baseline gap-1">
        <dt className="text-[var(--text-muted)]">slots saved by quiet waiting</dt>
        <dd className="font-mono text-[var(--text-primary)]">
          {capacity.slots_saved_by_quiet_waiting}
        </dd>
      </div>
    </dl>
  );
}

/**
 * Resolve control for one `needs_human` item. The reason is required: the
 * gateway records the decision in the case audit trail, and "resolved" with no
 * why is the noise the attention surface exists to remove.
 */
function ResolveForm({
  item,
  onResolve,
}: {
  item: AttentionItem;
  onResolve: (item: AttentionItem, reason: string) => Promise<string | null>;
}) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputId = `attention-resolve-${item.id.replace(/[^A-Za-z0-9_-]/g, "-")}`;

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const detail = reason.trim();
    if (!detail || busy) return;
    setBusy(true);
    setError(await onResolve(item, detail));
    setBusy(false);
  };

  return (
    <form
      className="mt-1.5 flex flex-wrap items-center gap-1.5"
      onSubmit={(event) => void submit(event)}
    >
      <label htmlFor={inputId} className="sr-only">
        Decision for case {item.case_id}
      </label>
      <input
        id={inputId}
        value={reason}
        onChange={(event) => setReason(event.target.value)}
        placeholder="Why? (recorded with the decision)"
        className="flex-1 min-w-[12rem] bg-[var(--bg-primary)] border border-[var(--border-color)] rounded px-2 py-1 text-xs text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:border-blue-500 focus:outline-none focus-visible:ring-1 focus-visible:ring-[var(--cc-swarm)]"
      />
      <button
        type="submit"
        disabled={busy || reason.trim() === ""}
        className="btn-action-ok px-2 py-0.5 rounded text-xs cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[var(--cc-swarm)]"
      >
        {busy ? "Recording…" : "Resolve"}
      </button>
      {error && <span role="alert" className="text-xs text-[var(--text-secondary)]">{error}</span>}
    </form>
  );
}

function AttentionItemRow({
  item,
  onResolve,
}: {
  item: AttentionItem;
  onResolve?: (item: AttentionItem, reason: string) => Promise<string | null>;
}) {
  const section = sectionFor(item.kind);
  return (
    <li
      className={cn("border-l-2 pl-2 py-1", section.border)}
      data-testid="attention-item"
      data-kind={item.kind}
    >
      <p className="text-xs text-[var(--text-muted)]">{metaLine(item)}</p>
      <p className="text-sm text-[var(--text-primary)] mt-0.5">
        {truncate(item.summary, 96) || "(no summary)"}
      </p>
      {item.detail && (
        <p className="text-xs text-[var(--text-secondary)] mt-0.5">
          {truncate(item.detail, 160)}
        </p>
      )}
      {item.human_action && (
        <p className="text-xs text-[var(--text-secondary)] mt-0.5">
          Next: {truncate(item.human_action, 120)}
        </p>
      )}
      {item.kind === "needs_human" && onResolve && (
        <ResolveForm item={item} onResolve={onResolve} />
      )}
    </li>
  );
}

export interface AttentionWorklistProps {
  items: readonly AttentionItem[];
  capacity: Capacity | null;
  /** `loading` until the first answer; `offline` for a dead governance bridge. */
  state: "loading" | "ready" | "offline";
  /** Non-availability failure (a 4xx/5xx the operator can read once). */
  failure?: string | null;
  /** Last successful action, announced politely. */
  notice?: string | null;
  onResolve?: (item: AttentionItem, reason: string) => Promise<string | null>;
}

/**
 * Pure render of the worklist — the fetch loop lives in `AttentionPanel`, so
 * this component can be rendered with fixtures (and without a network) to pin
 * the ordering and the offline/empty states.
 */
export function AttentionWorklist({
  items,
  capacity,
  state,
  failure,
  notice,
  onResolve,
}: AttentionWorklistProps) {
  const sections = useMemo(() => {
    // Order inside a section is the canonical worklist order; the section order
    // above is what puts a pending human decision before everything else.
    const ordered = worklistOrder(items);
    return SECTIONS.map((section) => ({
      ...section,
      items: ordered.filter((item) => item.kind === section.kind),
    })).filter((section) => section.items.length > 0);
  }, [items]);

  if (state === "loading") {
    return (
      <p className="text-sm text-[var(--text-muted)]" role="status">
        Loading the worklist…
      </p>
    );
  }

  if (state === "offline") {
    // Quiet by design: the bridge being down is an operational state, not an
    // error to shout about — the panel retries on its own.
    return (
      <EmptyState
        icon="shield"
        title="Governance plane offline"
        description="The worklist cannot be read while the gateway bridge is down. Retrying automatically."
      />
    );
  }

  return (
    <>
      {capacity && <CapacityStrip capacity={capacity} />}
      {failure && (
        <p className="text-xs text-[var(--text-secondary)] mb-2" role="status">
          Worklist read failed: {failure}
        </p>
      )}
      {notice && (
        <p className="text-xs text-[var(--text-muted)] mb-2" role="status">
          {notice}
        </p>
      )}
      {items.length === 0 ? (
        <EmptyState
          icon="inbox"
          title="Worklist clear"
          description="No case is waiting on a decision or an event."
        />
      ) : (
        sections.map((section) => (
          <section key={section.kind} className="mb-3 last:mb-0" data-testid="attention-section">
            <div className="flex items-center gap-1.5 mb-1">
              <i className={cn("w-1.5 h-1.5 rounded-full shrink-0", section.dot)} aria-hidden="true" />
              <h3 className="text-xs font-semibold uppercase tracking-wide text-[var(--text-secondary)]">
                {section.title}
              </h3>
              <span className="text-xs text-[var(--text-muted)]">{section.items.length}</span>
            </div>
            <p className="text-xs text-[var(--text-muted)] mb-1.5">{section.hint}</p>
            <ul className="space-y-1.5" aria-label={`${section.title} items`}>
              {section.items.map((item) => (
                <AttentionItemRow key={item.id} item={item} onResolve={onResolve} />
              ))}
            </ul>
          </section>
        ))
      )}
    </>
  );
}

export interface AttentionPanelProps {
  /** Render without the Card wrapper (for a view that already provides one). */
  embedded?: boolean;
  className?: string;
}

/**
 * The operator "todo" worklist for the governance plane.
 *
 * Self-fetching: the gateway's attention state is not part of the gRPC
 * dashboard snapshot, so the panel owns its poll loop instead of requiring a
 * provider wired through `App.tsx`. A `503 governance_unavailable` renders as
 * one quiet offline note — never a thrown render, never a toast per poll.
 *
 * A dead bridge clears the rows rather than greying them: unlike task data
 * (which is still true while the stream is interrupted), an attention item may
 * have been resolved elsewhere, and acting on a stale decision is worse than
 * showing none.
 */
export function AttentionPanel({ embedded = false, className }: AttentionPanelProps) {
  const [items, setItems] = useState<AttentionItem[]>([]);
  const [capacity, setCapacity] = useState<Capacity | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "offline">("loading");
  const [failure, setFailure] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [refreshToken, setRefreshToken] = useState(0);

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      const result = await fetchAttention();
      if (cancelled) return;
      if (result.ok) {
        setItems(result.data.items);
        setCapacity(result.data.capacity);
        setState("ready");
        setFailure(null);
        return;
      }
      // Only the documented "bridge down" answers are quiet; anything else is
      // reported once, in place, instead of being swallowed.
      const offline = isOffline(result);
      setState(offline ? "offline" : "ready");
      setFailure(offline ? null : `${result.error} (HTTP ${result.status})`);
      setItems([]);
      setCapacity(null);
    };

    void load();
    const timer = window.setInterval(() => void load(), POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [refreshToken]);

  const handleResolve = useCallback(
    async (item: AttentionItem, reason: string): Promise<string | null> => {
      const result = await resolveAttention(item.id, {
        resolution_kind: "human_decision",
        detail: reason,
      });
      if (!result.ok) {
        if (isOffline(result)) {
          setState("offline");
          setItems([]);
          setCapacity(null);
          return "Governance plane offline — the decision was not recorded.";
        }
        return `Could not record the decision: ${result.error} (HTTP ${result.status}).`;
      }
      setItems((current) => current.filter((entry) => entry.id !== item.id));
      setNotice(`Resolved case ${shortId(item.case_id)}.`);
      return null;
    },
    [],
  );

  const needsHuman = useMemo(
    () => items.filter((item) => item.kind === "needs_human").length,
    [items],
  );

  const content = (
    <AttentionWorklist
      items={items}
      capacity={capacity}
      state={state}
      failure={failure}
      notice={notice}
      onResolve={handleResolve}
    />
  );

  const header = (
    <CardHeader>
      <CardTitle>Attention</CardTitle>
      <div className="flex items-center gap-2">
        {state === "offline" && <Badge variant="unavailable">Offline</Badge>}
        <Badge>{needsHuman > 0 ? `${needsHuman} need you` : items.length}</Badge>
        <button
          type="button"
          onClick={() => setRefreshToken((token) => token + 1)}
          aria-label="Refresh worklist"
          title="Refresh worklist"
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
