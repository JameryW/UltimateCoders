import { useState } from "react";
import * as api from "@/api/endpoints";
import { showToast } from "@/components/ui/toast";
import type { GrpcSubmitResult } from "@/hooks/useGrpcWeb";

interface TaskSubmitFormProps {
  /** If provided, submit via gRPC-Web (Rust server). Falls back to REST (Python) if undefined. */
  grpcSubmitTask?: (description: string, projectId: string) => Promise<GrpcSubmitResult>;
  /** Called after successful submission with the new task ID. */
  onTaskCreated?: (taskId: string) => void;
}

function shortId(id: string, len = 8): string {
  return id ? id.substring(0, len) : "--";
}

export function TaskSubmitForm({ grpcSubmitTask, onTaskCreated }: TaskSubmitFormProps) {
  const [description, setDescription] = useState("");
  const [projectId, setProjectId] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const mode = grpcSubmitTask ? "gRPC" : "REST";
  const modeLabel = grpcSubmitTask ? "via gRPC-Web" : "via REST";

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const desc = description.trim();
    if (!desc) {
      showToast("Task description is required", "error");
      return;
    }
    setSubmitting(true);
    try {
      if (grpcSubmitTask) {
        // gRPC-Web path -> Rust server -> local_worker
        const resp = await grpcSubmitTask(desc, projectId.trim());
        if (resp.success) {
          showToast(
            resp.subtaskCount > 0
              ? `Task submitted ${modeLabel}: ${shortId(resp.taskId)} -- ${resp.subtaskCount} subtask${resp.subtaskCount > 1 ? "s" : ""}`
              : `Task submitted ${modeLabel}: ${shortId(resp.taskId)}`,
            "success",
          );
          setDescription("");
          setProjectId("");
          onTaskCreated?.(resp.taskId);
        } else {
          showToast(`Submit failed ${modeLabel}: ${resp.status}`, "error");
        }
      } else {
        // REST path -> Python FastAPI
        const result = await api.submitTask(desc, projectId.trim());
        if (result.success) {
          showToast(`Task submitted ${modeLabel}: ${shortId(result.task_id ?? "")}`, "success");
          setDescription("");
          setProjectId("");
          if (result.task_id) onTaskCreated?.(result.task_id);
        } else {
          showToast(`Submit failed ${modeLabel}: ${result.error ?? "unknown"}`, "error");
        }
      }
    } catch (err) {
      showToast(`Submit failed ${modeLabel}: ${String(err)}`, "error");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="max-w-7xl mx-auto px-4 pt-4">
      <div className="rounded-lg border border-dark-700 bg-dark-800 p-4">
        <div className="flex items-center gap-2 mb-3">
          <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
            Submit Task
          </h2>
          <span className={`text-xs px-1.5 py-0.5 rounded ${grpcSubmitTask ? "bg-blue-900/50 text-blue-300" : "bg-green-900/50 text-green-300"}`}>
            {mode}
          </span>
        </div>
        <form onSubmit={handleSubmit} className="flex flex-col md:flex-row gap-3">
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={2}
            aria-label="Task description"
            className="flex-1 bg-dark-900 border border-dark-700 rounded-md px-3 py-2 text-sm text-gray-200 resize-vertical focus:border-blue-500 focus:outline-none"
            placeholder="Describe the coding task..."
          />
          <input
            type="text"
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
            aria-label="Project ID (optional)"
            className="w-full md:w-40 bg-dark-900 border border-dark-700 rounded-md px-3 py-2 text-sm text-gray-200 focus:border-blue-500 focus:outline-none"
            placeholder="Project ID (optional)"
          />
          <button
            type="submit"
            disabled={submitting}
            className="bg-blue-900/50 text-blue-300 border border-blue-500 rounded-md px-5 py-2 text-sm font-medium cursor-pointer hover:bg-blue-900/70 disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap"
          >
            {submitting ? "Submitting..." : "Submit Task"}
          </button>
        </form>
      </div>
    </div>
  );
}
