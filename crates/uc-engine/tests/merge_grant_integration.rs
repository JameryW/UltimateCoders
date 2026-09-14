//! Integration tests for the T9 merge-barrier grant store (#651 / D9 #646
//! on top of D5 #634).
//!
//! These need a **real PostgreSQL** (same conventions as
//! `graph_store_integration.rs`): `UC_PG_URL_TEST` must point at 127.0.0.1;
//! no reachable Postgres ⇒ every test prints `SKIP:` and returns; probe
//! databases are dropped `WITH (FORCE)`; keep `--test-threads=1`.
//!
//! ```text
//! cargo test -p uc-engine --features storage -- --ignored --test-threads=1 merge_grant
//! ```

#![cfg(feature = "storage")]

use sqlx::postgres::PgPoolOptions;
use uc_engine::GraphStore;
use uc_types::{sha256_hex, MergeOutcomeReport};

fn pg_test_url() -> String {
    std::env::var("UC_PG_URL_TEST").unwrap_or_else(|_| {
        "postgresql://ultimate_coders:ultimate_coders@127.0.0.1:5432/ultimate_coders".to_string()
    })
}

fn url_for_database(base: &str, database: &str) -> String {
    let split = base.rfind('/').expect("URL has a database segment");
    format!("{}{}", &base[..=split], database)
}

fn unique_prefix() -> String {
    let id = uuid::Uuid::new_v4();
    let short: String = id.to_string().replace('-', "").chars().take(8).collect();
    format!("mg{short}")
}

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

async fn connect_probe(name: &str) -> GraphStore {
    let store = GraphStore::connect(&url_for_database(&pg_test_url(), name))
        .await
        .expect("connect to the fresh probe database should succeed");
    assert!(store.is_connected().await);
    store
}

async fn drop_force(name: &str) {
    let pool = PgPoolOptions::new()
        .max_connections(1)
        .connect(&pg_test_url())
        .await
        .expect("cleanup connection");
    sqlx::query(&format!("DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        .execute(&pool)
        .await
        .expect("cleanup: DROP DATABASE WITH (FORCE)");
    pool.close().await;
}

/// Seed one graph row + `states` node rows (node ids `n-1..n`, 1-based).
/// SUCCEEDED nodes get a `node_completions` row with `result_ref = out-i`.
async fn seed_graph(store: &GraphStore, graph_id: &str, states: &[&str]) {
    sqlx::query(
        "INSERT INTO execution_graphs (graph_id, project_id, status) VALUES ($1, 'p-test', 'InProgress')",
    )
    .bind(graph_id)
    .execute(store.pool().as_ref())
    .await
    .expect("insert graph row");
    for (i, state) in states.iter().enumerate() {
        let node_id = format!("n-{}", i + 1);
        sqlx::query("INSERT INTO graph_nodes (graph_id, node_id, state) VALUES ($1, $2, $3)")
            .bind(graph_id)
            .bind(&node_id)
            .bind(state)
            .execute(store.pool().as_ref())
            .await
            .expect("insert node row");
        if *state == "SUCCEEDED" {
            sqlx::query(
                "INSERT INTO node_completions (node_id, graph_id, winning_attempt_id, result_ref) \
                 VALUES ($1, $2, 'a-1', $3)",
            )
            .bind(&node_id)
            .bind(graph_id)
            .bind(format!("out-{}", i + 1))
            .execute(store.pool().as_ref())
            .await
            .expect("insert completion row");
        }
    }
}

fn outcome(status: &str) -> MergeOutcomeReport {
    MergeOutcomeReport {
        status: status.to_string(),
        merged_branches: vec!["uc/subtask/aa".to_string()],
        conflict_branches: Vec::new(),
        push_status: "pushed".to_string(),
    }
}

#[tokio::test]
#[ignore]
async fn merge_grant_refused_on_non_quiescent_graph() {
    let db = format!("uc_git_{}", unique_prefix());
    if create_probe_db(&db).await.is_none() {
        return;
    }
    let store = connect_probe(&db).await;
    let graph_id = format!("g-{}", &db[7..]);

    // A RUNNING node ⇒ not quiescent ⇒ no grant.
    seed_graph(&store, &graph_id, &["SUCCEEDED", "RUNNING"]).await;
    let decision = store.issue_merge_grant(&graph_id).await.expect("grant");
    assert!(!decision.granted, "non-quiescent graph must be refused");
    assert!(decision.merge_idempotency_key.is_empty());
    assert!(decision.error.contains("not quiescent"));

    drop_force(&db).await;
}

#[tokio::test]
#[ignore]
async fn merge_grant_quiescent_graph_issues_then_consumed_replay_is_noop() {
    let db = format!("uc_git_{}", unique_prefix());
    if create_probe_db(&db).await.is_none() {
        return;
    }
    let store = connect_probe(&db).await;
    let graph_id = format!("g-{}", &db[7..]);

    seed_graph(&store, &graph_id, &["SUCCEEDED", "SUCCEEDED", "FAILED"]).await;
    let d1 = store.issue_merge_grant(&graph_id).await.expect("grant 1");
    assert!(d1.granted);
    assert!(!d1.idempotent_replay);
    assert_eq!(d1.merge_idempotency_key.len(), 32);

    // Re-issue before consumption (crash-recovery path): same key, not replay.
    let d2 = store.issue_merge_grant(&graph_id).await.expect("grant 2");
    assert!(d2.granted && !d2.idempotent_replay);
    assert_eq!(d2.merge_idempotency_key, d1.merge_idempotency_key);

    // Consume, then replay the consumed key → granted=true + replay=true.
    let rep = store
        .report_merge_outcome(&graph_id, &d1.merge_idempotency_key, &outcome("merged"))
        .await
        .expect("report 1");
    assert!(rep.accepted && !rep.idempotent_replay);

    let d3 = store.issue_merge_grant(&graph_id).await.expect("grant 3");
    assert!(d3.granted && d3.idempotent_replay);
    assert_eq!(d3.merge_idempotency_key, d1.merge_idempotency_key);

    // Consumed-key report replay → accepted=true, idempotent_replay=true (no-op).
    let rep2 = store
        .report_merge_outcome(&graph_id, &d1.merge_idempotency_key, &outcome("merged"))
        .await
        .expect("report 2");
    assert!(rep2.accepted && rep2.idempotent_replay);

    // The stored outcome is the FIRST report (replay wrote nothing).
    let stored: Option<(Option<serde_json::Value>,)> =
        sqlx::query_as("SELECT outcome FROM merge_grants WHERE graph_id = $1")
            .bind(&graph_id)
            .fetch_optional(store.pool().as_ref())
            .await
            .expect("read outcome");
    let (json,) = stored.expect("grant row exists");
    assert_eq!(json.expect("outcome json")["push_status"], "pushed");

    drop_force(&db).await;
}

#[tokio::test]
#[ignore]
async fn merge_report_unknown_or_superseded_key_is_rejected() {
    let db = format!("uc_git_{}", unique_prefix());
    if create_probe_db(&db).await.is_none() {
        return;
    }
    let store = connect_probe(&db).await;
    let graph_id = format!("g-{}", &db[7..]);

    // No grant row at all → unknown key.
    let rep_none = store
        .report_merge_outcome(&graph_id, "does-not-exist", &outcome("merged"))
        .await
        .expect("report none");
    assert!(!rep_none.accepted && !rep_none.idempotent_replay);

    // Superseded key: issue a grant, then re-issue with a DIFFERENT graph
    // state (one fewer SUCCEEDED node ⇒ different key) — the old key's row
    // was replaced, so its report loses like a late commit_once loser.
    seed_graph(&store, &graph_id, &["SUCCEEDED", "SUCCEEDED"]).await;
    let first = store.issue_merge_grant(&graph_id).await.expect("first");
    assert!(first.granted);

    sqlx::query("DELETE FROM node_completions WHERE graph_id = $1 AND node_id = 'n-2'")
        .bind(&graph_id)
        .execute(store.pool().as_ref())
        .await
        .expect("drop completion");
    sqlx::query("UPDATE graph_nodes SET state = 'FAILED' WHERE graph_id = $1 AND node_id = 'n-2'")
        .bind(&graph_id)
        .execute(store.pool().as_ref())
        .await
        .expect("demote node");

    let second = store.issue_merge_grant(&graph_id).await.expect("second");
    assert!(second.granted && second.merge_idempotency_key != first.merge_idempotency_key);

    // The stale aggregation presents the OLD key → rejected.
    let rep_stale = store
        .report_merge_outcome(&graph_id, &first.merge_idempotency_key, &outcome("merged"))
        .await
        .expect("stale report");
    assert!(!rep_stale.accepted);

    // The current key's report is accepted.
    let rep_current = store
        .report_merge_outcome(&graph_id, &second.merge_idempotency_key, &outcome("merged"))
        .await
        .expect("current report");
    assert!(rep_current.accepted);

    drop_force(&db).await;
}

#[tokio::test]
#[ignore]
async fn merge_grant_key_binds_succeeded_set_and_output_hashes() {
    let db = format!("uc_git_{}", unique_prefix());
    if create_probe_db(&db).await.is_none() {
        return;
    }
    let store = connect_probe(&db).await;
    let graph_id = format!("g-{}", &db[7..]);

    seed_graph(&store, &graph_id, &["SUCCEEDED", "SUCCEEDED"]).await;
    let d = store.issue_merge_grant(&graph_id).await.expect("grant");

    // The key must equal the cross-language derivation over the SUCCEEDED
    // set + sha256(result_ref) pairs (out-1 → n-1, out-2 → n-2).
    let s1 = sha256_hex(b"out-1");
    let s2 = sha256_hex(b"out-2");
    let preimage = format!("merge:{graph_id}:n-1,n-2:n-1={s1};n-2={s2}");
    let expected = sha256_hex(preimage.as_bytes())[..32].to_string();
    assert_eq!(d.merge_idempotency_key, expected);

    // Changing ONLY an output hash (same node set) must move the key.
    sqlx::query(
        "UPDATE node_completions SET result_ref = 'out-X' WHERE graph_id = $1 AND node_id = 'n-1'",
    )
    .bind(&graph_id)
    .execute(store.pool().as_ref())
    .await
    .expect("mutate output");
    let d2 = store.issue_merge_grant(&graph_id).await.expect("grant 2");
    assert_ne!(d2.merge_idempotency_key, d.merge_idempotency_key);

    drop_force(&db).await;
}
