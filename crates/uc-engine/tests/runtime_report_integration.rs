//! Run explicitly with UC_PG_URL_TEST and --ignored. No silent DB skips.
#![cfg(feature = "storage")]

use uc_engine::runtime_metrics::read_runtime_report;
use uc_engine::GraphStore;

#[tokio::test]
#[ignore = "requires a real PostgreSQL database with permission to run graph migrations"]
async fn postgres_runtime_report_uses_one_graph_and_never_counts_step_usage_twice() {
    let url = std::env::var("UC_PG_URL_TEST").unwrap_or_else(|_| {
        "postgresql://ultimate_coders:ultimate_coders@127.0.0.1:5432/ultimate_coders".into()
    });
    let store = GraphStore::connect(&url)
        .await
        .expect("live PostgreSQL required");
    let graph = format!("report-{}", uuid::Uuid::new_v4());
    let build = format!("{graph}-build");
    let review = format!("{graph}-review");
    let pool = store.pool().as_ref();
    sqlx::query("INSERT INTO execution_graphs (graph_id, status) VALUES ($1, 'SUCCEEDED')")
        .bind(&graph)
        .execute(pool)
        .await
        .expect("graph fixture");
    sqlx::query("INSERT INTO graph_nodes (graph_id, node_id, type, state) VALUES ($1,$2,'subtask','SUCCEEDED'),($1,$3,'review','SUCCEEDED')")
        .bind(&graph).bind(&build).bind(&review).execute(pool).await.expect("node fixtures");
    sqlx::query(
        "INSERT INTO task_attempts (attempt_id,graph_id,node_id,status,retry_no,started_at,finished_at) VALUES \
         ($2 || '-0',$1,$2,'FAILED',0,NOW(),NOW() + INTERVAL '20 milliseconds'), \
         ($2 || '-1',$1,$2,'SUCCEEDED',1,NOW(),NOW() + INTERVAL '60 milliseconds'), \
         ($3 || '-0',$1,$3,'SUCCEEDED',0,NOW(),NOW() + INTERVAL '20 milliseconds')"
    ).bind(&graph).bind(&build).bind(&review).execute(pool).await.expect("attempt fixtures");
    sqlx::query(
        "INSERT INTO execution_events (graph_id,event_type,tokens,cost,payload) VALUES \
         ($1,'node_succeeded',10,0.123456,'{\"steps\":[{\"usage\":{\"input_tokens\":999}}]}'), \
         ($1,'node_succeeded',NULL,NULL,'{}'), \
         ($1,'late_result',999,999,'{}')",
    )
    .bind(&graph)
    .execute(pool)
    .await
    .expect("usage fixtures");
    let first = read_runtime_report(pool, &graph)
        .await
        .expect("read report");
    let repeat = read_runtime_report(pool, &graph)
        .await
        .expect("repeat report");
    let missing = read_runtime_report(pool, "absent' OR '1'='1").await;
    sqlx::query(
        "UPDATE task_attempts SET started_at = NULL WHERE graph_id = $1 AND status = 'FAILED'",
    )
    .bind(&graph)
    .execute(pool)
    .await
    .expect("missing timing fixture");
    let partial = read_runtime_report(pool, &graph)
        .await
        .expect("partial report");
    sqlx::query("DELETE FROM execution_events WHERE graph_id = $1")
        .bind(&graph)
        .execute(pool)
        .await
        .expect("event cleanup");
    sqlx::query("DELETE FROM execution_graphs WHERE graph_id = $1")
        .bind(&graph)
        .execute(pool)
        .await
        .expect("graph cleanup");
    assert_eq!(first.useful_work_ratio, Some(0.6));
    assert_eq!(first.coordination_ratio, Some(0.2));
    assert_eq!(first.activation_inflation, Some(1.5));
    assert_eq!(first.metrics.reported_tokens, Some(10));
    assert_eq!(first.metrics.reported_cost_usd.as_deref(), Some("0.123456"));
    assert_eq!(first.metrics.successful_events, 2);
    assert_eq!(first.metrics.token_reported_events, 1);
    assert_eq!(first.metrics.cost_reported_events, 1);
    assert_eq!(
        serde_json::to_value(first).expect("json"),
        serde_json::to_value(repeat).expect("json")
    );
    assert!(matches!(missing, Err(uc_types::EngineError::NotFound(_))));
    assert_eq!(partial.useful_work_ratio, None);
    assert_eq!(partial.coordination_ratio, None);
    assert_eq!(partial.metrics.timed_terminal_attempts, 2);
}
