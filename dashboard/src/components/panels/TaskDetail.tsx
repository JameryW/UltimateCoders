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
  const idRef = useRef(`mermaid-${Date.now()}`);

  useEffect(() => {
    const hasDeps = subtasks.some((st) => st.depends_on.length > 0);
    if (!hasDeps || subtasks.length === 0) return;

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

    renderMermaid(idRef.current, graphDef).then((svg) => {
      if (svg) setSvg(DOMPurify.sanitize(svg));
    });
  }, [subtasks]);

  if (!svg) return null;

  return (
    <div className="mb-2">
      <p className="text-xs text-gray-400 mb-1">Subtask DAG:</p>
      <div dangerouslySetInnerHTML={{ __html: svg }} />
    </div>
  );
}

export function TaskDetail({ task, interactionLog }: TaskDetailProps) {
  const subtasks = task.subtasks ?? [];
  const [filterSubtaskId, setFilterSubtaskId] = useState("");

  return (
    <div className="pl-5 py-2 text-xs text-gray-400 space-y-3">
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
