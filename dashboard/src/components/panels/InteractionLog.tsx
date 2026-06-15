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
  return { border: "border-l-gray-600", dot: "text-gray-400" };
}

function eventContent(ev: TaskEvent): string {
  const d = ev.data;
  if (ev.type === "llm_request") return `LLM request: ${String(d.model ?? "")}`;
  if (ev.type === "subtask_started") return `Started: ${String(d.description ?? "").substring(0, 40)}`;
  if (ev.type === "tool_call") return `Tool: ${String(d.tool ?? "")} ${String(d.input_summary ?? "").substring(0, 60)}`;
  if (ev.type === "tool_result") return `Result: ${String(d.result_summary ?? "").substring(0, 80)}`;
  if (ev.type === "subtask_completed") return `Completed: ${String(d.summary ?? "").substring(0, 60)}`;
  if (ev.type === "subtask_failed") return `Failed: ${String(d.error ?? "").substring(0, 60)}`;
  if (ev.type === "task_completed") return `Task ${String(d.status ?? "completed")}`;
  return ev.type;
}

export function InteractionLog({ events, filterSubtaskId }: InteractionLogProps) {
  const filtered = filterSubtaskId
    ? events.filter((e) => e.subtask_id === filterSubtaskId)
    : events;

  if (filtered.length === 0) {
    return <p className="text-xs text-gray-500">No interaction events yet</p>;
  }

  return (
    <div className="max-h-64 overflow-y-auto space-y-0.5">
      {filtered.map((ev, i) => {
        const style = eventTypeStyle(ev.type);
        const time = ev.timestamp
          ? new Date(ev.timestamp).toLocaleTimeString()
          : "--";
        return (
          <div
            key={`${ev.timestamp}-${i}`}
            className={`border-l-2 ${style.border} pl-2 py-0.5 text-xs`}
          >
            <span className="text-gray-500 mr-1">{time}</span>
            <span className={style.dot}>●</span>{" "}
            <span className="text-gray-300">{eventContent(ev)}</span>
          </div>
        );
      })}
    </div>
  );
}
