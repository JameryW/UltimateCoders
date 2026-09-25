import { useCallback, useEffect, useState } from "react";
import { useGrpcWeb, type GrpcConnectionState } from "@/hooks/useGrpcWeb";

const capabilities = [
  {
    index: "01",
    title: "DAG task orchestration",
    detail: "把一个复杂需求拆成依赖清晰的执行图，按波次调度、汇总结果，并支持暂停、恢复和取消。",
    signal: "plan → dispatch → review",
    tone: "mint",
  },
  {
    index: "02",
    title: "Capability-aware workers",
    detail: "Worker Registry 根据能力、负载和心跳选择执行者，从本地 fallback 平滑扩展到多 Worker 集群。",
    signal: "match by capability + load",
    tone: "blue",
  },
  {
    index: "03",
    title: "Hybrid Search + Memory",
    detail: "Text、Semantic、AST 检索与分层 Memory 共享上下文，让 Agent 在多仓库中保持连续的工程记忆。",
    signal: "text / semantic / AST",
    tone: "violet",
  },
  {
    index: "04",
    title: "Dashboard-first control",
    detail: "从 Web Dashboard 提交任务、查看 Worker 与执行事件，并控制暂停、恢复和取消。",
    signal: "Dashboard ↔ Gateway",
    tone: "amber",
  },
  {
    index: "05",
    title: "Event-driven recovery",
    detail: "任务状态通过事件广播、Checkpoint 和 WatchTask 实时同步；连接中断后可继续观察，而不是丢失进度。",
    signal: "events + checkpoints",
    tone: "rose",
  },
  {
    index: "06",
    title: "Local to cluster",
    detail: "同一套产品从本地内存 fallback、Docker Gateway 到 NATS + 多 Worker 部署，按规模渐进扩展。",
    signal: "local → distributed",
    tone: "teal",
  },
] as const;

const systemLayers = [
  {
    index: "01",
    label: "Web interface",
    title: "Dashboard + TaskService",
    route: "intent in",
    detail: "从 Dashboard 提交任务并查看结果，通过 TaskService 控制同一个执行图。",
    tags: ["#/dashboard", "TaskService", "gRPC-Web"],
    tone: "mint",
  },
  {
    index: "02",
    label: "Control plane",
    title: "Rust Gateway",
    route: "plan + coordinate",
    detail: "TaskService、WorkerService 和 WatchTask 把任务、能力和事件组织成一个控制面。",
    tags: ["DAG", "WorkerRegistry", "gRPC-Web"],
    tone: "blue",
  },
  {
    index: "03",
    label: "Execution plane",
    title: "Workers that match",
    route: "dispatch + execute",
    detail: "按能力、负载和心跳选择 Worker，在隔离工作区运行 Claude Code、Codex 或 Grok Build。",
    tags: ["NATS", "sandbox", "multi-worker"],
    tone: "amber",
  },
  {
    index: "04",
    label: "Knowledge plane",
    title: "Search + Memory",
    route: "context attached",
    detail: "Text、Semantic、AST 检索与分层 Memory 为每个 subtask 补上跨仓库上下文。",
    tags: ["hybrid search", "TiKV", "Qdrant / PG"],
    tone: "violet",
  },
  {
    index: "05",
    label: "Event plane",
    title: "Signals that return",
    route: "observe + recover",
    detail: "TaskEvent、Checkpoint 和结果流回到 Dashboard，执行过程可追踪、可恢复。",
    tags: ["WatchTask", "checkpoint", "TaskEvent"],
    tone: "teal",
  },
] as const;

const workflow = [
  { step: "01", label: "Intent", title: "Describe the change", detail: "在 Dashboard 输入自然语言任务。" },
  { step: "02", label: "Plan", title: "Build the execution graph", detail: "Orchestrator 生成带依赖关系的 DAG。" },
  { step: "03", label: "Run", title: "Dispatch to the right worker", detail: "Gateway 按能力和负载调度 Worker。" },
  { step: "04", label: "Observe", title: "Stream every state change", detail: "TaskEvent 通过 gRPC-Web 返回。" },
  { step: "05", label: "Recover", title: "Resume with context", detail: "Memory、Checkpoint 和事件让执行可恢复。" },
] as const;

const scenarios = [
  {
    tag: "MODERNIZE",
    title: "跨仓库升级",
    detail: "同时定位 API、Worker 和测试变更，保留依赖关系并统一汇总结果。",
    value: "从定位到合并，减少跨仓库往返",
  },
  {
    tag: "DELIVER",
    title: "并行交付",
    detail: "将独立子任务分发给不同能力的 Worker，实时查看波次进度和负载。",
    value: "提高吞吐，同时保留可观察性",
  },
  {
    tag: "RECOVER",
    title: "故障与调试",
    detail: "事件、日志和任务状态集中在一个终端里，连接短暂中断也能继续追踪。",
    value: "失败可定位、可重试、可恢复",
  },
  {
    tag: "SCALE",
    title: "从本地到集群",
    detail: "先用本地 fallback 验证流程，再接入 Docker Gateway、NATS 和多实例 Worker。",
    value: "同一产品路径，逐步扩大规模",
  },
] as const;

type RuntimeSnapshot = {
  gatewayStatus: string;
  gatewayVersion: string;
  taskTotal: number | null;
  lastSyncedAt: number | null;
};

const initialRuntime: RuntimeSnapshot = {
  gatewayStatus: "等待 Gateway",
  gatewayVersion: "—",
  taskTotal: null,
  lastSyncedAt: null,
};

function connectionLabel(state: GrpcConnectionState) {
  switch (state) {
    case "connected":
      return "Gateway transport connected";
    case "connecting":
      return "Connecting to Gateway transport";
    case "reconnecting":
      return "Reconnecting to Gateway transport";
    case "error":
      return "Gateway needs attention";
    default:
      return "Gateway transport disconnected";
  }
}

function formatSyncTime(timestamp: number | null) {
  if (timestamp === null) {
    return "waiting for first sync";
  }

  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(timestamp);
}

export default function HomePage() {
  const { connectionState, healthCheck, listTasks } = useGrpcWeb({ enabled: true });
  const [runtime, setRuntime] = useState<RuntimeSnapshot>(initialRuntime);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [activeSection, setActiveSection] = useState(() => window.location.hash.slice(1) || "/");

  const refreshRuntime = useCallback(async () => {
    setIsRefreshing(true);
    const [health, tasks] = await Promise.allSettled([healthCheck(), listTasks()]);
    setRuntime((current) => ({
      gatewayStatus: health.status === "fulfilled" ? health.value.status || "healthy" : current.gatewayStatus,
      gatewayVersion: health.status === "fulfilled" ? health.value.version || "—" : current.gatewayVersion,
      taskTotal: tasks.status === "fulfilled" ? tasks.value.total : current.taskTotal,
      lastSyncedAt:
        health.status === "fulfilled" || tasks.status === "fulfilled"
          ? Date.now()
          : current.lastSyncedAt,
    }));
    setIsRefreshing(false);
  }, [healthCheck, listTasks]);

  useEffect(() => {
    const initialTimer = window.setTimeout(() => void refreshRuntime(), 0);
    const timer = window.setInterval(() => void refreshRuntime(), 30_000);
    return () => {
      window.clearTimeout(initialTimer);
      window.clearInterval(timer);
    };
  }, [refreshRuntime]);

  useEffect(() => {
    const handleHashChange = () => setActiveSection(window.location.hash.slice(1) || "/");
    window.addEventListener("hashchange", handleHashChange);
    return () => window.removeEventListener("hashchange", handleHashChange);
  }, []);

  const isLive = connectionState === "connected";

  return (
    <main className="home-page">
      <div className="home-grid-glow" aria-hidden="true" />
      <div className="home-shell">
        <nav className="home-nav" aria-label="Primary navigation">
          <a className="home-brand" href="#/" aria-label="UltimateCoders overview">
            <span className="home-brand-mark">UC</span>
            <span>
              <strong>UltimateCoders</strong>
              <small>distributed AI coding</small>
            </span>
          </a>
          <div className="home-nav-links">
            <a className={`home-nav-link ${activeSection === "/" ? "home-nav-link-active" : ""}`} href="#/">Overview</a>
            <a className={`home-nav-link ${activeSection === "home-capabilities" ? "home-nav-link-active" : ""}`} href="#home-capabilities">Capabilities</a>
            <a className={`home-nav-link ${activeSection === "home-workflow" ? "home-nav-link-active" : ""}`} href="#home-workflow">Execution flow</a>
            <a className="home-nav-link" href="#/dashboard">Operations dashboard <span aria-hidden="true">↗</span></a>
          </div>
        </nav>

        <section className="home-hero" aria-labelledby="home-hero-title">
          <div className="home-hero-copy">
            <div className="home-eyebrow"><span className="home-eyebrow-dot" /> ORCHESTRATE · EXECUTE · REMEMBER</div>
            <h1 id="home-hero-title">From intent to<br /><em>observable execution.</em></h1>
            <p className="home-hero-lede">
              一个入口连接 Gateway、Worker、Search 和 Memory，把复杂 Coding 任务变成可调度、可观察、可恢复的执行链。
            </p>
            <div className="home-hero-actions">
              <a className="home-button home-button-primary" href="#/dashboard">Open dashboard <span aria-hidden="true">→</span></a>
              <a className="home-button home-button-secondary" href="#home-workflow">See how it works <span aria-hidden="true">↓</span></a>
            </div>
            <div className="home-trust-line">
              <span>BUILT FOR REAL REPOSITORIES</span>
              <span className="home-trust-divider" />
              <span>LOCAL-FIRST · CLUSTER-READY</span>
            </div>
          </div>

          <div className="home-runtime-card" aria-label="Live runtime status">
            <div className="home-runtime-topline">
              <span className={`home-status-dot ${isLive ? "home-status-dot-live" : ""}`} />
              <span>{connectionLabel(connectionState)}</span>
              <span className={`home-runtime-mode ${isLive ? "home-runtime-mode-live" : ""}`}>
                {isLive ? "LIVE" : "PREVIEW"}
              </span>
              <button className="home-refresh-button" type="button" onClick={() => void refreshRuntime()} disabled={isRefreshing}>
                {isRefreshing ? "syncing" : "refresh"}
              </button>
            </div>
            <div className="home-runtime-title">Runtime surface</div>
            <div className="home-runtime-address">gRPC-Web · WatchTask · TaskService</div>
            <div className="home-runtime-divider" />
            <div className="home-runtime-grid">
              <div>
                <span>gateway</span>
                <strong className={runtime.gatewayStatus === "healthy" ? "home-live-text" : ""}>
                  {runtime.gatewayStatus}
                </strong>
              </div>
              <div><span>version</span><strong>{runtime.gatewayVersion}</strong></div>
              <div><span>tasks observed</span><strong>{runtime.taskTotal ?? "—"}</strong></div>
              <div><span>event stream</span><strong className={isLive ? "home-live-text" : ""}>{isLive ? "watching" : "standby"}</strong></div>
            </div>
            <div className="home-runtime-foot">
              <span>live snapshot · {formatSyncTime(runtime.lastSyncedAt)}</span>
              <span>auto refresh 30s</span>
            </div>
          </div>
        </section>

        <div className="home-metrics" aria-label="Product summary">
          <div><strong>01</strong><span>orchestration layer</span></div>
          <div><strong>05</strong><span>execution stages</span></div>
          <div><strong>∞</strong><span>worker scale path</span></div>
          <div><strong>24/7</strong><span>event visibility</span></div>
        </div>

        <section className="home-section" id="home-capabilities" aria-labelledby="capabilities-title">
          <div className="home-section-heading">
            <div>
              <div className="home-section-kicker">THE PRODUCT SURFACE</div>
              <h2 id="capabilities-title">One system.<br /><em>Every layer connected.</em></h2>
            </div>
            <p>不是一个单点 Terminal demo，而是一套可以从本地验证扩展到分布式执行的 AI Coding 基础设施。</p>
          </div>
          <div className="home-capability-grid">
            {capabilities.map((capability) => (
              <article className={`home-capability-card home-tone-${capability.tone}`} key={capability.index}>
                <div className="home-card-topline"><span>{capability.index}</span><span className="home-card-signal">{capability.signal}</span></div>
                <h3>{capability.title}</h3>
                <p>{capability.detail}</p>
                <div className="home-card-line" />
              </article>
            ))}
          </div>
          <div className="home-system-map" id="home-system" aria-label="UltimateCoders product layers">
            <div className="home-system-map-heading">
              <div>
                <div className="home-section-kicker">THE PRODUCT MAP</div>
                <h3>Five layers.<br /><em>One execution surface.</em></h3>
              </div>
              <p>五个面板对应真实边界：入口、控制、执行、上下文和事件。</p>
            </div>
            <div className="home-system-layer-grid">
              {systemLayers.map((layer) => (
                <article className={`home-system-layer home-tone-${layer.tone}`} key={layer.index}>
                  <div className="home-system-layer-topline"><span>{layer.index}</span><b>{layer.route}</b></div>
                  <div className="home-system-layer-label">{layer.label}</div>
                  <h4>{layer.title}</h4>
                  <p>{layer.detail}</p>
                  <div className="home-system-layer-tags">
                    {layer.tags.map((tag) => <code key={tag}>{tag}</code>)}
                  </div>
                </article>
              ))}
            </div>
            <div className="home-system-route" aria-label="Product data route">
              <span>intent</span><i aria-hidden="true">→</i><span>task + DAG</span><i aria-hidden="true">→</i><span>worker</span><i aria-hidden="true">→</i><span>events + memory</span>
            </div>
          </div>
        </section>

        <section className="home-section home-workflow-section" id="home-workflow" aria-labelledby="workflow-title">
          <div className="home-section-heading home-section-heading-tight">
            <div>
              <div className="home-section-kicker">THE EXECUTION LOOP</div>
              <h2 id="workflow-title">A visible path from<br /><em>request to result.</em></h2>
            </div>
            <p>每一个阶段都有明确的边界和事件：输入不再消失在黑盒里，Worker 也不再是不可见的后台进程。</p>
          </div>
          <div className="home-workflow-track">
            {workflow.map((item, index) => (
              <div className="home-workflow-item" key={item.step}>
                <div className="home-workflow-marker"><span>{item.step}</span>{index < workflow.length - 1 && <i aria-hidden="true" />}</div>
                <div className="home-workflow-label">{item.label}</div>
                <h3>{item.title}</h3>
                <p>{item.detail}</p>
              </div>
            ))}
          </div>
        </section>

        <section className="home-section home-scenarios-section" aria-labelledby="scenarios-title">
          <div className="home-section-heading home-section-heading-tight">
            <div>
              <div className="home-section-kicker">WHY IT MATTERS</div>
              <h2 id="scenarios-title">Designed for the work<br /><em>behind the prompt.</em></h2>
            </div>
            <a className="home-inline-link" href="#/dashboard">Open live dashboard <span aria-hidden="true">↗</span></a>
          </div>
          <div className="home-scenario-grid">
            {scenarios.map((scenario) => (
              <article className="home-scenario-card" key={scenario.tag}>
                <span className="home-scenario-tag">{scenario.tag}</span>
                <h3>{scenario.title}</h3>
                <p>{scenario.detail}</p>
                <strong>{scenario.value}</strong>
              </article>
            ))}
          </div>
        </section>

        <section className="home-final-cta" aria-label="Open dashboard">
          <div>
            <div className="home-section-kicker">READY WHEN YOU ARE</div>
            <h2>Turn the next idea<br /><em>into a traceable run.</em></h2>
          </div>
          <div className="home-final-actions">
            <a className="home-button home-button-primary" href="#/dashboard">Open operations dashboard <span aria-hidden="true">→</span></a>
            <span>real Gateway connection · live task events</span>
          </div>
        </section>

        <footer className="home-footer">
          <span>ULTIMATECODERS / PRODUCT OVERVIEW</span>
          <span>Gateway-backed · dashboard-first · worker-ready</span>
        </footer>
      </div>
    </main>
  );
}
