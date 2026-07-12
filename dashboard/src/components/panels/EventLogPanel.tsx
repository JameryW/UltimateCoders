import { useState, useMemo, memo, useRef, useCallback, useEffect, useDeferredValue } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { EmptyState } from "@/components/ui/empty-state";
import type { DashboardEvent } from "@/types/dashboard";

function eventTypeColor(type: string): string {
  if (type.startsWith("task_submitted")) return "text-blue-500";
  if (type.startsWith("task_completed")) return "text-green-500";
  if (type.startsWith("task_failed")) return "text-red-500";
  if (type.startsWith("task_pause")) return "text-yellow-500";
  if (type.startsWith("task_resume")) return "text-green-500";
  if (type.startsWith("subtask_started") || type.startsWith("subtask_assigned")) return "text-blue-400";
  if (type.startsWith("subtask_completed")) return "text-green-400";
  if (type.startsWith("subtask_failed")) return "text-red-400";
  if (type.startsWith("subtask_progress")) return "text-cyan-400";
  if (type.startsWith("scheduler_trigger")) return "text-green-500";
  return "text-[var(--text-secondary)]";
}

function eventTypeBg(type: string): string {
  if (type.startsWith("task_submitted")) return "evt-submitted";
  if (type.startsWith("task_completed")) return "evt-completed";
  if (type.startsWith("task_failed")) return "evt-failed";
  if (type.startsWith("task_pause")) return "evt-pause";
  if (type.startsWith("task_resume")) return "evt-resume";
  if (type.startsWith("subtask_started") || type.startsWith("subtask_assigned")) return "evt-started";
  if (type.startsWith("subtask_completed")) return "evt-completed";
  if (type.startsWith("subtask_failed")) return "evt-failed";
  if (type.startsWith("subtask_progress")) return "evt-started";
  if (type.startsWith("scheduler_trigger")) return "evt-trigger";
  return "evt-default";
}

function formatTimestamp(ts: string): string {
  try {
    const d = new Date(ts);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return ts;
  }
}

function eventSummary(details: Record<string, unknown>): string {
  const keys = Object.keys(details);
  if (keys.length === 0) return "";
  const first = keys[0]!;
  const val = details[first];
  if (typeof val === "string") return `${first}: ${val}`;
  return `${first}: ${JSON.stringify(val)}`;
}

/** Compact summary for subtask_progress events: "agent · phase · NN%".
 *  Highlights the coding agent, phase, and percent — the three fields users
 *  care about for real-time execution telemetry. */
function progressSummary(details: Record<string, unknown>): string {
  const parts: string[] = [];
  const agent = details.step_agent;
  if (typeof agent === "string" && agent) parts.push(agent);
  const phase = details.phase;
  if (typeof phase === "string" && phase) parts.push(phase);
  const percent = details.percent;
  if (typeof percent === "number") parts.push(`${Math.round(percent)}%`);
  return parts.join(" · ");
}

/** ponytail: stable key — timestamp+type is unique per event in practice */
function eventKey(evt: DashboardEvent): string {
  return `${evt.timestamp}-${evt.type}`;
}


// ponytail: export events as JSON download
function exportEvents(events: DashboardEvent[]): void {
  const blob = new Blob([JSON.stringify(events, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "events-" + new Date().toISOString().slice(0, 10) + ".json";
  a.click();
  URL.revokeObjectURL(url);
}

const ERROR_TYPES: ReadonlySet<string> = new Set(["task_failed", "subtask_failed"]);

export const EventLogPanel = memo(function EventLogPanel({ events, stale, onSelectTask }: { events: DashboardEvent[]; stale?: boolean; onSelectTask?: (taskId: string) => void }) {
  const [typeFilter, setTypeFilter] = useState<string | null>(null);
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [timeRange, setTimeRange] = useState<"5m" | "30m" | "1h" | "all">("all");
  const [searchQuery, setSearchQuery] = useState("");
  const [expandedIdx, setExpandedIdx] = useState<number | null>(null);
  // ponytail: deferred search — expensive JSON.stringify filter only runs after user pauses typing
  const deferredSearch = useDeferredValue(searchQuery);

  const uniqueTypes = useMemo(
    () => [...new Set(events.map((e) => e.type))],
    [events]
  );

  const TIME_RANGE_MS: Record<string, number> = { "5m": 300_000, "30m": 1_800_000, "1h": 3_600_000 };

  const filteredEvents = useMemo(() => {
    let result = events;
    // Time range filter
    if (timeRange !== "all") {
      const cutoff = Date.now() - (TIME_RANGE_MS[timeRange] ?? 0);
      result = result.filter((e) => new Date(e.timestamp).getTime() >= cutoff);
    }
    if (errorsOnly) {
      result = result.filter((e) => ERROR_TYPES.has(e.type));
    } else if (typeFilter) {
      result = result.filter((e) => e.type === typeFilter);
    }
    if (deferredSearch.trim()) {
      const q = deferredSearch.toLowerCase();
      result = result.filter((e) =>
        e.type.toLowerCase().includes(q) ||
        JSON.stringify(e.details).toLowerCase().includes(q)
      );
    }
    return result;
  }, [events, typeFilter, errorsOnly, timeRange, deferredSearch]);

  // ── Virtual scrolling + tail mode ──────────────────────
  const scrollRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);

  const virtualizer = useVirtualizer({
    count: filteredEvents.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 28, // ponytail: ~28px per row (py-1 + text-xs)
    overscan: 5,
  });

  // Detect user scroll: pause auto-scroll when scrolling up
  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
    setAutoScroll(atBottom);
  }, []);

  // Auto-scroll to bottom when new events arrive and autoScroll is on
  useEffect(() => {
    if (!autoScroll || filteredEvents.length === 0) return;
    virtualizer.scrollToIndex(filteredEvents.length - 1, { align: "end" });
  }, [filteredEvents.length, autoScroll, virtualizer]);

  const scrollToLatest = useCallback(() => {
    setAutoScroll(true);
    virtualizer.scrollToIndex(filteredEvents.length - 1, { align: "end" });
  }, [filteredEvents.length, virtualizer]);

  return (
    <Card stale={stale}>
      <CardHeader>
        <CardTitle>Event Log</CardTitle>
        <div className="flex items-center gap-2">
          <Badge>{filteredEvents.length}</Badge>
          {filteredEvents.length > 0 && (
            <button
              onClick={() => exportEvents(filteredEvents)}
              className="btn-action-info px-2 py-0.5 rounded text-xs cursor-pointer"
              aria-label="Export events"
            >
              Export
            </button>
          )}
        </div>
      </CardHeader>

      {events.length > 0 && (
        <div className="space-y-2 mb-2">
          <div className="flex items-center gap-2">
            <button
              onClick={() => { setErrorsOnly(!errorsOnly); if (!errorsOnly) setTypeFilter(null); }}
              className={cn(
                "text-xs px-2 py-0.5 rounded cursor-pointer border",
                errorsOnly
                  ? "bg-red-500/20 text-red-400 border-red-500/40"
                  : "text-[var(--text-muted)] border-[var(--border-color)] hover:text-red-400"
              )}
            >
              Errors
            </button>
            {!errorsOnly && uniqueTypes.length > 1 && (
              <div className="flex flex-wrap gap-1">
                {uniqueTypes.map((type) => (
                  <button
                    key={type}
                    onClick={() => setTypeFilter(typeFilter === type ? null : type)}
                    className={cn(
                      "text-xs px-1.5 py-0.5 rounded cursor-pointer",
                      eventTypeBg(type),
                      eventTypeColor(type),
                      typeFilter === type && "ring-1 ring-[var(--text-muted)]"
                    )}
                  >
                    {type}
                  </button>
                ))}
              </div>
            )}
          </div>
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search events..."
            className="w-full bg-[var(--bg-primary)] border border-[var(--border-color)] rounded-md px-3 py-2 text-sm text-[var(--text-primary)] placeholder-[var(--text-muted)] focus:border-blue-500 focus:outline-none"
          />
          {/* Time range filter */}
          <div className="flex items-center bg-[var(--bg-primary)] rounded-md border border-[var(--border-color)] overflow-hidden">
            {(["5m", "30m", "1h", "all"] as const).map((r) => (
              <button
                key={r}
                onClick={() => setTimeRange(r)}
                className={cn(
                  "text-xs px-2 py-0.5 transition-colors cursor-pointer",
                  timeRange === r ? "bg-blue-600 text-white" : "text-[var(--text-muted)] hover:text-[var(--text-primary)]"
                )}
              >
                {r === "all" ? "All" : r}
              </button>
            ))}
          </div>
        </div>
      )}

      {filteredEvents.length === 0 ? (
        <EmptyState
          icon="activity"
          title={events.length === 0 ? "No events yet" : "No matching events"}
          description={events.length === 0 ? "Events will appear here as tasks are submitted and processed" : "Try adjusting your search or filter"}
        />
      ) : (
        <div className="relative">
          <div
            ref={scrollRef}
            onScroll={onScroll}
            className="max-h-64 overflow-y-auto"
            aria-label="Event log"
            aria-live="polite"
          >
            <div
              style={{
                height: `${virtualizer.getTotalSize()}px`,
                width: "100%",
                position: "relative",
              }}
            >
              {virtualizer.getVirtualItems().map((item) => {
                const evt = filteredEvents[item.index]!;
                const isExpanded = expandedIdx === item.index;
                return (
                  <div
                    key={eventKey(evt)}
                    style={{
                      position: "absolute",
                      top: 0,
                      left: 0,
                      width: "100%",
                      height: `${item.size}px`,
                      transform: `translateY(${item.start}px)`,
                    }}
                    className={cn("flex items-start gap-2 px-2 py-1 text-xs", eventTypeBg(evt.type))}
                  >
                    <span className="text-[var(--text-muted)] shrink-0 font-mono">
                      {formatTimestamp(evt.timestamp)}
                    </span>
                    <span className={cn("shrink-0 font-medium", eventTypeColor(evt.type))}>
                      {evt.type}
                    </span>
                    {/* ponytail: clickable task_id to navigate to task */}
                    {evt.details.task_id && typeof evt.details.task_id === "string" && onSelectTask ? (
                      <button
                        onClick={() => onSelectTask(evt.details.task_id as string)}
                        className="text-blue-400 hover:text-blue-300 hover:underline font-mono shrink-0"
                        title="Go to task"
                      >
                        {(evt.details.task_id as string).slice(0, 8)}
                      </button>
                    ) : null}
                    {Object.keys(evt.details).length > 0 && (
                      isExpanded ? (
                        <pre
                          className="text-[var(--text-muted)] text-[10px] whitespace-pre-wrap break-all cursor-pointer hover:text-[var(--text-secondary)]"
                          onClick={() => setExpandedIdx(null)}
                        >
                          {JSON.stringify(evt.details, null, 2)}
                        </pre>
                      ) : (
                        <span
                          className={cn(
                            "truncate cursor-pointer hover:text-[var(--text-secondary)]",
                            evt.type === "subtask_progress"
                              ? "text-cyan-400 font-medium"
                              : "text-[var(--text-muted)]"
                          )}
                          onClick={() => setExpandedIdx(item.index)}
                          title="Click to expand"
                        >
                          {evt.type === "subtask_progress"
                            ? progressSummary(evt.details) || eventSummary(evt.details)
                            : eventSummary(evt.details)}
                        </span>
                      )
                    )}
                  </div>
                );
              })}
            </div>
          </div>

          {/* "Return to latest" button when user scrolled up */}
          {!autoScroll && (
            <button
              onClick={scrollToLatest}
              className="absolute bottom-1 right-2 text-xs bg-blue-500/80 text-white px-2 py-0.5 rounded hover:bg-blue-500 transition-colors"
            >
              ↓ Latest
            </button>
          )}
        </div>
      )}
    </Card>
  );
});
