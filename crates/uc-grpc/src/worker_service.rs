//! WorkerService implementation — Worker registration and lifecycle management.
//!
//! Workers call RegisterWorker on startup, WorkerHeartbeat periodically,
//! and DeregisterWorker on graceful shutdown. The gateway maintains an
//! in-memory WorkerRegistry as the source of truth for worker state.

use std::collections::HashMap;

use tonic::{Request, Response, Status};
use uc_types::{EngineApi, CONTRACT_VERSION};

use crate::server::GrpcServer;
use crate::ultimate_coders::worker_service_server::WorkerService;
use crate::ultimate_coders::*;

// ── WorkerRegistry ───────────────────────────────────────────────

/// In-memory registry of connected workers.
///
/// Tracks worker capabilities, load, and heartbeat state.
/// This is the gateway's source of truth for worker discovery,
/// supplementing (and eventually replacing) NATS-based heartbeat tracking.
pub struct WorkerRegistry {
    workers: HashMap<String, RegisteredWorker>,
}

/// A worker that has registered with the gateway.
#[derive(Debug, Clone)]
pub struct RegisteredWorker {
    pub id: String,
    pub capabilities: Vec<String>,
    pub max_capacity: u32,
    pub current_load: u32,
    pub metadata: String,
    /// Execution contract the worker declared at registration
    /// ([`uc_types::CONTRACT_VERSION`] handshake). Empty = legacy
    /// pre-handshake worker: accepted for observability, never dispatchable.
    pub contract_version: String,
    /// Execution scopes (T8 #650 / D8 #645): normalized project_ids the
    /// worker serves. Empty = OPEN worker (accepts any scope).
    pub projects: Vec<String>,
    pub registered_at: chrono::DateTime<chrono::Utc>,
    pub last_heartbeat: chrono::DateTime<chrono::Utc>,
}

impl RegisteredWorker {
    /// Whether the worker is considered available (heartbeat not stale).
    pub fn is_available(&self, stale_timeout_secs: f64) -> bool {
        let age = (chrono::Utc::now() - self.last_heartbeat).num_seconds() as f64;
        // "available" means the worker can take more work: heartbeat fresh
        // AND under capacity. Without the capacity check, a saturated worker
        // (or one with max_capacity==0) was returned as available, causing
        // dispatch_ready_subtasks to over-assign to it.
        age <= stale_timeout_secs && self.current_load < self.max_capacity
    }

    /// Load as a percentage of max capacity.
    pub fn load_percent(&self) -> u32 {
        if self.max_capacity == 0 {
            return 100;
        }
        (self.current_load * 100) / self.max_capacity
    }

    /// Whether this worker serves tasks of the given scope (T8 #650).
    ///
    /// OPEN workers (empty `projects`) serve every scope — including the
    /// legacy empty scope. A scoped worker matches only when its declared
    /// project list contains `project_id`; because registration trims and
    /// drops blank entries, a scoped worker can never claim the empty scope,
    /// so empty-scope tasks only ever go to open workers.
    pub fn serves_scope(&self, project_id: &str) -> bool {
        self.projects.is_empty() || self.projects.iter().any(|p| p == project_id)
    }
}

impl WorkerRegistry {
    /// Stale timeout in seconds — workers not heartbeating within this are considered unavailable.
    const STALE_TIMEOUT_SECS: f64 = 60.0;

    pub fn new() -> Self {
        Self {
            workers: HashMap::new(),
        }
    }

    /// Register a new worker or re-register an existing one.
    ///
    /// `contract_version` is the execution contract the worker speaks. A
    /// non-empty version that differs from [`CONTRACT_VERSION`] is REFUSED
    /// (mixed-version deployment must fail loudly at the handshake, not
    /// silently at dispatch). Empty = legacy pre-handshake worker: accepted
    /// (observability) but never dispatchable — see [`Self::dispatch_gate`].
    pub fn register(
        &mut self,
        worker_id: String,
        capabilities: Vec<String>,
        max_capacity: u32,
        metadata: String,
        contract_version: String,
    ) -> Result<(), String> {
        self.register_with_projects(
            worker_id,
            capabilities,
            max_capacity,
            metadata,
            contract_version,
            Vec::new(),
        )
    }

    /// Register with explicit execution scopes (T8 #650 / D8 #645).
    ///
    /// `projects` is normalized on entry: entries are trimmed, blank entries
    /// dropped, duplicates removed (order preserved). An empty list means an
    /// OPEN worker that serves every scope.
    pub fn register_with_projects(
        &mut self,
        worker_id: String,
        capabilities: Vec<String>,
        max_capacity: u32,
        metadata: String,
        contract_version: String,
        projects: Vec<String>,
    ) -> Result<(), String> {
        if worker_id.is_empty() {
            return Err("worker_id cannot be empty".to_string());
        }
        if !contract_version.is_empty() && contract_version != CONTRACT_VERSION {
            return Err(format!(
                "contract_version mismatch: worker '{}' declares '{}', gateway speaks '{}' \
                 — gateway and workers must be upgraded in lockstep",
                worker_id, contract_version, CONTRACT_VERSION
            ));
        }
        let now = chrono::Utc::now();
        let worker = RegisteredWorker {
            id: worker_id.clone(),
            capabilities,
            max_capacity,
            current_load: 0,
            metadata,
            contract_version,
            projects: normalize_projects(projects),
            registered_at: now,
            last_heartbeat: now,
        };
        let is_reregister = self.workers.contains_key(&worker_id);
        self.workers.insert(worker_id.clone(), worker);
        if is_reregister {
            tracing::info!(worker_id = %worker_id, "Worker re-registered");
        } else {
            tracing::info!(worker_id = %worker_id, "Worker registered");
        }
        Ok(())
    }

    /// Process a heartbeat from a worker.
    pub fn heartbeat(&mut self, worker_id: &str, current_load: u32) -> Result<(), String> {
        let worker = self.workers.get_mut(worker_id).ok_or_else(|| {
            format!(
                "Worker '{}' not registered — call RegisterWorker first",
                worker_id
            )
        })?;
        worker.last_heartbeat = chrono::Utc::now();
        worker.current_load = current_load;
        Ok(())
    }

    /// Deregister a worker (graceful shutdown).
    pub fn deregister(&mut self, worker_id: &str) -> Result<(), String> {
        if self.workers.remove(worker_id).is_some() {
            tracing::info!(worker_id = %worker_id, "Worker deregistered");
            Ok(())
        } else {
            Err(format!("Worker '{}' not found", worker_id))
        }
    }

    /// Get all registered workers.
    pub fn workers(&self) -> &HashMap<String, RegisteredWorker> {
        &self.workers
    }

    /// Get available workers (heartbeat not stale, has capacity).
    pub fn available_workers(&self) -> Vec<&RegisteredWorker> {
        self.workers
            .values()
            .filter(|w| w.is_available(Self::STALE_TIMEOUT_SECS))
            .collect()
    }

    /// Find workers that have ALL the specified capabilities.
    pub fn workers_with_capabilities(&self, required: &[String]) -> Vec<&RegisteredWorker> {
        let required_set: std::collections::HashSet<_> = required.iter().collect();
        self.available_workers()
            .into_iter()
            .filter(|w| {
                let worker_caps: std::collections::HashSet<_> = w.capabilities.iter().collect();
                required_set.is_subset(&worker_caps)
            })
            .collect()
    }

    /// Find workers that have ALL the specified capabilities AND declared the
    /// gateway's [`CONTRACT_VERSION`] — the only workers dispatchable to.
    pub fn dispatchable_workers_with_capabilities(
        &self,
        required: &[String],
    ) -> Vec<&RegisteredWorker> {
        self.workers_with_capabilities(required)
            .into_iter()
            .filter(|w| w.contract_version == CONTRACT_VERSION)
            .collect()
    }

    /// The capability + scope + contract-version hard gate consulted by
    /// `publish_ready_subtasks` / `dispatch_ready_subtasks` before marking a
    /// subtask Assigned and publishing it.
    ///
    /// Filter order (T8 #650 / D8 #645): capability match first (unchanged),
    /// then the scope hard filter, then the contract-version check — a
    /// worker that does not serve `project_id` is never a dispatch candidate.
    pub fn dispatch_gate(&self, required: &[String], project_id: &str) -> WorkerDispatchGate {
        let candidates = self.workers_with_capabilities(required);
        if candidates.is_empty() {
            // No capability-matching available worker. With required
            // capabilities that is today's "keep Pending" case; without,
            // preserve the legacy best-effort publish (queue-group
            // subscribers may exist without ever gRPC-registering —
            // NATS-only deployments).
            return if required.is_empty() {
                WorkerDispatchGate::Dispatch
            } else {
                WorkerDispatchGate::NoCapableWorker
            };
        }
        // Scope hard filter: scoped workers must serve the task's project.
        let scope_matched: Vec<&RegisteredWorker> = candidates
            .iter()
            .copied()
            .filter(|w| w.serves_scope(project_id))
            .collect();
        if scope_matched.is_empty() {
            return WorkerDispatchGate::NoScopeMatchedWorker {
                workers: candidates.iter().map(|w| w.id.clone()).collect(),
            };
        }
        if scope_matched
            .iter()
            .all(|w| w.contract_version != CONTRACT_VERSION)
        {
            return WorkerDispatchGate::NoVersionMatchedWorker {
                workers: scope_matched
                    .iter()
                    .map(|w| (w.id.clone(), w.contract_version.clone()))
                    .collect(),
            };
        }
        WorkerDispatchGate::Dispatch
    }

    /// Mark workers with stale heartbeats as unavailable (returns stale worker IDs).
    pub fn stale_worker_ids(&self) -> Vec<String> {
        self.workers
            .values()
            .filter(|w| !w.is_available(Self::STALE_TIMEOUT_SECS))
            .map(|w| w.id.clone())
            .collect()
    }

    /// Convert to proto WorkerProto list for ListWorkers response.
    pub fn to_worker_protos(&self) -> Vec<WorkerProto> {
        let now = chrono::Utc::now();
        self.workers
            .values()
            .map(|w| {
                let age = (now - w.last_heartbeat).num_seconds() as f64;
                let available = w.is_available(Self::STALE_TIMEOUT_SECS);
                WorkerProto {
                    id: w.id.clone(),
                    capabilities: w.capabilities.clone(),
                    current_load: w.current_load,
                    max_capacity: w.max_capacity,
                    load_percent: w.load_percent(),
                    last_heartbeat: w.last_heartbeat.to_rfc3339(),
                    heartbeat_age_seconds: age,
                    heartbeat_stale: !available,
                    is_available: available,
                    metadata: w.metadata.clone(),
                }
            })
            .collect()
    }
}

impl Default for WorkerRegistry {
    fn default() -> Self {
        Self::new()
    }
}

/// Normalize a raw `projects` registration list (T8 #650): trim each entry,
/// drop blanks, remove duplicates preserving first-seen order.
fn normalize_projects(raw: Vec<String>) -> Vec<String> {
    let mut out: Vec<String> = Vec::with_capacity(raw.len());
    for p in raw {
        let t = p.trim();
        if !t.is_empty() && !out.iter().any(|e| e == t) {
            out.push(t.to_string());
        }
    }
    out
}

/// Outcome of the capability + scope + contract-version dispatch gate for one
/// subtask (see [`WorkerRegistry::dispatch_gate`]).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WorkerDispatchGate {
    /// At least one available capability-matching worker declared the
    /// gateway's contract version — safe to mark Assigned and publish.
    Dispatch,
    /// `required_capabilities` unmet by any available worker → keep Pending
    /// (pre-T1 behavior).
    NoCapableWorker,
    /// Capability-matching workers are available, but NONE serve the task's
    /// scope (T8 #650): every candidate is scoped to other project_ids and
    /// the task's scope matches none. Keep Pending — a scoped worker must
    /// never receive a foreign-scope node. Carries the rejected candidate ids.
    NoScopeMatchedWorker { workers: Vec<String> },
    /// Capability- and scope-matching workers are available, but NONE declared
    /// [`CONTRACT_VERSION`] (mixed-version cluster: legacy workers with an
    /// empty version, or a version skew that slipped past registration).
    /// Keep Pending and surface it LOUDLY — never dispatch silently.
    /// Carries `(worker_id, declared_version)` of the rejected candidates.
    NoVersionMatchedWorker { workers: Vec<(String, String)> },
}

// ── WorkerService gRPC implementation ─────────────────────────────

#[tonic::async_trait]
impl<E: EngineApi + Send + Sync + 'static> WorkerService for GrpcServer<E> {
    async fn register_worker(
        &self,
        request: Request<RegisterWorkerRequest>,
    ) -> Result<Response<RegisterWorkerResponse>, Status> {
        let req = request.into_inner();
        if req.worker_id.is_empty() {
            return Ok(Response::new(RegisterWorkerResponse {
                success: false,
                worker_id: String::new(),
                error: Some("worker_id cannot be empty".to_string()),
            }));
        }

        let mut registry = self.worker_registry().write().await;
        match registry.register_with_projects(
            req.worker_id.clone(),
            req.capabilities,
            req.max_capacity,
            req.metadata,
            req.contract_version.clone(),
            req.projects,
        ) {
            Ok(()) => {
                if req.contract_version.is_empty() {
                    tracing::warn!(
                        worker_id = %req.worker_id,
                        "Worker registered without contract_version — accepted but NOT \
                         dispatchable (legacy worker; upgrade in lockstep with the gateway)"
                    );
                }
                Ok(Response::new(RegisterWorkerResponse {
                    success: true,
                    worker_id: req.worker_id,
                    error: None,
                }))
            }
            Err(e) => {
                tracing::warn!(worker_id = %req.worker_id, error = %e, "Worker registration refused");
                Ok(Response::new(RegisterWorkerResponse {
                    success: false,
                    worker_id: req.worker_id,
                    error: Some(e),
                }))
            }
        }
    }

    async fn worker_heartbeat(
        &self,
        request: Request<WorkerHeartbeatRequest>,
    ) -> Result<Response<WorkerHeartbeatResponse>, Status> {
        let req = request.into_inner();
        // Handshake is re-asserted on every heartbeat: a non-empty version
        // that no longer matches the gateway means the pair has drifted —
        // refuse loudly instead of keeping a non-dispatchable worker alive.
        if !req.contract_version.is_empty() && req.contract_version != CONTRACT_VERSION {
            let error = format!(
                "contract_version mismatch: worker '{}' declares '{}', gateway speaks '{}' \
                 — gateway and workers must be upgraded in lockstep",
                req.worker_id, req.contract_version, CONTRACT_VERSION
            );
            tracing::warn!(worker_id = %req.worker_id, error = %error, "Worker heartbeat refused");
            return Ok(Response::new(WorkerHeartbeatResponse {
                accepted: false,
                error: Some(error),
            }));
        }
        let mut registry = self.worker_registry().write().await;
        match registry.heartbeat(&req.worker_id, req.current_load) {
            Ok(()) => Ok(Response::new(WorkerHeartbeatResponse {
                accepted: true,
                error: None,
            })),
            Err(e) => Ok(Response::new(WorkerHeartbeatResponse {
                accepted: false,
                error: Some(e),
            })),
        }
    }

    async fn deregister_worker(
        &self,
        request: Request<DeregisterWorkerRequest>,
    ) -> Result<Response<DeregisterWorkerResponse>, Status> {
        let req = request.into_inner();
        let mut registry = self.worker_registry().write().await;
        match registry.deregister(&req.worker_id) {
            Ok(()) => Ok(Response::new(DeregisterWorkerResponse {
                success: true,
                error: None,
            })),
            Err(e) => Ok(Response::new(DeregisterWorkerResponse {
                success: false,
                error: Some(e),
            })),
        }
    }

    async fn scale_workers(
        &self,
        request: Request<ScaleWorkersRequest>,
    ) -> Result<Response<ScaleWorkersResponse>, Status> {
        let req = request.into_inner();
        tracing::info!(
            action = %req.action,
            target_count = req.target_count,
            worker_id = %req.worker_id,
            "ScaleWorkers request received"
        );

        match req.action.as_str() {
            "deregister" => {
                if req.worker_id.is_empty() {
                    return Ok(Response::new(ScaleWorkersResponse {
                        success: false,
                        error: Some("worker_id is required for action='deregister'".to_string()),
                        actual_count: 0,
                        message: String::new(),
                    }));
                }
                let mut registry = self.worker_registry().write().await;
                let worker_id = req.worker_id.clone();
                match registry.deregister(&worker_id) {
                    Ok(()) => {
                        let actual = registry.workers().len() as u32;
                        tracing::info!(worker_id = %worker_id, actual_count = actual, "Worker force-deregistered via ScaleWorkers");
                        Ok(Response::new(ScaleWorkersResponse {
                            success: true,
                            error: None,
                            actual_count: actual,
                            message: format!("Worker '{}' deregistered", worker_id),
                        }))
                    }
                    Err(e) => {
                        let actual = registry.workers().len() as u32;
                        tracing::warn!(worker_id = %worker_id, error = %e, "ScaleWorkers deregister failed");
                        Ok(Response::new(ScaleWorkersResponse {
                            success: false,
                            error: Some(e),
                            actual_count: actual,
                            message: String::new(),
                        }))
                    }
                }
            }
            "scale" => {
                // Shell out to docker compose to set the worker instance
                // count, fanned out over the hosts listed in
                // UC_SCALE_HOSTS (default: the gateway's own daemon).
                let compose_file = std::env::var("UC_COMPOSE_FILE")
                    .unwrap_or_else(|_| "/app/docker/docker-compose.yml".to_string());
                let compose_project =
                    std::env::var("UC_COMPOSE_PROJECT").unwrap_or_else(|_| "docker".to_string());
                let dry_run =
                    scale_dry_run_enabled(std::env::var("UC_SCALE_DRY_RUN").ok().as_deref());

                // Validate compose file exists before shelling out. Skipped
                // in dry-run: the plan must be inspectable on gateways that
                // have no local compose file (e.g. validating UC_SCALE_HOSTS
                // before the real deployment).
                if !dry_run {
                    let compose_path = std::path::Path::new(&compose_file);
                    if !compose_path.exists() {
                        tracing::warn!(compose_file = %compose_file, "Compose file not found");
                        return Ok(Response::new(ScaleWorkersResponse {
                            success: false,
                            error: Some(format!(
                                "Compose file not found: '{}' (set UC_COMPOSE_FILE to the correct path)",
                                compose_file
                            )),
                            actual_count: 0,
                            message: String::new(),
                        }));
                    }
                }

                let target = req.target_count;
                let hosts = parse_scale_hosts(std::env::var("UC_SCALE_HOSTS").ok().as_deref());
                let assignments = split_target_across_hosts(target, &hosts);
                tracing::info!(
                    compose_file = %compose_file,
                    compose_project = %compose_project,
                    target_count = target,
                    dry_run,
                    assignments = ?assignments,
                    "Scaling workers via docker compose"
                );

                // Dry-run: report the exact per-host plan without touching
                // any docker daemon. actual_count reports the PLANNED count.
                if dry_run {
                    let detail = assignments
                        .iter()
                        .map(|(h, s)| format!("{h}={s}"))
                        .collect::<Vec<_>>()
                        .join("; ");
                    tracing::info!(target_count = target, hosts = ?hosts, "DRY-RUN scale plan");
                    return Ok(Response::new(ScaleWorkersResponse {
                        success: true,
                        error: None,
                        actual_count: target,
                        message: format!(
                            "DRY-RUN: would scale {target} worker instance(s) across {} host(s) [{detail}]; no docker invoked (unset UC_SCALE_DRY_RUN to apply)",
                            hosts.len()
                        ),
                    }));
                }

                // Per-host best-effort: one unreachable host must not block
                // scaling of the others. Shares are absolute per host.
                let mut results = Vec::with_capacity(assignments.len());
                for (host, share) in &assignments {
                    let docker_host =
                        (!host.eq_ignore_ascii_case("local")).then_some(host.as_str());
                    let outcome =
                        run_compose_scale(&compose_file, &compose_project, *share, docker_host)
                            .await;
                    results.push((host.clone(), *share, outcome));
                }

                // Workers self-register asynchronously on container start;
                // the registry reconciles via the existing RegisterWorker
                // path. actual_count sums the shares we successfully
                // requested without blocking on registration.
                let actual_count: u32 = results
                    .iter()
                    .filter(|(_, _, r)| r.is_ok())
                    .map(|(_, share, _)| *share)
                    .sum();
                let detail = results
                    .iter()
                    .map(|(h, s, r)| match r {
                        Ok(()) => format!("{}={}", h, s),
                        Err(e) => format!("{}=FAILED({})", h, e),
                    })
                    .collect::<Vec<_>>()
                    .join("; ");
                let failures: Vec<String> = results
                    .iter()
                    .filter_map(|(_, _, r)| r.as_ref().err().cloned())
                    .collect();

                if failures.is_empty() {
                    tracing::info!(target_count = target, hosts = ?hosts, "docker compose scale succeeded");
                    Ok(Response::new(ScaleWorkersResponse {
                        success: true,
                        error: None,
                        actual_count,
                        message: format!(
                            "Scaled worker instances across {} host(s) [{}]; workers self-register asynchronously",
                            hosts.len(),
                            detail
                        ),
                    }))
                } else {
                    tracing::warn!(
                        failures = failures.len(),
                        total_hosts = hosts.len(),
                        "docker compose scale partially failed"
                    );
                    Ok(Response::new(ScaleWorkersResponse {
                        success: false,
                        error: Some(failures.join("; ")),
                        actual_count,
                        message: format!("Partial scale [{}]", detail),
                    }))
                }
            }
            other => {
                tracing::warn!(action = other, "Unknown ScaleWorkers action");
                Ok(Response::new(ScaleWorkersResponse {
                    success: false,
                    error: Some(format!(
                        "Unknown action: '{}' (expected 'scale' or 'deregister')",
                        other
                    )),
                    actual_count: 0,
                    message: String::new(),
                }))
            }
        }
    }
}

// ── Cross-host scale helpers ─────────────────────────────────────

/// Parse `UC_SCALE_HOSTS` into per-host docker connection specs.
///
/// Comma/semicolon-separated list. The special value `local` (case-
/// insensitive) means "the daemon this gateway itself reaches" — no
/// `DOCKER_HOST` override, i.e. today's single-host behavior. Any other
/// entry is passed to `docker compose` as `DOCKER_HOST` for that
/// invocation, so plain `ssh://user@host` specs work without pre-created
/// docker contexts. Unset/empty/blank → `["local"]`.
fn parse_scale_hosts(raw: Option<&str>) -> Vec<String> {
    let hosts: Vec<String> = raw
        .unwrap_or("")
        .split([',', ';'])
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(String::from)
        .collect();
    if hosts.is_empty() {
        vec!["local".to_string()]
    } else {
        hosts
    }
}

/// Even-split of a target worker count across hosts, preserving input
/// order: the first `target % hosts.len()` hosts take one extra worker.
fn split_target_across_hosts(target: u32, hosts: &[String]) -> Vec<(String, u32)> {
    let n = hosts.len().max(1);
    let base = target / n as u32;
    let rem = target % n as u32;
    hosts
        .iter()
        .enumerate()
        .map(|(i, h)| (h.clone(), base + u32::from(i < rem as usize)))
        .collect()
}

/// Truthy check for `UC_SCALE_DRY_RUN`. Accepts "1", "true", "yes", "on"
/// case-insensitively (trimmed); anything else — including unset/empty —
/// means the scale action executes for real.
fn scale_dry_run_enabled(raw: Option<&str>) -> bool {
    matches!(
        raw.map(str::trim).map(str::to_ascii_lowercase),
        Some(ref v) if matches!(v.as_str(), "1" | "true" | "yes" | "on")
    )
}

/// Run one `docker compose up -d --no-deps --scale worker=N worker`
/// invocation against `docker_host` (`None` = the gateway's own daemon).
/// `--no-deps` is MANDATORY: worker depends_on gateway, and the gateway
/// itself is issuing this command (would deadlock without it).
async fn run_compose_scale(
    compose_file: &str,
    compose_project: &str,
    count: u32,
    docker_host: Option<&str>,
) -> Result<(), String> {
    let label = docker_host.unwrap_or("local");
    let mut cmd = tokio::process::Command::new("docker");
    cmd.arg("compose")
        .arg("-p")
        .arg(compose_project)
        .arg("-f")
        .arg(compose_file)
        .arg("up")
        .arg("-d")
        .arg("--no-deps")
        .arg("--scale")
        .arg(format!("worker={}", count))
        .arg("worker");
    if let Some(dh) = docker_host {
        cmd.env("DOCKER_HOST", dh);
    }

    match cmd.output().await {
        Ok(output) if output.status.success() => {
            tracing::info!(host = %label, count, "docker compose scale succeeded");
            Ok(())
        }
        Ok(output) => {
            let stderr = String::from_utf8_lossy(&output.stderr);
            let stdout = String::from_utf8_lossy(&output.stdout);
            let snippet = if !stderr.trim().is_empty() {
                stderr.lines().take(5).collect::<Vec<_>>().join("; ")
            } else {
                stdout.lines().take(5).collect::<Vec<_>>().join("; ")
            };
            Err(format!(
                "{}: docker compose scale failed (exit {:?}): {}",
                label,
                output.status.code(),
                snippet
            ))
        }
        Err(e) => Err(format!(
            "{}: Failed to invoke docker CLI: {} (ensure docker is installed and reachable from this host)",
            label, e
        )),
    }
}

// ── Tests ─────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registry_register_and_lookup() {
        let mut reg = WorkerRegistry::new();
        reg.register(
            "w-1".to_string(),
            vec!["python".to_string(), "docker".to_string()],
            5,
            String::new(),
            String::new(),
        )
        .unwrap();
        assert!(reg.workers().contains_key("w-1"));
        let w = &reg.workers()["w-1"];
        assert_eq!(w.capabilities, vec!["python", "docker"]);
        assert_eq!(w.max_capacity, 5);
    }

    #[test]
    fn registry_rejects_empty_id() {
        let mut reg = WorkerRegistry::new();
        assert!(reg
            .register(String::new(), vec![], 1, String::new(), String::new())
            .is_err());
    }

    #[test]
    fn registry_heartbeat_updates_load() {
        let mut reg = WorkerRegistry::new();
        reg.register(
            "w-1".to_string(),
            vec!["code".to_string()],
            3,
            String::new(),
            String::new(),
        )
        .unwrap();
        reg.heartbeat("w-1", 2).unwrap();
        assert_eq!(reg.workers()["w-1"].current_load, 2);
    }

    // Regression: is_available used to check only heartbeat staleness, so a
    // worker at full load (or max_capacity==0) was "available" and got
    // over-assigned. Now it also requires current_load < max_capacity.
    #[test]
    fn is_available_excludes_saturated_and_zero_capacity_workers() {
        let mut reg = WorkerRegistry::new();
        reg.register(
            "full".to_string(),
            vec!["code".to_string()],
            2, // max_capacity
            String::new(),
            String::new(),
        )
        .unwrap();
        reg.heartbeat("full", 2).unwrap(); // load == capacity → saturated
        assert!(
            !reg.workers()["full"].is_available(WorkerRegistry::STALE_TIMEOUT_SECS),
            "saturated worker must not be available"
        );

        reg.register(
            "zero".to_string(),
            vec!["code".to_string()],
            0, // max_capacity == 0 → can never take work
            String::new(),
            String::new(),
        )
        .unwrap();
        reg.heartbeat("zero", 0).unwrap();
        assert!(
            !reg.workers()["zero"].is_available(WorkerRegistry::STALE_TIMEOUT_SECS),
            "zero-capacity worker must not be available"
        );

        // A worker with spare capacity is available.
        reg.register(
            "spare".to_string(),
            vec!["code".to_string()],
            3,
            String::new(),
            String::new(),
        )
        .unwrap();
        reg.heartbeat("spare", 1).unwrap();
        assert!(
            reg.workers()["spare"].is_available(WorkerRegistry::STALE_TIMEOUT_SECS),
            "worker with spare capacity should be available"
        );

        // available_workers / workers_with_capabilities reflect the filter.
        let avail: Vec<&str> = reg
            .available_workers()
            .iter()
            .map(|w| w.id.as_str())
            .collect();
        assert!(avail.contains(&"spare"));
        assert!(!avail.contains(&"full"));
        assert!(!avail.contains(&"zero"));
    }

    #[test]
    fn registry_heartbeat_unknown_worker() {
        let mut reg = WorkerRegistry::new();
        assert!(reg.heartbeat("unknown", 0).is_err());
    }

    #[test]
    fn registry_deregister() {
        let mut reg = WorkerRegistry::new();
        reg.register("w-1".to_string(), vec![], 1, String::new(), String::new())
            .unwrap();
        reg.deregister("w-1").unwrap();
        assert!(!reg.workers().contains_key("w-1"));
    }

    #[test]
    fn registry_deregister_unknown() {
        let mut reg = WorkerRegistry::new();
        assert!(reg.deregister("unknown").is_err());
    }

    #[test]
    fn registry_workers_with_capabilities() {
        let mut reg = WorkerRegistry::new();
        reg.register(
            "w-rust".to_string(),
            vec!["rust".to_string(), "docker".to_string()],
            3,
            String::new(),
            String::new(),
        )
        .unwrap();
        reg.register(
            "w-python".to_string(),
            vec!["python".to_string()],
            3,
            String::new(),
            String::new(),
        )
        .unwrap();
        reg.register(
            "w-both".to_string(),
            vec!["rust".to_string(), "python".to_string()],
            3,
            String::new(),
            String::new(),
        )
        .unwrap();

        let rust_workers = reg.workers_with_capabilities(&["rust".to_string()]);
        assert_eq!(rust_workers.len(), 2);

        let both = reg.workers_with_capabilities(&["rust".to_string(), "python".to_string()]);
        assert_eq!(both.len(), 1);
        assert_eq!(both[0].id, "w-both");
    }

    #[test]
    fn registry_to_worker_protos() {
        let mut reg = WorkerRegistry::new();
        reg.register(
            "w-1".to_string(),
            vec!["code".to_string()],
            3,
            String::new(),
            String::new(),
        )
        .unwrap();
        let protos = reg.to_worker_protos();
        assert_eq!(protos.len(), 1);
        assert_eq!(protos[0].id, "w-1");
        assert_eq!(protos[0].capabilities, vec!["code"]);
        assert!(protos[0].is_available);
    }

    #[test]
    fn registry_proto_carries_registration_metadata() {
        // Cross-host observability: whatever the worker sent in
        // RegisterWorkerRequest.metadata must round-trip into
        // WorkerProto.metadata for ListWorkers consumers.
        let mut reg = WorkerRegistry::new();
        reg.register(
            "host-b-w1".to_string(),
            vec!["code".to_string()],
            3,
            r#"{"hostname":"host-b","pid":42}"#.to_string(),
            String::new(),
        )
        .unwrap();
        reg.register(
            "local-w2".to_string(),
            vec!["code".to_string()],
            3,
            String::new(),
            String::new(),
        )
        .unwrap();

        let protos = reg.to_worker_protos();
        let by_id = |id: &str| protos.iter().find(|p| p.id == id).unwrap();
        assert_eq!(
            by_id("host-b-w1").metadata,
            r#"{"hostname":"host-b","pid":42}"#
        );
        assert_eq!(by_id("local-w2").metadata, "");
    }

    #[test]
    fn registry_reregister_resets_state() {
        let mut reg = WorkerRegistry::new();
        reg.register(
            "w-1".to_string(),
            vec!["python".to_string()],
            3,
            String::new(),
            String::new(),
        )
        .unwrap();
        reg.heartbeat("w-1", 2).unwrap();
        // Re-register with new capabilities
        reg.register(
            "w-1".to_string(),
            vec!["rust".to_string()],
            5,
            String::new(),
            String::new(),
        )
        .unwrap();
        let w = &reg.workers()["w-1"];
        assert_eq!(w.capabilities, vec!["rust"]);
        assert_eq!(w.max_capacity, 5);
        assert_eq!(w.current_load, 0); // reset on re-register
    }

    // ── contract_version handshake (T1 #637) ─────────────────────

    #[test]
    fn registry_refuses_mismatched_contract_version() {
        let mut reg = WorkerRegistry::new();
        let err = reg
            .register(
                "w-old".to_string(),
                vec!["code".to_string()],
                3,
                String::new(),
                "v0".to_string(),
            )
            .unwrap_err();
        assert!(
            err.contains("contract_version mismatch") && err.contains("v0"),
            "error must name the offending version: {err}"
        );
        assert!(
            !reg.workers().contains_key("w-old"),
            "refused worker must not enter the registry"
        );
    }

    #[test]
    fn registry_accepts_matching_and_empty_contract_version() {
        let mut reg = WorkerRegistry::new();
        reg.register(
            "w-new".to_string(),
            vec!["code".to_string()],
            3,
            String::new(),
            CONTRACT_VERSION.to_string(),
        )
        .unwrap();
        reg.register(
            "w-legacy".to_string(),
            vec!["code".to_string()],
            3,
            String::new(),
            String::new(), // empty = legacy pre-handshake worker
        )
        .unwrap();
        assert_eq!(reg.workers()["w-new"].contract_version, CONTRACT_VERSION);
        assert_eq!(reg.workers()["w-legacy"].contract_version, "");
    }

    #[test]
    fn dispatch_gate_only_admits_version_matched_workers() {
        let mut reg = WorkerRegistry::new();
        let caps = |s: &str| vec![s.to_string()];

        // Empty registry, no capabilities → today's best-effort publish
        // (NATS-only deployments without gRPC registration).
        assert_eq!(reg.dispatch_gate(&[], ""), WorkerDispatchGate::Dispatch);
        // Empty registry, capabilities required → keep Pending (unchanged).
        assert_eq!(
            reg.dispatch_gate(&caps("rust"), ""),
            WorkerDispatchGate::NoCapableWorker
        );

        // Legacy worker (empty version) is registered but NOT dispatchable.
        reg.register(
            "w-legacy".to_string(),
            caps("rust"),
            3,
            String::new(),
            String::new(),
        )
        .unwrap();
        assert_eq!(
            reg.dispatch_gate(&caps("rust"), ""),
            WorkerDispatchGate::NoVersionMatchedWorker {
                workers: vec![("w-legacy".to_string(), String::new())]
            }
        );
        // Capability requirement still short-circuits before the version gate.
        assert_eq!(
            reg.dispatch_gate(&caps("docker"), ""),
            WorkerDispatchGate::NoCapableWorker
        );

        // A version-matched worker flips the gate back to Dispatch, even
        // while the legacy worker remains registered.
        reg.register(
            "w-new".to_string(),
            caps("rust"),
            3,
            String::new(),
            CONTRACT_VERSION.to_string(),
        )
        .unwrap();
        assert_eq!(
            reg.dispatch_gate(&caps("rust"), ""),
            WorkerDispatchGate::Dispatch
        );
        assert_eq!(reg.dispatch_gate(&[], ""), WorkerDispatchGate::Dispatch);

        // dispatchable_workers_with_capabilities excludes the legacy worker.
        let ids: Vec<_> = reg
            .dispatchable_workers_with_capabilities(&caps("rust"))
            .iter()
            .map(|w| w.id.as_str())
            .collect();
        assert_eq!(ids, vec!["w-new"]);
    }

    #[tokio::test]
    async fn register_worker_rpc_refuses_mismatched_version() {
        let server = make_server();
        let resp = server
            .register_worker(Request::new(RegisterWorkerRequest {
                worker_id: "w-skew".to_string(),
                capabilities: vec!["code".to_string()],
                max_capacity: 2,
                metadata: String::new(),
                contract_version: "v2".to_string(),
                projects: vec![],
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(!resp.success);
        let err = resp.error.unwrap();
        assert!(err.contains("contract_version mismatch") && err.contains("v2"));
        assert!(!server
            .worker_registry()
            .read()
            .await
            .workers()
            .contains_key("w-skew"));
    }

    #[tokio::test]
    async fn register_worker_rpc_accepts_matched_and_empty_versions() {
        let server = make_server();
        let matched = server
            .register_worker(Request::new(RegisterWorkerRequest {
                worker_id: "w-matched".to_string(),
                capabilities: vec![],
                max_capacity: 1,
                metadata: String::new(),
                contract_version: CONTRACT_VERSION.to_string(),
                projects: vec![],
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(matched.success, "{:?}", matched.error);

        let legacy = server
            .register_worker(Request::new(RegisterWorkerRequest {
                worker_id: "w-legacy".to_string(),
                capabilities: vec![],
                max_capacity: 1,
                metadata: String::new(),
                contract_version: String::new(),
                projects: vec![],
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(
            legacy.success,
            "empty version must stay accepted: {:?}",
            legacy.error
        );
        let reg = server.worker_registry().read().await;
        assert_eq!(reg.workers()["w-legacy"].contract_version, "");
    }

    #[tokio::test]
    async fn heartbeat_rpc_refuses_mismatched_version_accepted_matched() {
        let server = make_server();
        server
            .register_worker(Request::new(RegisterWorkerRequest {
                worker_id: "w-hb".to_string(),
                capabilities: vec![],
                max_capacity: 2,
                metadata: String::new(),
                contract_version: CONTRACT_VERSION.to_string(),
                projects: vec![],
            }))
            .await
            .unwrap();

        // Mismatched non-empty version → refused loudly.
        let refused = server
            .worker_heartbeat(Request::new(WorkerHeartbeatRequest {
                worker_id: "w-hb".to_string(),
                current_load: 1,
                contract_version: "v9".to_string(),
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(!refused.accepted);
        assert!(refused.error.unwrap().contains("contract_version mismatch"));

        // Matching version → accepted.
        let ok = server
            .worker_heartbeat(Request::new(WorkerHeartbeatRequest {
                worker_id: "w-hb".to_string(),
                current_load: 1,
                contract_version: CONTRACT_VERSION.to_string(),
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(ok.accepted, "{:?}", ok.error);
        assert_eq!(
            server.worker_registry().read().await.workers()["w-hb"].current_load,
            1
        );

        // Empty (legacy caller) → accepted, preserves prior behavior.
        let legacy_ok = server
            .worker_heartbeat(Request::new(WorkerHeartbeatRequest {
                worker_id: "w-hb".to_string(),
                current_load: 0,
                contract_version: String::new(),
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(legacy_ok.accepted, "{:?}", legacy_ok.error);
    }

    // ── ScaleWorkers handler tests ──────────────────────────────────

    use crate::server::GrpcServer;
    use tonic::Request;
    use uc_engine::LocalEngine;

    fn make_server() -> GrpcServer<LocalEngine> {
        GrpcServer::new(LocalEngine::new_fallback())
    }

    #[tokio::test]
    async fn scale_workers_deregister_removes_worker() {
        let server = make_server();
        // Seed the registry with a worker via the register RPC.
        server
            .register_worker(Request::new(RegisterWorkerRequest {
                worker_id: "w-scale-1".to_string(),
                capabilities: vec!["python".to_string()],
                max_capacity: 2,
                metadata: String::new(),
                contract_version: String::new(),
                projects: vec![],
            }))
            .await
            .unwrap();

        // Pre-condition: registry has 1 worker.
        {
            let reg = server.worker_registry().read().await;
            assert_eq!(reg.workers().len(), 1);
        }

        // Force-deregister via ScaleWorkers action="deregister".
        let resp = server
            .scale_workers(Request::new(ScaleWorkersRequest {
                action: "deregister".to_string(),
                target_count: 0,
                worker_id: "w-scale-1".to_string(),
            }))
            .await
            .unwrap()
            .into_inner();

        assert!(resp.success, "deregister should succeed: {:?}", resp.error);
        assert_eq!(resp.actual_count, 0);
        assert!(resp.message.contains("w-scale-1"));

        // Registry no longer contains the worker.
        let reg = server.worker_registry().read().await;
        assert!(!reg.workers().contains_key("w-scale-1"));
    }

    #[tokio::test]
    async fn scale_workers_deregister_unknown_worker_fails() {
        let server = make_server();
        let resp = server
            .scale_workers(Request::new(ScaleWorkersRequest {
                action: "deregister".to_string(),
                target_count: 0,
                worker_id: "ghost-worker".to_string(),
            }))
            .await
            .unwrap()
            .into_inner();

        assert!(!resp.success);
        assert!(resp.error.as_ref().unwrap().contains("ghost-worker"));
    }

    #[tokio::test]
    async fn scale_workers_deregister_empty_id_fails() {
        let server = make_server();
        let resp = server
            .scale_workers(Request::new(ScaleWorkersRequest {
                action: "deregister".to_string(),
                target_count: 0,
                worker_id: String::new(),
            }))
            .await
            .unwrap()
            .into_inner();

        assert!(!resp.success);
        assert!(resp.error.as_ref().unwrap().contains("worker_id"));
    }

    #[tokio::test]
    async fn scale_workers_scale_error_paths() {
        // These tests exercise the scale action's error handling WITHOUT a real
        // docker daemon. They are combined into one test to avoid env-var races
        // (UC_COMPOSE_FILE is process-global; parallel tests would contend).

        let server = make_server();

        // ── Case 1: compose file does not exist ──
        std::env::set_var("UC_COMPOSE_FILE", "/nonexistent/uc-test-compose-12345.yml");
        let resp = server
            .scale_workers(Request::new(ScaleWorkersRequest {
                action: "scale".to_string(),
                target_count: 3,
                worker_id: String::new(),
            }))
            .await
            .unwrap()
            .into_inner();

        assert!(
            !resp.success,
            "scale should fail without compose file: {:?}",
            resp.message
        );
        let err = resp.error.unwrap();
        assert!(
            err.contains("Compose file not found"),
            "error should mention missing compose file, got: {}",
            err
        );

        // ── Case 2: compose file exists but is invalid ──
        // Either docker CLI is missing (invoke error) or compose fails parsing.
        // Both must produce success=false without panicking.
        // (/dev/null only "exists" on Unix — on Windows the existence pre-check
        // would fire first and test a different branch. An empty temp file
        // exists everywhere, so all platforms reach the compose invocation.)
        let bad_compose = std::env::temp_dir().join("uc_test_not_a_compose.yml");
        std::fs::write(&bad_compose, b"").expect("write temp compose file");
        std::env::set_var("UC_COMPOSE_FILE", bad_compose.to_str().unwrap());
        let resp = server
            .scale_workers(Request::new(ScaleWorkersRequest {
                action: "scale".to_string(),
                target_count: 1,
                worker_id: String::new(),
            }))
            .await
            .unwrap()
            .into_inner();

        assert!(
            !resp.success,
            "scale with empty compose should not succeed: {:?}",
            resp.message
        );
        let err = resp.error.unwrap();
        assert!(
            err.contains("docker") || err.contains("compose") || err.contains("exit"),
            "error should reference docker/compose, got: {}",
            err
        );

        // ── Case 3: dry-run plans without touching docker ──
        // Even with a NONEXISTENT compose file (which would fail the real
        // path's pre-check), dry-run must return a success plan containing
        // every host with its share, and actual_count == planned target.
        std::env::set_var("UC_COMPOSE_FILE", "/nonexistent/uc-test-compose-12345.yml");
        std::env::set_var("UC_SCALE_DRY_RUN", "true");
        let resp = server
            .scale_workers(Request::new(ScaleWorkersRequest {
                action: "scale".to_string(),
                target_count: 3,
                worker_id: String::new(),
            }))
            .await
            .unwrap()
            .into_inner();

        assert!(
            resp.success,
            "dry-run must succeed despite missing compose file: {:?}",
            resp.error
        );
        assert_eq!(resp.actual_count, 3, "planned count == target in dry-run");
        assert!(
            resp.message.contains("DRY-RUN") && resp.message.contains("local=3"),
            "message should carry the plan, got: {}",
            resp.message
        );
        assert!(
            !resp.message.contains("FAILED"),
            "dry-run must not invoke docker: {:?}",
            resp.message
        );

        // Restore defaults so other tests are unaffected.
        std::env::remove_var("UC_COMPOSE_FILE");
        std::env::remove_var("UC_SCALE_DRY_RUN");
    }

    #[tokio::test]
    async fn scale_workers_unknown_action_fails() {
        let server = make_server();
        let resp = server
            .scale_workers(Request::new(ScaleWorkersRequest {
                action: "bogus".to_string(),
                target_count: 0,
                worker_id: String::new(),
            }))
            .await
            .unwrap()
            .into_inner();

        assert!(!resp.success);
        assert!(resp.error.as_ref().unwrap().contains("Unknown action"));
    }

    // ── Cross-host scale helpers ─────────────────────────────────

    #[test]
    fn parse_scale_hosts_defaults_to_local() {
        assert_eq!(parse_scale_hosts(None), vec!["local".to_string()]);
        assert_eq!(parse_scale_hosts(Some("")), vec!["local".to_string()]);
        assert_eq!(parse_scale_hosts(Some("  ")), vec!["local".to_string()]);
    }

    #[test]
    fn parse_scale_hosts_splits_trims_and_keeps_order() {
        assert_eq!(
            parse_scale_hosts(Some("local, ssh://u@h2 ;ssh://u@h3")),
            vec![
                "local".to_string(),
                "ssh://u@h2".to_string(),
                "ssh://u@h3".to_string()
            ]
        );
        // Semicolon separator and stray separators are tolerated.
        assert_eq!(
            parse_scale_hosts(Some(";ssh://a@b;")),
            vec!["ssh://a@b".to_string()]
        );
    }

    #[test]
    fn scale_dry_run_enabled_variants() {
        for truthy in [
            "1", "true", "TRUE", "True", "yes", "Yes", "on", "ON", "  on  ",
        ] {
            assert!(
                scale_dry_run_enabled(Some(truthy)),
                "expected truthy: {truthy:?}"
            );
        }
        for falsy in [
            None,
            Some(""),
            Some("  "),
            Some("0"),
            Some("false"),
            Some("off"),
            Some("no"),
            Some("garbage"),
        ] {
            assert!(!scale_dry_run_enabled(falsy), "expected falsy: {falsy:?}");
        }
    }

    #[test]
    fn split_target_even_and_remainder() {
        let hosts =
            |names: &[&str]| -> Vec<String> { names.iter().map(|s| s.to_string()).collect() };
        let h3 = hosts(&["local", "h2", "h3"]);
        assert_eq!(
            split_target_across_hosts(10, &h3),
            vec![
                ("local".to_string(), 4),
                ("h2".to_string(), 3),
                ("h3".to_string(), 3)
            ]
        );
        let h2 = hosts(&["local", "h2"]);
        assert_eq!(
            split_target_across_hosts(3, &h2),
            vec![("local".to_string(), 2), ("h2".to_string(), 1)]
        );
    }

    #[test]
    fn split_target_edge_cases() {
        let single = vec!["local".to_string()];
        // Single host keeps today's exact behavior.
        assert_eq!(
            split_target_across_hosts(5, &single),
            vec![("local".to_string(), 5)]
        );
        // Zero target scales every host down to zero.
        let two = vec!["a".to_string(), "b".to_string()];
        assert_eq!(
            split_target_across_hosts(0, &two),
            vec![("a".to_string(), 0), ("b".to_string(), 0)]
        );
    }

    // ── ExecutionScope: worker projects registration (T8 #650 / D8 #645) ──

    #[test]
    fn normalize_projects_trims_drops_blanks_and_dedupes() {
        assert_eq!(normalize_projects(vec![]), Vec::<String>::new());
        assert_eq!(
            normalize_projects(vec!["  alpha ".to_string(), "beta".to_string()]),
            vec!["alpha".to_string(), "beta".to_string()]
        );
        // Blank entries dropped — a scoped worker can never claim "".
        assert_eq!(
            normalize_projects(vec!["".to_string(), "  ".to_string(), "alpha".to_string()]),
            vec!["alpha".to_string()]
        );
        // Duplicates removed, first-seen order kept.
        assert_eq!(
            normalize_projects(vec![
                "b".to_string(),
                "a".to_string(),
                " b ".to_string(),
                "a".to_string()
            ]),
            vec!["b".to_string(), "a".to_string()]
        );
    }

    #[test]
    fn register_with_projects_normalizes_and_serves_scope() {
        let mut reg = WorkerRegistry::new();
        reg.register_with_projects(
            "w-scoped".to_string(),
            vec!["code".to_string()],
            3,
            String::new(),
            String::new(),
            vec![" alpha ".to_string(), "".to_string(), "alpha".to_string()],
        )
        .unwrap();
        let w = &reg.workers()["w-scoped"];
        assert_eq!(w.projects, vec!["alpha".to_string()]);
        assert!(w.serves_scope("alpha"));
        assert!(!w.serves_scope("beta"));
        assert!(
            !w.serves_scope(""),
            "scoped worker never serves empty scope"
        );

        // register() (legacy path) = open worker: serves every scope.
        reg.register(
            "w-open".to_string(),
            vec!["code".to_string()],
            3,
            String::new(),
            String::new(),
        )
        .unwrap();
        let open = &reg.workers()["w-open"];
        assert!(open.projects.is_empty());
        assert!(open.serves_scope(""));
        assert!(open.serves_scope("alpha"));
        assert!(open.serves_scope("anything"));
    }

    #[test]
    fn dispatch_gate_scope_hard_filter() {
        let mut reg = WorkerRegistry::new();
        let caps = |s: &str| vec![s.to_string()];

        // Scoped worker, version-matched, serving only project "alpha".
        reg.register_with_projects(
            "w-alpha".to_string(),
            caps("rust"),
            3,
            String::new(),
            CONTRACT_VERSION.to_string(),
            vec!["alpha".to_string()],
        )
        .unwrap();

        // Same scope → dispatchable.
        assert_eq!(
            reg.dispatch_gate(&caps("rust"), "alpha"),
            WorkerDispatchGate::Dispatch
        );
        // Foreign scope → keep Pending, LOUDLY (scoped worker must never
        // receive a foreign-scope node).
        assert_eq!(
            reg.dispatch_gate(&caps("rust"), "beta"),
            WorkerDispatchGate::NoScopeMatchedWorker {
                workers: vec!["w-alpha".to_string()]
            }
        );
        // Empty scope (legacy task) → only open workers qualify; the scoped
        // worker must not receive it.
        assert_eq!(
            reg.dispatch_gate(&caps("rust"), ""),
            WorkerDispatchGate::NoScopeMatchedWorker {
                workers: vec!["w-alpha".to_string()]
            }
        );

        // An open worker (version-matched) serves every scope, including
        // foreign and empty ones, even while the scoped worker is registered.
        reg.register(
            "w-open".to_string(),
            caps("rust"),
            3,
            String::new(),
            CONTRACT_VERSION.to_string(),
        )
        .unwrap();
        assert_eq!(
            reg.dispatch_gate(&caps("rust"), "beta"),
            WorkerDispatchGate::Dispatch
        );
        assert_eq!(
            reg.dispatch_gate(&caps("rust"), ""),
            WorkerDispatchGate::Dispatch
        );

        // Empty registry + no required capabilities stays best-effort
        // Dispatch (NATS-only deployments) regardless of scope.
        let empty_reg = WorkerRegistry::new();
        assert_eq!(
            empty_reg.dispatch_gate(&[], "alpha"),
            WorkerDispatchGate::Dispatch
        );
    }

    #[test]
    fn dispatch_gate_scope_filters_before_version_gate() {
        let mut reg = WorkerRegistry::new();
        // Legacy (empty contract_version) worker scoped to "alpha".
        reg.register_with_projects(
            "w-legacy-alpha".to_string(),
            vec!["rust".to_string()],
            3,
            String::new(),
            String::new(),
            vec!["alpha".to_string()],
        )
        .unwrap();

        // In-scope: capability + scope pass, version gate fires.
        assert_eq!(
            reg.dispatch_gate(&["rust".to_string()], "alpha"),
            WorkerDispatchGate::NoVersionMatchedWorker {
                workers: vec![("w-legacy-alpha".to_string(), String::new())]
            }
        );
        // Out-of-scope: scope gate fires first (version never consulted).
        assert_eq!(
            reg.dispatch_gate(&["rust".to_string()], "beta"),
            WorkerDispatchGate::NoScopeMatchedWorker {
                workers: vec!["w-legacy-alpha".to_string()]
            }
        );
    }

    #[tokio::test]
    async fn register_worker_rpc_carries_projects() {
        let server = make_server();
        let resp = server
            .register_worker(Request::new(RegisterWorkerRequest {
                worker_id: "w-scope-rpc".to_string(),
                capabilities: vec!["code".to_string()],
                max_capacity: 2,
                metadata: String::new(),
                contract_version: CONTRACT_VERSION.to_string(),
                projects: vec!["alpha".to_string(), "  ".to_string()],
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(resp.success, "{:?}", resp.error);

        let reg = server.worker_registry().read().await;
        let w = &reg.workers()["w-scope-rpc"];
        // Blank entry dropped at the RPC boundary.
        assert_eq!(w.projects, vec!["alpha".to_string()]);
    }
}
