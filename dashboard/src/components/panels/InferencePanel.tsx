import { useEffect, useRef, useState } from "react";
import { getStoredToken } from "@/hooks/useAuth";
import { cn } from "@/lib/utils";

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

const ARTIFACT_NAMES = ["report.json", "benchmarks.json", "graph.json", "accepted.patch"] as const;
const POLL_INTERVAL_MS = 5000;
/** Frontend-only: flag panel stale when last successful poll exceeds ~2 intervals. */
const STALE_AFTER_MS = POLL_INTERVAL_MS * 2;

export type InferenceStateTone = "healthy" | "fail" | "pending";

/**
 * Map experiment / job / verdict labels onto CC neon tones.
 * healthy/accepted → dispatch; fail/rejected → red; pending/unknown → amber/lead.
 */
// eslint-disable-next-line react-refresh/only-export-components -- unit-tested helper
export function inferenceStateTone(state: string | undefined | null): InferenceStateTone {
  const value = (state ?? "").toLowerCase();
  if (
    value.includes("fail")
    || value.includes("reject")
    || value.includes("error")
    || value === "denied"
    || value === "cancelled"
    || value === "canceled"
  ) {
    return "fail";
  }
  if (
    value.includes("accept")
    || value.includes("success")
    || value === "healthy"
    || value === "ok"
    || value === "completed"
    || value === "done"
    || value === "delivered"
    || value === "idle"
  ) {
    return "healthy";
  }
  // pending / unknown / running / empty → amber/lead
  return "pending";
}

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

function RecoveryAction({
  job,
  onChange,
  terminal,
}: {
  job: Job;
  onChange: () => void;
  terminal?: boolean;
}) {
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
  if (terminal) {
    return (
      <div className="terminal-inference-recovery-actions" data-testid="terminal-inference-recovery-actions">
        {!job.remote_id && (
          <input
            aria-label="远端任务 ID"
            className="terminal-inference-input"
            placeholder="已确认的远端任务 ID"
            value={remoteId}
            onChange={(e) => setRemoteId(e.target.value)}
            data-testid="terminal-inference-remote-id"
          />
        )}
        <button
          type="button"
          className="terminal-inference-chip is-active lead"
          disabled={busy || (!job.remote_id && !remoteId)}
          onClick={() => void reconcile()}
          data-testid="terminal-inference-chip-reconcile"
        >
          {job.remote_id ? "验证退出证据" : "验证并关联远端任务"}
        </button>
        {message && <span role="status" className="terminal-inference-recovery-msg">{message}</span>}
      </div>
    );
  }
  return (
    <div className="flex gap-2 items-center flex-wrap">
      {!job.remote_id && <input aria-label="远端任务 ID" placeholder="已确认的远端任务 ID" value={remoteId} onChange={(e) => setRemoteId(e.target.value)} />}
      <button disabled={busy || (!job.remote_id && !remoteId)} onClick={() => void reconcile()}>
        {job.remote_id ? "验证退出证据" : "验证并关联远端任务"}
      </button>
      <span role="status">{message}</span>
    </div>
  );
}

/** Presentational resource rows — exported for SSR CC locks without REST. */
export function InferenceResourceRows({
  resources,
  terminal,
}: {
  resources: Resource[];
  terminal: boolean;
}) {
  if (resources.length === 0) {
    return terminal
      ? <p className="terminal-empty-line" data-testid="terminal-inference-empty-resources">no resources yet</p>
      : null;
  }
  if (terminal) {
    return (
      <div className="terminal-inference-table" data-testid="terminal-inference-resources" role="table" aria-label="Inference resources">
        <div className="terminal-inference-table-head" role="row">
          <span>resource</span><span>holders / capacity</span><span>queue</span>
        </div>
        {resources.map((resource) => {
          const holders = Object.keys(resource.holders ?? {}).length;
          const load = resource.capacity > 0 ? holders / resource.capacity : 0;
          const tone = load >= 1 ? "fail" : load > 0 ? "pending" : "healthy";
          return (
            <div
              key={resource.key}
              className={cn("terminal-inference-row", `tone-${tone}`)}
              data-testid="terminal-inference-row"
              data-tone={tone}
              data-kind="resource"
              role="row"
            >
              <strong>{resource.key}</strong>
              <span>{holders} / {resource.capacity}</span>
              <span>{resource.queue?.length ?? 0}</span>
            </div>
          );
        })}
      </div>
    );
  }
  return (
    <div className="overflow-auto">
      <table className="w-full text-left text-sm">
        <thead><tr><th>资源</th><th>占用 / 容量</th><th>等待</th></tr></thead>
        <tbody>
          {resources.map((resource) => (
            <tr key={resource.key}>
              <td>{resource.key}</td>
              <td>{Object.keys(resource.holders ?? {}).length} / {resource.capacity}</td>
              <td>{resource.queue?.length ?? 0}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Presentational experiment rows — exported for SSR CC locks without REST. */
export function InferenceExperimentRows({
  experiments,
  terminal,
  onDownload,
}: {
  experiments: Experiment[];
  terminal: boolean;
  onDownload: (id: string, name: string) => void;
}) {
  if (experiments.length === 0) return null;
  if (terminal) {
    return (
      <div className="terminal-inference-table" data-testid="terminal-inference-experiments" role="table" aria-label="Inference experiments">
        <div className="terminal-inference-table-head" role="row">
          <span>experiment</span><span>state</span><span>oracle</span><span>delivery</span><span>artifacts</span>
        </div>
        {experiments.map((experiment) => {
          const stateTone = inferenceStateTone(experiment.state);
          const verdictLabel = experiment.verdict
            ? (experiment.verdict.accepted ? "接受" : "拒绝")
            : "—";
          const verdictTone = experiment.verdict
            ? (experiment.verdict.accepted ? "healthy" : "fail")
            : "pending";
          const deliveryTone = inferenceStateTone(experiment.delivery?.status);
          const artifactNames = Object.keys(experiment.artifacts ?? {}).filter((name) =>
            (ARTIFACT_NAMES as readonly string[]).includes(name),
          );
          return (
            <div
              key={experiment.key}
              className={cn("terminal-inference-row", `tone-${stateTone}`)}
              data-testid="terminal-inference-row"
              data-tone={stateTone}
              data-kind="experiment"
              role="row"
            >
              <div className="terminal-inference-id">
                <strong>{experiment.key.slice(0, 12)}</strong>
                {experiment.identity?.graph_id && <small>{experiment.identity.graph_id}</small>}
                {experiment.remote_id && <small>{experiment.remote_id}</small>}
              </div>
              <span className={cn("terminal-inference-state", `tone-${stateTone}`)}>{experiment.state}</span>
              <div className={cn("terminal-inference-verdict", `tone-${verdictTone}`)}>
                <span>{verdictLabel}</span>
                {experiment.verdict?.reasons?.length ? (
                  <small>{experiment.verdict.reasons.join("; ")}</small>
                ) : null}
              </div>
              <span className={cn("terminal-inference-state", `tone-${deliveryTone}`)}>
                {experiment.delivery?.status ?? "—"}
              </span>
              <div className="terminal-inference-artifacts">
                <span className="terminal-inference-artifact-state">{experiment.artifact_state ?? "本机制品"}</span>
                {artifactNames.map((name) => (
                  <button
                    key={name}
                    type="button"
                    className="terminal-inference-chip is-active swarm"
                    data-testid="terminal-inference-chip-artifact"
                    onClick={() => onDownload(experiment.key, name)}
                  >
                    {name}
                  </button>
                ))}
              </div>
            </div>
          );
        })}
      </div>
    );
  }
  return (
    <div className="overflow-auto">
      <table className="w-full text-left text-sm">
        <thead>
          <tr><th>实验 / 任务</th><th>状态</th><th>Oracle</th><th>交付</th><th>制品</th></tr>
        </thead>
        <tbody>
          {experiments.map((experiment) => (
            <tr key={experiment.key}>
              <td>
                {experiment.key.slice(0, 12)}
                <br />
                {experiment.identity?.graph_id}
                <br />
                {experiment.remote_id}
              </td>
              <td>{experiment.state}</td>
              <td>
                {experiment.verdict ? (experiment.verdict.accepted ? "接受" : "拒绝") : "—"}
                <br />
                {experiment.verdict?.reasons?.join("; ")}
              </td>
              <td>{experiment.delivery?.status ?? "—"}</td>
              <td>
                {experiment.artifact_state ?? "本机制品"}
                <br />
                {Object.keys(experiment.artifacts ?? {})
                  .filter((name) => (ARTIFACT_NAMES as readonly string[]).includes(name))
                  .map((name) => (
                    <button key={name} className="mr-2 underline" onClick={() => onDownload(experiment.key, name)}>
                      {name}
                    </button>
                  ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Recovery jobs strip — exported for SSR. */
export function InferenceRecoveryList({
  jobs,
  terminal,
  onChange,
}: {
  jobs: Job[];
  terminal: boolean;
  onChange: () => void;
}) {
  const recovery = jobs.filter((job) => ["submission_unknown", "cleanup_pending"].includes(job.state));
  if (recovery.length === 0) return null;
  if (terminal) {
    return (
      <div className="terminal-inference-recovery-list" data-testid="terminal-inference-recovery-list">
        {recovery.map((job) => {
          const tone = inferenceStateTone(job.state);
          return (
            <div
              key={job.key}
              className={cn("terminal-inference-recovery", `tone-${tone}`)}
              data-testid="terminal-inference-recovery"
              data-tone={tone}
            >
              <div className="terminal-inference-recovery-head">
                <strong>{job.key}</strong>
                <span className={cn("terminal-inference-state", `tone-${tone}`)}>{job.state}</span>
                <span>{job.remote_id ?? "远端 ID 未知"}</span>
              </div>
              <RecoveryAction job={job} onChange={onChange} terminal />
            </div>
          );
        })}
      </div>
    );
  }
  return (
    <>
      {recovery.map((job) => (
        <div key={job.key} className="border rounded p-3">
          <p>{job.key} · {job.state} · {job.remote_id ?? "远端 ID 未知"}</p>
          <RecoveryAction job={job} onChange={onChange} />
        </div>
      ))}
    </>
  );
}

/** Ops strip counts — exported for SSR. */
export function InferenceOpsStrip({
  operations,
  terminal,
}: {
  operations: Operations | null;
  terminal: boolean;
}) {
  const pending = operations?.outbox.pending?.count ?? 0;
  const oldest = Math.round(operations?.outbox.pending?.oldest_age_seconds ?? 0);
  const cleanup = operations?.leases.cleanup_pending?.count ?? 0;
  if (terminal) {
    return (
      <div className="terminal-inference-ops" data-testid="terminal-inference-ops" aria-label="Inference operations">
        <span className={cn("terminal-inference-stat", pending > 0 ? "tone-pending" : "tone-healthy")} data-testid="terminal-inference-stat">
          outbox [{pending}]
        </span>
        <span className={cn("terminal-inference-stat", oldest > 0 ? "tone-pending" : "tone-healthy")} data-testid="terminal-inference-stat">
          oldest wait [{oldest}s]
        </span>
        <span className={cn("terminal-inference-stat", cleanup > 0 ? "tone-pending" : "tone-healthy")} data-testid="terminal-inference-stat">
          cleanup leases [{cleanup}]
        </span>
      </div>
    );
  }
  return (
    <p>
      待交付 {pending} · 最长等待 {oldest} 秒 · 隔离工作区 {cleanup}
    </p>
  );
}

/** Full presentational board for SSR fixtures (no fetch). */
export function InferenceBoard({
  experiments,
  operations,
  error,
  stale = false,
  terminal,
  cursor = "",
  nextCursor = null,
  onCursorHome,
  onCursorNext,
  onDownload,
  onRecoveryChange,
}: {
  experiments: Experiment[];
  operations: Operations | null;
  error?: string;
  stale?: boolean;
  terminal: boolean;
  cursor?: string;
  nextCursor?: string | null;
  onCursorHome?: () => void;
  onCursorNext?: () => void;
  onDownload: (id: string, name: string) => void;
  onRecoveryChange?: () => void;
}) {
  const recoveryCount = (operations?.jobs ?? []).filter((job) =>
    ["submission_unknown", "cleanup_pending"].includes(job.state),
  ).length;
  const body = (
    <>
      {error && (
        <p
          role="alert"
          className={cn(terminal ? "terminal-inference-error" : undefined)}
          data-testid={terminal ? "terminal-inference-error" : undefined}
        >
          {error}
        </p>
      )}
      <InferenceOpsStrip operations={operations} terminal={terminal} />
      <InferenceResourceRows resources={operations?.resources ?? []} terminal={terminal} />
      <InferenceExperimentRows experiments={experiments} terminal={terminal} onDownload={onDownload} />
      {!experiments.length && !error && (
        <p
          className={cn(terminal ? "terminal-empty-line" : undefined)}
          data-testid={terminal ? "terminal-inference-empty" : undefined}
        >
          {terminal ? "waiting for inference experiments…" : "暂无推理实验"}
        </p>
      )}
      <div className={cn(terminal ? "terminal-inference-pager" : "flex gap-3")}>
        <button
          type="button"
          className={cn(terminal && "terminal-inference-chip", terminal && !cursor && "is-disabled")}
          disabled={!cursor}
          onClick={() => onCursorHome?.()}
          data-testid={terminal ? "terminal-inference-chip-home" : undefined}
        >
          首页
        </button>
        <button
          type="button"
          className={cn(terminal && "terminal-inference-chip", terminal && !nextCursor && "is-disabled")}
          disabled={!nextCursor}
          onClick={() => onCursorNext?.()}
          data-testid={terminal ? "terminal-inference-chip-next" : undefined}
        >
          下一页
        </button>
      </div>
      <InferenceRecoveryList
        jobs={operations?.jobs ?? []}
        terminal={terminal}
        onChange={() => onRecoveryChange?.()}
      />
    </>
  );

  if (terminal) {
    return (
      <section
        className={cn("terminal-inference-panel", stale && "is-stale")}
        data-testid="terminal-inference-panel"
        data-stale={stale ? "true" : "false"}
        aria-label="Inference experiments"
      >
        <div className="terminal-inference-head cc-session-log-head" data-testid="terminal-inference-head">
          <span>inference · experiments</span>
          <b data-testid="terminal-inference-count">
            experiments {experiments.length} · recovery {recoveryCount}
          </b>
        </div>
        {stale && (
          <div className="terminal-inference-stale" data-testid="terminal-inference-stale" role="status">
            poll stale · last success exceeded 2 intervals
          </div>
        )}
        {body}
      </section>
    );
  }

  return (
    <section className="p-4 space-y-4">
      <h2>推理实验与资源</h2>
      {body}
    </section>
  );
}

export function InferencePanel({
  variant = "default",
}: {
  /** Command-center monospaced inference shell (no bare h2; neon rows/chips). */
  variant?: "default" | "terminal";
} = {}) {
  const terminal = variant === "terminal";
  const [experiments, setExperiments] = useState<Experiment[]>([]);
  const [operations, setOperations] = useState<Operations | null>(null);
  const [error, setError] = useState("");
  const [cursor, setCursor] = useState("");
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [stale, setStale] = useState(false);
  const lastSuccessRef = useRef<number | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let staleTimer: ReturnType<typeof setInterval> | undefined;
    async function load() {
      try {
        const [page, ops] = await Promise.all([
          request<{ experiments: Experiment[]; next_cursor: string | null }>(`experiments?after=${encodeURIComponent(cursor)}`, controller.signal),
          request<Operations>("inference/operations", controller.signal),
        ]);
        if (!Array.isArray(page.experiments) || !Array.isArray(ops.jobs) || !Array.isArray(ops.resources)) throw new Error("Invalid inference response");
        if (!controller.signal.aborted) {
          setExperiments(page.experiments);
          setNextCursor(page.next_cursor);
          setOperations(ops);
          setError("");
          lastSuccessRef.current = Date.now();
          setStale(false);
        }
      } catch (e) {
        if (!controller.signal.aborted) setError(`推理状态暂不可用：${String(e)}`);
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(() => void load(), POLL_INTERVAL_MS);
      }
    }
    void load();
    if (terminal && typeof window !== "undefined") {
      staleTimer = window.setInterval(() => {
        const last = lastSuccessRef.current;
        if (last == null) return;
        setStale(Date.now() - last > STALE_AFTER_MS);
      }, 1000);
    }
    return () => {
      controller.abort();
      clearTimeout(timer);
      if (staleTimer) clearInterval(staleTimer);
    };
  }, [cursor, refresh, terminal]);

  async function download(id: string, name: string) {
    try {
      const response = await fetch(`/dashboard/api/experiments/${encodeURIComponent(id)}/artifacts/${encodeURIComponent(name)}`, {
        headers: authHeaders(),
      });
      if (!response.ok) throw new Error(`Download ${response.status}`);
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = name;
      link.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(`制品下载失败：${String(e)}`);
    }
  }

  return (
    <InferenceBoard
      experiments={experiments}
      operations={operations}
      error={error}
      stale={terminal ? stale : false}
      terminal={terminal}
      cursor={cursor}
      nextCursor={nextCursor}
      onCursorHome={() => setCursor("")}
      onCursorNext={() => { if (nextCursor) setCursor(nextCursor); }}
      onDownload={(id, name) => { void download(id, name); }}
      onRecoveryChange={() => setRefresh((value) => value + 1)}
    />
  );
}
