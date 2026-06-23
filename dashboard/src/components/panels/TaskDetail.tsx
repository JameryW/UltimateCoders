import { useState, useEffect, useRef } from "react";
import DOMPurify from "dompurify";
import type { TaskSummary, SubtaskSummary, TaskEvent } from "@/types/dashboard";
import { InteractionLog } from "@/components/panels/InteractionLog";
import type { FileBrowserNavigateEvent } from "@/components/panels/FileBrowser";
import { renderMermaid } from "@/lib/mermaid";
import { cn, shortId, truncate, statusBadgeClass } from "@/lib/utils";

interface TaskDetailProps {
  task: TaskSummary;
  interactionLog: TaskEvent[];
  onNavigateFile?: (nav: FileBrowserNavigateEvent) => void;
}

function OutputFiles({ events, onNavigateFile }: { events: TaskEvent[]; onNavigateFile?: (nav: FileBrowserNavigateEvent) => void }) {
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
      <p className="text-xs text-[var(--text-secondary)] mb-1">
        Output Files: <span className="text-[var(--text-muted)]">{files.length} changed</span>
      </p>
      <div className="space-y-0.5">
        {files.map((f) => {
          const icon = f.type === "created" ? "+" : f.type === "deleted" ? "−" : "~";
          const color = f.type === "created" ? "text-green-500" : f.type === "deleted" ? "text-red-500" : "text-yellow-500";
          const bg = f.type === "created" ? "file-created" : f.type === "deleted" ? "file-deleted" : "file-modified";
          return (
            <button
              key={f.path}
              onClick={() => onNavigateFile?.({ repoId: "default", path: f.path })}
              className={cn("flex items-center gap-1.5 py-0.5 px-2 rounded text-xs w-full text-left hover:opacity-80", bg)}
            >
              <span className={cn("font-mono font-bold w-3 text-center", color)}>{icon}</span>
              <span className="font-mono text-[var(--text-primary)] truncate flex-1" title={f.path}>{f.path}</span>
              <span className={cn("text-[10px] px-1 rounded", color)}>{f.type.toUpperCase()}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function SubtaskDAG({ subtasks }: { subtasks: SubtaskSummary[] }) {
  const [svg, setSvg] = useState<string | null>(null);
  const [renderFailed, setRenderFailed] = useState(false);
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
    // Status-aware class definitions
    graphDef += "  classDef completed fill:#22c55e,stroke:#16a34a,color:#fff\n";
    graphDef += "  classDef failed fill:#ef4444,stroke:#dc2626,color:#fff\n";
    graphDef += "  classDef in_progress fill:#3b82f6,stroke:#2563eb,color:#fff\n";
    graphDef += "  classDef pending fill:#6b7280,stroke:#4b5563,color:#fff\n";

    for (const st of subtasks) {
      const nodeId = idMap[st.id]!;
      const label = truncate(st.description, 25).replace(/"/g, "'");
      graphDef += `  ${nodeId}["${label}"]\n`;
      // Apply status class
      const cls = st.status === "completed" ? "completed"
        : st.status === "failed" ? "failed"
        : st.status === "in_progress" ? "in_progress"
        : "pending";
      graphDef += `  class ${nodeId} ${cls}\n`;
      for (const depId of st.depends_on) {
        const depNode = idMap[depId];
        if (depNode) graphDef += `  ${depNode} --> ${nodeId}\n`;
      }
    }

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
        <p className="text-xs text-[var(--text-secondary)] mb-1">Subtask DAG:</p>
        <p className="text-xs text-[var(--text-muted)] italic">No dependency graph available</p>
      </div>
    );
  }

  return (
    <div className="mb-2">
      <p className="text-xs text-[var(--text-secondary)] mb-1">Subtask DAG:</p>
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
        <span className="text-xs text-[var(--text-secondary)]">Progress</span>
        <span className="text-xs text-[var(--text-muted)]">{completed}/{total} ({pct}%)</span>
      </div>
      <div className="h-1.5 bg-[var(--bg-surface-alt)] rounded-full overflow-hidden flex">
        {completed > 0 && <div className="bg-green-500 h-full" style={{ width: (completed / total) * 100 + "%" }} />}
        {failed > 0 && <div className="bg-red-500 h-full" style={{ width: (failed / total) * 100 + "%" }} />}
      </div>
      {failed > 0 && <span className="text-[10px] text-red-500">{failed} failed</span>}
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
      <p className="text-xs text-[var(--text-secondary)] mb-1">Timeline:</p>
      <div className="space-y-0.5 max-h-48 overflow-y-auto">
        {recent.map((ev, i) => (
          <div key={`${ev.timestamp}-${ev.type}-${i}`} className="flex items-center gap-1.5 text-xs">
            <span>{typeIcon[ev.type] ?? "•"}</span>
            <span className="text-[var(--text-muted)] w-16 shrink-0">{ev.timestamp ? new Date(ev.timestamp).toLocaleTimeString() : ""}</span>
            <span className="text-[var(--text-primary)] truncate">{ev.type}</span>
            {ev.subtask_id && <span className="text-[var(--text-muted)] font-mono">{shortId(ev.subtask_id)}</span>}
          </div>
        ))}
      </div>
      {!showAll && events.length > EVENT_PREVIEW_COUNT && (
        <button onClick={() => setShowAll(true)} className="text-[10px] text-blue-500 hover:underline mt-1">
          Show all {events.length} events
        </button>
      )}
    </div>
  );
}

export function TaskDetail({ task, interactionLog, onNavigateFile }: TaskDetailProps) {
  const subtasks = task.subtasks ?? [];
  const [filterSubtaskId, setFilterSubtaskId] = useState("");
  const [expandedResults, setExpandedResults] = useState<Record<string, boolean>>({});

  return (
    <div className="pl-5 py-2 text-xs text-[var(--text-secondary)] space-y-3" role="region" aria-label={`Task detail: ${task.description}`}>
      {/* Task ID with copy button */}
      <div className="flex items-center gap-2">
        <span className="font-mono text-[var(--text-muted)]">{shortId(task.id)}</span>
        <button
          onClick={() => { navigator.clipboard.writeText(task.id); }}
          className="text-[var(--text-muted)] hover:text-[var(--text-primary)] transition-colors cursor-pointer"
          aria-label="Copy task ID"
          title="Copy full task ID"
        >
          <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
          </svg>
        </button>
      </div>
      {/* Progress bar */}
      <SubtaskProgressBar subtasks={subtasks} />

      {/* Event timeline */}
      <EventTimeline events={interactionLog} />

      {/* Subtask list */}
      {subtasks.length > 0 && (
        <div>
          <p className="text-[var(--text-primary)] font-medium mb-1">Subtasks:</p>
          <div className="space-y-0.5">
            {subtasks.map((st) => (
              <div key={st.id}>
                <div className="flex items-center justify-between gap-1">
                  <span className="truncate max-w-[200px]">
                    {truncate(st.description, 50)}
                    {st.depends_on.length > 0 && (
                      <span className="text-[var(--text-muted)] ml-2">
                        deps: {st.depends_on.map((d) => shortId(d)).join(", ")}
                      </span>
                    )}
                  </span>
                  <div className="flex items-center gap-1 shrink-0">
                    {st.assigned_worker && (
                      <span className="text-[10px] bg-cyan-500/20 text-cyan-400 px-1.5 py-0.5 rounded font-mono" title={`Worker: ${st.assigned_worker}`}>
                        {shortId(st.assigned_worker)}
                      </span>
                    )}
                    <span className={cn("text-xs px-1.5 py-0.5 rounded", statusBadgeClass(st.status))}>
                      {st.status}
                    </span>
                  </div>
                </div>
                {st.result && (
                  <div className="mt-0.5 ml-2">
                    {st.result.length > 120 && !expandedResults[st.id] ? (
                      <p
                        className="text-[10px] text-[var(--text-muted)] truncate cursor-pointer hover:text-[var(--text-secondary)]"
                        title="Click to expand"
                        onClick={() => setExpandedResults((prev) => ({ ...prev, [st.id]: true }))}
                      >
                        ↳ {truncate(st.result, 120)} <span className="text-cyan-500">[+]</span>
                      </p>
                    ) : st.result.length > 120 && expandedResults[st.id] ? (
                      <div>
                        <pre
                          className="text-[10px] text-[var(--text-muted)] whitespace-pre-wrap break-words max-h-48 overflow-y-auto cursor-pointer hover:text-[var(--text-secondary)] bg-[var(--bg-primary)] border border-[var(--border-color)] rounded p-1.5 mt-0.5"
                          onClick={() => setExpandedResults((prev) => ({ ...prev, [st.id]: false }))}
                          title="Click to collapse"
                        >
                          {st.result}
                        </pre>
                        <span className="text-[10px] text-cyan-500 cursor-pointer" onClick={() => setExpandedResults((prev) => ({ ...prev, [st.id]: false }))}>[-] collapse</span>
                      </div>
                    ) : (
                      <p className="text-[10px] text-[var(--text-muted)] truncate" title={st.result}>
                        ↳ {truncate(st.result, 120)}
                      </p>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Interaction log with subtask filter */}
      <div>
        <div className="flex items-center justify-between mb-1">
          <span className="text-[var(--text-primary)] font-medium">Interaction Log:</span>
          {subtasks.length > 1 && (
            <select
              value={filterSubtaskId}
              onChange={(e) => setFilterSubtaskId(e.target.value)}
              aria-label="Filter by subtask"
              className="text-xs bg-[var(--bg-primary)] border border-[var(--border-color)] text-[var(--text-primary)] rounded px-2 py-1"
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
      <OutputFiles events={interactionLog} onNavigateFile={onNavigateFile} />

      {/* Mermaid DAG */}
      <SubtaskDAG subtasks={subtasks} />
    </div>
  );
}
