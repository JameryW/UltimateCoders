import { useState, useMemo, memo, useRef, useCallback, useEffect, useDeferredValue } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { EmptyState } from "@/components/ui/empty-state";
import { sessionRoleForEventType, type SessionEventRole } from "@/lib/taskProgress";
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
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
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

/** Compact summary for subtask_progress events: "agent · phase · NN%". */
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

function eventMessage(evt: DashboardEvent): string {
  if (evt.type === "subtask_progress") {
    return progressSummary(evt.details) || eventSummary(evt.details) || evt.type.replace(/_/g, " ");
  }
  const summary = eventSummary(evt.details);
  return summary ? `${evt.type.replace(/_/g, " ")} → ${summary}` : evt.type.replace(/_/g, " ");
}

/** Identity-stable key — timestamp+type (+task/subtask when present). */
function eventKey(evt: DashboardEvent): string {
  const taskId = typeof evt.details?.task_id === "string" ? evt.details.task_id : "";
  const subtaskId = typeof evt.details?.subtask_id === "string" ? evt.details.subtask_id : "";
  return `${evt.timestamp}|${evt.type}|${taskId}|${subtaskId}`;
}

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

const TIME_RANGE_MS: Record<string, number> = { "5m": 300_000, "30m": 1_800_000, "1h": 3_600_000 };

const ROW_HEIGHT_DEFAULT = 28;
const ROW_HEIGHT_TERMINAL = 24;

function chipRoleClass(role: SessionEventRole): string {
  return `role-${role}`;
}

export const EventLogPanel = memo(function EventLogPanel({
  events,
  stale,
  onSelectTask,
  variant = "default",
}: {
  events: DashboardEvent[];
  stale?: boolean;
  onSelectTask?: (taskId: string) => void;
  /** Command-center monospaced session rows (role colors, terminal chips). */
  variant?: "default" | "terminal";
}) {
  const terminal = variant === "terminal";
  const [typeFilter, setTypeFilter] = useState<string | null>(null);
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [timeRange, setTimeRange] = useState<"5m" | "30m" | "1h" | "all">("all");
  const [searchQuery, setSearchQuery] = useState("");
  const [expandedIdx, setExpandedIdx] = useState<number | null>(null);
  const deferredSearch = useDeferredValue(searchQuery);

  const uniqueTypes = useMemo(
    () => [...new Set(events.map((e) => e.type))],
    [events],
  );

  const filteredEvents = useMemo(() => {
    let result = events;
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
        e.type.toLowerCase().includes(q)
        || JSON.stringify(e.details).toLowerCase().includes(q),
      );
    }
    return result;
  }, [events, typeFilter, errorsOnly, timeRange, deferredSearch]);

  const scrollRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);

  const virtualizer = useVirtualizer({
    count: filteredEvents.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => (terminal ? ROW_HEIGHT_TERMINAL : ROW_HEIGHT_DEFAULT),
    overscan: 5,
  });

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
    setAutoScroll(atBottom);
  }, []);

  useEffect(() => {
    if (!autoScroll || filteredEvents.length === 0) return;
    virtualizer.scrollToIndex(filteredEvents.length - 1, { align: "end" });
  }, [filteredEvents.length, autoScroll, virtualizer]);

  const scrollToLatest = useCallback(() => {
    setAutoScroll(true);
    virtualizer.scrollToIndex(filteredEvents.length - 1, { align: "end" });
  }, [filteredEvents.length, virtualizer]);

  const filters = events.length > 0 ? (
    <div className={cn(terminal ? "terminal-logs-filters" : "space-y-2 mb-2")}>
      <div className={cn("flex items-center gap-2 flex-wrap", terminal && "terminal-logs-filter-row")}>
        <button
          type="button"
          onClick={() => { setErrorsOnly(!errorsOnly); if (!errorsOnly) setTypeFilter(null); }}
          className={cn(
            terminal
              ? cn("terminal-log-chip", errorsOnly && "is-active error")
              : cn(
                "text-xs px-2 py-0.5 rounded cursor-pointer border",
                errorsOnly
                  ? "bg-red-500/20 text-red-400 border-red-500/40"
                  : "text-[var(--text-muted)] border-[var(--border-color)] hover:text-red-400",
              ),
          )}
          data-testid={terminal ? "terminal-log-chip-errors" : undefined}
        >
          Errors
        </button>
        {!errorsOnly && uniqueTypes.length > 1 && (
          <div className={cn("flex flex-wrap gap-1", terminal && "terminal-logs-type-chips")}>
            {uniqueTypes.map((type) => {
              const role = sessionRoleForEventType(type);
              return (
                <button
                  key={type}
                  type="button"
                  onClick={() => setTypeFilter(typeFilter === type ? null : type)}
                  className={cn(
                    terminal
                      ? cn(
                        "terminal-log-chip",
                        chipRoleClass(role),
                        typeFilter === type && "is-active",
                      )
                      : cn(
                        "text-xs px-1.5 py-0.5 rounded cursor-pointer",
                        eventTypeBg(type),
                        eventTypeColor(type),
                        typeFilter === type && "ring-1 ring-[var(--text-muted)]",
                      ),
                  )}
                  data-testid={terminal ? "terminal-log-chip-type" : undefined}
                  data-event-type={type}
                  data-session-role={role}
                >
                  {type}
                </button>
              );
            })}
          </div>
        )}
        {terminal && filteredEvents.length > 0 && (
          <button
            type="button"
            onClick={() => exportEvents(filteredEvents)}
            className="terminal-log-chip role-dispatch"
            aria-label="Export events"
            data-testid="terminal-log-chip-export"
          >
            Export
          </button>
        )}
      </div>
      <input
        type="text"
        value={searchQuery}
        onChange={(e) => setSearchQuery(e.target.value)}
        placeholder={terminal ? "search events…" : "Search events..."}
        className={cn(
          terminal
            ? "terminal-logs-search"
            : "w-full bg-[var(--bg-primary)] border border-[var(--border-color)] rounded-md px-3 py-2 text-sm text-[var(--text-primary)] placeholder-[var(--text-muted)] focus:border-blue-500 focus:outline-none",
        )}
        aria-label="Search events"
        data-testid={terminal ? "terminal-logs-search" : undefined}
      />
      <div
        className={cn(
          terminal
            ? "terminal-logs-range"
            : "flex items-center bg-[var(--bg-primary)] rounded-md border border-[var(--border-color)] overflow-hidden",
        )}
        role="group"
        aria-label="Time range"
      >
        {(["5m", "30m", "1h", "all"] as const).map((r) => (
          <button
            key={r}
            type="button"
            onClick={() => setTimeRange(r)}
            className={cn(
              terminal
                ? cn("terminal-log-chip", timeRange === r && "is-active swarm")
                : cn(
                  "text-xs px-2 py-0.5 transition-colors cursor-pointer",
                  timeRange === r ? "bg-blue-600 text-white" : "text-[var(--text-muted)] hover:text-[var(--text-primary)]",
                ),
            )}
            data-testid={terminal ? "terminal-log-chip-range" : undefined}
          >
            {r === "all" ? "All" : r}
          </button>
        ))}
      </div>
    </div>
  ) : null;

  const emptyMessage = events.length === 0
    ? (terminal ? "waiting for live events…" : null)
    : (terminal ? "no matching events · adjust filters" : null);

  const virtualItems = virtualizer.getVirtualItems();
  // SSR / zero-metric scroll parents yield no virtual rows — fall back to a
  // capped static list so terminal session roles still render in markup.
  const rowIndexes = virtualItems.length > 0
    ? virtualItems.map((item) => item.index)
    : filteredEvents.slice(0, 40).map((_, index) => index);
  const useVirtualLayout = virtualItems.length > 0;

  const stream = filteredEvents.length === 0 ? (
    terminal ? (
      <div className="terminal-empty-line" data-testid="terminal-logs-empty">
        {emptyMessage}
      </div>
    ) : (
      <EmptyState
        icon="activity"
        title={events.length === 0 ? "No events yet" : "No matching events"}
        description={events.length === 0 ? "Events will appear here as tasks are submitted and processed" : "Try adjusting your search or filter"}
      />
    )
  ) : (
    <div className={cn("relative", terminal && "terminal-logs-stream-wrap")}>
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className={cn(terminal ? "terminal-logs-stream" : "max-h-64 overflow-y-auto")}
        aria-label="Event log"
        aria-live="polite"
        data-testid={terminal ? "terminal-logs-stream" : undefined}
      >
        <div
          style={useVirtualLayout ? {
            height: `${virtualizer.getTotalSize()}px`,
            width: "100%",
            position: "relative",
          } : undefined}
        >
          {rowIndexes.map((rowIndex) => {
            const evt = filteredEvents[rowIndex]!;
            const isExpanded = expandedIdx === rowIndex;
            const role = sessionRoleForEventType(evt.type);
            const virtualRow = virtualItems.find((item) => item.index === rowIndex);
            const layoutStyle = useVirtualLayout && virtualRow
              ? {
                  position: "absolute" as const,
                  top: 0,
                  left: 0,
                  width: "100%",
                  height: `${virtualRow.size}px`,
                  transform: `translateY(${virtualRow.start}px)`,
                }
              : undefined;
            const taskId = typeof evt.details.task_id === "string" ? evt.details.task_id : null;
            const hasDetails = Object.keys(evt.details).length > 0;

            if (terminal) {
              return (
                <div
                  key={eventKey(evt)}
                  className={cn(
                    "cc-session-row",
                    `cc-session-role-${role}`,
                    hasDetails && "cc-session-expandable",
                  )}
                  data-session-role={role}
                  data-event-type={evt.type}
                  data-testid="terminal-log-row"
                  style={layoutStyle}
                  onClick={() => {
                    if (taskId && onSelectTask && !hasDetails) {
                      onSelectTask(taskId);
                      return;
                    }
                    if (hasDetails) setExpandedIdx(isExpanded ? null : rowIndex);
                  }}
                >
                  <span className="cc-session-line">
                    <time>{formatTimestamp(evt.timestamp)}</time>
                    {" "}
                    <span className="cc-session-role">{role}</span>
                    {" "}
                    <span className="cc-session-msg">{eventMessage(evt)}</span>
                    {taskId && onSelectTask && (
                      <button
                        type="button"
                        className="terminal-logs-task-jump"
                        onClick={(e) => {
                          e.stopPropagation();
                          onSelectTask(taskId);
                        }}
                        title="Go to task"
                      >
                        {taskId.slice(0, 8)}
                      </button>
                    )}
                    {hasDetails && (
                      <span className="cc-session-expand">{isExpanded ? "[-]" : "[+]"}</span>
                    )}
                  </span>
                  {isExpanded && (
                    <pre
                      className="terminal-logs-expand"
                      onClick={() => setExpandedIdx(null)}
                    >
                      {JSON.stringify(evt.details, null, 2)}
                    </pre>
                  )}
                </div>
              );
            }

            return (
              <div
                key={eventKey(evt)}
                style={layoutStyle}
                className={cn("flex items-start gap-2 px-2 py-1 text-xs", eventTypeBg(evt.type))}
              >
                <span className="text-[var(--text-muted)] shrink-0 font-mono">
                  {formatTimestamp(evt.timestamp)}
                </span>
                <span className={cn("shrink-0 font-medium", eventTypeColor(evt.type))}>
                  {evt.type}
                </span>
                {taskId && onSelectTask ? (
                  <button
                    type="button"
                    onClick={() => onSelectTask(taskId)}
                    className="text-blue-400 hover:text-blue-300 hover:underline font-mono shrink-0"
                    title="Go to task"
                  >
                    {taskId.slice(0, 8)}
                  </button>
                ) : null}
                {hasDetails && (
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
                          : "text-[var(--text-muted)]",
                      )}
                      onClick={() => setExpandedIdx(rowIndex)}
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

      {!autoScroll && (
        <button
          type="button"
          onClick={scrollToLatest}
          className={cn(
            terminal
              ? "terminal-logs-latest"
              : "absolute bottom-1 right-2 text-xs bg-blue-500/80 text-white px-2 py-0.5 rounded hover:bg-blue-500 transition-colors",
          )}
          data-testid={terminal ? "terminal-logs-latest" : undefined}
        >
          ↓ Latest
        </button>
      )}
    </div>
  );

  if (terminal) {
    return (
      <section
        className={cn("terminal-logs-panel", stale && "is-stale")}
        aria-label="Event log"
        data-testid="terminal-logs-panel"
        data-stale={stale ? "true" : "false"}
      >
        <div className="cc-session-log-head terminal-logs-head">
          <span>session log · events</span>
          <b data-testid="terminal-logs-count">{filteredEvents.length}</b>
        </div>
        {stale && (
          <div className="terminal-logs-stale" role="status" data-testid="terminal-logs-stale">
            stream stale · waiting for gateway refresh
          </div>
        )}
        {filters}
        {stream}
      </section>
    );
  }

  return (
    <Card stale={stale}>
      <CardHeader>
        <CardTitle>Event Log</CardTitle>
        <div className="flex items-center gap-2">
          <Badge>{filteredEvents.length}</Badge>
          {filteredEvents.length > 0 && (
            <button
              type="button"
              onClick={() => exportEvents(filteredEvents)}
              className="btn-action-info px-2 py-0.5 rounded text-xs cursor-pointer"
              aria-label="Export events"
            >
              Export
            </button>
          )}
        </div>
      </CardHeader>
      {filters}
      {stream}
    </Card>
  );
});
