/**
 * Knowledge-layer overview helpers.
 *
 * The backend aggregates the authoritative knowledge namespaces
 * (`attempt_chain`, `knowledge_evidence`, `knowledge_claims`,
 * `knowledge_pages`) into a counts-only payload for one task
 * (`python/ultimate_coders/knowledge/dashboard.py`, served at
 * `GET /dashboard/api/knowledge`). Claim statements, patch diffs, failure
 * excerpts and page bodies never appear in that payload, so every formatter
 * here reads known numeric fields and never an arbitrary blob.
 *
 * A zero counter means "nothing captured in this window", not "no such
 * concept": the scan is bounded by `limit` and a full window says
 * `truncated`, which the stage line surfaces so a reader does not mistake a
 * bounded sample for the whole knowledge base.
 */

export interface KnowledgeOverviewCounts {
  total?: number;
  completed?: number;
  failed?: number;
  cleanup_pending?: number;
  promoted?: number;
  verified?: number;
  active?: number;
  locked?: number;
  by_outcome?: Record<string, number>;
  by_kind?: Record<string, number>;
  by_status?: Record<string, number>;
}

export interface KnowledgeOverviewGate {
  allowed?: number;
  denied?: number;
  other?: number;
  reasons?: Record<string, number>;
}

/** `GET /dashboard/api/knowledge` response (`schema: knowledge-overview/1`). */
export interface KnowledgeOverview {
  schema?: string;
  task_id?: string;
  stage?: string;
  truncated?: boolean;
  scanned?: { attempts?: number; evidence?: number; claims?: number; pages?: number };
  attempts?: KnowledgeOverviewCounts;
  evidence?: KnowledgeOverviewCounts;
  claims?: KnowledgeOverviewCounts;
  gate?: KnowledgeOverviewGate;
  pages?: KnowledgeOverviewCounts;
}

/** The endpoint URL for one task's overview, with the stored token appended. */
export function knowledgeOverviewQuery(
  taskId: string,
  { limit, token }: { limit?: number; token?: string | null } = {},
): string {
  const params = [`task_id=${encodeURIComponent(taskId)}`];
  if (limit) params.push(`limit=${limit}`);
  if (token) params.push(`token=${encodeURIComponent(token)}`);
  return `/dashboard/api/knowledge?${params.join("&")}`;
}

function count(value: number | undefined): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

/** True when every aggregate is zero -- the task has captured nothing yet. */
export function knowledgeEmpty(overview: KnowledgeOverview): boolean {
  return (
    count(overview.attempts?.total) === 0 &&
    count(overview.evidence?.total) === 0 &&
    count(overview.claims?.total) === 0 &&
    count(overview.pages?.total) === 0
  );
}

/** "stage shadow · truncated sample" (stage disabled stays explicit). */
export function knowledgeStageLine(overview: KnowledgeOverview): string {
  const stage = (overview.stage || "unknown").trim() || "unknown";
  const suffix = overview.truncated ? " · truncated sample" : "";
  return `stage ${stage}${suffix}`;
}

/** "attempts 5 · evidence 9 · claims 2 · pages 1". */
export function knowledgeCountsLine(overview: KnowledgeOverview): string {
  return [
    `attempts ${count(overview.attempts?.total)}`,
    `evidence ${count(overview.evidence?.total)}`,
    `claims ${count(overview.claims?.total)}`,
    `pages ${count(overview.pages?.total)}`,
  ].join(" · ");
}

const MAX_GATE_REASONS = 3;

/**
 * "gate allowed 3 · denied 2 · reasons: graph_scope_missing×2, ...".
 *
 * `other` counts gate-decision rows whose payload was not a boolean verdict
 * (nor a recognised denial), so it is surfaced separately instead of being
 * folded into either side.
 */
export function knowledgeGateLine(overview: KnowledgeOverview): string {
  const gate = overview.gate ?? {};
  const allowed = count(gate.allowed);
  const denied = count(gate.denied);
  const other = count(gate.other);
  const parts = [`allowed ${allowed}`, `denied ${denied}`];
  if (other > 0) parts.push(`other ${other}`);
  const reasons = Object.entries(gate.reasons ?? {})
    .filter(([, value]) => count(value) > 0)
    .sort((a, b) => count(b[1]) - count(a[1]) || a[0].localeCompare(b[0]))
    .slice(0, MAX_GATE_REASONS)
    .map(([reason, value]) => `${reason}×${count(value)}`);
  const header = `gate ${parts.join(" · ")}`;
  return reasons.length > 0 ? `${header} · reasons: ${reasons.join(", ")}` : header;
}

/** "outcomes failed 1 · completed 2" for whatever the aggregate recorded. */
export function knowledgeOutcomeLine(overview: KnowledgeOverview): string {
  const outcomes = Object.entries(overview.attempts?.by_outcome ?? {})
    .filter(([, value]) => count(value) > 0)
    .sort((a, b) => count(b[1]) - count(a[1]) || a[0].localeCompare(b[0]))
    .map(([name, value]) => `${name} ${count(value)}`);
  return outcomes.length > 0 ? `outcomes ${outcomes.join(" · ")}` : "outcomes none recorded";
}
