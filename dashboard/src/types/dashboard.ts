/** Dashboard API response types — mirrors backend dashboard-spec.md contracts. */

// ── Health ──────────────────────────────────────────────

export interface HealthComponent {
  name: string;
  status: string;
  details?: string;
}

export interface HealthData {
  available: boolean;
  status: string;
  version?: string;
  uptime_seconds?: number;
  components: HealthComponent[];
  error?: string;
}

// ── Workers ─────────────────────────────────────────────

export interface WorkerInfo {
  id: string;
  capabilities: string[];
  current_load: number;
  max_capacity: number;
  load_percent: number;
  last_heartbeat: string;
  heartbeat_age_seconds: number;
  heartbeat_stale: boolean;
  is_available: boolean;
}

export interface WorkersData {
  available: boolean;
  workers: WorkerInfo[];
  total: number;
  available_count: number;
}

// ── Tasks ───────────────────────────────────────────────

export interface SubtaskSummary {
  id: string;
  description: string;
  status: string;
  depends_on: string[];
  assigned_worker?: string;
  result?: string;
  /** Files modified by this subtask (from subtask_completed event). */
  modified_files?: Array<{ path: string; type: string }>;
  /** Current retry attempt (0 = first attempt). */
  retry_count?: number;
  /** Last error message if subtask failed. */
  error?: string;
}

export interface TaskSummary {
  id: string;
  description: string;
  status: string;
  project_id: string;
  subtask_count: number;
  created_at: string;
  updated_at: string;
  subtasks?: SubtaskSummary[];
}

export interface TasksData {
  available: boolean;
  tasks: TaskSummary[];
  total: number;
  status_counts: Record<string, number>;
  pending_task_count: number;
}

// ── Scheduler ───────────────────────────────────────────

export interface NightWindow {
  start: string;
  end: string;
  timezone: string;
  is_active: boolean;
}

export interface ScheduledJob {
  id: string;
  description: string;
  enabled: boolean;
  cron_expression?: string;
  execute_after?: string;
}

export interface ExecutionHistory {
  task_id: string;
  status: string;
  started_at?: string;
  completed_at?: string;
  result_summary?: string;
}

export interface SchedulerData {
  available: boolean;
  is_running: boolean;
  night_window: NightWindow | null;
  jobs: ScheduledJob[];
  execution_history: ExecutionHistory[];
}

// ── Circuit Breaker / Rate Limiter ──────────────────────

export interface CircuitBreakerInfo {
  available: boolean;
  state: string;
  failure_count: number;
  failure_threshold: number;
  total_calls: number;
  total_rejected: number;
  recovery_timeout_seconds: number;
  last_failure?: string;
  error?: string;
}

export interface RateLimiterInfo {
  available: boolean;
  rpm_available: number;
  tpm_available: number;
  active_count: number;
  total_requests: number;
  remaining_ratio: number;
  window_seconds: number;
  error?: string;
}

export interface CircuitBreakerData {
  available: boolean;
  circuit_breaker: CircuitBreakerInfo;
  rate_limiter: RateLimiterInfo;
  engine_circuit_breaker: Record<string, unknown>;
  engine_rate_limiter: Record<string, unknown>;
}

// ── Events ──────────────────────────────────────────────

export interface DashboardEvent {
  timestamp: string;
  type: string;
  details: Record<string, unknown>;
}

export interface EventsData {
  available: boolean;
  events: DashboardEvent[];
  total: number;
}

// ── SSE Task Events ─────────────────────────────────────

export interface TaskEvent {
  timestamp: string;
  type: string;
  task_id: string;
  subtask_id?: string;
  data: Record<string, unknown>;
  /** SSE event id (set by useSSE, not present in gRPC-Web events). Used for dedup. */
  _sseId?: string;
}

// ── Metrics (observability) ─────────────────────────────

export interface TaskMetrics {
  avg_duration_ms: number;
  p50_duration_ms: number;
  p95_duration_ms: number;
  p99_duration_ms: number;
  retry_rate: number;
  slow_tasks_count: number;
  total_completed: number;
  total_failed: number;
  success_rate: number;
}

export interface WorkerMetrics {
  avg_heartbeat_age_seconds: number;
  per_worker_tool_calls: Record<string, number>;
  per_worker_subtask_count: Record<string, number>;
  cluster_load_pct: number;
}

export interface EventMetrics {
  events_per_minute: number;
  error_spike: boolean;
  event_type_counts: Record<string, number>;
}

export interface SystemMetrics {
  uptime_seconds: number;
  circuit_breaker_state: string;
  rate_limiter_remaining_ratio: number;
  cluster_utilization_pct: number;
}

export interface MetricsSample {
  timestamp: number;
  events_per_minute: number;
  avg_duration_ms: number;
  error_rate: number;
  cluster_utilization: number;
}

export interface MetricsSnapshot {
  task: TaskMetrics;
  worker: WorkerMetrics;
  event: EventMetrics;
  system: SystemMetrics;
  trend: MetricsSample[];
}

// ── SSE Full Snapshot ───────────────────────────────────

export interface DashboardSnapshot {
  timestamp?: string;
  health?: HealthData;
  workers?: WorkersData;
  tasks?: TasksData;
  scheduler?: SchedulerData;
  circuit_breaker?: CircuitBreakerData;
  events?: DashboardEvent[];
  recent_task_events?: TaskEvent[];
  metrics?: MetricsSnapshot;
}

// ── POST Response ───────────────────────────────────────

export interface ActionResponse {
  success: boolean;
  error?: string;
  [key: string]: unknown;
}

export interface TaskSubmitResponse extends ActionResponse {
  task_id?: string;
  status?: string;
  subtask_count?: number;
  subtasks?: SubtaskSummary[];
}

// ── File Browser ────────────────────────────────────────

export interface RepoInfo {
  repo_id: string;
  local_path: string;
  exists: boolean;
}

export interface ReposData {
  available: boolean;
  repos: RepoInfo[];
  total: number;
}

export interface DirEntry {
  name: string;
  path: string;
  type: "directory" | "file";
  size: number;
}

export interface DirectoryListing {
  repo_id: string;
  path: string;
  entries: DirEntry[];
  total: number;
}

export interface FileContent {
  repo_id: string;
  path: string;
  binary: boolean;
  size: number;
  content?: string;
  language?: string;
  truncated?: boolean;
  lines?: number;
}
