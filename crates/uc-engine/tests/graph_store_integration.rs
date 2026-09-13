//! Integration tests for the T2 graph-state row tables (#638).
//!
//! These need a **real PostgreSQL** — the graph store has no in-memory
//! fallback (`GraphStore::connect` errors when the DB is unreachable), which
//! also means every green run here genuinely touched a database. Run:
//!
//! ```text
//! docker compose -f docker/docker-compose.yml up -d --wait postgres
//! UC_PG_URL_TEST=postgresql://ultimate_coders:ultimate_coders@127.0.0.1:5432/ultimate_coders \
//!   cargo test -p uc-engine --features storage -- --ignored --test-threads=1 graph
//! ```
//!
//! Conventions pinned by the research note + existing `storage_integration.rs`:
//! - `UC_PG_URL_TEST` must point at **127.0.0.1**, not `localhost` (IPv6
//!   stalls ~10s per connection).
//! - No reachable Postgres ⇒ every test prints `SKIP: …` and returns — the
//!   skip is visible in the output, never a silent green.
//! - Probe databases are dropped with `WITH (FORCE)` (a lingering idle pool
//!   backend would otherwise hang the DROP).
//! - Keep `--test-threads=1`: `CREATE TABLE IF NOT EXISTS` is not
//!   concurrency-safe on a cold database (pg_type_typname_nsp_index loser);
//!   the migration-race test inside this file is serialized by the #631
//!   advisory lock itself, so it is meaningful regardless of thread count.
//! - Each test uses a unique id prefix, deletes its own rows at start (rerun
//!   on a warm DB stays deterministic) and cleans up after itself.

#![cfg(feature = "storage")]

use std::collections::HashMap;

use sqlx::postgres::PgPoolOptions;
use uc_engine::GraphStore;
use uc_types::{
    DispatchMode, Subtask, SubtaskResult, SubtaskStatus, Task, TaskId, TaskStatus, WorkerId,
};

// ── Environment / gating helpers ─────────────────────────────────────

fn pg_test_url() -> String {
    std::env::var("UC_PG_URL_TEST").unwrap_or_else(|_| {
        "postgresql://ultimate_coders:ultimate_coders@127.0.0.1:5432/ultimate_coders".to_string()
    })
}

/// Swap the database name in a `postgresql://user:pass@host:port/db` URL.
fn url_for_database(base: &str, database: &str) -> String {
    let split = base.rfind('/').expect("URL has a database segment");
    format!("{}{}", &base[..=split], database)
}

fn unique_prefix() -> String {
    let id = uuid::Uuid::new_v4();
    let short: String = id.to_string().replace('-', "").chars().take(8).collect();
    format!("gt{short}")
}

/// `TIMESTAMPTZ` text rendering inside the determinism dumps: convert to UTC
/// explicitly so the session TimeZone can never leak into the comparison.
fn ts_expr(col: &str) -> String {
    format!("coalesce(to_char({col} AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US'), '-')")
}

/// Visible-skip guard: `connect_or_skip` returns `None` (after a loud `SKIP:`
/// line) when no Postgres is reachable. On `Some`, `is_connected()` is
/// asserted so a dead server can never produce a false green.
async fn connect_or_skip(tag: &str) -> Option<GraphStore> {
    let url = pg_test_url();
    match GraphStore::connect(&url).await {
        Ok(store) => {
            assert!(
                store.is_connected().await,
                "GraphStore::connect returned Ok but the pool cannot query — \
                 this integration test would silently verify nothing"
            );
            Some(store)
        }
        Err(e) => {
            eprintln!("SKIP {tag}: no PostgreSQL reachable at {url} ({e}) — set UC_PG_URL_TEST");
            None
        }
    }
}

/// Create a throwaway probe database (cold — nothing migrated yet). `None` =
/// visible skip (no reachable server).
async fn create_probe_db(name: &str) -> Option<bool> {
    let maintenance = match PgPoolOptions::new()
        .max_connections(1)
        .connect(&pg_test_url())
        .await
    {
        Ok(pool) => pool,
        Err(e) => {
            eprintln!(
                "SKIP probe-db: no PostgreSQL reachable at {} ({e}) — set UC_PG_URL_TEST",
                pg_test_url()
            );
            return None;
        }
    };
    let created = sqlx::query(&format!("CREATE DATABASE {name}"))
        .execute(&maintenance)
        .await
        .is_ok();
    drop(maintenance);
    if !created {
        panic!("CREATE DATABASE {name} failed on a reachable server");
    }
    Some(true)
}

/// A `GraphStore` migrated onto a fresh probe database.
async fn connect_probe(name: &str) -> GraphStore {
    let store = GraphStore::connect(&url_for_database(&pg_test_url(), name))
        .await
        .expect("connect to the fresh probe database should succeed");
    assert!(
        store.is_connected().await,
        "probe GraphStore must actually talk to the probe database"
    );
    store
}

async fn drop_force(name: &str) {
    let pool = PgPoolOptions::new()
        .max_connections(1)
        .connect(&pg_test_url())
        .await
        .expect("cleanup connection");
    // DROP with FORCE: a lingering idle backend would otherwise hang a plain
    // DROP DATABASE on the very connection that issued it.
    sqlx::query(&format!("DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        .execute(&pool)
        .await
        .expect("cleanup: DROP DATABASE WITH (FORCE)");
    pool.close().await;
}

// ── Fixture builders ─────────────────────────────────────────────────

fn ts(sec: i64) -> chrono::DateTime<chrono::Utc> {
    chrono::DateTime::from_timestamp(sec, 0).expect("in-range test timestamp")
}

/// Rust-shape subtask (what the `tasks.subtasks` JSONB actually stores).
/// A `Completed` subtask carries a `result` (worker + completed_at), the
/// other states carry none — mirroring the live gateway shape.
fn subtask(
    id: &str,
    parent: &str,
    status: SubtaskStatus,
    depends: Vec<String>,
    retry: u32,
) -> Subtask {
    Subtask {
        id: TaskId(id.to_string()),
        parent_id: TaskId(parent.to_string()),
        description: format!("step {id}"),
        status: status.clone(),
        assigned_worker: matches!(
            status,
            SubtaskStatus::Assigned | SubtaskStatus::InProgress | SubtaskStatus::Completed
        )
        .then(|| WorkerId("w1".to_string())),
        depends_on: depends.into_iter().map(TaskId).collect(),
        file_constraints: Vec::new(),
        expected_output: String::new(),
        result: (status == SubtaskStatus::Completed).then(|| SubtaskResult {
            subtask_id: TaskId(id.to_string()),
            worker_id: WorkerId("w1".to_string()),
            modified_files: Vec::new(),
            summary: "done".to_string(),
            success: true,
            completed_at: ts(1_700_000_000),
            result: None,
        }),
        dispatch_mode: DispatchMode::default(),
        dispatch_retry_count: 0,
        retry_count: retry,
        required_capabilities: vec!["rust".to_string()],
        agent_config_json: None,
        steps: Vec::new(),
    }
}

fn rust_task(id: &str, status: TaskStatus, subtasks: Vec<Subtask>) -> Task {
    Task {
        id: TaskId(id.to_string()),
        description: "t2 integration fixture".to_string(),
        project_id: format!("{id}-proj"),
        status,
        subtasks,
        created_at: ts(1_700_000_100),
        updated_at: ts(1_700_000_200),
    }
}

/// The legacy `tasks` table DDL (mirrors `PostgresTaskBackend::run_migrations`)
/// so a graph-only test database always has the source-A shape to insert into.
async fn ensure_tasks_table(store: &GraphStore) {
    sqlx::query(
        r#"
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            project_id TEXT NOT NULL,
            status TEXT NOT NULL,
            subtasks JSONB NOT NULL DEFAULT '[]',
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        )
        "#,
    )
    .execute(store.pool().as_ref())
    .await
    .expect("tasks table DDL (mirror of task_store.rs)");
}

async fn insert_tasks_row(store: &GraphStore, task: &Task, status_str: &str) {
    let subtasks_json = serde_json::to_value(&task.subtasks).expect("subtasks serialize");
    sqlx::query(
        "INSERT INTO tasks (id, description, project_id, status, subtasks, created_at, updated_at) \
         VALUES ($1, $2, $3, $4, $5, $6, $7)",
    )
    .bind(&task.id.0)
    .bind(&task.description)
    .bind(&task.project_id)
    .bind(status_str)
    .bind(subtasks_json)
    .bind(task.created_at)
    .bind(task.updated_at)
    .execute(store.pool().as_ref())
    .await
    .expect("fixture row into tasks");
}

async fn delete_tasks_rows(store: &GraphStore, ids: &[String]) {
    for id in ids {
        sqlx::query("DELETE FROM tasks WHERE id = $1")
            .bind(id)
            .execute(store.pool().as_ref())
            .await
            .expect("tasks fixture cleanup");
    }
}

/// Remove this fixture's graphs (nodes/attempts/completions cascade via FK).
async fn purge_graphs(store: &GraphStore, ids: &[String]) {
    for id in ids {
        sqlx::query("DELETE FROM execution_graphs WHERE graph_id = $1")
            .bind(id)
            .execute(store.pool().as_ref())
            .await
            .expect("execution_graphs cleanup");
    }
}

/// Deterministic dump of the five graph tables as `|`-joined text, either
/// full-database (probe DBs, determinism test) or scoped to one fixture's
/// graph ids (tests sharing the default DB with parallel neighbours).
/// `result_ref` is **excluded** (PRD: compare the projection of everything
/// except that column); every other column participates, so a drift in a
/// default, a constraint-visible value, or an order fails the comparison.
async fn dump_graphs(store: &GraphStore, ids: Option<&[String]>) -> String {
    use sqlx::Row;

    let w = if ids.is_some() {
        " WHERE graph_id = ANY($1)"
    } else {
        ""
    };
    let queries = [
        format!(
            "SELECT concat_ws('|', graph_id, project_id, status, version::text, imported::text, {}, {}) \
             FROM execution_graphs{w} ORDER BY graph_id",
            ts_expr("created_at"),
            ts_expr("updated_at")
        ),
        format!(
            "SELECT concat_ws('|', graph_id, node_id, type, state, dependencies::text, \
             dependency_policy, scope_id, input_refs::text, output_refs::text, priority::text, \
             {}, optional::text, effect_class, required_capabilities::text, version::text) \
             FROM graph_nodes{w} ORDER BY graph_id, node_id",
            ts_expr("deadline")
        ),
        format!(
            "SELECT concat_ws('|', attempt_id, graph_id, node_id, coalesce(worker_id,'-'), \
             coalesce(worker_epoch::text,'-'), status, retry_no::text, {}, {}, {}) \
             FROM task_attempts{w} ORDER BY attempt_id",
            ts_expr("started_at"),
            ts_expr("heartbeat_at"),
            ts_expr("finished_at")
        ),
        format!(
            "SELECT concat_ws('|', node_id, graph_id, winning_attempt_id, {}) \
             FROM node_completions{w} ORDER BY node_id",
            ts_expr("committed_at")
        ),
        format!(
            "SELECT concat_ws('|', seq::text, graph_id, coalesce(node_id,'-'), event_type) \
             FROM execution_events{w} ORDER BY seq",
        ),
    ];
    let mut out = String::new();
    for q in &queries {
        let mut builder = sqlx::query(q);
        if let Some(ids) = ids {
            builder = builder.bind(ids);
        }
        let rows = builder
            .fetch_all(store.pool().as_ref())
            .await
            .expect("dump query");
        for row in &rows {
            let line: String = row.try_get(0).expect("dump row is text");
            out.push_str(&line);
            out.push('\n');
        }
        out.push_str("#\n"); // table separator: cross-table shifts can't hide
    }
    out
}

async fn dump_graph_tables(store: &GraphStore) -> String {
    dump_graphs(store, None).await
}

async fn dump_scope(store: &GraphStore, ids: &[String]) -> String {
    dump_graphs(store, Some(ids)).await
}

// ── 1. Migration surface (five tables, constraints, reserved columns) ──

#[tokio::test]
#[ignore]
async fn graph_migration_creates_five_tables_with_reserved_columns() {
    let name = format!("uc_git_{}", unique_prefix());
    if create_probe_db(&name).await.is_none() {
        return;
    }
    let store = connect_probe(&name).await;

    let tables: Vec<(String,)> = sqlx::query_as(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() ORDER BY tablename",
    )
    .fetch_all(store.pool().as_ref())
    .await
    .expect("pg_tables");
    let names: Vec<&str> = tables.iter().map(|(n,)| n.as_str()).collect();
    for expected in [
        "execution_graphs",
        "graph_nodes",
        "node_completions",
        "task_attempts",
        "execution_events",
    ] {
        assert!(
            names.contains(&expected),
            "migration must create {expected}, found: {names:?}"
        );
    }

    // Reserved columns (the assessment's leftover-risk closure points):
    // cost/tokens/duration_ms on execution_events, effect_class default on
    // graph_nodes, worker_epoch on task_attempts.
    let cols: Vec<(String, String)> = sqlx::query_as(
        "SELECT column_name, data_type FROM information_schema.columns \
         WHERE table_schema = current_schema() AND table_name = 'execution_events'",
    )
    .fetch_all(store.pool().as_ref())
    .await
    .expect("information_schema");
    let col_map: HashMap<&str, &str> = cols.iter().map(|(n, t)| (n.as_str(), t.as_str())).collect();
    assert_eq!(col_map.get("cost").copied(), Some("numeric"));
    assert_eq!(col_map.get("tokens").copied(), Some("bigint"));
    assert_eq!(col_map.get("duration_ms").copied(), Some("bigint"));
    assert_eq!(col_map.get("seq").copied(), Some("bigint")); // BIGSERIAL

    let (effect_default,): (Option<String>,) = sqlx::query_as(
        "SELECT column_default FROM information_schema.columns \
         WHERE table_schema = current_schema() AND table_name = 'graph_nodes' \
           AND column_name = 'effect_class'",
    )
    .fetch_one(store.pool().as_ref())
    .await
    .expect("effect_class default");
    assert_eq!(effect_default.as_deref(), Some("'requires_worker'::text"));

    let (epoch_type,): (String,) = sqlx::query_as(
        "SELECT data_type FROM information_schema.columns \
         WHERE table_schema = current_schema() AND table_name = 'task_attempts' \
           AND column_name = 'worker_epoch'",
    )
    .fetch_one(store.pool().as_ref())
    .await
    .expect("worker_epoch column exists");
    assert_eq!(epoch_type, "bigint");

    // Unique constraints: UNIQUE(node_id, retry_no) on attempts and the
    // commit-once node_id PK on completions (schema-only guarantee in T2).
    let unique_index_columns = r#"
        SELECT string_agg(a.attname, ',' ORDER BY array_position(i.indkey, a.attnum))
        FROM pg_index i
        JOIN pg_class r ON r.oid = i.indrelid
        JOIN pg_attribute a ON a.attrelid = r.oid AND a.attnum = ANY(i.indkey)
        WHERE r.relname = $1 AND i.indisunique
        GROUP BY i.indexrelid
    "#;
    let attempt_uniques: Vec<(String,)> = sqlx::query_as(unique_index_columns)
        .bind("task_attempts")
        .fetch_all(store.pool().as_ref())
        .await
        .expect("attempt unique indexes");
    assert!(
        attempt_uniques
            .iter()
            .any(|(cols,)| cols == "node_id,retry_no"),
        "task_attempts must carry UNIQUE(node_id, retry_no), got: {attempt_uniques:?}"
    );

    let completion_pks: Vec<(String,)> = sqlx::query_as(unique_index_columns)
        .bind("node_completions")
        .fetch_all(store.pool().as_ref())
        .await
        .expect("completion unique indexes");
    assert!(
        completion_pks.iter().any(|(cols,)| cols == "node_id"),
        "node_completions.node_id must be unique/PK (commit-once schema), got: {completion_pks:?}"
    );

    // Idempotent migration: re-running over an already-migrated DB is a no-op.
    uc_engine::graph_store::run_migrations(store.pool())
        .await
        .expect("graph migrations must be idempotent");

    drop(store);
    drop_force(&name).await;
}

// ── 2. Source A: PG tasks JSONB backfill (mapping + idempotency) ──────

#[tokio::test]
#[ignore]
async fn graph_backfill_source_a_maps_status_and_is_idempotent() {
    let Some(store) = connect_or_skip("graph_backfill_a").await else {
        return;
    };
    ensure_tasks_table(&store).await;

    let p = unique_prefix();
    let id = format!("{p}-a");
    purge_graphs(&store, std::slice::from_ref(&id)).await;
    delete_tasks_rows(&store, std::slice::from_ref(&id)).await;

    let n1 = format!("{p}-n1");
    let n2 = format!("{p}-n2");
    let n3 = format!("{p}-n3");
    let n4 = format!("{p}-n4");
    let n5 = format!("{p}-n5");
    let n6 = format!("{p}-n6");
    let task = rust_task(
        &id,
        TaskStatus::InProgress,
        vec![
            subtask(&n1, &id, SubtaskStatus::Completed, vec![], 1),
            subtask(&n2, &id, SubtaskStatus::Assigned, vec![n1.clone()], 0),
            subtask(&n3, &id, SubtaskStatus::InProgress, vec![], 0),
            subtask(&n4, &id, SubtaskStatus::Pending, vec![], 0),
            subtask(&n5, &id, SubtaskStatus::Failed, vec![], 2),
            subtask(&n6, &id, SubtaskStatus::Conflicted, vec![], 0),
        ],
    );
    insert_tasks_row(&store, &task, "InProgress").await;

    let stats1 = store
        .backfill_from_tasks_table()
        .await
        .expect("source A backfill");
    assert!(stats1.graphs >= 1, "run 1 must insert our graph row");

    // PRD status mapping, per node row.
    let states: Vec<(String, String)> = sqlx::query_as(
        "SELECT node_id, state FROM graph_nodes WHERE graph_id = $1 ORDER BY node_id",
    )
    .bind(&id)
    .fetch_all(store.pool().as_ref())
    .await
    .expect("node states");
    let mut want: Vec<(String, String)> = vec![
        (n1.clone(), "SUCCEEDED".to_string()),
        (n2.clone(), "RUNNING".to_string()),
        (n3.clone(), "RUNNING".to_string()),
        (n4.clone(), "READY".to_string()),
        (n5.clone(), "FAILED".to_string()),
        (n6.clone(), "FAILED".to_string()),
    ];
    want.sort();
    assert_eq!(
        states, want,
        "Completed→SUCCEEDED, Assigned/InProgress→RUNNING, Pending→READY, Failed/Conflicted→FAILED"
    );

    // Graph row: version=1, imported=true, RUNNING (InProgress→RUNNING).
    let graph: Option<(String, i64, bool)> = sqlx::query_as(
        "SELECT status, version, imported FROM execution_graphs WHERE graph_id = $1",
    )
    .bind(&id)
    .fetch_optional(store.pool().as_ref())
    .await
    .expect("graph row");
    assert_eq!(graph, Some(("RUNNING".to_string(), 1, true)));

    // Attempts: none for READY; Completed keeps retry_no=1; RUNNING rows carry
    // started_at; the completion row exists in the SAME projection as SUCCEEDED.
    let attempts: Vec<(String, String, i32, bool, bool)> = sqlx::query_as(
        "SELECT node_id, status, retry_no, started_at IS NOT NULL, finished_at IS NOT NULL \
         FROM task_attempts WHERE graph_id = $1 ORDER BY node_id",
    )
    .bind(&id)
    .fetch_all(store.pool().as_ref())
    .await
    .expect("attempts");
    assert_eq!(attempts.len(), 5, "READY must have no attempt row");
    let by_node: HashMap<String, (String, i32, bool, bool)> = attempts
        .iter()
        .map(|(node, status, retry, started, finished)| {
            (node.clone(), (status.clone(), *retry, *started, *finished))
        })
        .collect();
    let (status, retry, started, finished) = by_node[&n1].clone();
    assert_eq!(status, "SUCCEEDED");
    assert_eq!(retry, 1, "retry_count must land in retry_no");
    assert!(
        started && finished,
        "completed attempt has started+finished"
    );
    let (status, _, started, finished) = by_node[&n3].clone();
    assert_eq!(status, "RUNNING");
    assert!(
        started,
        "InProgress/Assigned→RUNNING attempt carries started_at"
    );
    assert!(!finished, "a running attempt has not finished");
    assert!(by_node.contains_key(&n2), "Assigned gets an attempt row");
    assert!(by_node.contains_key(&n5));
    assert!(by_node.contains_key(&n6), "Conflicted→FAILED attempt");
    assert!(!by_node.contains_key(&n4));

    let (completions,): (i64,) =
        sqlx::query_as("SELECT count(*) FROM node_completions WHERE graph_id = $1")
            .bind(&id)
            .fetch_one(store.pool().as_ref())
            .await
            .expect("completion count");
    assert_eq!(completions, 1, "exactly the Completed node commits once");

    // Double-run idempotency: same DB, second run inserts nothing, counts are
    // byte-identical (PRD: 同库跑两遍行数不变). Scoped to our graph so a
    // parallel test on the shared database cannot move the dump.
    let dump1 = dump_scope(&store, std::slice::from_ref(&id)).await;
    let stats2 = store
        .backfill_from_tasks_table()
        .await
        .expect("second backfill run");
    assert_eq!(stats2.graphs, 0, "re-run must insert no graph rows");
    assert_eq!(stats2.nodes, 0, "re-run must insert no node rows");
    assert_eq!(stats2.attempts, 0, "re-run must insert no attempt rows");
    assert_eq!(
        stats2.completions, 0,
        "re-run must insert no completion rows"
    );
    let dump2 = dump_scope(&store, std::slice::from_ref(&id)).await;
    assert_eq!(
        dump1, dump2,
        "backfill re-run must leave every graph table unchanged"
    );

    delete_tasks_rows(&store, std::slice::from_ref(&id)).await;
    purge_graphs(&store, &[id]).await;
}

// ── 3. Source B: .uc/tasks import (opt-in dir, newer-checkpoint, counts) ──

/// Build a fixture `.uc` dir: `tasks/<id>.json` + a strictly-newer
/// `checkpoints/<id>.snap.json` for the second task. Returns the dir and the
/// two graph ids.
fn build_uc_fixture_dir(prefix: &str) -> (std::path::PathBuf, Vec<String>) {
    let root = std::env::temp_dir().join(format!("uc_graph_fix_{prefix}"));
    let _ = std::fs::remove_dir_all(&root);
    let tasks = root.join("tasks");
    let checkpoints = root.join("checkpoints");
    std::fs::create_dir_all(&tasks).expect("fixture tasks dir");
    std::fs::create_dir_all(&checkpoints).expect("fixture checkpoints dir");

    let id_a = format!("{prefix}-a");
    let task_a = serde_json::json!({
        "id": id_a,
        "description": "mixed statuses",
        "status": "in_progress",
        "controlState": { "resumeFromWave": 1 },
        "projectId": "proj-x",
        "createdAt": 1700000100000i64,
        "savedAt": 1700000200000i64,
        "subtasks": [
            { "id": format!("{prefix}-n1"), "status": "completed", "dependsOn": [],
              "retryCount": 1, "completedAt": 1700000000000i64,
              "requiredCapabilities": ["rust"] },
            { "id": format!("{prefix}-n2"), "status": "assigned",
              "dependsOn": [format!("{prefix}-n1")], "startedAt": 1700000150000i64 },
            { "id": format!("{prefix}-n3"), "status": "pending", "dependsOn": [] },
            { "id": format!("{prefix}-n4"), "status": "failed", "dependsOn": [] },
            { "id": format!("{prefix}-n5"), "status": "cancelled", "dependsOn": [] },
            { "id": format!("{prefix}-n6"), "status": "conflicted", "dependsOn": [] },
        ]
    });
    std::fs::write(
        tasks.join(format!("{id_a}.json")),
        serde_json::to_vec_pretty(&task_a).expect("fixture a json"),
    )
    .expect("write task a");

    // The task file says in_progress with one pending node; the strictly
    // newer checkpoint (savedAt 999 > 100) says completed — import must take
    // the checkpoint's content (savedAt-newer rule, mirrors TaskStore F46).
    let id_b = format!("{prefix}-b");
    let task_b = serde_json::json!({
        "id": id_b,
        "status": "in_progress",
        "projectId": "proj-y",
        "createdAt": 1700000300000i64,
        "savedAt": 100i64,
        "subtasks": [ { "id": format!("{prefix}-m1"), "status": "pending", "dependsOn": [] } ]
    });
    let checkpoint_b = serde_json::json!({
        "id": id_b,
        "status": "completed",
        "projectId": "proj-y",
        "createdAt": 1700000300000i64,
        "savedAt": 999i64,
        "subtasks": [ { "id": format!("{prefix}-m1"), "status": "completed",
                        "dependsOn": [], "completedAt": 1700000400000i64 } ]
    });
    std::fs::write(
        tasks.join(format!("{id_b}.json")),
        serde_json::to_vec_pretty(&task_b).expect("fixture b json"),
    )
    .expect("write task b");
    std::fs::write(
        checkpoints.join(format!("{id_b}.snap.json")),
        serde_json::to_vec_pretty(&checkpoint_b).expect("checkpoint b json"),
    )
    .expect("write checkpoint b");

    (root, vec![id_a, id_b])
}

async fn assert_source_b_content(store: &GraphStore, prefix: &str, ids: &[String]) {
    let (graphs,): (i64,) =
        sqlx::query_as("SELECT count(*) FROM execution_graphs WHERE graph_id = ANY($1)")
            .bind(ids)
            .fetch_one(store.pool().as_ref())
            .await
            .expect("imported graph count");
    assert_eq!(graphs, 2);

    let (nodes,): (i64,) =
        sqlx::query_as("SELECT count(*) FROM graph_nodes WHERE graph_id = ANY($1)")
            .bind(ids)
            .fetch_one(store.pool().as_ref())
            .await
            .expect("imported node count");
    assert_eq!(nodes, 7, "6 nodes in a, 1 in b");

    let (attempts,): (i64,) =
        sqlx::query_as("SELECT count(*) FROM task_attempts WHERE graph_id = ANY($1)")
            .bind(ids)
            .fetch_one(store.pool().as_ref())
            .await
            .expect("imported attempt count");
    assert_eq!(attempts, 6, "a: n1,n2,n4,n5,n6 (n3 READY none) + b: m1");

    let (completions,): (i64,) =
        sqlx::query_as("SELECT count(*) FROM node_completions WHERE graph_id = ANY($1)")
            .bind(ids)
            .fetch_one(store.pool().as_ref())
            .await
            .expect("imported completion count");
    assert_eq!(completions, 2, "a/n1 + b/m1 (checkpoint-completed)");

    // TS camelCase statuses → node states, including cancelled→CANCELLED.
    let states: HashMap<String, String> =
        sqlx::query_as("SELECT node_id, state FROM graph_nodes WHERE graph_id = ANY($1)")
            .bind(ids)
            .fetch_all(store.pool().as_ref())
            .await
            .expect("node states")
            .into_iter()
            .collect();
    assert_eq!(states[&format!("{prefix}-n1")], "SUCCEEDED");
    assert_eq!(states[&format!("{prefix}-n2")], "RUNNING");
    assert_eq!(states[&format!("{prefix}-n3")], "READY");
    assert_eq!(states[&format!("{prefix}-n4")], "FAILED");
    assert_eq!(states[&format!("{prefix}-n5")], "CANCELLED");
    assert_eq!(states[&format!("{prefix}-n6")], "FAILED");
    assert_eq!(
        states[&format!("{prefix}-m1")],
        "SUCCEEDED",
        "the newer checkpoint must replace the task file's pending node"
    );

    // Every imported graph: version=1, imported=TRUE.
    let versions: Vec<(String, i64, bool)> = sqlx::query_as(
        "SELECT graph_id, version, imported FROM execution_graphs WHERE graph_id = ANY($1) \
         ORDER BY graph_id",
    )
    .bind(ids)
    .fetch_all(store.pool().as_ref())
    .await
    .expect("imported graph metadata");
    assert_eq!(versions.len(), 2);
    for (gid, version, imported) in &versions {
        assert_eq!(*version, 1, "imported graph {gid} must start at version 1");
        assert!(
            *imported,
            "imported graph {gid} must carry the imported marker"
        );
    }
}

#[tokio::test]
#[ignore]
async fn graph_import_source_b_honors_newer_checkpoint_and_is_idempotent() {
    let Some(store) = connect_or_skip("graph_import_b").await else {
        return;
    };
    let p = unique_prefix();
    let (dir, ids) = build_uc_fixture_dir(&p);
    purge_graphs(&store, &ids).await;

    let stats = store.import_tasks_dir(&dir).await.expect("source B import");
    assert_eq!(stats.graphs, 2, "both fixture graphs must be imported");
    assert_source_b_content(&store, &p, &ids).await;

    // Re-import: files exist but PG already has the graphs — never touched
    // again (D2 constraint 1), zero inserts, byte-identical dump.
    let dump1 = dump_scope(&store, &ids).await;
    let stats2 = store
        .import_tasks_dir(&dir)
        .await
        .expect("source B re-import");
    assert_eq!(stats2.skipped_existing, 2);
    assert_eq!(stats2.graphs, 0);
    let dump2 = dump_scope(&store, &ids).await;
    assert_eq!(dump1, dump2, "re-import must not mutate any graph row");

    purge_graphs(&store, &ids).await;
    std::fs::remove_dir_all(&dir).ok();
}

// ── 4. Determinism: same input, two fresh databases ──────────────────

#[tokio::test]
#[ignore]
async fn graph_import_is_deterministic_across_two_fresh_databases() {
    let name_a = format!("uc_git_{}", unique_prefix());
    let name_b = format!("uc_git_{}", unique_prefix());
    if create_probe_db(&name_a).await.is_none() {
        return;
    }
    if create_probe_db(&name_b).await.is_none() {
        drop_force(&name_a).await;
        return;
    }

    let p = unique_prefix();
    let (dir, ids) = build_uc_fixture_dir(&p);

    let store_a = connect_probe(&name_a).await;
    let store_b = connect_probe(&name_b).await;
    store_a
        .import_tasks_dir(&dir)
        .await
        .expect("import into probe db A");
    store_b
        .import_tasks_dir(&dir)
        .await
        .expect("import into probe db B");
    assert_source_b_content(&store_a, &p, &ids).await;

    let dump_a = dump_graph_tables(&store_a).await;
    let dump_b = dump_graph_tables(&store_b).await;
    assert!(dump_a.contains(&p), "dump must contain our fixture rows");
    assert_eq!(
        dump_a, dump_b,
        "same input must produce byte-identical tables (all columns but result_ref)"
    );

    purge_graphs(&store_a, &ids).await;
    purge_graphs(&store_b, &ids).await;
    std::fs::remove_dir_all(&dir).ok();
    drop(store_a);
    drop(store_b);
    drop_force(&name_a).await;
    drop_force(&name_b).await;
}

// ── 5. Concurrent cold-start migration (advisory-lock #631 pattern) ──

/// `CREATE TABLE IF NOT EXISTS` is NOT concurrency-safe: two sessions that
/// both see "no such table" both proceed and the loser dies on
/// `pg_type_typname_nsp_index`. The `hold_schema_migrations_lock(pool,
/// "graph")` guard must serialize N simultaneous first boots. Concurrency is
/// `tokio::join!`, so this is meaningful regardless of `--test-threads`.
#[tokio::test]
#[ignore]
async fn graph_concurrent_cold_start_migrations_are_serialized() {
    let name = format!("uc_git_{}", unique_prefix());
    if create_probe_db(&name).await.is_none() {
        return;
    }

    let url = url_for_database(&pg_test_url(), &name);
    let four = tokio::join!(
        GraphStore::connect(&url),
        GraphStore::connect(&url),
        GraphStore::connect(&url),
        GraphStore::connect(&url),
    );

    let mut stores = Vec::new();
    let mut failures = Vec::new();
    for (index, result) in [four.0, four.1, four.2, four.3].into_iter().enumerate() {
        match result {
            Ok(store) => stores.push(store),
            Err(error) => failures.push(format!("replica {index}: {error}")),
        }
    }
    // Release every pool attached to the probe database before dropping it.
    drop(stores);
    drop_force(&name).await;

    assert!(
        failures.is_empty(),
        "concurrent cold-start graph migrations must serialize; failures: {failures:?}"
    );
}

// ── 6. Shadow write + warn-only diff ─────────────────────────────────

#[tokio::test]
#[ignore]
async fn graph_shadow_diff_is_warn_only_and_never_rewrites() {
    let Some(store) = connect_or_skip("graph_shadow_diff").await else {
        return;
    };
    let p = unique_prefix();
    let id = format!("{p}-s1");
    purge_graphs(&store, std::slice::from_ref(&id)).await;

    let n1 = format!("{p}-n1");
    let n2 = format!("{p}-n2");
    let task = rust_task(
        &id,
        TaskStatus::InProgress,
        vec![
            subtask(&n1, &id, SubtaskStatus::Completed, vec![], 0),
            subtask(&n2, &id, SubtaskStatus::Pending, vec![], 0),
        ],
    );
    store
        .upsert_task_shadow(&task)
        .await
        .expect("shadow upsert");

    // Shadow mirror of the authoritative entry: no diff.
    let same = store.shadow_diff(&task).await.expect("shadow diff");
    assert!(
        same.is_empty(),
        "faithful shadow must diff empty, got {same:?}"
    );
    let (imported, version): (bool, i64) =
        sqlx::query_as("SELECT imported, version FROM execution_graphs WHERE graph_id = $1")
            .bind(&id)
            .fetch_one(store.pool().as_ref())
            .await
            .expect("shadow graph row");
    assert!(!imported, "shadow writes are not imports");
    assert_eq!(version, 1);

    // Fabricate a divergence: different task status, different node statuses,
    // and an extra in-memory node that the row tables never saw.
    let mut diverged = task.clone();
    diverged.status = TaskStatus::Failed;
    diverged.subtasks[0].status = SubtaskStatus::Failed;
    diverged.subtasks[1].status = SubtaskStatus::Completed;
    diverged.subtasks.push(subtask(
        &format!("{p}-n9"),
        &id,
        SubtaskStatus::Pending,
        vec![],
        0,
    ));

    let diffs = store.shadow_diff(&diverged).await.expect("shadow diff");
    assert!(
        diffs.len() >= 3,
        "expected status + node divergences, got {diffs:?}"
    );
    assert!(
        diffs.iter().any(|d| d.contains("status")),
        "graph status diff missing: {diffs:?}"
    );
    assert!(
        diffs.iter().any(|d| d.contains(&format!("{p}-n9"))),
        "missing node must be reported: {diffs:?}"
    );

    // Warn-only: the diff read must NOT have rewritten anything.
    let (status,): (String,) =
        sqlx::query_as("SELECT status FROM execution_graphs WHERE graph_id = $1")
            .bind(&id)
            .fetch_one(store.pool().as_ref())
            .await
            .expect("graph status after diff");
    assert_eq!(status, "RUNNING", "shadow_diff is read-only: no write-back");
    let (n1_state,): (String,) =
        sqlx::query_as("SELECT state FROM graph_nodes WHERE graph_id = $1 AND node_id = $2")
            .bind(&id)
            .bind(&n1)
            .fetch_one(store.pool().as_ref())
            .await
            .expect("n1 state");
    assert_eq!(n1_state, "SUCCEEDED", "row tables keep the pre-diff state");
    let n9_exists: Option<(String,)> =
        sqlx::query_as("SELECT node_id FROM graph_nodes WHERE graph_id = $1 AND node_id = $2")
            .bind(&id)
            .bind(format!("{p}-n9"))
            .fetch_optional(store.pool().as_ref())
            .await
            .expect("n9 probe");
    assert!(
        n9_exists.is_none(),
        "the diff must not materialize memory-only nodes"
    );

    purge_graphs(&store, &[id]).await;
}

// ── 7. Cross-backend ordering consistency ────────────────────────────

/// The row tables' `ORDER BY created_at DESC, graph_id` must agree with the
/// in-memory projection's `(created_at desc, id asc)` — including the
/// created_at tie that the tasks table's own ORDER BY leaves to `id`. Only a
/// cross-backend test catches this class of drift (assessment lesson).
#[tokio::test]
#[ignore]
async fn graph_row_order_matches_in_memory_order() {
    let Some(store) = connect_or_skip("graph_order").await else {
        return;
    };
    ensure_tasks_table(&store).await;

    let p = unique_prefix();
    // Same created_at for three rows (forces the tie-break), one older row.
    let fixtures = [
        (format!("{p}-b"), ts(1_700_000_500)),
        (format!("{p}-a"), ts(1_700_000_500)),
        (format!("{p}-c"), ts(1_700_000_500)),
        (format!("{p}-old"), ts(1_699_999_000)),
    ];
    let ids: Vec<String> = fixtures.iter().map(|(id, _)| id.clone()).collect();
    purge_graphs(&store, &ids).await;
    delete_tasks_rows(&store, &ids).await;

    for (id, created) in &fixtures {
        let mut task = rust_task(id, TaskStatus::Planning, Vec::new());
        task.created_at = *created;
        task.updated_at = *created;
        insert_tasks_row(&store, &task, "Planning").await;
    }
    store
        .backfill_from_tasks_table()
        .await
        .expect("backfill for ordering test");

    let listed = store
        .list_graph_ids_newest_first()
        .await
        .expect("row-table order");
    let mine: Vec<String> = listed
        .into_iter()
        .filter(|g| ids.iter().any(|i| i == g))
        .collect();

    // In-memory projection of the same source rows, stable sort.
    let mut memory: Vec<(String, chrono::DateTime<chrono::Utc>)> = fixtures.into_iter().collect();
    memory.sort_by(|(ida, ca), (idb, cb)| cb.cmp(ca).then_with(|| ida.cmp(idb)));
    let expected: Vec<String> = memory.into_iter().map(|(id, _)| id).collect();

    assert_eq!(
        mine, expected,
        "row-table ORDER BY and the in-memory sort must agree, ties included"
    );

    delete_tasks_rows(&store, &ids).await;
    purge_graphs(&store, &ids).await;
}
