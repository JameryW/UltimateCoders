//! T5 #641 acceptance: NATS-down behavior via the unified Executor
//! abstraction (D4 #633 Q1/Q2).
//!
//! When the JetStream transport is down:
//! - `local_safe` / `read_only` nodes keep making progress through the
//!   LocalExecutor (HOW/WHERE degrade),
//! - coding nodes (`effect_class = requires_worker`) stay READY with a
//!   user-visible alert,
//! - WHAT is byte-level unchanged — routing never touches description,
//!   steps, dependencies, or capabilities,
//! - `effect_class` flows from `Subtask` into the graph projection
//!   (`NodeRow`) that backs `graph_nodes`.

use uc_engine::graph_store::project_task;
use uc_engine::scheduler::executor::{
    AttemptStatus, Executor, ExecutorError, ExecutorSelector, LocalExecutor, LocalNodeHandler,
    RouteDecision,
};
use uc_types::{
    DispatchMode, EffectClass, ExecutionEnvelope, Subtask, SubtaskStatus, Task, TaskId, TaskStatus,
};

// ── Scripted transport (never pretends to be a real JS client) ──────

/// Stand-in for NatsExecutor with configurable health. The selector only
/// consults `available()` and forwards `execute()`; real-JS behavior is
/// covered by the Python JetStream integration tests.
struct ScriptedTransport {
    up: bool,
}

impl Executor for ScriptedTransport {
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
    ) -> Result<uc_engine::scheduler::executor::AttemptOutcome, ExecutorError> {
        if self.up {
            Ok(uc_engine::scheduler::executor::AttemptOutcome {
                status: AttemptStatus::Dispatched,
                detail: envelope.idempotency_key.clone(),
                alert: None,
            })
        } else {
            Err(ExecutorError::TransportUnavailable(
                "JetStream unavailable (NATS down)".into(),
            ))
        }
    }
}

// ── Local handler: runs read-only/local-safe tool work in-process ────

struct ToolRunnerHandler;

impl LocalNodeHandler for ToolRunnerHandler {
    fn execute_local(
        &self,
        envelope: &ExecutionEnvelope,
        subtask: &Subtask,
    ) -> Result<uc_engine::scheduler::executor::AttemptOutcome, ExecutorError> {
        // D4 Q2 whitelist — the handler itself must refuse non-whitelisted
        // classes even if the selector ever mis-routes them here.
        assert!(
            matches!(
                subtask.effect_class,
                EffectClass::ReadOnly | EffectClass::LocalSafe
            ),
            "handler only takes whitelisted classes, got {:?}",
            subtask.effect_class
        );
        Ok(uc_engine::scheduler::executor::AttemptOutcome {
            status: AttemptStatus::CompletedLocal,
            detail: format!("tool work for {} completed in-process", envelope.node_id),
            alert: None,
        })
    }
}

// ── Fixture: one graph, three node classes ───────────────────────────

fn make_subtask(id: &str, effect_class: EffectClass, description: &str) -> Subtask {
    Subtask {
        id: TaskId(id.to_string()),
        parent_id: TaskId("t-graph".to_string()),
        description: description.to_string(),
        status: SubtaskStatus::Pending,
        assigned_worker: None,
        depends_on: vec![],
        file_constraints: Vec::new(),
        expected_output: format!("output of {id}"),
        result: None,
        dispatch_mode: DispatchMode::PreferRemote,
        effect_class,
        dispatch_retry_count: 0,
        retry_count: 0,
        required_capabilities: vec!["tooling".to_string()],
        agent_config_json: None,
        steps: Vec::new(),
    }
}

fn make_task() -> Task {
    Task {
        id: TaskId("t-graph".to_string()),
        description: "mixed-effect graph".to_string(),
        project_id: "p1".to_string(),
        status: TaskStatus::InProgress,
        subtasks: vec![
            make_subtask("st-tool", EffectClass::LocalSafe, "run repo-wide grep"),
            make_subtask("st-read", EffectClass::ReadOnly, "summarize open PRs"),
            make_subtask(
                "st-code",
                EffectClass::RequiresWorker,
                "implement feature X",
            ),
        ],
        created_at: chrono::Utc::now(),
        updated_at: chrono::Utc::now(),
    }
}

fn selector(transport_up: bool) -> ExecutorSelector {
    ExecutorSelector::new(
        std::sync::Arc::new(ScriptedTransport { up: transport_up }),
        std::sync::Arc::new(LocalExecutor::new(std::sync::Arc::new(ToolRunnerHandler))),
    )
}

#[test]
fn nats_down_local_safe_graph_progresses_coding_stays_ready() {
    let task = make_task();
    let sel = selector(false);

    let mut progressed = 0usize;
    let mut held = 0usize;
    for st in &task.subtasks {
        let env = ExecutionEnvelope::for_dispatch(&task.id.0, &st.id.0, st.dispatch_retry_count);
        let outcome = sel
            .execute(&env, st)
            .expect("selector always yields an outcome");
        match outcome.status {
            AttemptStatus::CompletedLocal => {
                progressed += 1;
                assert!(outcome.alert.is_none(), "progress must not alert");
            }
            AttemptStatus::Unavailable => {
                held += 1;
                let alert = outcome.alert.clone().expect("held node must alert");
                assert!(
                    alert.contains("READY") && alert.contains("requires_worker"),
                    "alert must name the effect class and READY state: {alert}"
                );
            }
            other => panic!("unexpected outcome for {}: {other:?}", st.id.0),
        }
    }
    assert_eq!(progressed, 2, "local_safe + read_only must progress");
    assert_eq!(held, 1, "the coding node must be held in READY");
}

#[test]
fn nats_down_routing_never_mutates_what() {
    // WHAT is frozen: the projection (description, dependencies,
    // capabilities, effect_class per node) must be byte-identical before
    // and after a full routing pass on the down transport.
    let mut task = make_task();
    let before = project_task(&task);
    let sel = selector(false);

    for st in &task.subtasks {
        let env = ExecutionEnvelope::for_dispatch(&task.id.0, &st.id.0, st.dispatch_retry_count);
        let _ = sel.execute(&env, st).unwrap();
    }
    let after = project_task(&task);

    assert_eq!(before.nodes.len(), after.nodes.len());
    for (b, a) in before.nodes.iter().zip(after.nodes.iter()) {
        assert_eq!(b.node_id, a.node_id);
        assert_eq!(b.dependencies, a.dependencies, "WHAT: dependencies");
        assert_eq!(
            b.required_capabilities, a.required_capabilities,
            "WHAT: capabilities"
        );
        assert_eq!(b.effect_class, a.effect_class, "WHAT: effect_class");
    }
    // Subtask-level WHAT too (description survives routing untouched).
    assert_eq!(task.subtasks[0].description, "run repo-wide grep");
    assert_eq!(task.subtasks[2].description, "implement feature X");
}

#[test]
fn nats_up_routes_everything_to_the_transport() {
    let task = make_task();
    let sel = selector(true);
    for st in &task.subtasks {
        let env = ExecutionEnvelope::for_dispatch(&task.id.0, &st.id.0, st.dispatch_retry_count);
        assert_eq!(sel.route(st), RouteDecision::Nats);
        let outcome = sel.execute(&env, st).unwrap();
        assert_eq!(outcome.status, AttemptStatus::Dispatched);
    }
}

#[test]
fn effect_class_projects_into_graph_node_rows() {
    // graph_nodes.effect_class: Rust-shape projection carries the subtask's
    // class; the legacy default is 'requires_worker'.
    let task = make_task();
    let p = project_task(&task);
    let by_id = |id: &str| {
        p.nodes
            .iter()
            .find(|n| n.node_id == id)
            .unwrap_or_else(|| panic!("node {id} missing"))
    };
    assert_eq!(by_id("st-tool").effect_class, "local_safe");
    assert_eq!(by_id("st-read").effect_class, "read_only");
    assert_eq!(by_id("st-code").effect_class, "requires_worker");
}

#[test]
fn local_safe_via_local_executor_only_when_whitelisted() {
    // D4 Q2 gate, directly: unwired handler → even local_safe stays READY.
    let sel = ExecutorSelector::new(
        std::sync::Arc::new(ScriptedTransport { up: false }),
        std::sync::Arc::new(LocalExecutor::unwired()),
    );
    let st = make_subtask("st-tool", EffectClass::LocalSafe, "run repo-wide grep");
    assert_eq!(sel.route(&st), RouteDecision::StayReady);
    let outcome = sel
        .execute(
            &ExecutionEnvelope::for_dispatch("t-graph", "st-tool", 0),
            &st,
        )
        .unwrap();
    assert_eq!(outcome.status, AttemptStatus::Unavailable);
    assert!(outcome.alert.unwrap().contains("no local handler wired"));
}
