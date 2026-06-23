import { useState, useMemo, memo, useRef, useCallback, useEffect } from "react";
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
  if (type.startsWith("circuit_breaker_reset")) return "text-yellow-500";
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
  if (type.startsWith("circuit_breaker_reset")) return "evt-cb-reset";
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

/** ponytail: stable key — timestamp+type is unique per event in practice */
function eventKey(evt: DashboardEvent): string {
  return `${evt.timestamp}-${evt.type}`;
}

export const EventLogPanel = memo(function EventLogPanel({ events, stale }: { events: DashboardEvent[]; stale?: boolean }) {
  const [typeFilter, setTypeFilter] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");

  const uniqueTypes = useMemo(
    () => [...new Set(events.map((e) => e.type))],
    [events]
  );

  const filteredEvents = useMemo(() => {
    let result = events;
    if (typeFilter) {
      result = result.filter((e) => e.type === typeFilter);
    }
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      result = result.filter((e) =>
        e.type.toLowerCase().includes(q) ||
        JSON.stringify(e.details).toLowerCase().includes(q)
      );
    }
    return result;
  }, [events, typeFilter, searchQuery]);

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
        <Badge>{filteredEvents.length}</Badge>
      </CardHeader>

      {events.length > 0 && (
        <div className="space-y-2 mb-2">
          {uniqueTypes.length > 1 && (
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
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search events..."
            className="w-full bg-[var(--bg-primary)] border border-[var(--border-color)] rounded-md px-3 py-2 text-sm text-[var(--text-primary)] placeholder-[var(--text-muted)] focus:border-blue-500 focus:outline-none"
          />
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
                    {Object.keys(evt.details).length > 0 && (
                      <span className="text-[var(--text-muted)] truncate">
                        {eventSummary(evt.details)}
                      </span>
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
