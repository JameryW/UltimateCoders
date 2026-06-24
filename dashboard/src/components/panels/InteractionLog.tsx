import { useRef, useCallback, useMemo, memo, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import type { TaskEvent } from "@/types/dashboard";

interface InteractionLogProps {
  events: TaskEvent[];
  filterSubtaskId?: string;
}

function eventTypeStyle(type: string): { border: string; dot: string } {
  if (type === "llm_request") return { border: "border-l-purple-500", dot: "text-purple-400" };
  if (type === "subtask_started") return { border: "border-l-yellow-500", dot: "text-yellow-400" };
  if (type === "tool_call") return { border: "border-l-blue-500", dot: "text-blue-400" };
  if (type === "tool_result") return { border: "border-l-green-500", dot: "text-green-400" };
  if (type === "subtask_completed") return { border: "border-l-green-500", dot: "text-green-400" };
  if (type === "subtask_failed") return { border: "border-l-red-500", dot: "text-red-400" };
  if (type === "task_completed") return { border: "border-l-green-500", dot: "text-green-400" };
  if (type === "sync_required") return { border: "border-l-orange-500", dot: "text-orange-400" };
  return { border: "border-l-[var(--border-color)]", dot: "text-[var(--text-secondary)]" };
}

function eventContent(ev: TaskEvent): string {
  const d = ev.data;
  if (ev.type === "llm_request") return `LLM request: ${String(d.model ?? "")}`;
  if (ev.type === "subtask_started") return `Started: ${String(d.description ?? "").substring(0, 40)}`;
  if (ev.type === "tool_call") return `Tool: ${String(d.tool ?? "")} ${String(d.input_summary ?? "").substring(0, 60)}`;
  if (ev.type === "tool_result") return `Result: ${String(d.result_summary ?? "").substring(0, 80)}`;
  if (ev.type === "subtask_completed") return `Completed: ${String(d.summary ?? "").substring(0, 60)}`;
  if (ev.type === "subtask_failed") return `Failed: ${String(d.error ?? "").substring(0, 60)}`;
  if (ev.type === "task_completed") return `Done: ${String(d.status ?? "completed")}`;
  if (ev.type === "sync_required") return `Sync required: ${String(d.reason ?? "unknown")}`;
  return ev.type;
}

// ponytail: determine if an event has expandable detail data
function hasDetailData(ev: TaskEvent): boolean {
  const d = ev.data;
  if (ev.type === "tool_call") return !!d.input || !!d.tool;
  if (ev.type === "tool_result") return !!d.result;
  if (ev.type === "llm_request") return !!d.prompt;
  if (ev.type === "subtask_failed") return !!d.error;
  return false;
}

function renderDetail(ev: TaskEvent): React.ReactNode {
  const d = ev.data;
  if (ev.type === "tool_call" && (d.input || d.tool)) {
    return (
      <div className="mt-0.5">
        {d.tool && <p className="text-[10px] text-[var(--text-muted)]">Tool: <span className="text-blue-400">{String(d.tool)}</span></p>}
        {d.input && <pre className="text-[10px] text-[var(--text-muted)] whitespace-pre-wrap break-all bg-[var(--bg-primary)]/50 rounded p-1 mt-0.5 max-h-32 overflow-y-auto">{String(d.input)}</pre>}
      </div>
    );
  }
  if (ev.type === "tool_result" && d.result) {
    return <pre className="text-[10px] text-[var(--text-muted)] whitespace-pre-wrap break-all bg-[var(--bg-primary)]/50 rounded p-1 mt-0.5 max-h-32 overflow-y-auto">{String(d.result)}</pre>;
  }
  if (ev.type === "llm_request" && d.prompt) {
    return <pre className="text-[10px] text-[var(--text-muted)] whitespace-pre-wrap break-all bg-[var(--bg-primary)]/50 rounded p-1 mt-0.5 max-h-32 overflow-y-auto">{String(d.prompt)}</pre>;
  }
  if (ev.type === "subtask_failed" && d.error) {
    return <pre className="text-[10px] text-red-400/80 whitespace-pre-wrap break-all bg-[var(--bg-primary)]/50 rounded p-1 mt-0.5 max-h-32 overflow-y-auto">{String(d.error)}</pre>;
  }
  return null;
}

const ROW_HEIGHT = 24;

export const InteractionLog = memo(function InteractionLog({ events, filterSubtaskId }: InteractionLogProps) {
  const filtered = useMemo(() =>
    filterSubtaskId
      ? events.filter((e) => e.subtask_id === filterSubtaskId)
      : events,
    [events, filterSubtaskId],
  );

  const parentRef = useRef<HTMLDivElement>(null);
  const [expandedIdx, setExpandedIdx] = useState<number | null>(null);

  const virtualizer = useVirtualizer({
    count: filtered.length,
    getScrollElement: () => parentRef.current,
    estimateSize: (idx) => expandedIdx === idx ? 120 : ROW_HEIGHT,
    overscan: 20,
  });

  if (filtered.length === 0) {
    return <p className="text-xs text-[var(--text-muted)]">No interaction events</p>;
  }

  return (
    <div ref={parentRef} className="max-h-64 overflow-auto space-y-0" role="log" aria-label="Interaction log" aria-live="polite">
      <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
        {virtualizer.getVirtualItems().map((virtualRow) => {
          const ev = filtered[virtualRow.index]!;
          const style = eventTypeStyle(ev.type);
          const time = ev.timestamp
            ? new Date(ev.timestamp).toLocaleTimeString()
            : "--";
          const isExpanded = expandedIdx === virtualRow.index;
          const canExpand = hasDetailData(ev);
          return (
            <div
              key={`${ev.timestamp}-${ev.type}-${virtualRow.index}`}
              className={`border-l-2 ${style.border} pl-2 py-0.5 text-xs ${canExpand ? "cursor-pointer hover:bg-[var(--bg-surface-alt)]/30" : ""}`}
              style={{
                position: "absolute",
                top: virtualRow.start,
                left: 0,
                width: "100%",
                height: virtualRow.size,
              }}
              onClick={() => canExpand && setExpandedIdx(isExpanded ? null : virtualRow.index)}
            >
              <span className="text-[var(--text-muted)] mr-1">{time}</span>
              <span className={style.dot}>●</span>{" "}
              <span className="text-[var(--text-primary)]">{eventContent(ev)}</span>
              {canExpand && <span className="text-[var(--text-muted)] ml-1">{isExpanded ? "[-]" : "[+]"}</span>}
              {isExpanded && renderDetail(ev)}
            </div>
          );
        })}
      </div>
    </div>
  );
});
