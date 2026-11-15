import { useCallback, useEffect, useRef, useState } from "react";
import { getStoredToken } from "@/hooks/useAuth";
import { useGrpcWeb } from "@/hooks/useGrpcWeb";
import {
  fetchWorkflowReplays,
  formatReplayDuration,
  WorkflowReplayRequestError,
  type WorkflowReplayRun,
} from "@/lib/replays";
import {
  DASHBOARD_HREF,
  HOME_DOCUMENT_TITLE,
  RUNTIME_POLL_INTERVAL_MS,
  RUNTIME_PROBE_TIMEOUT_MS,
  WORKFLOW_HREF,
  chineseCount,
  countWord,
  formatSyncAge,
  joinChinese,
  nextPollDelay,
  padOrdinal,
  pollingActive,
  probeGateway,
  runtimeBadge,
  runtimeStatusLabel,
  showcaseMetrics,
  showcasePrimaryCta,
  showcaseRuntimeMode,
  transportLabel,
  type ProbeState,
} from "@/lib/homeRuntime";

const capabilities = [
  {
    title: "DAG task orchestration",
    detail: "把一个复杂需求拆成依赖清晰的执行图，按波次调度、汇总结果，并支持暂停、恢复和取消。",
    signal: "plan → dispatch → review",
    tone: "mint",
  },
  {
    title: "Capability-aware workers",
    detail: "Worker Registry 根据能力、负载和心跳选择执行者，从本地 fallback 平滑扩展到多 Worker 集群。",
    signal: "match by capability + load",
    tone: "blue",
  },
  {
    title: "Hybrid Search + Memory",
    detail: "Text、Semantic、AST 检索与分层 Memory 共享上下文，让 Agent 在多仓库中保持连续的工程记忆。",
    signal: "text / semantic / AST",
    tone: "violet",
  },
  {
    title: "Dashboard-first control",
    detail: "从 Web Dashboard 提交任务、查看 Worker 与执行事件，并控制暂停、恢复和取消。",
    signal: "Dashboard ↔ Gateway",
    tone: "amber",
  },
  {
    title: "Event-driven recovery",
    detail: "任务状态通过事件广播、Checkpoint 和 WatchTask 实时同步；连接中断后可继续观察，而不是丢失进度。",
    signal: "events + checkpoints",
    tone: "rose",
  },
  {
    title: "Local to cluster",
    detail: "同一套产品从本地内存 fallback、Docker Gateway 到 NATS + 多 Worker 部署，按规模渐进扩展。",
    signal: "local → distributed",
    tone: "teal",
  },
] as const;

const systemLayers = [
  {
    label: "Web interface",
    summary: "入口",
    title: "Dashboard + TaskService",
    route: "intent in",
    detail: "从 Dashboard 提交任务并查看结果，通过 TaskService 控制同一个执行图。",
    tags: ["#/dashboard", "TaskService", "gRPC-Web"],
    tone: "mint",
  },
  {
    label: "Control plane",
    summary: "控制",
    title: "Rust Gateway",
    route: "plan + coordinate",
    detail: "TaskService、WorkerService 和 WatchTask 把任务、能力和事件组织成一个控制面。",
    tags: ["DAG", "WorkerRegistry", "gRPC-Web"],
    tone: "blue",
  },
  {
    label: "Execution plane",
    summary: "执行",
    title: "Workers that match",
    route: "dispatch + execute",
    detail: "按能力、负载和心跳选择 Worker，在隔离工作区运行 Claude Code、Codex 或 Grok Build。",
    tags: ["NATS", "sandbox", "multi-worker"],
    tone: "amber",
  },
  {
    label: "Knowledge plane",
    summary: "上下文",
    title: "Search + Memory",
    route: "context attached",
    detail: "Text、Semantic、AST 检索与分层 Memory 为每个 subtask 补上跨仓库上下文。",
    tags: ["hybrid search", "TiKV", "Qdrant / PG"],
    tone: "violet",
  },
  {
    label: "Event plane",
    summary: "事件",
    title: "Signals that return",
    route: "observe + recover",
    detail: "TaskEvent、Checkpoint 和结果流回到 Dashboard，执行过程可追踪、可恢复。",
    tags: ["WatchTask", "checkpoint", "TaskEvent"],
    tone: "teal",
  },
] as const;

const workflow = [
  { label: "Intent", title: "Describe the change", detail: "在 Dashboard 输入自然语言任务。" },
  { label: "Plan", title: "Build the execution graph", detail: "Orchestrator 生成带依赖关系的 DAG。" },
  { label: "Run", title: "Dispatch to the right worker", detail: "Gateway 按能力和负载调度 Worker。" },
  { label: "Observe", title: "Stream every state change", detail: "TaskEvent 通过 gRPC-Web 返回。" },
  { label: "Recover", title: "Resume with context", detail: "Memory、Checkpoint 和事件让执行可恢复。" },
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

/** In-page destinations, in document order: also the order the scroll-spy checks. */
const navSections = [
  { id: "home-overview", label: "Overview" },
  { id: "home-capabilities", label: "Capabilities" },
  { id: "home-workflow", label: "Execution flow" },
  { id: "home-replay", label: "Replay" },
  { id: "home-scenarios", label: "Use cases" },
] as const;

type ReplayState = "idle" | "loading" | "ready" | "empty" | "auth" | "error";

function shortReplayId(value: string): string {
  if (value.length <= 18) return value;
  return `${value.slice(0, 9)}…${value.slice(-6)}`;
}

function replayAge(updatedAt: string | null): string {
  if (!updatedAt) return "no timestamp";
  const parsed = Date.parse(updatedAt);
  if (!Number.isFinite(parsed)) return "no timestamp";
  return formatSyncAge(parsed, Date.now());
}

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

export default function HomePage() {
  const [probe, setProbe] = useState<ProbeState>("probing");
  const [probeNonce, setProbeNonce] = useState(0);
  const probeAbortRef = useRef<AbortController | null>(null);
  // Only talk to the Gateway once a probe proved there is one behind this host:
  // the showcase is also published as a purely static page.
  const { connectionState, healthCheck, listTasks, connect } = useGrpcWeb({
    enabled: probe === "reachable",
  });
  const connectRef = useRef(connect);
  connectRef.current = connect;

  const [runtime, setRuntime] = useState<RuntimeSnapshot>(initialRuntime);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [consecutiveFailures, setConsecutiveFailures] = useState(0);
  const failuresRef = useRef(0);
  const [activeSection, setActiveSection] = useState<string>(navSections[0].id);
  const [replays, setReplays] = useState<WorkflowReplayRun[]>([]);
  const [replayState, setReplayState] = useState<ReplayState>("idle");
  const [activeReplayId, setActiveReplayId] = useState<string | null>(null);
  const [replayCursor, setReplayCursor] = useState(0);
  const [replayPlaying, setReplayPlaying] = useState(false);
  const replayAbortRef = useRef<AbortController | null>(null);

  const mode = showcaseRuntimeMode({
    probe,
    connected: connectionState === "connected",
    consecutiveFailures,
  });
  const badge = runtimeBadge(mode);
  const isLive = mode === "live";
  const statusLabel = isLive ? transportLabel(connectionState) : runtimeStatusLabel(mode);
  const canRefresh = probe === "reachable";

  // Only a host that answered the probe may be advertised as a dashboard; on a
  // static showcase the primary action opens the execution loop and explains why.
  const primaryCta = showcasePrimaryCta(mode);
  const primaryIsDashboard = primaryCta.href === DASHBOARD_HREF;
  const primaryArrow = primaryIsDashboard ? "→" : "↓";
  const secondaryCta = primaryIsDashboard
    ? { label: "See how it works", href: WORKFLOW_HREF, arrow: "↓" }
    : { label: "Open operations dashboard", href: DASHBOARD_HREF, arrow: "→" };

  const refreshRuntime = useCallback(async (): Promise<boolean> => {
    setIsRefreshing(true);
    const [health, tasks] = await Promise.allSettled([healthCheck(), listTasks()]);
    setIsRefreshing(false);
    if (health.status !== "fulfilled") {
      failuresRef.current += 1;
      setConsecutiveFailures(failuresRef.current);
      return false;
    }
    failuresRef.current = 0;
    setConsecutiveFailures(0);
    setRuntime((current) => ({
      gatewayStatus: health.value.status || "healthy",
      gatewayVersion: health.value.version || "—",
      taskTotal: tasks.status === "fulfilled" ? tasks.value.total : current.taskTotal,
      lastSyncedAt: Date.now(),
    }));
    return true;
  }, [healthCheck, listTasks]);

  const loadReplays = useCallback(async (signal?: AbortSignal): Promise<void> => {
    setReplayState("loading");
    try {
      const runs = await fetchWorkflowReplays(fetch, getStoredToken(), signal);
      if (signal?.aborted) return;
      setReplays(runs);
      setReplayState(runs.length > 0 ? "ready" : "empty");
      setActiveReplayId((current) =>
        current && runs.some((run) => run.id === current) ? current : runs[0]?.id ?? null,
      );
      setReplayCursor(0);
      setReplayPlaying(false);
    } catch (error) {
      if (signal?.aborted || (error instanceof DOMException && error.name === "AbortError")) {
        return;
      }
      if (error instanceof WorkflowReplayRequestError && (error.status === 401 || error.status === 403)) {
        setReplays([]);
        setReplayState("auth");
        setReplayPlaying(false);
        return;
      }
      setReplays([]);
      setReplayState("error");
      setReplayPlaying(false);
    }
  }, []);

  useEffect(() => {
    if (probe !== "reachable") return;
    const controller = new AbortController();
    replayAbortRef.current = controller;
    // Defer the first fetch by a microtask: `loadReplays` flips the state to
    // "loading" synchronously, and a state update in the effect body would
    // cascade a render before the fetch even starts.
    queueMicrotask(() => {
      if (!controller.signal.aborted) void loadReplays(controller.signal);
    });
    return () => {
      controller.abort();
      if (replayAbortRef.current === controller) replayAbortRef.current = null;
    };
  }, [probe, loadReplays]);

  const activeReplay = replays.find((run) => run.id === activeReplayId) ?? replays[0] ?? null;
  const replaySteps = activeReplay?.steps ?? [];
  const replayCursorIndex = replaySteps.length > 0
    ? Math.min(replayCursor, replaySteps.length - 1)
    : 0;
  const currentReplayStep = replaySteps[replayCursorIndex] ?? null;

  useEffect(() => {
    if (!replayPlaying || !activeReplay || replaySteps.length === 0) return;
    const timer = window.setTimeout(() => {
      if (replayCursor >= replaySteps.length - 1) {
        setReplayPlaying(false);
        return;
      }
      setReplayCursor(replayCursor + 1);
    }, 1_200);
    return () => window.clearTimeout(timer);
  }, [activeReplay, replayCursor, replayPlaying, replaySteps.length]);

  const selectReplay = useCallback((run: WorkflowReplayRun) => {
    setActiveReplayId(run.id);
    setReplayCursor(0);
    setReplayPlaying(false);
  }, []);

  const toggleReplay = useCallback(() => {
    if (!activeReplay) return;
    if (replayPlaying) {
      setReplayPlaying(false);
      return;
    }
    if (replayCursor >= replaySteps.length - 1) setReplayCursor(0);
    setReplayPlaying(true);
  }, [activeReplay, replayCursor, replayPlaying, replaySteps.length]);

  const handleReplayRefresh = useCallback(() => {
    replayAbortRef.current?.abort();
    const controller = new AbortController();
    replayAbortRef.current = controller;
    void loadReplays(controller.signal);
  }, [loadReplays]);

  const probeOnce = useCallback(async (): Promise<ProbeState> => {
    probeAbortRef.current?.abort();
    const controller = new AbortController();
    probeAbortRef.current = controller;
    const timer = window.setTimeout(() => controller.abort(), RUNTIME_PROBE_TIMEOUT_MS);
    try {
      return await probeGateway(fetch, controller.signal, getStoredToken());
    } finally {
      window.clearTimeout(timer);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    setProbe("probing");
    void probeOnce().then((verdict) => {
      if (cancelled) return;
      setProbe(verdict);
      // The hook's own mount-time connect saw `enabled: false`, so open the
      // stream now that the backend is known to exist.
      if (verdict === "reachable") connectRef.current();
    });
    return () => {
      cancelled = true;
      probeAbortRef.current?.abort();
    };
  }, [probeOnce, probeNonce]);

  // Poll only while the card claims a live or connecting transport. Keying the
  // loop on the mode is what makes it stop when the card says OFFLINE — and what
  // makes it restart by itself when a manual retry brings the Gateway back.
  useEffect(() => {
    if (!pollingActive(mode)) return;
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      const ok = await refreshRuntime();
      if (cancelled) return;
      // Back off on failure instead of retrying every 30s forever; the mode
      // turns "stalled" once the budget is spent and the cleanup cancel wins.
      timer = window.setTimeout(
        () => void tick(),
        ok ? RUNTIME_POLL_INTERVAL_MS : nextPollDelay(failuresRef.current),
      );
    };
    timer = window.setTimeout(() => void tick(), 0);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [mode, refreshRuntime]);

  // Title: the shell's <title> names the operations dashboard.
  useEffect(() => {
    const previousTitle = document.title;
    document.title = HOME_DOCUMENT_TITLE;
    return () => {
      document.title = previousTitle;
    };
  }, []);

  // Scroll-spy: the sticky header highlights the section in view.
  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") return;
    const tracked = navSections
      .map((section) => document.getElementById(section.id))
      .filter((element): element is HTMLElement => element !== null);
    if (tracked.length === 0) return;
    const visible = new Set<string>();
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) visible.add(entry.target.id);
          else visible.delete(entry.target.id);
        }
        const first = tracked.find((element) => visible.has(element.id));
        setActiveSection(first ? first.id : navSections[0].id);
      },
      { rootMargin: "-96px 0px -55% 0px", threshold: 0 },
    );
    for (const element of tracked) observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const handleRefresh = useCallback(() => {
    if (canRefresh) {
      void refreshRuntime();
      return;
    }
    // Not reachable: re-probe rather than hammer a backend that is not there.
    setProbeNonce((nonce) => nonce + 1);
  }, [canRefresh, refreshRuntime]);

  return (
    <main className="home-page">
      <div className="home-grid-glow" aria-hidden="true" />
      <a className="home-skip-link" href="#home-content">
        Skip to content
      </a>
      <header className="home-topbar">
        <div className="home-shell">
          <nav className="home-nav" aria-label="Primary navigation">
            <a className="home-brand" href="#home-overview">
              <span className="home-brand-mark">UC</span>
              <span>
                <strong>UltimateCoders</strong>
                <small>distributed AI coding</small>
              </span>
            </a>
            <div className="home-nav-links">
              {navSections.map((section) => (
                <a
                  key={section.id}
                  className={`home-nav-link ${activeSection === section.id ? "home-nav-link-active" : ""}`}
                  href={`#${section.id}`}
                  aria-current={activeSection === section.id ? "true" : undefined}
                >
                  {section.label}
                </a>
              ))}
              <a className="home-nav-link" href="#/dashboard">
                Operations dashboard <span aria-hidden="true">↗</span>
              </a>
            </div>
          </nav>
        </div>
        <div className="home-scroll-progress" aria-hidden="true" />
      </header>

      <div className="home-shell" id="home-content" tabIndex={-1}>
        <section className="home-hero" id="home-overview" aria-labelledby="home-hero-title">
          <div className="home-hero-copy">
            <div className="home-eyebrow"><span className="home-eyebrow-dot" /> ORCHESTRATE · EXECUTE · REMEMBER</div>
            <h1 id="home-hero-title">From intent to<br /><em>observable execution.</em></h1>
            <p className="home-hero-lede">
              一个入口连接 Gateway、Worker、Search 和 Memory，把复杂 Coding 任务变成可调度、可观察、可恢复的执行链。
            </p>
            <div className="home-hero-actions">
              <a className="home-button home-button-primary" href={primaryCta.href}>
                {primaryCta.label} <span aria-hidden="true">{primaryArrow}</span>
              </a>
              <a className="home-button home-button-secondary" href={secondaryCta.href}>
                {secondaryCta.label} <span aria-hidden="true">{secondaryCta.arrow}</span>
              </a>
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
              <span>{statusLabel}</span>
              <span className={`home-runtime-mode ${isLive ? "home-runtime-mode-live" : ""}`}>
                {badge}
              </span>
              <button className="home-refresh-button" type="button" onClick={handleRefresh} disabled={isRefreshing || probe === "probing"}>
                {isRefreshing ? "syncing" : canRefresh && mode !== "stalled" ? "refresh" : "retry"}
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
            {mode === "preview" ? (
              <p className="home-runtime-hint">
                静态展品：这个域名没有连接 Gateway，运行时数据来自最近一次成功快照。点 retry 可重新探测。
              </p>
            ) : null}
            {mode === "stalled" ? (
              <p className="home-runtime-hint">
                Gateway 已停止响应：自动刷新已暂停，卡片上的数据是最后一次成功快照。点 retry 重新探测。
              </p>
            ) : null}
            <div className="home-runtime-foot">
              <span>live snapshot · {formatSyncAge(runtime.lastSyncedAt, Date.now())}</span>
              <span>
                {mode === "stalled"
                  ? "auto refresh paused"
                  : canRefresh
                    ? "auto refresh 30s"
                    : "no live API on this host"}
              </span>
            </div>
          </div>
        </section>

        <div className="home-metrics" aria-label="Product summary">
          {showcaseMetrics(workflow.length).map((metric) => (
            <div key={metric.label}><strong>{metric.value}</strong><span>{metric.label}</span></div>
          ))}
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
            {capabilities.map((capability, position) => (
              <article className={`home-capability-card home-tone-${capability.tone}`} key={capability.title}>
                <div className="home-card-topline"><span>{padOrdinal(position + 1)}</span><span className="home-card-signal">{capability.signal}</span></div>
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
                <h3>{countWord(systemLayers.length)} layers.<br /><em>One execution surface.</em></h3>
              </div>
              <p>
                {chineseCount(systemLayers.length)}个面板对应真实边界：
                {joinChinese(systemLayers.map((layer) => layer.summary))}。
              </p>
            </div>
            <div className="home-system-layer-grid">
              {systemLayers.map((layer, position) => (
                <article className={`home-system-layer home-tone-${layer.tone}`} key={layer.title}>
                  <div className="home-system-layer-topline"><span>{padOrdinal(position + 1)}</span><b>{layer.route}</b></div>
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
              <div className="home-workflow-item" key={item.title}>
                <div className="home-workflow-marker"><span>{padOrdinal(index + 1)}</span>{index < workflow.length - 1 && <i aria-hidden="true" />}</div>
                <div className="home-workflow-label">{item.label}</div>
                <h3>{item.title}</h3>
                <p>{item.detail}</p>
              </div>
            ))}
          </div>
        </section>

        <section className="home-section home-replay-section" id="home-replay" aria-labelledby="replay-title">
          <div className="home-section-heading home-section-heading-tight">
            <div>
              <div className="home-section-kicker">EXECUTION REPLAY</div>
              <h2 id="replay-title">Every step,<br /><em>replayed from evidence.</em></h2>
            </div>
            <p>首页直接读取最近的工作流执行：逐步播放每一步的 agent、状态、摘要和耗时，不必先进入调试面板。</p>
          </div>

          <div className="home-replay-toolbar">
            <span className={`home-replay-source ${probe === "reachable" ? "home-replay-source-live" : ""}`}>
              <i aria-hidden="true" /> {probe === "reachable" ? (replayState === "auth" ? "sign-in required" : replayState === "loading" ? "loading runs" : "event-derived") : "gateway offline"}
            </span>
            <button
              className="home-replay-refresh"
              type="button"
              onClick={handleReplayRefresh}
              disabled={!canRefresh || replayState === "loading"}
            >
              refresh replay
            </button>
          </div>

          <div className="home-replay-shell">
            <div className="home-replay-runs" aria-label="Recent workflow runs">
              {probe !== "reachable" ? (
                <p className="home-replay-hint">连接 Gateway 后，这里会显示最近的工作流执行。静态展品仍可浏览上面的执行循环。</p>
              ) : replayState === "loading" && replays.length === 0 ? (
                <p className="home-replay-hint">Reading recent workflow events…</p>
              ) : replayState === "auth" ? (
                <div className="home-replay-auth">
                  <p className="home-replay-hint">回放与其它 Dashboard 读取面使用同一鉴权。登录后可查看真实工作流执行。</p>
                  <a className="home-replay-auth-link" href="#/dashboard">sign in on the operations dashboard →</a>
                </div>
              ) : replayState === "error" ? (
                <div className="home-replay-error">
                  <p className="home-replay-hint">回放数据暂时不可用。Gateway 可能已恢复，但 REST 事件读取失败。</p>
                  <button type="button" onClick={handleReplayRefresh}>try again</button>
                </div>
              ) : replays.length === 0 ? (
                <p className="home-replay-hint">还没有可回放的工作流执行。带 steps 的任务完成后，每一步的进度事件会在这里组成时间线。</p>
              ) : (
                replays.map((run) => (
                  <button
                    className={`home-replay-run ${activeReplay?.id === run.id ? "home-replay-run-active" : ""}`}
                    type="button"
                    key={run.id}
                    onClick={() => selectReplay(run)}
                    aria-pressed={activeReplay?.id === run.id}
                  >
                    <span className={`home-replay-badge home-replay-badge-${run.status}`}>{run.status}</span>
                    <strong title={run.taskId}>{shortReplayId(run.taskId || "task")}</strong>
                    <small>{shortReplayId(run.subtaskId || "subtask")} · {run.completedSteps}/{run.stepTotal} steps</small>
                    <span className="home-replay-run-age">{replayAge(run.updatedAt)}</span>
                  </button>
                ))
              )}
            </div>

            <div className="home-replay-stage" aria-live="polite">
              {activeReplay ? (
                <>
                  <div className="home-replay-stage-top">
                    <div className="home-replay-title">
                      <span className={`home-replay-badge home-replay-badge-${activeReplay.status}`}>{activeReplay.status}</span>
                      <strong title={activeReplay.taskId}>{shortReplayId(activeReplay.taskId || "task")}</strong>
                      <small>{shortReplayId(activeReplay.subtaskId || "subtask")} · updated {replayAge(activeReplay.updatedAt)}</small>
                    </div>
                    <span className="home-replay-counter">{replayCursorIndex + 1} / {replaySteps.length}</span>
                  </div>

                  <input
                    className="home-replay-scrubber"
                    type="range"
                    min={0}
                    max={Math.max(0, replaySteps.length - 1)}
                    step={1}
                    value={replayCursorIndex}
                    aria-label="Replay step"
                    onChange={(event) => {
                      setReplayPlaying(false);
                      setReplayCursor(Number(event.target.value));
                    }}
                  />

                  <ol className="home-replay-timeline">
                    {replaySteps.map((step, index) => (
                      <li key={`${step.index}-${index}`}>
                        <button
                          className={`home-replay-step ${index === replayCursorIndex ? "home-replay-step-current" : ""} ${index < replayCursorIndex ? "home-replay-step-past" : ""}`}
                          type="button"
                          onClick={() => {
                            setReplayCursor(index);
                            setReplayPlaying(false);
                          }}
                          aria-current={index === replayCursorIndex ? "step" : undefined}
                        >
                          <span className={`home-replay-dot home-replay-dot-${step.status}`} aria-hidden="true" />
                          <span className="home-replay-step-ordinal">{padOrdinal(step.index + 1)}</span>
                          <span className="home-replay-step-copy">
                            <strong>{step.agent || "agent"}</strong>
                            <small>{step.summary || step.status}</small>
                          </span>
                          <span className="home-replay-step-meta">
                            <em className={`home-replay-step-status home-replay-step-status-${step.status}`}>{step.status}</em>
                            <time>{formatReplayDuration(step.durationMs)}</time>
                          </span>
                        </button>
                      </li>
                    ))}
                  </ol>

                  <div className="home-replay-controls">
                    <button
                      type="button"
                      onClick={() => {
                        setReplayPlaying(false);
                        setReplayCursor(Math.max(0, replayCursorIndex - 1));
                      }}
                      disabled={replayCursorIndex === 0}
                      aria-label="Previous step"
                    >
                      ←
                    </button>
                    <button
                      className="home-replay-play"
                      type="button"
                      onClick={toggleReplay}
                      aria-pressed={replayPlaying}
                    >
                      {replayPlaying ? "❚❚ pause" : "▶ play"}
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setReplayPlaying(false);
                        setReplayCursor(Math.min(replaySteps.length - 1, replayCursorIndex + 1));
                      }}
                      disabled={replayCursorIndex >= replaySteps.length - 1}
                      aria-label="Next step"
                    >
                      →
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setReplayPlaying(false);
                        setReplayCursor(0);
                      }}
                      disabled={replayCursorIndex === 0}
                    >
                      ↺ reset
                    </button>
                    <span className="home-replay-progress-label">
                      {currentReplayStep
                        ? `${currentReplayStep.status} · ${formatReplayDuration(currentReplayStep.durationMs)}`
                        : ""}
                    </span>
                  </div>

                  {currentReplayStep ? (
                    <p className="home-replay-current">
                      {currentReplayStep.summary || "No summary was recorded for this step."}
                    </p>
                  ) : null}
                </>
              ) : (
                <p className="home-replay-hint home-replay-stage-empty">
                  {probe === "reachable"
                    ? replayState === "auth"
                      ? "登录后可在这里逐步查看真实工作流执行。"
                      : "最近还没有工作流执行可回放。"
                    : "连接 Gateway 后可查看实时工作流回放。"}
                </p>
              )}
            </div>
          </div>
        </section>

        <section className="home-section home-scenarios-section" id="home-scenarios" aria-labelledby="scenarios-title">
          <div className="home-section-heading home-section-heading-tight">
            <div>
              <div className="home-section-kicker">WHY IT MATTERS</div>
              <h2 id="scenarios-title">Designed for the work<br /><em>behind the prompt.</em></h2>
            </div>
            <a className="home-inline-link" href={primaryCta.href}>
              {primaryIsDashboard ? "Open live dashboard" : "See the execution loop"}{" "}
              <span aria-hidden="true">{primaryIsDashboard ? "↗" : "↓"}</span>
            </a>
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
            <a className="home-button home-button-primary" href={primaryCta.href}>
              {primaryCta.label} <span aria-hidden="true">{primaryArrow}</span>
            </a>
            <span>{primaryCta.note}</span>
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
