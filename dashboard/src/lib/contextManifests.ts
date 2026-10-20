/**
 * Context-manifest trail helpers.
 *
 * The runtime records one summary per compile under the key
 * `task:subtask:attempt` (python/ultimate_coders/context_outbox.py). This
 * module turns those records into something a human can scan. The summary
 * carries no prompt content -- only counts, budgets and drop reasons -- so
 * every formatter here reads known fields and never an arbitrary blob.
 *
 * Ordering belongs to the endpoint: this module asks for its `order=recent`
 * time window and renders it as returned. The default key order is the
 * *oldest* key window (#751), and re-sorting a time window here would undo
 * it; `manifestOrderKey` remains for parsing `task:subtask:attempt`.
 */

export interface ContextManifestSummary {
  budget_tokens?: number;
  total_tokens?: number;
  stable_tokens?: number;
  risk?: string;
  over_budget?: boolean;
  items_by_tier?: Record<string, number>;
  dropped?: Array<{ item_id?: string; tier?: number; reason?: string }>;
}

export interface ContextManifestRecord {
  key: string;
  summary?: ContextManifestSummary;
  recorded_at?: number;
}

export interface ManifestKeyParts {
  taskId: string;
  subtaskId: string;
  attempt: number;
}

/**
 * Split `task:subtask:attempt` into (prefix, numeric attempt). The backend's
 * task scan sorts by exactly this key, so parsing reuses it; a non-numeric
 * tail is attempt 0 (the backend's `int(attempt) if attempt.isdigit() else 0`).
 */
export function manifestOrderKey(key: string): [string, number] {
  const at = key.lastIndexOf(":");
  const prefix = at < 0 ? key : key.slice(0, at);
  const tail = at < 0 ? "" : key.slice(at + 1);
  return [prefix, /^\d+$/.test(tail) ? Number(tail) : 0];
}

/**
 * The panel's request: the newest compiles, newest first. The endpoint's
 * default order is the *oldest* key window, so asking for it would hide the
 * latest attempts exactly when the trail truncates; `order=recent` is the
 * real time window (#751).
 */
export function manifestTrailQuery(
  taskId: string,
  order: "recent" | "key" = "recent",
): string {
  return `/dashboard/api/context/manifests?task_id=${encodeURIComponent(taskId)}&order=${order}`;
}

/**
 * When a compile was recorded, as UTC `HH:MM:SSZ` -- deterministic on every
 * machine (local time would make the rendered trail depend on the viewer's
 * zone). A missing/non-finite timestamp is not a time: "" rather than an
 * invented "Invalid Date"/"NaN".
 */
export function manifestRecordedAt(recorded_at?: number): string {
  if (typeof recorded_at !== "number" || !Number.isFinite(recorded_at)) return "";
  return new Date(recorded_at * 1000).toISOString().slice(11, 19) + "Z";
}

/**
 * Split `task:subtask:attempt` for display. The last two colons are the
 * separators, so `task-10:s1:0` yields taskId `task-10` -- a split on every
 * colon would mangle ids that contain one.
 */
export function parseManifestKey(key: string): ManifestKeyParts {
  const [prefix, attempt] = manifestOrderKey(key);
  const at = prefix.lastIndexOf(":");
  if (at < 0) return { taskId: prefix, subtaskId: "", attempt };
  return {
    taskId: prefix.slice(0, at),
    subtaskId: prefix.slice(at + 1),
    attempt,
  };
}

/** One-line scan of a compile: what it cost, what it dropped. */
export function manifestHeadline(
  summary: ContextManifestSummary | undefined,
): string {
  if (!summary) return "no summary recorded";
  const parts: string[] = [];
  const budget = summary.budget_tokens;
  const total = summary.total_tokens;
  if (typeof budget === "number" && typeof total === "number") {
    parts.push(`${total}/${budget} tok`);
  } else if (typeof total === "number") {
    parts.push(`${total} tok`);
  }
  if (typeof summary.stable_tokens === "number") {
    parts.push(`stable ${summary.stable_tokens}`);
  }
  if (summary.risk) parts.push(`risk ${summary.risk}`);
  if (summary.over_budget) parts.push("over budget");
  const dropped = summary.dropped?.length ?? 0;
  if (dropped > 0) parts.push(`${dropped} dropped`);
  return parts.join(" · ");
}

/** Per-tier item counts, in tier order ("P0 policy" sorts before "P1 state"). */
export function tierCountsLine(
  itemsByTier: Record<string, number> | undefined,
): string {
  if (!itemsByTier) return "";
  return Object.entries(itemsByTier)
    .filter(([, count]) => count > 0)
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([tier, count]) => `${tier} ${count}`)
    .join(" · ");
}

/** Drop reasons as one line: `P3 trace: budget (2)`. */
export function droppedLine(
  dropped: ContextManifestSummary["dropped"],
): string {
  if (!dropped || dropped.length === 0) return "";
  const counts = new Map<string, number>();
  for (const entry of dropped) {
    const label = entry.reason ?? "unspecified";
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([reason, count]) => (count > 1 ? `${reason} (${count})` : reason))
    .join(" · ");
}

/**
 * The count line under the trail header. `shown` is the window size (how
 * many records came back); `total` is the scan count (how many exist in
 * scope, from the endpoint's `total` field). When they differ the window
 * is truncated and the line must say so -- rendering `shown` as "N
 * recorded" understates the trail exactly when it is long enough to
 * matter. A missing/undefined `total` (older backend) degrades to the
 * shown count with no "of N" claim invented.
 */
export function manifestCountLine(
  shown: number,
  total: number | undefined,
): string {
  if (typeof total !== "number" || !Number.isFinite(total)) return `${shown} recorded`;
  if (total > shown) return `${shown} of ${total} recorded`;
  return `${total} recorded`;
}
