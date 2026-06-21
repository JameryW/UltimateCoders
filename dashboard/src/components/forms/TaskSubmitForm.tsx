import { useState } from "react";
import { showToast } from "@/components/ui/toast";
import { shortId } from "@/lib/utils";
import type { GrpcSubmitResult } from "@/hooks/useGrpcWeb";

interface TaskSubmitFormProps {
  /** Submit via gRPC-Web (Rust server). */
  grpcSubmitTask?: (description: string, projectId: string) => Promise<GrpcSubmitResult>;
  /** Called after successful submission with the new task ID. */
  onTaskCreated?: (taskId: string) => void;
  /** Optimistic insert: add task to list before event arrives. */
  onOptimisticAdd?: (taskId: string, description: string, projectId: string, subtaskCount: number, subtasks?: Array<{ id: string; description: string; status: string; dependsOn: string[] }>) => void;
}

export function TaskSubmitForm({ grpcSubmitTask, onTaskCreated, onOptimisticAdd }: TaskSubmitFormProps) {
  const [description, setDescription] = useState("");
  const [projectId, setProjectId] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const desc = description.trim();
    if (!desc) {
      showToast("Task description is required", "error");
      return;
    }
    if (!grpcSubmitTask) {
      showToast("Cannot submit -- gRPC-Web disconnected. Try reconnecting.", "error");
      return;
    }
    setSubmitting(true);
    try {
      const resp = await grpcSubmitTask(desc, projectId.trim());
      if (resp.success) {
        showToast(
          resp.subtaskCount > 0
            ? `Task submitted via gRPC-Web: ${shortId(resp.taskId)} -- ${resp.subtaskCount} subtask${resp.subtaskCount > 1 ? "s" : ""}`
            : `Task submitted via gRPC-Web: ${shortId(resp.taskId)}`,
          "success",
        );
        onOptimisticAdd?.(resp.taskId, desc, projectId.trim(), resp.subtaskCount, resp.subtasks);
        setDescription("");
        setProjectId("");
        onTaskCreated?.(resp.taskId);
      } else {
        showToast(`Submit failed via gRPC-Web: ${resp.status}`, "error");
      }
    } catch (err) {
      const msg = err instanceof Error && err.message.includes("not connected")
        ? `Cannot submit -- gRPC-Web disconnected. Try reconnecting.`
        : `Submit failed via gRPC-Web: ${String(err)}`;
      showToast(msg, "error");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="max-w-7xl mx-auto px-4 pt-4">
      <div className="rounded-lg border border-[var(--border-color)] bg-[var(--bg-surface)] p-4">
        <div className="flex items-center gap-2 mb-3">
          <h2 className="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">
            Submit Task
          </h2>
          <span className={`text-xs px-1.5 py-0.5 rounded ${grpcSubmitTask ? "status-in_progress" : "status-submitted"}`}>
            gRPC
          </span>
        </div>
        <form onSubmit={handleSubmit} className="flex flex-col md:flex-row gap-3">
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={2}
            aria-label="Task description"
            className="flex-1 bg-[var(--bg-primary)] border border-[var(--border-color)] rounded-md px-3 py-2 text-sm text-[var(--text-primary)] resize-vertical focus:border-blue-500 focus:outline-none"
            placeholder="Describe the coding task..."
          />
          <input
            type="text"
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
            aria-label="Project ID (optional)"
            className="w-full md:w-40 bg-[var(--bg-primary)] border border-[var(--border-color)] rounded-md px-3 py-2 text-sm text-[var(--text-primary)] focus:border-blue-500 focus:outline-none"
            placeholder="Project ID (optional)"
          />
          <button
            type="submit"
            disabled={submitting || !grpcSubmitTask}
            className="btn-action-info border border-blue-500 rounded-md px-5 py-2 text-sm font-medium cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap"
          >
            {submitting ? "Submitting..." : "Submit Task"}
          </button>
        </form>
      </div>
    </div>
  );
}
