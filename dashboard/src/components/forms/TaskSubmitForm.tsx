import { useState } from "react";
import * as api from "@/api/endpoints";
import { showToast } from "@/components/ui/toast";

export function TaskSubmitForm() {
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
    setSubmitting(true);
    try {
      const result = await api.submitTask(desc, projectId.trim());
      if (result.success) {
        showToast(`Task submitted: ${shortId(result.task_id ?? "")}`, "success");
        setDescription("");
        setProjectId("");
      } else {
        showToast(`Submit failed: ${result.error ?? "unknown"}`, "error");
      }
    } catch (err) {
      showToast(`Submit failed: ${String(err)}`, "error");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="max-w-7xl mx-auto px-4 pt-4">
      <div className="rounded-lg border border-dark-700 bg-dark-800 p-4">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide mb-3">
          Submit Task
        </h2>
        <form onSubmit={handleSubmit} className="flex flex-col md:flex-row gap-3">
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={2}
            className="flex-1 bg-dark-900 border border-dark-700 rounded-md px-3 py-2 text-sm text-gray-200 resize-vertical focus:border-blue-500 focus:outline-none"
            placeholder="Describe the coding task..."
          />
          <input
            type="text"
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
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

function shortId(id: string, len = 8): string {
  return id ? id.substring(0, len) : "--";
}
