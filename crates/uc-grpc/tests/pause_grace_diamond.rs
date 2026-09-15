//! D6 pause semantics — diamond-graph TS-free end-to-end (T6 #642).
//!
//! Pause → one branch commits while paused → grace expiry hard-stops the
//! still-RUNNING branch through the graph plane (`pause_grace_expired`) →
//! resume re-opens the dispatch gate and the re-armed node schedules again.
//!
//! Real PostgreSQL (graph plane) + in-memory legacy TaskStore wired to the
//! real `GraphStore` as its shadow. Skips visibly (no `#[ignore]` needed to
//! keep the default `cargo test -p uc-grpc --all-features` run green on
//! machines without PG, but the run DOES assert when PG is reachable —
//! gate by `UC_PG_URL_TEST`, default `127.0.0.1:5432/ultimate_coders`).
//!
//! Run explicitly: `cargo test -p uc-grpc --all-features --test pause_grace_diamond -- --ignored`
//! (the `#[ignore]` keeps it out of the unit-count baseline; running it is
//! part of the T6 gate discipline, same as uc-engine's graph integration).

#![cfg(feature = "storage")]

use std::sync::Arc;
use std::time::Duration;

use tokio::sync::Mutex;
use uc_engine::GraphStore;
use uc_grpc::server::{cancel_pause_grace_timer, spawn_pause_grace_timer, TaskStore};
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
    // Provision the throwaway database through a maintenance connection.
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

/// Project the legacy task into the graph plane, the way the gateway would.
///
/// The mirror is best-effort: `TaskStore::persist_task` spawns
/// `shadow_persist` and returns, and T2 keeps that fan-out off the primary
/// path on purpose. It also demonstrably does not land in every environment —
/// the main-only `storage integration tests` job saw an unprojected graph for
/// the entire 5 s of a bounded poll (granular_cancel_e2e.rs:170). A wait
/// cannot repair an unreliable channel, and these tests exist to exercise
/// *graph-plane* verbs, so setup must not depend on it.
///
/// `shadow_persist` is a warn-only wrapper around this exact call, so the
/// graph receives what the sink would have written. The fan-out itself stays
/// covered by the `uc-grpc` unit test
/// `persist_task_fans_out_to_graph_shadow_sink`.
async fn project_legacy_task(graph: &GraphStore, legacy: &Mutex<TaskStore>, task_id: &str) {
    let task = {
        let guard = legacy.lock().await;
        guard.get_task(task_id).cloned().expect("probe task")
    };
    graph
        .upsert_task_shadow(&task)
        .await
        .expect("shadow projection");
}

#[tokio::test]
#[ignore = "needs PostgreSQL; run with --ignored (T6 #642 D6 diamond gate)"]
async fn diamond_pause_commit_branch_grace_hard_stop_resume_redispatch() {
    let probe = "uc_t6_d6_diamond";
    let Some(graph) = connect_probe(probe).await else {
        return; // visible skip already printed
    };
    let graph = Arc::new(graph);

    // Legacy store (in-memory) wired to the REAL graph shadow — the same
    // dual-plane shape the gateway runs with storage enabled.
    let sink: Arc<dyn uc_engine::GraphShadowSink> = graph.clone();
    let mut legacy = TaskStore::new();
    legacy.set_graph_shadow(sink.clone());
    let legacy = Arc::new(Mutex::new(legacy));

    // Diamond: a → (b, c) → d, all legacy-Pending (mirror: READY / CREATED).
    let task_id = "t6-d6-diamond".to_string();
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
            "diamond pause/grace/resume",
            "p1",
        )
        .expect("diamond upsert");
    }
    project_legacy_task(&graph, &legacy, &task_id).await;

    // Branch A: schedule + commit → b/c flip READY (transactional recompute),
    // d stays CREATED (b, c unmet).
    let a_attempt = graph
        .schedule_attempt(&task_id, "st-a", Some("w1"))
        .await
        .expect("schedule a")
        .expect("st-a must be READY after the mirror");
    assert!(
        graph
            .commit_once(&task_id, "st-a", &a_attempt, Some("done-a"), None)
            .await
            .expect("commit a"),
        "the first commit on a fresh attempt must win"
    );
    // Branch B in flight; branch C left READY-undispatched (paused next).
    graph
        .schedule_attempt(&task_id, "st-b", Some("w2"))
        .await
        .expect("schedule b")
        .expect("st-b READY after a's commit");
    // Mirror the legacy rows: a completed, b in flight.
    {
        let mut s = legacy.lock().await;
        s.update_subtask_status(&task_id, "st-a", SubtaskStatus::Completed);
        s.update_subtask_status(&task_id, "st-b", SubtaskStatus::InProgress);
    }
    settle().await;
    assert_eq!(
        graph.node_state(&task_id, "st-d").await.expect("node read"),
        Some("CREATED".to_string()),
        "d stays CREATED until BOTH branches succeed"
    );

    // ── Pause ────────────────────────────────────────────────────────
    {
        let mut s = legacy.lock().await;
        assert!(s.pause_task(&task_id).is_ok(), "pause an InProgress task");
        assert_eq!(
            s.get_ready_subtasks(&task_id).len(),
            0,
            "the paused gate holds: no ready subtasks leave the store while paused"
        );
    }
    let timers: Arc<std::sync::Mutex<std::collections::HashMap<String, tokio::task::AbortHandle>>> =
        Arc::new(std::sync::Mutex::new(std::collections::HashMap::new()));
    spawn_pause_grace_timer(
        task_id.clone(),
        Duration::from_millis(80),
        legacy.clone(),
        timers.clone(),
        uc_grpc::server::no_pause_grace_nats(),
    );
    tokio::time::sleep(Duration::from_millis(400)).await;

    // ── Grace expiry: single-branch commit survives, running branch dies ──
    assert_eq!(
        graph.node_state(&task_id, "st-a").await.expect("node read"),
        Some("SUCCEEDED".to_string()),
        "the committed branch is immutable across pause/grace"
    );
    assert_eq!(
        graph.attempt_states(&task_id, "st-b").await.expect("reads"),
        vec!["FAILED".to_string()],
        "the grace hard stop failed the running branch's attempt"
    );
    assert_eq!(
        graph.node_state(&task_id, "st-b").await.expect("node read"),
        Some("READY".to_string()),
        "budget left (retry 0 of 3): the node re-arms to READY"
    );
    assert_eq!(
        graph.node_state(&task_id, "st-c").await.expect("node read"),
        Some("READY".to_string()),
        "the undispatched branch keeps its READY state"
    );
    {
        let s = legacy.lock().await;
        let t = s.get_task(&task_id).expect("task present");
        assert_eq!(t.status, uc_types::TaskStatus::Paused);
        let b = t.subtasks.iter().find(|x| x.id.0 == "st-b").unwrap();
        assert_eq!(
            b.status,
            SubtaskStatus::Pending,
            "the sweep bridge reverted the in-flight branch to Pending"
        );
        assert!(b.assigned_worker.is_none(), "assignment cleared");
        let a = t.subtasks.iter().find(|x| x.id.0 == "st-a").unwrap();
        assert_eq!(
            a.status,
            SubtaskStatus::Completed,
            "the committed branch's legacy row is untouched"
        );
        assert!(
            s.get_ready_subtasks(&task_id).is_empty(),
            "still paused: the gate keeps holding after the hard stop"
        );
    }

    // ── Resume: gate re-opens, re-armed nodes schedule again ──────────
    {
        let mut s = legacy.lock().await;
        assert!(s.resume_task(&task_id).is_ok());
        assert_eq!(
            s.get_task(&task_id).unwrap().status,
            uc_types::TaskStatus::InProgress
        );
        let mut ready: Vec<String> = s
            .get_ready_subtasks(&task_id)
            .into_iter()
            .map(|st| st.id.0)
            .collect();
        ready.sort();
        assert_eq!(
            ready,
            vec!["st-b".to_string(), "st-c".to_string()],
            "resume re-dispatch seam: both re-armed and undispatched READY nodes go out"
        );
    }
    // Fence discipline: the re-armed node schedules a fresh attempt (epoch
    // bump rides the next schedule — the failed attempt cannot be reused).
    graph
        .schedule_attempt(&task_id, "st-b", Some("w3"))
        .await
        .expect("re-schedule b after resume")
        .expect("the re-armed node accepts a new attempt");
    assert_eq!(
        graph.attempt_states(&task_id, "st-b").await.expect("reads"),
        vec!["FAILED".to_string(), "RUNNING".to_string()],
        "attempt history: failed by grace, then a fresh RUNNING attempt"
    );

    // Cleanup: timers map (already fired) + probe database.
    cancel_pause_grace_timer(&task_id, timers);
    drop(legacy);
    drop(graph);
    drop_force(probe).await;
}
