import { useEffect, useState } from "react";
import { getStoredToken } from "@/hooks/useAuth";

interface Experiment {
  key: string;
  state: string;
  identity?: { graph_id?: string };
  remote_id?: string;
  artifact_state?: string;
  verdict?: { accepted: boolean; reasons?: string[] };
  delivery?: { status: string };
  artifacts?: Record<string, { sha256: string; size: number }>;
}
interface Job { key: string; state: string; version: number; remote_id?: string; backend_id?: string }
interface Resource { key: string; capacity: number; holders: Record<string, unknown>; queue: Array<{ operation_id: string }> }
interface Operations { jobs: Job[]; resources: Resource[]; outbox: Record<string, { count: number; oldest_age_seconds: number }>; leases: Record<string, { count: number }> }

function authHeaders(body?: object): Record<string, string> {
  const token = getStoredToken();
  return {
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(body ? { "Content-Type": "application/json" } : {}),
  };
}

async function request<T>(path: string, signal?: AbortSignal, body?: object): Promise<T> {
  const response = await fetch(`/dashboard/api/${path}`, {
    signal, method: body ? "POST" : "GET",
    headers: authHeaders(body),
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) throw new Error(`API ${response.status}`);
  return response.json();
}

function RecoveryAction({ job, onChange }: { job: Job; onChange: () => void }) {
  const [remoteId, setRemoteId] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const action = job.remote_id ? "confirm_stop" : "attach";
  async function reconcile() {
    setBusy(true);
    try {
      await request(`inference/operations/${encodeURIComponent(job.key)}/reconcile`, undefined,
        { action, expected_version: job.version, remote_id: remoteId });
      setMessage("证据已验证，恢复记录已保存");
      onChange();
    } catch (error) { setMessage(`恢复未完成，资源继续保留：${String(error)}`); }
    finally { setBusy(false); }
  }
  return <div className="flex gap-2 items-center flex-wrap">
    {!job.remote_id && <input aria-label="远端任务 ID" placeholder="已确认的远端任务 ID" value={remoteId} onChange={(e) => setRemoteId(e.target.value)} />}
    <button disabled={busy || (!job.remote_id && !remoteId)} onClick={() => void reconcile()}>
      {job.remote_id ? "验证退出证据" : "验证并关联远端任务"}
    </button><span role="status">{message}</span>
  </div>;
}

export function InferencePanel() {
  const [experiments, setExperiments] = useState<Experiment[]>([]);
  const [operations, setOperations] = useState<Operations | null>(null);
  const [error, setError] = useState("");
  const [cursor, setCursor] = useState("");
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function load() {
      try {
        const [page, ops] = await Promise.all([
          request<{ experiments: Experiment[]; next_cursor: string | null }>(`experiments?after=${encodeURIComponent(cursor)}`, controller.signal),
          request<Operations>("inference/operations", controller.signal),
        ]);
        if (!Array.isArray(page.experiments) || !Array.isArray(ops.jobs) || !Array.isArray(ops.resources)) throw new Error("Invalid inference response");
        if (!controller.signal.aborted) { setExperiments(page.experiments); setNextCursor(page.next_cursor); setOperations(ops); setError(""); }
      } catch (e) { if (!controller.signal.aborted) setError(`推理状态暂不可用：${String(e)}`); }
      finally { if (!controller.signal.aborted) timer = setTimeout(() => void load(), 5000); }
    }
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [cursor, refresh]);

  async function download(id: string, name: string) {
    try {
      const response = await fetch(`/dashboard/api/experiments/${encodeURIComponent(id)}/artifacts/${encodeURIComponent(name)}`, {
        headers: authHeaders(),
      });
      if (!response.ok) throw new Error(`Download ${response.status}`);
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a"); link.href = url; link.download = name; link.click();
      URL.revokeObjectURL(url);
    } catch (e) { setError(`制品下载失败：${String(e)}`); }
  }
  return <section className="p-4 space-y-4">
    <h2>推理实验与资源</h2>
    {error && <p role="alert">{error}</p>}
    <p>待交付 {operations?.outbox.pending?.count ?? 0} · 最长等待 {Math.round(operations?.outbox.pending?.oldest_age_seconds ?? 0)} 秒 · 隔离工作区 {operations?.leases.cleanup_pending?.count ?? 0}</p>
    <div className="overflow-auto"><table className="w-full text-left text-sm"><thead><tr><th>资源</th><th>占用 / 容量</th><th>等待</th></tr></thead>
      <tbody>{operations?.resources.map((resource) => <tr key={resource.key}><td>{resource.key}</td><td>{Object.keys(resource.holders ?? {}).length} / {resource.capacity}</td><td>{resource.queue?.length ?? 0}</td></tr>)}</tbody></table></div>
    <div className="overflow-auto"><table className="w-full text-left text-sm"><thead><tr><th>实验 / 任务</th><th>状态</th><th>Oracle</th><th>交付</th><th>制品</th></tr></thead>
      <tbody>{experiments.map((experiment) => <tr key={experiment.key}><td>{experiment.key.slice(0, 12)}<br />{experiment.identity?.graph_id}<br />{experiment.remote_id}</td><td>{experiment.state}</td>
        <td>{experiment.verdict ? experiment.verdict.accepted ? "接受" : "拒绝" : "—"}<br />{experiment.verdict?.reasons?.join("; ")}</td><td>{experiment.delivery?.status ?? "—"}</td>
        <td>{experiment.artifact_state ?? "本机制品"}<br />{Object.keys(experiment.artifacts ?? {}).filter((name) => ["report.json", "benchmarks.json", "graph.json", "accepted.patch"].includes(name)).map((name) => <button key={name} className="mr-2 underline" onClick={() => void download(experiment.key, name)}>{name}</button>)}</td></tr>)}</tbody></table></div>
    {!experiments.length && !error && <p>暂无推理实验</p>}
    <div className="flex gap-3"><button disabled={!cursor} onClick={() => setCursor("")}>首页</button><button disabled={!nextCursor} onClick={() => nextCursor && setCursor(nextCursor)}>下一页</button></div>
    {operations?.jobs.filter((job) => ["submission_unknown", "cleanup_pending"].includes(job.state)).map((job) => <div key={job.key} className="border rounded p-3"><p>{job.key} · {job.state} · {job.remote_id ?? "远端 ID 未知"}</p><RecoveryAction job={job} onChange={() => setRefresh((value) => value + 1)} /></div>)}
  </section>;
}
