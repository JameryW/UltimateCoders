import { useState, useEffect, useRef } from "react";
import DOMPurify from "dompurify";
import type { TaskSummary, SubtaskSummary, TaskEvent } from "@/types/dashboard";
import { InteractionLog } from "@/components/panels/InteractionLog";
import { renderMermaid } from "@/lib/mermaid";
import { cn, shortId, truncate } from "@/lib/utils";

interface TaskDetailProps {
  task: TaskSummary;
  interactionLog: TaskEvent[];
}

function statusBadgeClass(status: string): string {
  switch (status) {
    case "completed": return "bg-green-900/50 text-green-300";
    case "failed": return "bg-red-900/50 text-red-300";
    case "paused": return "bg-yellow-900/50 text-yellow-300";
    case "in_progress": return "bg-blue-900/50 text-blue-300";
    default: return "bg-gray-800 text-gray-400";
  }
}

function OutputFiles({ events }: { events: TaskEvent[] }) {
  const files: { path: string; type: string; subtask: string }[] = [];
  for (const ev of events) {
    if (ev.type === "subtask_completed" && ev.data.modified_files) {
      const mf = ev.data.modified_files as Array<{ path: string; type: string }>;
      for (const f of mf) {
        files.push({ path: f.path, type: f.type, subtask: ev.subtask_id ?? "" });
      }
    }
  }
  if (files.length === 0) return null;

  return (
    <div className="mb-2">
      <p className="text-xs text-gray-400 mb-1">
        Output Files: <span className="text-gray-500">{files.length} changed</span>
      </p>
      <div className="space-y-0.5">
        {files.map((f, i) => {
          const icon = f.type === "created" ? "+" : f.type === "deleted" ? "−" : "~";
          const color = f.type === "created" ? "text-green-400" : f.type === "deleted" ? "text-red-400" : "text-yellow-400";
          const bg = f.type === "created" ? "bg-green-900/20" : f.type === "deleted" ? "bg-red-900/20" : "bg-yellow-900/20";
          return (
            <div key={i} className={cn("flex items-center gap-1.5 py-0.5 px-2 rounded text-xs", bg)}>
              <span className={cn("font-mono font-bold w-3 text-center", color)}>{icon}</span>
              <span className="font-mono text-gray-300 truncate flex-1" title={f.path}>{f.path}</span>
              <span className={cn("text-[10px] px-1 rounded", color)}>{f.type.toUpperCase()}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function SubtaskDAG({ subtasks }: { subtasks: SubtaskSummary[] }) {
  const [svg, setSvg] = useState<string | null>(null);
  const [renderFailed, setRenderFailed] = useState(false);
  // Counter ensures a fresh Mermaid DOM ID on every render cycle,
  // preventing collisions when the same task is closed and re-opened.
  const idCounter = useRef(0);

  useEffect(() => {
    const hasDeps = subtasks.some((st) => st.depends_on.length > 0);
    if (!hasDeps || subtasks.length === 0) {
      setSvg(null);
      setRenderFailed(false);
      return;
    }

    const idMap: Record<string, string> = {};
    subtasks.forEach((st, i) => {
      idMap[st.id] = `s${i + 1}`;
    });

    let graphDef = "graph LR\n";
    for (const st of subtasks) {
      const nodeId = idMap[st.id]!;
      const label = truncate(st.description, 25).replace(/"/g, "'");
      graphDef += `  ${nodeId}["${label}"]\n`;
      for (const depId of st.depends_on) {
        const depNode = idMap[depId];
        if (depNode) graphDef += `  ${depNode} --> ${nodeId}\n`;
      }
    }

    // Increment counter each effect run so Mermaid gets a unique container ID
    idCounter.current += 1;
    const mermaidId = `mermaid-dag-${idCounter.current}-${Date.now()}`;

    renderMermaid(mermaidId, graphDef).then((result) => {
      if (result) {
        setSvg(DOMPurify.sanitize(result));
        setRenderFailed(false);
      } else {
        setSvg(null);
        setRenderFailed(true);
      }
    });
  }, [subtasks]);

  if (!svg && !renderFailed) return null;

  if (renderFailed && !svg) {
    return (
      <div className="mb-2">
        <p className="text-xs text-gray-400 mb-1">Subtask DAG:</p>
        <p className="text-xs text-gray-500 italic">No dependency graph available</p>
      </div>
    );
  }

  return (
    <div className="mb-2">
      <p className="text-xs text-gray-400 mb-1">Subtask DAG:</p>
      <div dangerouslySetInnerHTML={{ __html: svg! }} role="img" aria-label="Subtask dependency graph" />
    </div>
  );
}


function SubtaskProgressBar({ subtasks }: { subtasks: SubtaskSummary[] }) {
  if (subtasks.length === 0) return null;
  const completed = subtasks.filter((s) => s.status === "completed").length;
  const failed = subtasks.filter((s) => s.status === "failed").length;
  const total = subtasks.length;
  const pct = Math.round((completed / total) * 100);
  return (
    <div className="mb-2">
      <div className="flex items-center justify-between mb-0.5">
        <span className="text-xs text-gray-400">Progress</span>
        <span className="text-xs text-gray-500">{completed}/{total} ({pct}%)</span>
      </div>
      <div className="h-1.5 bg-gray-800 rounded-full overflow-hidden flex">
        {completed > 0 && <div className="bg-green-500 h-full" style={{ width: (completed / total) * 100 + "%" }} />}
        {failed > 0 && <div className="bg-red-500 h-full" style={{ width: (failed / total) * 100 + "%" }} />}
      </div>
      {failed > 0 && <span className="text-[10px] text-red-400">{failed} failed</span>}
    </div>
  );
}

function EventTimeline({ events }: { events: TaskEvent[] }) {
  const [showAll, setShowAll] = useState(false);
  if (events.length === 0) return null;
  const EVENT_PREVIEW_COUNT = 50;
  const recent = showAll ? [...events].reverse() : events.slice(-EVENT_PREVIEW_COUNT).reverse();
  const typeIcon: Record<string, string> = {
    task_submitted: "📤", subtask_started: "▶️", subtask_assigned: "👤",
    subtask_completed: "✅", subtask_failed: "❌", tool_call: "🔧",
    tool_result: "📋", task_completed: "🏁",
  };
  return (
    <div className="mb-2">
      <p className="text-xs text-gray-400 mb-1">Timeline:</p>
      <div className="space-y-0.5 max-h-48 overflow-y-auto">
        {recent.map((ev, i) => (
          <div key={i} className="flex items-center gap-1.5 text-xs">
            <span>{typeIcon[ev.type] ?? "•"}</span>
            <span className="text-gray-500 w-16 shrink-0">{ev.timestamp ? new Date(ev.timestamp).toLocaleTimeString() : ""}</span>
            <span className="text-gray-300 truncate">{ev.type}</span>
            {ev.subtask_id && <span className="text-gray-600 font-mono">{shortId(ev.subtask_id)}</span>}
          </div>
        ))}
      </div>
      {!showAll && events.length > EVENT_PREVIEW_COUNT && (
        <button onClick={() => setShowAll(true)} className="text-[10px] text-blue-400 hover:underline mt-1">
          Show all {events.length} events
        </button>
      )}
    </div>
  );
}

export function TaskDetail({ task, interactionLog }: TaskDetailProps) {
  const subtasks = task.subtasks ?? [];
  const [filterSubtaskId, setFilterSubtaskId] = useState("");

  return (
    <div className="pl-5 py-2 text-xs text-gray-400 space-y-3" role="region" aria-label={`Task detail: ${task.description}`}>
      {/* Progress bar */}
      <SubtaskProgressBar subtasks={subtasks} />

      {/* Event timeline */}
      <EventTimeline events={interactionLog} />

      {/* Subtask list */}
      {subtasks.length > 0 && (
        <div>
          <p className="text-gray-300 font-medium mb-1">Subtasks:</p>
          <div className="space-y-0.5">
            {subtasks.map((st) => (
              <div key={st.id} className="flex items-center justify-between">
                <span className="truncate max-w-[200px]">
                  {truncate(st.description, 50)}
                  {st.depends_on.length > 0 && (
                    <span className="text-gray-600 ml-2">
                      deps: {st.depends_on.map((d) => shortId(d)).join(", ")}
                    </span>
                  )}
                </span>
                <span className={cn("text-xs px-1.5 py-0.5 rounded", statusBadgeClass(st.status))}>
                  {st.status}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Interaction log with subtask filter */}
      <div>
        <div className="flex items-center justify-between mb-1">
          <span className="text-gray-300 font-medium">Interaction Log:</span>
          {subtasks.length > 1 && (
            <select
              value={filterSubtaskId}
              onChange={(e) => setFilterSubtaskId(e.target.value)}
              aria-label="Filter by subtask"
              className="text-xs bg-dark-900 border border-dark-700 text-gray-300 rounded px-2 py-1"
            >
              <option value="">All subtasks</option>
              {subtasks.map((st) => (
                <option key={st.id} value={st.id}>
                  {truncate(st.description, 25)} ({shortId(st.id)})
                </option>
              ))}
            </select>
          )}
        </div>
        <InteractionLog events={interactionLog} filterSubtaskId={filterSubtaskId || undefined} />
      </div>

      {/* Output files */}
      <OutputFiles events={interactionLog} />

      {/* Mermaid DAG */}
      <SubtaskDAG subtasks={subtasks} />
    </div>
  );
}
