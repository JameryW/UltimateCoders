//! Unified subtask executor abstraction (T5 #641, D4 #633).
//!
//! One trait, four executors:
//!
//! - [`NatsExecutor`] — the JetStream dispatch plane. **Hard JS dependency**
//!   (D4 Q1): it publishes to the `uc.subtask.execute` subject served by the
//!   `UC_SUBTASKS` stream and never falls back to a core-NATS at-most-once
//!   path. Stream provisioning is the gateway's job; a publish failure
//!   surfaces as [`ExecutorError::TransportUnavailable`] so the selector can
//!   route by `effect_class`.
//! - [`LocalExecutor`] — in-process execution for nodes whose
//!   `effect_class` whitelists it (D4 Q2: `read_only` / `local_safe` only).
//!   The work body is injected via [`LocalNodeHandler`]; until the gateway
//!   wires a tool-runner, the default handler refuses and the node stays
//!   READY with an alert.
//! - [`SandboxExecutor`] — placeholder for the sandbox runtime (post-T7).
//! - [`RemoteExecutor`] — placeholder for a dedicated remote runtime pool
//!   (post-T7).
//!
//! [`ExecutorSelector`] is the routing authority: NATS available → Nats;
//! transport down → Local only for whitelisted effect classes (unless
//! `dispatch_mode = Remote`, which must never run locally), otherwise the
//! node stays READY and an alert is raised. WHAT never changes — routing
//! touches neither description, steps, nor dependencies; only HOW/WHERE
//! degrade.
//!
//! Side-mount note: this module does not rewire the legacy dispatch mouth
//! (uc-grpc server.rs). The graph runtime switches to it in T6; until then
//! legacy behavior is untouched (dual-plane ruling).

use std::sync::Arc;

use uc_types::{DispatchMode, EffectClass, ExecutionEnvelope, Subtask};

/// Result of attempting to run one dispatch envelope through an executor.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AttemptOutcome {
    pub status: AttemptStatus,
    pub detail: String,
    /// User-visible alert text (e.g. coding node held in READY on transport
    /// loss). `None` when nothing needs surfacing.
    pub alert: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AttemptStatus {
    /// Envelope handed to the transport (JS publish accepted).
    Dispatched,
    /// Executed synchronously by this executor (LocalExecutor).
    CompletedLocal,
    /// The executor ran the attempt and it failed.
    Failed,
    /// The executor cannot take work right now (transport down, unsupported).
    Unavailable,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ExecutorError {
    /// Transport (JetStream) unavailable — the only error class that
    /// triggers effect_class-gated fallback routing.
    TransportUnavailable(String),
    /// This executor refuses the work (effect_class not whitelisted, no
    /// local handler wired, capability not implemented).
    Unsupported(String),
    /// The attempt itself failed while the executor was healthy.
    ExecutionFailed(String),
}

impl std::fmt::Display for ExecutorError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ExecutorError::TransportUnavailable(m) => write!(f, "transport unavailable: {m}"),
            ExecutorError::Unsupported(m) => write!(f, "unsupported: {m}"),
            ExecutorError::ExecutionFailed(m) => write!(f, "execution failed: {m}"),
        }
    }
}

/// One dispatch executor. Sync by design — the dispatch mouths run inside
/// `block_in_place` contexts (see `dispatcher.rs`); async I/O is driven via
/// `Handle::block_on` inside implementations.
pub trait Executor: Send + Sync {
    fn name(&self) -> &'static str;
    /// Whether this executor can accept work right now.
    fn available(&self) -> bool;
    /// Execute one dispatch envelope.
    fn execute(
        &self,
        envelope: &ExecutionEnvelope,
        subtask: &Subtask,
    ) -> Result<AttemptOutcome, ExecutorError>;
}

// ── NatsExecutor (JetStream dispatch plane) ─────────────────────────

/// JetStream-backed executor. **Hard JS dependency** (D4 Q1): publishes to
/// `uc.subtask.execute` (served by the gateway-provisioned `UC_SUBTASKS`
/// stream). There is no core-NATS fallback path — when the connection is
/// down the executor reports unavailable and the selector routes by
/// `effect_class`.
#[cfg(feature = "messaging")]
pub struct NatsExecutor {
    client: async_nats::Client,
    subject: String,
}

#[cfg(feature = "messaging")]
impl NatsExecutor {
    pub fn new(client: async_nats::Client) -> Self {
        Self {
            client,
            subject: "uc.subtask.execute".to_string(),
        }
    }

    /// Override the dispatch subject (tests / exotic deployments).
    pub fn with_subject(mut self, subject: String) -> Self {
        self.subject = subject;
        self
    }
}

#[cfg(feature = "messaging")]
impl Executor for NatsExecutor {
    fn name(&self) -> &'static str {
        "nats"
    }

    fn available(&self) -> bool {
        matches!(
            self.client.connection_state(),
            async_nats::connection::State::Connected
        )
    }

    fn execute(
        &self,
        envelope: &ExecutionEnvelope,
        subtask: &Subtask,
    ) -> Result<AttemptOutcome, ExecutorError> {
        // Same wire shape as the gateway/legacy publishers (T1 #637, T4
        // #640): envelope is the single identity source, Nats-Msg-Id carries
        // the idempotency key so the stream's duplicate_window collapses
        // re-sends.
        let payload = serde_json::json!({
            "description": subtask.description,
            "expected_output": subtask.expected_output,
            "file_constraints": subtask.file_constraints,
            "steps": subtask.steps,
            "required_capabilities": subtask.required_capabilities,
            "dispatch_mode": subtask.dispatch_mode,
            "effect_class": subtask.effect_class,
            // ── execution envelope ──────────────────────────────────
            "graph_id": envelope.graph_id,
            "node_id": envelope.node_id,
            "attempt_id": envelope.attempt_id,
            "idempotency_key": envelope.idempotency_key,
            "worker_epoch": envelope.worker_epoch,
            "contract_version": envelope.contract_version,
        });
        let bytes = serde_json::to_vec(&payload).map_err(|e| {
            ExecutorError::ExecutionFailed(format!("envelope serialization failed: {e}"))
        })?;
        let mut headers = async_nats::HeaderMap::new();
        headers.insert("Nats-Msg-Id", envelope.idempotency_key.as_str());

        let client = self.client.clone();
        let subject = self.subject.clone();
        let publish = tokio::runtime::Handle::current().block_on(async {
            client
                .publish_with_headers(subject, headers, bytes.into())
                .await
        });
        match publish {
            Ok(()) => Ok(AttemptOutcome {
                status: AttemptStatus::Dispatched,
                detail: format!("dispatched via JetStream ({})", envelope.idempotency_key),
                alert: None,
            }),
            Err(e) => Err(ExecutorError::TransportUnavailable(format!(
                "JetStream publish failed: {e}"
            ))),
        }
    }
}

// ── LocalExecutor (effect_class-whitelisted in-process execution) ───

/// The injected work body for [`LocalExecutor`].
pub trait LocalNodeHandler: Send + Sync {
    fn execute_local(
        &self,
        envelope: &ExecutionEnvelope,
        subtask: &Subtask,
    ) -> Result<AttemptOutcome, ExecutorError>;
}

/// In-process executor for nodes whose `effect_class` whitelists local
/// execution (D4 Q2: `read_only` / `local_safe` — tool-class, no
/// worktree/CLI dependency). The work body is injected: the gateway wires a
/// real tool-runner in a later ticket; unwired, execution refuses and the
/// node stays READY with an alert.
pub struct LocalExecutor {
    handler: Option<Arc<dyn LocalNodeHandler>>,
}

impl LocalExecutor {
    /// Unwired executor — routing prefers it only if [`LocalExecutor::has_handler`].
    pub fn unwired() -> Self {
        Self { handler: None }
    }

    pub fn new(handler: Arc<dyn LocalNodeHandler>) -> Self {
        Self {
            handler: Some(handler),
        }
    }

    pub fn has_handler(&self) -> bool {
        self.handler.is_some()
    }
}

impl Executor for LocalExecutor {
    fn name(&self) -> &'static str {
        "local"
    }

    fn available(&self) -> bool {
        self.has_handler()
    }

    fn execute(
        &self,
        envelope: &ExecutionEnvelope,
        subtask: &Subtask,
    ) -> Result<AttemptOutcome, ExecutorError> {
        match &self.handler {
            Some(h) => h.execute_local(envelope, subtask),
            None => Err(ExecutorError::Unsupported(
                "no local node handler wired on the gateway".to_string(),
            )),
        }
    }
}

// ── Placeholders (explicit, loud, ungated) ──────────────────────────

/// Placeholder for the sandbox runtime (lands post-T7). Refuses all work so
/// a mis-route fails loudly instead of pretending.
pub struct SandboxExecutor;

impl Executor for SandboxExecutor {
    fn name(&self) -> &'static str {
        "sandbox"
    }
    fn available(&self) -> bool {
        false
    }
    fn execute(
        &self,
        _envelope: &ExecutionEnvelope,
        _subtask: &Subtask,
    ) -> Result<AttemptOutcome, ExecutorError> {
        Err(ExecutorError::Unsupported(
            "SandboxExecutor lands with the sandbox runtime (post-T7)".to_string(),
        ))
    }
}

/// Placeholder for a dedicated remote runtime pool (lands post-T7). Refuses
/// all work so a mis-route fails loudly instead of pretending.
pub struct RemoteExecutor;

impl Executor for RemoteExecutor {
    fn name(&self) -> &'static str {
        "remote"
    }
    fn available(&self) -> bool {
        false
    }
    fn execute(
        &self,
        _envelope: &ExecutionEnvelope,
        _subtask: &Subtask,
    ) -> Result<AttemptOutcome, ExecutorError> {
        Err(ExecutorError::Unsupported(
            "RemoteExecutor lands with the dedicated remote runtime (post-T7)".to_string(),
        ))
    }
}

// ── ExecutorSelector (routing authority) ────────────────────────────

/// Where a ready node should run.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RouteDecision {
    /// Publish via the JetStream transport.
    Nats,
    /// Execute in-process (whitelisted effect class, handler wired).
    Local,
    /// Keep the node READY and raise an alert.
    StayReady,
}

/// Routes a ready node to an executor. NATS available → Nats. Transport
/// down → Local only for `read_only`/`local_safe` nodes with a wired
/// handler (and never for `dispatch_mode = Remote`); everything else stays
/// READY with an alert. WHAT is frozen — routing never mutates the subtask.
pub struct ExecutorSelector {
    nats: Arc<dyn Executor>,
    local: Arc<LocalExecutor>,
}

impl ExecutorSelector {
    pub fn new(nats: Arc<dyn Executor>, local: Arc<LocalExecutor>) -> Self {
        Self { nats, local }
    }

    /// Routing decision for one node given current transport health.
    pub fn route(&self, subtask: &Subtask) -> RouteDecision {
        if self.nats.available() {
            return RouteDecision::Nats;
        }
        // Transport down — D4 Q2 whitelist. `dispatch_mode = Remote` means
        // "must execute on a remote worker": it never runs locally,
        // regardless of effect_class.
        if subtask.dispatch_mode == DispatchMode::Remote {
            return RouteDecision::StayReady;
        }
        if !matches!(
            subtask.effect_class,
            EffectClass::ReadOnly | EffectClass::LocalSafe
        ) {
            return RouteDecision::StayReady;
        }
        if !self.local.has_handler() {
            return RouteDecision::StayReady;
        }
        RouteDecision::Local
    }

    /// Route and execute in one step. A `StayReady` route yields
    /// `Ok(AttemptOutcome { status: Unavailable, alert: Some(..) })` — the
    /// caller surfaces the alert and leaves the node READY.
    pub fn execute(
        &self,
        envelope: &ExecutionEnvelope,
        subtask: &Subtask,
    ) -> Result<AttemptOutcome, ExecutorError> {
        match self.route(subtask) {
            RouteDecision::Nats => self.nats.execute(envelope, subtask),
            RouteDecision::Local => self.local.execute(envelope, subtask),
            RouteDecision::StayReady => {
                let why = if subtask.dispatch_mode == DispatchMode::Remote {
                    format!(
                        "dispatch_mode=remote requires the remote transport (effect_class={})",
                        subtask.effect_class.as_str()
                    )
                } else if !matches!(
                    subtask.effect_class,
                    EffectClass::ReadOnly | EffectClass::LocalSafe
                ) {
                    "effect_class=requires_worker cannot run locally".to_string()
                } else {
                    "no local handler wired".to_string()
                };
                Ok(AttemptOutcome {
                    status: AttemptStatus::Unavailable,
                    detail: format!("node {} held in READY: {why}", envelope.node_id),
                    alert: Some(format!(
                        "node {} (effect_class={}, dispatch_mode={:?}) cannot progress while \
                         the JetStream transport is down: {why}; it stays READY and resumes \
                         via at-least-once redelivery when the transport recovers",
                        envelope.node_id,
                        subtask.effect_class.as_str(),
                        subtask.dispatch_mode,
                    )),
                })
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use uc_types::SubtaskStatus;

    fn subtask(effect_class: EffectClass, dispatch_mode: DispatchMode) -> Subtask {
        Subtask {
            id: uc_types::TaskId("st-1".into()),
            parent_id: uc_types::TaskId("t-1".into()),
            description: "do the thing".into(),
            status: SubtaskStatus::Pending,
            assigned_worker: None,
            depends_on: vec![],
            file_constraints: vec![],
            expected_output: "done".into(),
            result: None,
            dispatch_mode,
            effect_class,
            dispatch_retry_count: 0,
            retry_count: 0,
            required_capabilities: vec![],
            agent_config_json: None,
            steps: vec![],
        }
    }

    /// Scripted stand-in for NatsExecutor — the selector only consults
    /// `available()` and forwards `execute()`.
    struct ScriptedExecutor {
        up: bool,
    }

    impl Executor for ScriptedExecutor {
        fn name(&self) -> &'static str {
            "scripted"
        }
        fn available(&self) -> bool {
            self.up
        }
        fn execute(
            &self,
            envelope: &ExecutionEnvelope,
            _subtask: &Subtask,
        ) -> Result<AttemptOutcome, ExecutorError> {
            if self.up {
                Ok(AttemptOutcome {
                    status: AttemptStatus::Dispatched,
                    detail: envelope.idempotency_key.clone(),
                    alert: None,
                })
            } else {
                Err(ExecutorError::TransportUnavailable("down".into()))
            }
        }
    }

    /// Records WHAT it was handed so tests can assert byte-level integrity.
    struct RecordingHandler {
        effect: EffectClass,
    }

    impl LocalNodeHandler for RecordingHandler {
        fn execute_local(
            &self,
            envelope: &ExecutionEnvelope,
            subtask: &Subtask,
        ) -> Result<AttemptOutcome, ExecutorError> {
            assert_eq!(subtask.effect_class, self.effect, "WHAT must be preserved");
            Ok(AttemptOutcome {
                status: AttemptStatus::CompletedLocal,
                detail: format!("ran {} locally", envelope.node_id),
                alert: None,
            })
        }
    }

    fn selector_up() -> ExecutorSelector {
        ExecutorSelector::new(
            Arc::new(ScriptedExecutor { up: true }),
            Arc::new(LocalExecutor::new(Arc::new(RecordingHandler {
                effect: EffectClass::LocalSafe,
            }))),
        )
    }

    fn selector_down() -> ExecutorSelector {
        ExecutorSelector::new(
            Arc::new(ScriptedExecutor { up: false }),
            Arc::new(LocalExecutor::new(Arc::new(RecordingHandler {
                effect: EffectClass::LocalSafe,
            }))),
        )
    }

    fn envelope() -> ExecutionEnvelope {
        ExecutionEnvelope::for_dispatch("t-1", "st-1", 0)
    }

    #[test]
    fn nats_available_routes_everything_to_nats() {
        let sel = selector_up();
        assert_eq!(
            sel.route(&subtask(
                EffectClass::RequiresWorker,
                DispatchMode::PreferRemote
            )),
            RouteDecision::Nats
        );
        assert_eq!(
            sel.route(&subtask(EffectClass::LocalSafe, DispatchMode::Remote)),
            RouteDecision::Nats
        );
        let out = sel
            .execute(
                &envelope(),
                &subtask(EffectClass::RequiresWorker, DispatchMode::PreferRemote),
            )
            .unwrap();
        assert_eq!(out.status, AttemptStatus::Dispatched);
    }

    #[test]
    fn nats_down_local_safe_progresses_locally() {
        // D4 Q2: local_safe nodes keep making progress when the transport
        // is down — HOW/WHERE degrade, WHAT does not.
        let sel = selector_down();
        assert_eq!(
            sel.route(&subtask(EffectClass::LocalSafe, DispatchMode::PreferRemote)),
            RouteDecision::Local
        );
        let out = sel
            .execute(
                &envelope(),
                &subtask(EffectClass::LocalSafe, DispatchMode::PreferRemote),
            )
            .unwrap();
        assert_eq!(out.status, AttemptStatus::CompletedLocal);
    }

    #[test]
    fn nats_down_read_only_progresses_locally() {
        let sel = selector_down();
        assert_eq!(
            sel.route(&subtask(EffectClass::ReadOnly, DispatchMode::PreferRemote)),
            RouteDecision::Local
        );
    }

    #[test]
    fn nats_down_requires_worker_stays_ready_with_alert() {
        // Coding nodes queue with an alert — no local execution, no
        // re-decomposition.
        let sel = selector_down();
        assert_eq!(
            sel.route(&subtask(
                EffectClass::RequiresWorker,
                DispatchMode::PreferRemote
            )),
            RouteDecision::StayReady
        );
        let out = sel
            .execute(
                &envelope(),
                &subtask(EffectClass::RequiresWorker, DispatchMode::PreferRemote),
            )
            .unwrap();
        assert_eq!(out.status, AttemptStatus::Unavailable);
        let alert = out
            .alert
            .expect("coding node held in READY must raise an alert");
        assert!(
            alert.contains("requires_worker"),
            "alert names the effect class: {alert}"
        );
        assert!(
            alert.contains("READY"),
            "alert says the node stays READY: {alert}"
        );
    }

    #[test]
    fn nats_down_remote_mode_never_runs_locally() {
        // "Must execute on a remote worker" overrides the whitelist.
        let sel = selector_down();
        assert_eq!(
            sel.route(&subtask(EffectClass::LocalSafe, DispatchMode::Remote)),
            RouteDecision::StayReady
        );
        let out = sel
            .execute(
                &envelope(),
                &subtask(EffectClass::LocalSafe, DispatchMode::Remote),
            )
            .unwrap();
        assert_eq!(out.status, AttemptStatus::Unavailable);
        assert!(out.alert.unwrap().contains("remote"));
    }

    #[test]
    fn nats_down_without_local_handler_stays_ready() {
        // The gateway has not wired a tool runner yet — even local_safe
        // nodes must not silently vanish; they stay READY with an alert.
        let sel = ExecutorSelector::new(
            Arc::new(ScriptedExecutor { up: false }),
            Arc::new(LocalExecutor::unwired()),
        );
        assert_eq!(
            sel.route(&subtask(EffectClass::LocalSafe, DispatchMode::PreferRemote)),
            RouteDecision::StayReady
        );
        let out = sel
            .execute(
                &envelope(),
                &subtask(EffectClass::LocalSafe, DispatchMode::PreferRemote),
            )
            .unwrap();
        assert_eq!(out.status, AttemptStatus::Unavailable);
        assert!(out.alert.unwrap().contains("no local handler wired"));
    }

    #[test]
    fn unwired_local_executor_refuses_loudly() {
        let local = LocalExecutor::unwired();
        let err = local
            .execute(
                &envelope(),
                &subtask(EffectClass::LocalSafe, DispatchMode::PreferRemote),
            )
            .unwrap_err();
        assert!(matches!(err, ExecutorError::Unsupported(_)));
    }

    #[test]
    fn routing_never_mutates_the_subtask() {
        // WHAT is frozen: the subtask handed to the local handler must be
        // byte-identical to the one routed (RecordingHandler asserts the
        // effect_class; here we assert the WHAT fields explicitly — Subtask
        // does not derive PartialEq, and adding one is not T5's call).
        let sel = selector_down();
        let st = subtask(EffectClass::LocalSafe, DispatchMode::PreferRemote);
        let (desc, deps_len, caps_len, expected, steps_len, effect) = (
            st.description.clone(),
            st.depends_on.len(),
            st.required_capabilities.len(),
            st.expected_output.clone(),
            st.steps.len(),
            st.effect_class,
        );
        let _ = sel.execute(&envelope(), &st).unwrap();
        assert_eq!(st.description, desc, "WHAT: description");
        assert_eq!(st.depends_on.len(), deps_len, "WHAT: dependencies");
        assert_eq!(
            st.required_capabilities.len(),
            caps_len,
            "WHAT: capabilities"
        );
        assert_eq!(st.expected_output, expected, "WHAT: expected_output");
        assert_eq!(st.steps.len(), steps_len, "WHAT: steps");
        assert_eq!(st.effect_class, effect, "WHAT: effect_class");
    }

    #[test]
    fn placeholders_refuse_loudly() {
        let env = envelope();
        let st = subtask(EffectClass::RequiresWorker, DispatchMode::PreferRemote);
        assert!(!SandboxExecutor.available());
        assert!(matches!(
            SandboxExecutor.execute(&env, &st),
            Err(ExecutorError::Unsupported(_))
        ));
        assert!(!RemoteExecutor.available());
        assert!(matches!(
            RemoteExecutor.execute(&env, &st),
            Err(ExecutorError::Unsupported(_))
        ));
    }
}
