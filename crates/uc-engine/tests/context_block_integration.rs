//! Integration tests for the T10 context-block read path (#652 / D10 #647).
//!
//! Needs a **real PostgreSQL** — same conventions as
//! `graph_store_integration.rs` / `merge_grant_integration.rs`:
//! `UC_PG_URL_TEST` at 127.0.0.1; no reachable Postgres ⇒ loud `SKIP:`;
//! probe databases dropped `WITH (FORCE)`; keep `--test-threads=1`.
//!
//! ```text
//! cargo test -p uc-engine --features storage -- --ignored --test-threads=1 context_block
//! ```

#![cfg(feature = "storage")]

use sqlx::postgres::PgPoolOptions;
use uc_engine::GraphStore;

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
    format!("cb{short}")
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

/// Seed one graph row + node rows. `specs` = (node_id, state, result_ref).
async fn seed_graph(store: &GraphStore, graph_id: &str, specs: &[(&str, &str, Option<&str>)]) {
    sqlx::query(
        "INSERT INTO execution_graphs (graph_id, project_id, status) VALUES ($1, 'p-test', 'InProgress')",
    )
    .bind(graph_id)
    .execute(store.pool().as_ref())
    .await
    .expect("insert graph row");
    for (node_id, state, result_ref) in specs {
        sqlx::query("INSERT INTO graph_nodes (graph_id, node_id, state) VALUES ($1, $2, $3)")
            .bind(graph_id)
            .bind(node_id)
            .bind(state)
            .execute(store.pool().as_ref())
            .await
            .expect("insert node row");
        if let Some(res) = result_ref {
            sqlx::query(
                "INSERT INTO node_completions (node_id, graph_id, winning_attempt_id, result_ref) \
                 VALUES ($1, $2, 'a-1', $3)",
            )
            .bind(node_id)
            .bind(graph_id)
            .bind(res)
            .execute(store.pool().as_ref())
            .await
            .expect("insert completion row");
        }
    }
}

fn find<'a>(entries: &'a [uc_types::ContextEntry], node_id: &str) -> &'a uc_types::ContextEntry {
    entries
        .iter()
        .find(|e| e.node_id == node_id)
        .unwrap_or_else(|| panic!("entry for {node_id} missing"))
}

#[tokio::test]
#[ignore]
async fn context_block_dep_outputs_reflect_committed_graph_state() {
    let db = format!("uc_git_{}", unique_prefix());
    if create_probe_db(&db).await.is_none() {
        return;
    }
    let store = connect_probe(&db).await;
    let graph_id = format!("g-{}", &db[7..]);

    // n-1: succeeded with a committed summary; n-2: succeeded without any
    // completion row (no commit yet); n-3: failed (no completion row —
    // commit_once only records wins); n-9: unknown to the plane.
    seed_graph(
        &store,
        &graph_id,
        &[
            ("n-1", "SUCCEEDED", Some("built the auth module")),
            ("n-2", "SUCCEEDED", None),
            ("n-3", "FAILED", None),
        ],
    )
    .await;

    let deps: Vec<String> = ["n-1", "n-2", "n-3", "n-9"]
        .iter()
        .map(|s| s.to_string())
        .collect();
    let entries = store
        .committed_dep_outputs(&graph_id, &deps)
        .await
        .expect("read committed outputs");

    let n1 = find(&entries, "n-1");
    assert!(n1.success);
    assert_eq!(n1.summary, "built the auth module");

    let n2 = find(&entries, "n-2");
    assert!(n2.success);
    assert_eq!(n2.summary, "");

    let n3 = find(&entries, "n-3");
    assert!(!n3.success, "FAILED node must not read as success");
    assert_eq!(
        n3.summary, "",
        "FAILED node has no completion row — empty summary"
    );

    assert!(
        !entries.iter().any(|e| e.node_id == "n-9"),
        "nodes unknown to the graph plane contribute no entry"
    );

    // Empty dep set → empty read (no query needed).
    assert!(store
        .committed_dep_outputs(&graph_id, &[])
        .await
        .expect("empty read")
        .is_empty());

    drop_force(&db).await;
}
