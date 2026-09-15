//! T7 #643 — granular cancel end-to-end on the dual-plane shape (C5).
//!
//! Scenario 1 — cancel-attempt-keep-node: a RUNNING attempt is cancelled
//! through the graph plane (fail_attempt budget semantics), the legacy row
//! requeues, the LATE result from the killed worker stays fenced (loses
//! commit_once), and a fresh attempt on the re-armed node commits and wins.
//!
//! Scenario 2 — node-level cancel: the gateway computes the downstream
//! closure and CANCELLEDs it terminally; the legacy mirror moves the live
//! rows to Failed (T4 mapping); siblings outside the closure and committed
//! ancestors are untouched.
//!
//! Real PostgreSQL (graph plane) + in-memory legacy TaskStore wired to the
//! real `GraphStore` as its shadow — the same dual-plane shape the gateway
//! runs with storage enabled.
//!
//! Run explicitly: `cargo test -p uc-grpc --all-features --test granular_cancel_e2e -- --ignored`

#![cfg(feature = "storage")]

use std::sync::Arc;
use std::time::Duration;

use tokio::sync::Mutex;
use uc_engine::GraphStore;
use uc_grpc::server::TaskStore;
use uc_types::{DispatchMode, EffectClass, Subtask, SubtaskStatus, TaskId};

/// PG URL for the shared test instance (uc-engine convention).
fn pg_test_url() -> String {
    std::env::var("UC_PG_URL_TEST").unwrap_or_else(|_| {
        "postgresql://ultimate_coders:ultimate_coders@127.0.0.1:5432/ultimate_coders".to_string()
    })
}

fn url_for_database(base: &str, name: &str) -> String {
    match base.rfind('/') {
        Some(idx) => format!("{}/{}", &base[..idx], name),
        None => format!("{}/{}", base, name),
    }
}

/// Fresh throwaway database, migrated, or `None` when PG is unreachable
/// (visible skip — the test never silently passes without PG).
async fn connect_probe(name: &str) -> Option<GraphStore> {
    let base = pg_test_url();
    let admin_url = url_for_database(&base, "postgres");
    let pool = match sqlx::postgres::PgPoolOptions::new()
        .max_connections(1)
        .connect(&admin_url)
        .await
    {
        Ok(p) => p,
        Err(e) => {
            eprintln!("SKIP {name}: no PostgreSQL reachable at {base} ({e}) — set UC_PG_URL_TEST");
            return None;
        }
    };
    sqlx::query(&format!("DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        .execute(&pool)
        .await
        .expect("probe db drop before create");
    sqlx::query(&format!("CREATE DATABASE {name}"))
        .execute(&pool)
        .await
        .expect("probe db create");
    pool.close().await;

    match GraphStore::connect(&url_for_database(&base, name)).await {
        Ok(store) => {
            assert!(
                store.is_connected().await,
                "probe GraphStore must actually talk to the probe database"
            );
            Some(store)
        }
        Err(e) => {
            eprintln!("SKIP {name}: GraphStore::connect failed ({e}) — set UC_PG_URL_TEST");
            None
        }
    }
}

async fn drop_force(name: &str) {
    let base = pg_test_url();
    let admin_url = url_for_database(&base, "postgres");
    if let Ok(pool) = sqlx::postgres::PgPoolOptions::new()
        .max_connections(1)
        .connect(&admin_url)
        .await
    {
        let _ = sqlx::query(&format!("DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
            .execute(&pool)
            .await;
        pool.close().await;
    }
}

fn subtask(id: &str, parent: &str, status: SubtaskStatus, depends: Vec<&str>) -> Subtask {
    Subtask {
        id: TaskId(id.to_string()),
        parent_id: TaskId(parent.to_string()),
        description: format!("step {id}"),
        status,
        assigned_worker: None,
        depends_on: depends.into_iter().map(|d| TaskId(d.to_string())).collect(),
        file_constraints: Vec::new(),
        expected_output: String::new(),
        result: None,
        dispatch_mode: DispatchMode::default(),
        effect_class: EffectClass::default(),
        dispatch_retry_count: 0,
        retry_count: 0,
        required_capabilities: vec!["rust".to_string()],
        agent_config_json: None,
        steps: Vec::new(),
    }
}

/// Let the async shadow fan-out settle (persist_task → shadow_persist).
async fn settle() {
    for _ in 0..30 {
        tokio::task::yield_now().await;
    }
    tokio::time::sleep(Duration::from_millis(50)).await;
}

/// Wait (bounded) for the fire-and-forget shadow mirror to publish
/// `expected`, then assert it.
///
/// `settle()` is not a synchronisation primitive: `persist_task` spawns
/// `shadow_persist` and returns, so a fixed nap only *usually* covers the
/// write. On a loaded runner it misses, the node row is still absent, and
/// `schedule_attempt` fails with a misleading "st-a READY" panic even though
/// the legacy upsert was fine. Polling removes the race; on timeout the panic
/// reports what the mirror actually holds and retries the write synchronously,
/// so the error `shadow_persist` swallows (warn-only by design) is visible.
async fn expect_node_states(
    graph: &GraphStore,
    legacy: &Mutex<TaskStore>,
    task_id: &str,
    expected: &[(&str, &str)],
) {
    let mut last: Vec<(String, Option<String>)> = Vec::new();
    for _ in 0..100 {
        last.clear();
        let mut all_ok = true;
        for (node_id, want) in expected {
            let got = graph
                .node_state(task_id, node_id)
                .await
                .expect("node_state read");
            if got.as_deref() != Some(*want) {
                all_ok = false;
            }
            last.push(((*node_id).to_string(), got));
        }
        if all_ok {
            return;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    // Retry the write synchronously so the error the fire-and-forget path
    // swallows (warn-only by design) shows up here. Never panic before this
    // point: the diagnostic is the whole reason the wait is bounded.
    let direct = match legacy.lock().await.get_task(task_id).cloned() {
        Some(task) => format!("{:?}", graph.upsert_task_shadow(&task).await),
        None => "task absent from the legacy store".to_string(),
    };
    panic!(
        "shadow mirror never published {expected:?} for {task_id} within 5s; \
         last read {last:?}; direct shadow write: {direct}"
    );
}

#[tokio::test]
#[ignore = "needs PostgreSQL; run with --ignored (T7 #643 C5 cancel e2e)"]
async fn attempt_cancel_rearms_node_late_result_fenced_fresh_attempt_commits() {
    let probe = "uc_t7_cancel_e2e_attempt";
    let Some(graph) = connect_probe(probe).await else {
        return; // visible skip already printed
    };
    let graph = Arc::new(graph);

    let sink: Arc<dyn uc_engine::GraphShadowSink> = graph.clone();
    let mut legacy = TaskStore::new();
    legacy.set_graph_shadow(sink.clone());
    let legacy = Arc::new(Mutex::new(legacy));

    // Chain a → b: a committed, b in flight on a "slow worker".
    let task_id = "t7-cancel-attempt".to_string();
    {
        let mut s = legacy.lock().await;
        s.update_task(
            &task_id,
            "InProgress",
            vec![
                subtask("st-a", &task_id, SubtaskStatus::Pending, vec![]),
                subtask("st-b", &task_id, SubtaskStatus::Pending, vec!["st-a"]),
            ],
            "granular cancel e2e (attempt)",
            "p1",
        )
        .expect("chain upsert");
    }
    // Wait for the fire-and-forget mirror to publish the root as READY --
    // the precondition `schedule_attempt` documents.
    expect_node_states(&graph, &legacy, &task_id, &[("st-a", "READY")]).await;

    let a_attempt = graph
        .schedule_attempt(&task_id, "st-a", Some("w1"))
        .await
        .expect("schedule a")
        .expect("st-a READY after the mirror");
    assert!(
        graph
            .commit_once(&task_id, "st-a", &a_attempt, Some("done-a"))
            .await
            .expect("commit a"),
        "first commit must win"
    );
    let b_attempt = graph
        .schedule_attempt(&task_id, "st-b", Some("w2"))
        .await
        .expect("schedule b")
        .expect("st-b READY after a's commit");
    {
        let mut s = legacy.lock().await;
        s.update_subtask_status(&task_id, "st-a", SubtaskStatus::Completed);
        s.update_subtask_status(&task_id, "st-b", SubtaskStatus::InProgress);
    }
    settle().await;

    // ── Attempt-level cancel (cancel-attempt-keep-node) ──────────────
    let swept = sink
        .cancel_running_attempt(&task_id, "st-b")
        .await
        .expect("the RUNNING attempt exists — cancel wins against the idle worker");
    assert_eq!(
        swept.attempt_id, b_attempt,
        "the RUNNING attempt was fenced"
    );
    assert!(swept.rearmed, "retry budget left: the node re-arms");
    assert_eq!(
        graph.attempt_states(&task_id, "st-b").await.expect("reads"),
        vec!["FAILED".to_string()],
        "the cancelled attempt is FAILED"
    );
    assert_eq!(
        graph.node_state(&task_id, "st-b").await.expect("node read"),
        Some("READY".to_string()),
        "the node is kept (READY), not terminal"
    );

    // Legacy mirror: the in-flight row requeues to Pending.
    {
        let s = legacy.lock().await;
        let t = s.get_task(&task_id).expect("task present");
        let b = t.subtasks.iter().find(|x| x.id.0 == "st-b").unwrap();
        assert_eq!(b.status, SubtaskStatus::Pending, "mirror requeued");
        assert!(b.assigned_worker.is_none(), "assignment cleared");
    }

    // ── Late result from the killed worker stays fenced ──────────────
    assert!(
        !graph
            .commit_once(&task_id, "st-b", &b_attempt, Some("late result"))
            .await
            .expect("late commit_once"),
        "the cancelled attempt must never commit"
    );
    assert_eq!(
        graph.node_state(&task_id, "st-b").await.expect("node read"),
        Some("READY".to_string()),
        "the fenced late result changed nothing"
    );

    // ── Fresh attempt on the re-armed node commits and wins ──────────
    let b2 = graph
        .schedule_attempt(&task_id, "st-b", Some("w3"))
        .await
        .expect("re-schedule b")
        .expect("the re-armed node accepts a fresh attempt");
    assert_eq!(
        graph.attempt_states(&task_id, "st-b").await.expect("reads"),
        vec!["FAILED".to_string(), "RUNNING".to_string()],
        "attempt history: cancelled, then a fresh RUNNING attempt"
    );
    assert!(
        graph
            .commit_once(&task_id, "st-b", &b2, Some("done-b"))
            .await
            .expect("commit b2"),
        "the fresh attempt commits"
    );
    assert_eq!(
        graph.node_state(&task_id, "st-b").await.expect("node read"),
        Some("SUCCEEDED".to_string()),
    );

    drop(legacy);
    drop(graph);
    drop_force(probe).await;
}

#[tokio::test]
#[ignore = "needs PostgreSQL; run with --ignored (T7 #643 C5 cancel e2e)"]
async fn node_cancel_closure_terminal_no_sibling_harm() {
    let probe = "uc_t7_cancel_e2e_nodes";
    let Some(graph) = connect_probe(probe).await else {
        return;
    };
    let graph = Arc::new(graph);

    let sink: Arc<dyn uc_engine::GraphShadowSink> = graph.clone();
    let mut legacy = TaskStore::new();
    legacy.set_graph_shadow(sink.clone());
    let legacy = Arc::new(Mutex::new(legacy));

    // Diamond a → (b, c) → d: a committed, b in flight, c READY-undispatched,
    // d CREATED. Cancelling b must take d (dependent) but NOT c (sibling).
    let task_id = "t7-cancel-nodes".to_string();
    {
        let mut s = legacy.lock().await;
        s.update_task(
            &task_id,
            "InProgress",
            vec![
                subtask("st-a", &task_id, SubtaskStatus::Pending, vec![]),
                subtask("st-b", &task_id, SubtaskStatus::Pending, vec!["st-a"]),
                subtask("st-c", &task_id, SubtaskStatus::Pending, vec!["st-a"]),
                subtask(
                    "st-d",
                    &task_id,
                    SubtaskStatus::Pending,
                    vec!["st-b", "st-c"],
                ),
            ],
            "granular cancel e2e (nodes)",
            "p1",
        )
        .expect("diamond upsert");
    }
    // Wait for the fire-and-forget mirror to publish the root as READY --
    // the precondition `schedule_attempt` documents.
    expect_node_states(&graph, &legacy, &task_id, &[("st-a", "READY")]).await;

    let a_attempt = graph
        .schedule_attempt(&task_id, "st-a", Some("w1"))
        .await
        .expect("schedule a")
        .expect("st-a READY");
    assert!(
        graph
            .commit_once(&task_id, "st-a", &a_attempt, Some("done-a"))
            .await
            .expect("commit a"),
        "first commit must win"
    );
    graph
        .schedule_attempt(&task_id, "st-b", Some("w2"))
        .await
        .expect("schedule b")
        .expect("st-b READY after a's commit");
    {
        let mut s = legacy.lock().await;
        s.update_subtask_status(&task_id, "st-a", SubtaskStatus::Completed);
        s.update_subtask_status(&task_id, "st-b", SubtaskStatus::InProgress);
    }
    settle().await;

    // Closure of b: b itself + d (d depends on b AND c — it's a dependent).
    let closure = sink
        .downstream_closure(&task_id, &["st-b".to_string()])
        .await;
    let mut closure = closure;
    closure.sort();
    assert_eq!(
        closure,
        vec!["st-b".to_string(), "st-d".to_string()],
        "the closure contains the root and its transitive dependents only"
    );

    // Node-level cancel over the closure.
    let cancelled = sink.cancel_nodes(&task_id, &closure).await;
    let mut cancelled = cancelled;
    cancelled.sort();
    assert_eq!(
        cancelled,
        vec!["st-b".to_string(), "st-d".to_string()],
        "exactly the live closure nodes were cancelled"
    );
    assert_eq!(
        graph.node_state(&task_id, "st-b").await.expect("node read"),
        Some("CANCELLED".to_string()),
        "cancelled is terminal for the in-flight branch"
    );
    assert_eq!(
        graph.node_state(&task_id, "st-d").await.expect("node read"),
        Some("CANCELLED".to_string()),
        "the dependent is cancelled with the root"
    );
    assert_eq!(
        graph.node_state(&task_id, "st-c").await.expect("node read"),
        Some("READY".to_string()),
        "the sibling outside the closure is untouched"
    );
    assert_eq!(
        graph.node_state(&task_id, "st-a").await.expect("node read"),
        Some("SUCCEEDED".to_string()),
        "the committed ancestor is immutable"
    );

    // Legacy mirror: live rows of the cancelled closure go Failed (T4
    // mapping); the sibling stays Pending; the completed row is untouched.
    {
        let mut s = legacy.lock().await;
        let moved = s.fail_subtasks(&task_id, &["st-b".to_string(), "st-d".to_string()]);
        assert_eq!(
            moved, 2,
            "b (InProgress) and d (Pending) mirrored to Failed"
        );
    }
    {
        let s = legacy.lock().await;
        let t = s.get_task(&task_id).expect("task present");
        let status_of = |id: &str| {
            t.subtasks
                .iter()
                .find(|x| x.id.0 == id)
                .unwrap()
                .status
                .clone()
        };
        assert_eq!(status_of("st-a"), SubtaskStatus::Completed);
        assert_eq!(status_of("st-b"), SubtaskStatus::Failed);
        assert_eq!(status_of("st-d"), SubtaskStatus::Failed);
        assert_eq!(
            status_of("st-c"),
            SubtaskStatus::Pending,
            "sibling untouched"
        );
    }

    // Re-cancelling a terminal node is a no-op (terminal guard).
    let again = sink.cancel_nodes(&task_id, &["st-b".to_string()]).await;
    assert!(again.is_empty(), "terminal nodes are skipped");

    drop(legacy);
    drop(graph);
    drop_force(probe).await;
}
