//! WorkerService implementation — Worker registration and lifecycle management.
//!
//! Workers call RegisterWorker on startup, WorkerHeartbeat periodically,
//! and DeregisterWorker on graceful shutdown. The gateway maintains an
//! in-memory WorkerRegistry as the source of truth for worker state.

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicU64, Ordering};

use tonic::{Request, Response, Status};
use uc_types::{EngineApi, Subtask, Task, CONTRACT_VERSION};

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
    /// T19 #670 — rejected-dispatch counters.
    ///
    /// **Two counters, not one.** "No reviewer exists other than the producer"
    /// and "the producer's identity is unknown" are different facts with
    /// different fixes (add a reviewer worker vs. fix the producer's reporting).
    /// Collapsing them would make the second undiagnosable: a node stuck
    /// `PENDING` with no way to tell which one it was — the failure shape this
    /// repo keeps paying for (T12's silent affinity, T17's fake `RUNNING`).
    ///
    /// Bumped inside [`Self::dispatch_gate`], so a rejection can never be
    /// rejected-without-being-counted.
    no_independent_reviewer: AtomicU64,
    producer_identity_unknown: AtomicU64,
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
    /// Files this worker worked on recently, newest first (T12 #654 /
    /// D12 #649). Normalized + bounded by [`normalize_recent_files`]; the
    /// affinity dimension of the placement score. Empty = no signal.
    pub recent_files: Vec<String>,
    /// Whether the worker bound its own durable consumer on
    /// `uc.subtask.execute.w.{worker_id}` (T12 #654). False = legacy worker:
    /// the gateway never targets its per-worker subject, so it is only ever
    /// reached through the shared overflow.
    pub per_worker_topic: bool,
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
            no_independent_reviewer: AtomicU64::new(0),
            producer_identity_unknown: AtomicU64::new(0),
        }
    }

    /// Review dispatches refused because *every* capability-matching worker was
    /// the producer of a node this one depends on (T19 #670). The node stays
    /// `PENDING`.
    pub fn no_independent_reviewer_count(&self) -> u64 {
        self.no_independent_reviewer.load(Ordering::Relaxed)
    }

    /// Review dispatches refused because a dependency's producing worker could
    /// not be determined, so independence could not be *verified* (T19 #670).
    /// Deliberately separate from [`Self::no_independent_reviewer_count`].
    pub fn producer_identity_unknown_count(&self) -> u64 {
        self.producer_identity_unknown.load(Ordering::Relaxed)
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
            // Placement signals arrive on the heartbeat (T12 #654): a worker
            // is never targeted by affinity until it reports both a
            // per-worker topic and its recent files.
            recent_files: Vec::new(),
            per_worker_topic: false,
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
    ///
    /// Legacy shape (no placement signals) — delegates to
    /// [`Self::heartbeat_with_signals`] with empty signals, which preserves
    /// pre-T12 behavior exactly: no recent files, no per-worker topic.
    pub fn heartbeat(&mut self, worker_id: &str, current_load: u32) -> Result<(), String> {
        self.heartbeat_with_signals(worker_id, current_load, &[], false)
    }

    /// Process a heartbeat carrying the placement signals (T12 #654 / D12 #649).
    ///
    /// `recent_files` is normalized and bounded here — the wire is untrusted
    /// (an unbounded list would make every dispatch O(files × candidates)).
    /// `per_worker_topic` is the worker's declaration that it bound its own
    /// durable consumer on `uc.subtask.execute.w.{worker_id}`; it is stored
    /// as-is because only the worker can know.
    pub fn heartbeat_with_signals(
        &mut self,
        worker_id: &str,
        current_load: u32,
        recent_files: &[String],
        per_worker_topic: bool,
    ) -> Result<(), String> {
        let worker = self.workers.get_mut(worker_id).ok_or_else(|| {
            format!(
                "Worker '{}' not registered — call RegisterWorker first",
                worker_id
            )
        })?;
        worker.last_heartbeat = chrono::Utc::now();
        worker.current_load = current_load;
        worker.recent_files = crate::placement::normalize_recent_files(recent_files);
        worker.per_worker_topic = per_worker_topic;
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
    /// Workers whose capabilities satisfy `required`, minus `exclude`.
    ///
    /// This is the **one** place the capability roster is computed (T19 #670):
    /// the dispatch gate, the candidate list and affinity scoring all descend
    /// from it, which is what makes the T12 invariant — *"scoring must never see
    /// a worker the gate would reject"* ([`Self::placement_target`]) — hold
    /// automatically once the exclusion lands here rather than in one mouth.
    ///
    /// `exclude` is empty for every non-review node, so non-review traffic is
    /// byte-identical to before.
    pub fn workers_with_capabilities_excluding(
        &self,
        required: &[String],
        exclude: &HashSet<String>,
    ) -> Vec<&RegisteredWorker> {
        let required_set: HashSet<_> = required.iter().collect();
        self.available_workers()
            .into_iter()
            .filter(|w| !exclude.contains(&w.id))
            .filter(|w| {
                let worker_caps: HashSet<_> = w.capabilities.iter().collect();
                required_set.is_subset(&worker_caps)
            })
            .collect()
    }

    /// [`Self::workers_with_capabilities_excluding`] with **no exclusion**.
    ///
    /// Kept for the capability-only callers (the test-only
    /// `dispatchable_workers_with_capabilities`). ⚠️ It is deliberately **not**
    /// a dispatch mouth: both mouths ([`Self::dispatch_gate`] and
    /// [`Self::dispatch_candidates`]) take the exclusion as a required
    /// parameter, so no dispatch path can drop a review node's exclusion by
    /// choosing the shorter call.
    pub fn workers_with_capabilities(&self, required: &[String]) -> Vec<&RegisteredWorker> {
        self.workers_with_capabilities_excluding(required, &HashSet::new())
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
    pub fn dispatch_gate(
        &self,
        required: &[String],
        project_id: &str,
        independence: &ReviewIndependence,
    ) -> WorkerDispatchGate {
        // Pre-exclusion roster: answers "does *anyone* hold the required
        // capability", which is what `NoCapableWorker` means. Kept separate from
        // the exclusion-filtered list below so that "nobody has the capability"
        // and "the only holders were the producers" stay distinguishable — the
        // two counters depend on that distinction.
        let capable = self.workers_with_capabilities(required);
        if capable.is_empty() {
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
        // ── T19 #670 / D16 #669 ruling A: independence ──────────────
        // A review node must not be claimed by whoever produced the node it
        // depends on. Two fail-closed checks, ordered most-specific-first:
        //
        //   1. the producer could not be identified at all ⇒ independence
        //      cannot be *verified* ⇒ refuse (counted apart from case 2);
        //   2. the producers were identified and they were the only workers
        //      holding the required capability ⇒ no independent reviewer.
        //
        // Both leave the node PENDING, which is visible on an existing surface.
        // The alternative — dispatching anyway — is silent self-review: the
        // verdict lands in the wire as an ordinary `SubtaskReview` and nothing
        // downstream can tell it apart. Neither check can fire for a non-review
        // node, because `review_independence` returns an empty constraint for
        // everything that `uc_engine::requires_independence` does not flag.
        if !independence.unknown_producers.is_empty() {
            self.producer_identity_unknown
                .fetch_add(1, Ordering::Relaxed);
            return WorkerDispatchGate::ProducerIdentityUnknown {
                dependencies: independence.unknown_producers.clone(),
            };
        }
        let candidates =
            self.workers_with_capabilities_excluding(required, &independence.excluded_producers);
        if candidates.is_empty() {
            self.no_independent_reviewer.fetch_add(1, Ordering::Relaxed);
            let mut producers: Vec<String> =
                independence.excluded_producers.iter().cloned().collect();
            // Sorted so the verdict (and the log line) is deterministic — a
            // HashSet's iteration order is not.
            producers.sort();
            return WorkerDispatchGate::NoIndependentReviewer { producers };
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

    /// Workers eligible to receive this node: capability match (hard) →
    /// scope hard filter (T8 #650) → contract-version check (T1 #637).
    ///
    /// Empty means the hard gate denies dispatch — the same three filters
    /// [`Self::dispatch_gate`] classifies, exposed as a list because affinity
    /// scoring (T12 #654) needs the candidates themselves, not just the
    /// verdict. Scoring must never see a worker the gate would reject.
    pub fn dispatch_candidates(
        &self,
        required: &[String],
        project_id: &str,
        exclude: &HashSet<String>,
    ) -> Vec<&RegisteredWorker> {
        self.workers_with_capabilities_excluding(required, exclude)
            .into_iter()
            .filter(|w| w.serves_scope(project_id))
            .filter(|w| w.contract_version == CONTRACT_VERSION)
            .collect()
    }

    /// Host a worker registered on — the stable `hostname` key of its
    /// registration metadata (#607). `None` when unknown.
    pub fn worker_host(&self, worker_id: &str) -> Option<String> {
        self.workers
            .get(worker_id)
            .and_then(|w| crate::placement::host_from_metadata(&w.metadata))
    }

    /// Affinity placement target for one node (T12 #654 / D12 #649).
    ///
    /// Candidates pass the same hard gates as [`Self::dispatch_gate`], plus a
    /// **per-worker-topic requirement**: a worker that never declared its own
    /// durable consumer is never targeted, because a targeted publish it
    /// cannot consume would strand the node until redelivery. A legacy worker
    /// therefore keeps receiving work through the shared overflow.
    ///
    /// `None` (no candidate clears the affinity threshold, or none declared a
    /// per-worker topic) means "publish to the shared subject" — placement is
    /// a soft preference, never a gate.
    pub fn placement_target(
        &self,
        required: &[String],
        project_id: &str,
        file_constraints: &[String],
        sibling_hosts: &HashSet<String>,
        exclude: &HashSet<String>,
    ) -> Option<crate::placement::Placement> {
        let candidates: Vec<crate::placement::PlacementCandidate<'_>> = self
            .dispatch_candidates(required, project_id, exclude)
            .into_iter()
            .filter(|w| w.per_worker_topic)
            .map(|w| crate::placement::PlacementCandidate {
                worker_id: w.id.as_str(),
                metadata: w.metadata.as_str(),
                recent_files: w.recent_files.as_slice(),
                current_load: w.current_load,
                max_capacity: w.max_capacity,
            })
            .collect();
        crate::placement::place(&candidates, file_constraints, sibling_hosts)
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
    /// T19 #670 — a review node whose dependencies' producers could not all be
    /// identified, so independence cannot be *verified*. Keep Pending:
    /// dispatching anyway would silently permit self-review. Carries the
    /// dependency ids whose producer is unknown.
    ProducerIdentityUnknown { dependencies: Vec<String> },
    /// T19 #670 — every capability-matching worker for this review node produced
    /// a node it depends on, so no *independent* reviewer exists. Keep Pending.
    /// Carries the excluded producer ids (sorted, for a deterministic verdict).
    NoIndependentReviewer { producers: Vec<String> },
}

/// T19 #670 — what a node's dependency structure says about who must **not**
/// execute it. Empty for every non-review node, which is what keeps this change
/// additive for ordinary traffic.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ReviewIndependence {
    /// Workers that produced the nodes this subtask depends on. Never dispatch
    /// candidates for it.
    pub excluded_producers: HashSet<String>,
    /// Dependencies whose producing worker is unknown (or whose row is missing
    /// from the snapshot). Independence cannot be verified ⇒ fail closed.
    pub unknown_producers: Vec<String>,
}

impl ReviewIndependence {
    /// True when nothing constrains this node's dispatch: every node that is not
    /// a review node, plus a review node with no dependencies at all (which has
    /// nothing to review — a producer-side modelling bug, out of T19's scope).
    pub fn is_unconstrained(&self) -> bool {
        self.excluded_producers.is_empty() && self.unknown_producers.is_empty()
    }
}

/// Derive the review-independence constraint for `subtask` (T19 #670).
///
/// Whether the constraint applies at all is decided by
/// [`uc_engine::requires_independence`] — the **same single condition** behind
/// the `review` node label ([`uc_engine::graph_store::node_type_for`]). Do not
/// re-derive it here (`if node_type == "review"` would be that rule written a
/// second time).
///
/// `task` is the snapshot the caller already holds. `None` (no snapshot) and a
/// dependency absent from it both count as an **unknown producer** ⇒ fail
/// closed, because "we cannot tell who produced it" must never degrade into
/// "anyone may review it".
///
/// The producing worker is read from `Subtask::assigned_worker`, which the
/// successful attempt re-establishes on its own status update
/// (`server.rs`, the `subtask_update.assigned_worker` branch). A review node is
/// only ever dispatched after its dependency reached `SUCCEEDED`, so that value
/// is present for any node that actually ran — the failure paths that clear the
/// field only affect the *retrying* window, in which no review can be READY yet.
/// Graphs that never reported a producer (PG backfill / `.uc/tasks` import) are
/// the real source of `unknown_producers`, which is why that case is counted
/// separately rather than folded into `no_independent_reviewer`.
pub fn review_independence(subtask: &Subtask, task: Option<&Task>) -> ReviewIndependence {
    let mut out = ReviewIndependence::default();
    if !uc_engine::requires_independence(&subtask.required_capabilities) {
        return out;
    }
    for dep in &subtask.depends_on {
        let producer = task
            .and_then(|t| t.subtasks.iter().find(|s| s.id == *dep))
            .and_then(|s| s.assigned_worker.as_ref())
            .map(|w| w.0.clone());
        match producer {
            Some(id) => {
                out.excluded_producers.insert(id);
            }
            None => out.unknown_producers.push(dep.0.clone()),
        }
    }
    out
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
        // T12 #654 / D12 #649: the heartbeat carries the placement signals —
        // a bounded recent-files summary (affinity input) and the worker's
        // per-worker-topic declaration. Legacy workers simply omit both.
        match registry.heartbeat_with_signals(
            &req.worker_id,
            req.current_load,
            &req.recent_files,
            req.per_worker_topic,
        ) {
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
        assert_eq!(
            reg.dispatch_gate(
                &[],
                "",
                &crate::worker_service::ReviewIndependence::default()
            ),
            WorkerDispatchGate::Dispatch
        );
        // Empty registry, capabilities required → keep Pending (unchanged).
        assert_eq!(
            reg.dispatch_gate(
                &caps("rust"),
                "",
                &crate::worker_service::ReviewIndependence::default()
            ),
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
            reg.dispatch_gate(
                &caps("rust"),
                "",
                &crate::worker_service::ReviewIndependence::default()
            ),
            WorkerDispatchGate::NoVersionMatchedWorker {
                workers: vec![("w-legacy".to_string(), String::new())]
            }
        );
        // Capability requirement still short-circuits before the version gate.
        assert_eq!(
            reg.dispatch_gate(
                &caps("docker"),
                "",
                &crate::worker_service::ReviewIndependence::default()
            ),
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
            reg.dispatch_gate(
                &caps("rust"),
                "",
                &crate::worker_service::ReviewIndependence::default()
            ),
            WorkerDispatchGate::Dispatch
        );
        assert_eq!(
            reg.dispatch_gate(
                &[],
                "",
                &crate::worker_service::ReviewIndependence::default()
            ),
            WorkerDispatchGate::Dispatch
        );

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
                recent_files: vec![],
                per_worker_topic: false,
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
                recent_files: vec![],
                per_worker_topic: false,
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
                recent_files: vec![],
                per_worker_topic: false,
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
            reg.dispatch_gate(
                &caps("rust"),
                "alpha",
                &crate::worker_service::ReviewIndependence::default()
            ),
            WorkerDispatchGate::Dispatch
        );
        // Foreign scope → keep Pending, LOUDLY (scoped worker must never
        // receive a foreign-scope node).
        assert_eq!(
            reg.dispatch_gate(
                &caps("rust"),
                "beta",
                &crate::worker_service::ReviewIndependence::default()
            ),
            WorkerDispatchGate::NoScopeMatchedWorker {
                workers: vec!["w-alpha".to_string()]
            }
        );
        // Empty scope (legacy task) → only open workers qualify; the scoped
        // worker must not receive it.
        assert_eq!(
            reg.dispatch_gate(
                &caps("rust"),
                "",
                &crate::worker_service::ReviewIndependence::default()
            ),
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
            reg.dispatch_gate(
                &caps("rust"),
                "beta",
                &crate::worker_service::ReviewIndependence::default()
            ),
            WorkerDispatchGate::Dispatch
        );
        assert_eq!(
            reg.dispatch_gate(
                &caps("rust"),
                "",
                &crate::worker_service::ReviewIndependence::default()
            ),
            WorkerDispatchGate::Dispatch
        );

        // Empty registry + no required capabilities stays best-effort
        // Dispatch (NATS-only deployments) regardless of scope.
        let empty_reg = WorkerRegistry::new();
        assert_eq!(
            empty_reg.dispatch_gate(
                &[],
                "alpha",
                &crate::worker_service::ReviewIndependence::default()
            ),
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
            reg.dispatch_gate(
                &["rust".to_string()],
                "alpha",
                &crate::worker_service::ReviewIndependence::default()
            ),
            WorkerDispatchGate::NoVersionMatchedWorker {
                workers: vec![("w-legacy-alpha".to_string(), String::new())]
            }
        );
        // Out-of-scope: scope gate fires first (version never consulted).
        assert_eq!(
            reg.dispatch_gate(
                &["rust".to_string()],
                "beta",
                &crate::worker_service::ReviewIndependence::default()
            ),
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

    // ── T12 #654 / D12 #649 — affinity placement signals ────────────

    /// Register a worker and immediately give it placement signals.
    fn signalled(
        reg: &mut WorkerRegistry,
        id: &str,
        host: &str,
        load: u32,
        capacity: u32,
        recent: &[&str],
        topic: bool,
    ) {
        reg.register(
            id.to_string(),
            vec!["code".to_string()],
            capacity,
            format!(r#"{{"hostname":"{host}"}}"#),
            CONTRACT_VERSION.to_string(),
        )
        .unwrap();
        let files: Vec<String> = recent.iter().map(|s| s.to_string()).collect();
        reg.heartbeat_with_signals(id, load, &files, topic).unwrap();
    }

    fn no_hosts() -> std::collections::HashSet<String> {
        std::collections::HashSet::new()
    }

    #[test]
    fn heartbeat_with_signals_bounds_and_normalizes_recent_files() {
        let mut reg = WorkerRegistry::new();
        reg.register(
            "w-sig".to_string(),
            vec!["code".to_string()],
            4,
            r#"{"hostname":"box-a"}"#.to_string(),
            CONTRACT_VERSION.to_string(),
        )
        .unwrap();
        // Blank + duplicate entries, plus far more files than the bound: the
        // registry normalizes and truncates, because the wire is untrusted.
        let mut files = vec![
            "src/a.rs".to_string(),
            "  ".to_string(),
            "src/a.rs".to_string(),
        ];
        for i in 0..(crate::placement::MAX_RECENT_FILES + 10) {
            files.push(format!("src/f{i}.rs"));
        }
        reg.heartbeat_with_signals("w-sig", 2, &files, true)
            .unwrap();

        let w = &reg.workers()["w-sig"];
        assert_eq!(w.current_load, 2);
        assert!(w.per_worker_topic);
        assert_eq!(w.recent_files.len(), crate::placement::MAX_RECENT_FILES);
        assert_eq!(w.recent_files[0], "src/a.rs");
        assert_eq!(
            w.recent_files
                .iter()
                .filter(|f| f.as_str() == "src/a.rs")
                .count(),
            1,
            "de-duplicated at the boundary"
        );
    }

    #[test]
    fn legacy_heartbeat_clears_placement_signals() {
        let mut reg = WorkerRegistry::new();
        signalled(&mut reg, "w-leg", "box-a", 1, 4, &["src/a.rs"], true);
        assert!(reg.workers()["w-leg"].per_worker_topic);

        // The declaration is per-heartbeat: a worker that stops declaring its
        // topic becomes untargetable again (and falls back to overflow) rather
        // than being grandfathered in as permanently targetable.
        reg.heartbeat("w-leg", 1).unwrap();
        let w = &reg.workers()["w-leg"];
        assert!(w.recent_files.is_empty());
        assert!(!w.per_worker_topic);
    }

    #[test]
    fn placement_target_skips_workers_that_never_declared_a_topic() {
        let mut reg = WorkerRegistry::new();
        // Identical affinity — only the declaration distinguishes them.
        signalled(&mut reg, "w-legacy", "box-a", 0, 4, &["src/a.rs"], false);
        signalled(&mut reg, "w-topic", "box-b", 0, 4, &["src/a.rs"], true);

        let picked = reg
            .placement_target(
                &["code".to_string()],
                "",
                &["src/a.rs".to_string()],
                &no_hosts(),
                &std::collections::HashSet::new(),
            )
            .expect("the declared worker is targetable");
        assert_eq!(picked.worker_id, "w-topic");
        assert_eq!(picked.subject, "uc.subtask.execute.w.w-topic");
    }

    #[test]
    fn placement_target_returns_none_when_no_affinity() {
        let mut reg = WorkerRegistry::new();
        signalled(&mut reg, "w-topic", "box-a", 0, 4, &["src/a.rs"], true);

        // No overlap → None, i.e. publish to the shared subject. Placement is
        // a soft preference; a miss must never strand the node.
        assert!(reg
            .placement_target(
                &["code".to_string()],
                "",
                &["src/unrelated.rs".to_string()],
                &no_hosts(),
                &std::collections::HashSet::new(),
            )
            .is_none());
    }

    #[test]
    fn placement_target_prefers_affinity_then_lower_load() {
        let mut reg = WorkerRegistry::new();
        // Equal affinity, different load → the lighter worker wins.
        signalled(&mut reg, "w-heavy", "box-a", 3, 4, &["src/a.rs"], true);
        signalled(&mut reg, "w-light", "box-b", 1, 4, &["src/a.rs"], true);
        let picked = reg
            .placement_target(
                &["code".to_string()],
                "",
                &["src/a.rs".to_string()],
                &no_hosts(),
                &std::collections::HashSet::new(),
            )
            .unwrap();
        assert_eq!(picked.worker_id, "w-light");

        // More overlapping files outranks load — a heavier but specialised
        // worker still wins on the signal that matters.
        signalled(
            &mut reg,
            "w-overlap",
            "box-c",
            2,
            4,
            &["src/a.rs", "src/b.rs"],
            true,
        );
        let picked = reg
            .placement_target(
                &["code".to_string()],
                "",
                &["src/a.rs".to_string(), "src/b.rs".to_string()],
                &no_hosts(),
                &std::collections::HashSet::new(),
            )
            .unwrap();
        assert_eq!(picked.worker_id, "w-overlap");
        assert_eq!(picked.affinity_hits, 2);
    }

    #[test]
    fn placement_target_respects_the_hard_gate() {
        let mut reg = WorkerRegistry::new();

        // Capability mismatch: an equally affine worker without `code` is
        // invisible to placement, exactly as it is to dispatch_gate.
        reg.register(
            "w-nocap".to_string(),
            vec!["search".to_string()],
            4,
            r#"{"hostname":"box-a"}"#.to_string(),
            CONTRACT_VERSION.to_string(),
        )
        .unwrap();
        reg.heartbeat_with_signals("w-nocap", 0, &["src/a.rs".to_string()], true)
            .unwrap();
        assert!(reg
            .placement_target(
                &["code".to_string()],
                "",
                &["src/a.rs".to_string()],
                &no_hosts(),
                &std::collections::HashSet::new(),
            )
            .is_none());

        // Scope mismatch: a scoped worker only serves its own project, so the
        // same node escapes it for a different scope.
        reg.register_with_projects(
            "w-scope".to_string(),
            vec!["code".to_string()],
            4,
            r#"{"hostname":"box-b"}"#.to_string(),
            CONTRACT_VERSION.to_string(),
            vec!["alpha".to_string()],
        )
        .unwrap();
        reg.heartbeat_with_signals("w-scope", 0, &["src/a.rs".to_string()], true)
            .unwrap();
        assert!(reg
            .placement_target(
                &["code".to_string()],
                "beta",
                &["src/a.rs".to_string()],
                &no_hosts(),
                &std::collections::HashSet::new(),
            )
            .is_none());
        assert!(reg
            .placement_target(
                &["code".to_string()],
                "alpha",
                &["src/a.rs".to_string()],
                &no_hosts(),
                &std::collections::HashSet::new(),
            )
            .is_some());

        // Version mismatch: a legacy pre-handshake worker is never targeted.
        reg.register(
            "w-legacy".to_string(),
            vec!["code".to_string()],
            4,
            r#"{"hostname":"box-c"}"#.to_string(),
            String::new(),
        )
        .unwrap();
        reg.heartbeat_with_signals("w-legacy", 0, &["src/a.rs".to_string()], true)
            .unwrap();
        assert!(reg
            .placement_target(
                &["code".to_string()],
                "",
                &["src/a.rs".to_string()],
                &no_hosts(),
                &std::collections::HashSet::new(),
            )
            .is_none());
    }

    #[test]
    fn placement_target_prefers_a_sibling_workers_host() {
        let mut reg = WorkerRegistry::new();
        // Identical affinity and load → locality decides. This is the
        // "same machine, warm cache" case: prefer where the task already runs.
        signalled(&mut reg, "w-remote", "box-x", 1, 4, &["src/a.rs"], true);
        signalled(&mut reg, "w-local", "box-y", 1, 4, &["src/a.rs"], true);

        let sibling_hosts: std::collections::HashSet<String> =
            ["box-y".to_string()].into_iter().collect();
        let picked = reg
            .placement_target(
                &["code".to_string()],
                "",
                &["src/a.rs".to_string()],
                &sibling_hosts,
                &std::collections::HashSet::new(),
            )
            .unwrap();
        assert_eq!(picked.worker_id, "w-local");
        assert!(picked.same_host);
    }

    #[test]
    fn worker_host_reads_the_stable_metadata_key() {
        let mut reg = WorkerRegistry::new();
        signalled(&mut reg, "w-host", "box-z", 0, 4, &[], false);
        assert_eq!(reg.worker_host("w-host").as_deref(), Some("box-z"));
        // Unknown worker / unparsable metadata degrade to None, never panic.
        assert_eq!(reg.worker_host("nobody"), None);
        reg.register(
            "w-nometa".to_string(),
            vec![],
            1,
            "not json".to_string(),
            CONTRACT_VERSION.to_string(),
        )
        .unwrap();
        assert_eq!(reg.worker_host("w-nometa"), None);
    }

    // ── T19 #670 / D16 #669 ruling A — review independence ──────────
    //
    // The constraint: a node labelled `review` must not be executed by a worker
    // that produced one of its dependencies. These tests drive the gate and the
    // placement scorer directly — independence is a parameter, so no store is
    // needed. The snapshot-reading half (`review_independence` against a live
    // `TaskStore`) lives in `server.rs` tests.

    /// Register a version-matched, unscoped worker holding exactly `caps`.
    fn worker(reg: &mut WorkerRegistry, id: &str, caps: &[&str]) {
        reg.register(
            id.to_string(),
            caps.iter().map(|c| (*c).to_string()).collect(),
            4,
            String::new(),
            CONTRACT_VERSION.to_string(),
        )
        .unwrap();
    }

    /// [`signalled`] with explicit capabilities — the review tests need workers
    /// that hold `review` as well as `code` (`signalled` hardcodes `["code"]`).
    fn signalled_with_caps(
        reg: &mut WorkerRegistry,
        id: &str,
        host: &str,
        load: u32,
        caps: &[&str],
        recent: &[&str],
        topic: bool,
    ) {
        reg.register(
            id.to_string(),
            caps.iter().map(|c| (*c).to_string()).collect(),
            4,
            format!(r#"{{"hostname":"{host}"}}"#),
            CONTRACT_VERSION.to_string(),
        )
        .unwrap();
        let files: Vec<String> = recent.iter().map(|s| s.to_string()).collect();
        reg.heartbeat_with_signals(id, load, &files, topic).unwrap();
    }

    /// A subtask row. `producer` is the worker that ran it (i.e. the value a
    /// successful attempt reports back into `assigned_worker`).
    fn mk_subtask(id: &str, deps: &[&str], caps: &[&str], producer: Option<&str>) -> Subtask {
        Subtask {
            id: uc_types::TaskId(id.into()),
            parent_id: uc_types::TaskId("t-19".into()),
            description: id.into(),
            status: uc_types::SubtaskStatus::Pending,
            assigned_worker: producer.map(|w| uc_types::WorkerId(w.into())),
            depends_on: deps.iter().map(|d| uc_types::TaskId((*d).into())).collect(),
            file_constraints: Vec::new(),
            expected_output: String::new(),
            result: None,
            dispatch_mode: uc_types::DispatchMode::default(),
            effect_class: uc_types::EffectClass::default(),
            dispatch_retry_count: 0,
            required_capabilities: caps.iter().map(|c| (*c).into()).collect(),
            agent_config_json: None,
            steps: Vec::new(),
            retry_count: 0,
        }
    }

    /// The task snapshot `review_independence` reads. Only `subtasks` matters.
    fn mk_task(subtasks: Vec<Subtask>) -> Task {
        let now = chrono::Utc::now();
        Task {
            id: uc_types::TaskId("t-19".into()),
            description: "d".into(),
            project_id: "p1".into(),
            status: uc_types::TaskStatus::InProgress,
            subtasks,
            created_at: now,
            updated_at: now,
        }
    }

    fn set(ids: &[&str]) -> HashSet<String> {
        ids.iter().map(|s| (*s).to_string()).collect()
    }

    #[test]
    fn review_node_is_never_dispatched_to_its_own_producer() {
        // Acceptance (T19 #670): the only worker holding `review` also produced
        // the node under review ⇒ refuse and keep PENDING. Pre-T19 this was a
        // *silent* self-review: the gate saw a capable worker and published.
        let mut reg = WorkerRegistry::new();
        worker(&mut reg, "w-dup", &["code", "review"]);

        let dep = mk_subtask("build", &[], &["code"], Some("w-dup"));
        let review = mk_subtask("review", &["build"], &["code", "review"], None);
        let task = mk_task(vec![dep, review.clone()]);

        let ind = review_independence(&review, Some(&task));
        assert_eq!(ind.excluded_producers, set(&["w-dup"]));
        assert!(ind.unknown_producers.is_empty());
        assert!(!ind.is_unconstrained());

        assert_eq!(
            reg.dispatch_gate(&review.required_capabilities, "p1", &ind),
            WorkerDispatchGate::NoIndependentReviewer {
                producers: vec!["w-dup".to_string()]
            }
        );
        assert_eq!(reg.no_independent_reviewer_count(), 1);
        assert_eq!(reg.producer_identity_unknown_count(), 0);

        // A second reviewer flips the same node back to dispatchable — the
        // refusal is about *this* roster, not about the node.
        worker(&mut reg, "w-other", &["code", "review"]);
        assert_eq!(
            reg.dispatch_gate(&review.required_capabilities, "p1", &ind),
            WorkerDispatchGate::Dispatch
        );
        // Counters count rejections, not state; the earlier one stands.
        assert_eq!(reg.no_independent_reviewer_count(), 1);
    }

    #[test]
    fn placement_never_targets_an_excluded_producer() {
        // Acceptance (T19 #670): the producer is deliberately the *strongest*
        // affinity match (it just touched the very files the review node names)
        // and the lighter-loaded worker. Pre-T19 placement handed the review
        // straight back to the author.
        let mut reg = WorkerRegistry::new();
        signalled_with_caps(
            &mut reg,
            "w-dup",
            "box-a",
            0,
            &["code", "review"],
            &["src/auth.rs", "src/b.rs"],
            true,
        );
        signalled_with_caps(
            &mut reg,
            "w-other",
            "box-b",
            3,
            &["code", "review"],
            &["src/auth.rs"],
            true,
        );

        let review = mk_subtask("review", &["build"], &["code", "review"], None);
        let caps = &review.required_capabilities;
        let files = vec!["src/auth.rs".to_string(), "src/b.rs".to_string()];
        let ind = ReviewIndependence {
            excluded_producers: set(&["w-dup"]),
            unknown_producers: Vec::new(),
        };

        // Differential: with no constraint the producer wins on affinity (2 hits
        // vs 1) — i.e. the exclusion is what changes the outcome, not the roster.
        let unconstrained = reg
            .placement_target(caps, "p1", &files, &no_hosts(), &set(&[]))
            .expect("the producer is the best match");
        assert_eq!(unconstrained.worker_id, "w-dup");
        assert_eq!(unconstrained.affinity_hits, 2);

        // With the constraint the same scoring run can only see the independent
        // reviewer: "scoring must never see a worker the gate would reject"
        // (T12 #654) holds because the exclusion lives in the shared roster.
        assert_eq!(
            reg.dispatch_gate(caps, "p1", &ind),
            WorkerDispatchGate::Dispatch
        );
        let candidates = reg.dispatch_candidates(caps, "p1", &ind.excluded_producers);
        assert_eq!(
            candidates.iter().map(|w| w.id.as_str()).collect::<Vec<_>>(),
            vec!["w-other"]
        );
        let constrained = reg
            .placement_target(caps, "p1", &files, &no_hosts(), &ind.excluded_producers)
            .expect("the reviewer is targetable");
        assert_eq!(constrained.worker_id, "w-other");
        assert_eq!(constrained.affinity_hits, 1);
        assert_eq!(reg.no_independent_reviewer_count(), 0);
    }

    #[test]
    fn unknown_producer_identity_fails_closed_and_is_counted_apart() {
        // Acceptance (T19 #670): the dependency ran, but nothing recorded *who*
        // ran it (PG backfill / `.uc/tasks` import). Independence cannot be
        // *verified*, so dispatch stays refused — "we cannot tell who produced
        // it" must never degrade into "anyone may review it".
        let mut reg = WorkerRegistry::new();
        worker(&mut reg, "w-any", &["code", "review"]);

        let dep = mk_subtask("build", &[], &["code"], None);
        let review = mk_subtask("review", &["build"], &["code", "review"], None);
        let task = mk_task(vec![dep, review.clone()]);

        let ind = review_independence(&review, Some(&task));
        assert!(ind.excluded_producers.is_empty());
        assert_eq!(ind.unknown_producers, vec!["build".to_string()]);
        assert!(!ind.is_unconstrained());

        assert_eq!(
            reg.dispatch_gate(&review.required_capabilities, "p1", &ind),
            WorkerDispatchGate::ProducerIdentityUnknown {
                dependencies: vec!["build".to_string()]
            }
        );
        // The two counters must never be collapsed: a reviewer *does* exist here
        // (w-any); what is missing is the provenance record. Reporting this as
        // `no_independent_reviewer` would send the operator to add a worker that
        // changes nothing.
        assert_eq!(reg.producer_identity_unknown_count(), 1);
        assert_eq!(reg.no_independent_reviewer_count(), 0);
    }

    #[test]
    fn independence_is_inert_for_every_non_review_node() {
        // Acceptance (T19 #670) — the regression guard, and the most important
        // one: a build node whose sole producer is also its only capable worker
        // must still dispatch. If the constraint ever leaked onto non-review
        // nodes, ordinary traffic would *stall* rather than fail loudly, which
        // is the worst possible shape for a regression.
        let mut reg = WorkerRegistry::new();
        worker(&mut reg, "w-dup", &["code"]);

        let dep = mk_subtask("seed", &[], &["code"], Some("w-dup"));
        let build = mk_subtask("build", &["seed"], &["code"], None);
        let task = mk_task(vec![dep, build.clone()]);

        let ind = review_independence(&build, Some(&task));
        assert!(
            ind.is_unconstrained(),
            "no constraint for a non-review node"
        );
        assert_eq!(
            reg.dispatch_gate(&build.required_capabilities, "p1", &ind),
            WorkerDispatchGate::Dispatch
        );
        assert_eq!(reg.no_independent_reviewer_count(), 0);
        assert_eq!(reg.producer_identity_unknown_count(), 0);

        // The same holds when the producer cannot be identified: for a
        // non-review node that is not a reason to refuse.
        let unknown = review_independence(&build, None);
        assert!(unknown.is_unconstrained());
        assert_eq!(
            reg.dispatch_gate(&build.required_capabilities, "p1", &unknown),
            WorkerDispatchGate::Dispatch
        );
    }

    #[test]
    fn a_review_node_can_never_take_the_best_effort_escape_hatch() {
        // `dispatch_gate` publishes best-effort (Dispatch) on an empty roster
        // when *nothing* is required — the NATS-only deployment path. The
        // independence checks sit after that early return, so the property that
        // keeps them reachable is structural: `requires_independence` is defined
        // by `review` ∈ required_capabilities, so a review node's requirement is
        // never empty and an empty roster lands on NoCapableWorker instead.
        let empty = WorkerRegistry::new();
        let review = mk_subtask("review", &["build"], &["review"], None);
        assert!(uc_engine::requires_independence(
            &review.required_capabilities
        ));
        assert!(
            !review.required_capabilities.is_empty(),
            "otherwise the early return would bypass the independence checks"
        );

        assert_eq!(
            empty.dispatch_gate(
                &review.required_capabilities,
                "p1",
                &ReviewIndependence::default()
            ),
            WorkerDispatchGate::NoCapableWorker
        );
        // The escape hatch itself is intact for the deployment it exists for.
        assert_eq!(
            empty.dispatch_gate(&[], "p1", &ReviewIndependence::default()),
            WorkerDispatchGate::Dispatch
        );
    }

    #[test]
    fn independence_outranks_scope_and_version_in_the_verdict() {
        // Most-specific-first ordering. A review node that would *also* fail the
        // scope filter must report the independence failure: the scope verdict
        // names candidate workers, which points the operator at a scoped worker
        // that is not the problem.
        let mut reg = WorkerRegistry::new();
        reg.register_with_projects(
            "w-scoped".to_string(),
            vec!["code".to_string(), "review".to_string()],
            4,
            String::new(),
            CONTRACT_VERSION.to_string(),
            vec!["other".to_string()],
        )
        .unwrap();
        let caps = vec!["code".to_string(), "review".to_string()];

        // Baseline: unconstrained, this roster reports the *scope* verdict.
        assert_eq!(
            reg.dispatch_gate(&caps, "p1", &ReviewIndependence::default()),
            WorkerDispatchGate::NoScopeMatchedWorker {
                workers: vec!["w-scoped".to_string()]
            }
        );

        // Producer unknown ⇒ that verdict, not the scope one.
        let unknown = ReviewIndependence {
            excluded_producers: set(&[]),
            unknown_producers: vec!["build".to_string()],
        };
        assert_eq!(
            reg.dispatch_gate(&caps, "p1", &unknown),
            WorkerDispatchGate::ProducerIdentityUnknown {
                dependencies: vec!["build".to_string()]
            }
        );

        // Producer known and it is the only candidate ⇒ that verdict, not the
        // scope one.
        let constrained = ReviewIndependence {
            excluded_producers: set(&["w-scoped"]),
            unknown_producers: Vec::new(),
        };
        assert_eq!(
            reg.dispatch_gate(&caps, "p1", &constrained),
            WorkerDispatchGate::NoIndependentReviewer {
                producers: vec!["w-scoped".to_string()]
            }
        );

        // And the scope verdict, when it does fire, never lists the excluded
        // producer: the candidates it names are post-exclusion.
        worker(&mut reg, "w-free", &["code", "review"]);
        // Excluding the producer leaves the unscoped worker ⇒ dispatchable.
        assert_eq!(
            reg.dispatch_gate(&caps, "p1", &constrained),
            WorkerDispatchGate::Dispatch
        );
        // Excluding the *unscoped* worker leaves only the out-of-scope one, and
        // the verdict names it alone.
        let only_scoped = ReviewIndependence {
            excluded_producers: set(&["w-free"]),
            unknown_producers: Vec::new(),
        };
        assert_eq!(
            reg.dispatch_gate(&caps, "p1", &only_scoped),
            WorkerDispatchGate::NoScopeMatchedWorker {
                workers: vec!["w-scoped".to_string()]
            }
        );
    }
}
