//! Read-only operational proxies for a durable execution graph.

use serde::Serialize;

/// Counts and sums from one database snapshot. Usage sums describe only
/// measured successful events, never the total cost of failed/running work.
#[derive(Debug, Default, Serialize)]
pub struct RuntimeMetrics {
    pub attempts: i64,
    pub activated_nodes: i64,
    pub terminal_attempts: i64,
    pub timed_terminal_attempts: i64,
    pub terminal_duration_ms: i64,
    pub useful_duration_ms: i64,
    pub review_duration_ms: i64,
    pub successful_events: i64,
    pub token_reported_events: i64,
    pub cost_reported_events: i64,
    pub reported_tokens: Option<i64>,
    /// Decimal text preserves PostgreSQL NUMERIC precision.
    pub reported_cost_usd: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct RuntimeReport {
    pub schema_version: u32,
    pub graph_id: String,
    pub useful_work_ratio: Option<f64>,
    pub coordination_ratio: Option<f64>,
    pub activation_inflation: Option<f64>,
    pub recommendations: Vec<&'static str>,
    pub metrics: RuntimeMetrics,
}

impl RuntimeMetrics {
    pub fn report(self, graph_id: &str) -> RuntimeReport {
        let complete =
            self.terminal_attempts > 0 && self.timed_terminal_attempts == self.terminal_attempts;
        let time_ratio = |numerator| {
            if complete && self.terminal_duration_ms > 0 {
                Some(numerator as f64 / self.terminal_duration_ms as f64)
            } else {
                None
            }
        };
        let mut recommendations = Vec::new();
        if self.timed_terminal_attempts < self.terminal_attempts {
            recommendations.push("collect_missing_attempt_timings");
        }
        if self.token_reported_events < self.successful_events
            || self.cost_reported_events < self.successful_events
        {
            recommendations.push("complete_successful_usage_reporting");
        }
        if self.attempts > self.activated_nodes {
            recommendations.push("inspect_repeated_activations");
        }
        RuntimeReport {
            schema_version: 1,
            graph_id: graph_id.to_string(),
            useful_work_ratio: time_ratio(self.useful_duration_ms),
            coordination_ratio: time_ratio(self.review_duration_ms),
            activation_inflation: (self.activated_nodes > 0)
                .then(|| self.attempts as f64 / self.activated_nodes as f64),
            recommendations,
            metrics: self,
        }
    }
}

/// Read an existing graph without migrating or modifying the database.
#[cfg(feature = "storage")]
pub async fn read_runtime_report(
    pool: &sqlx::PgPool,
    graph_id: &str,
) -> Result<RuntimeReport, uc_types::EngineError> {
    use sqlx::Row;
    use uc_types::EngineError;
    let storage = |e| EngineError::StorageError(format!("runtime report: {e}"));
    let mut tx = pool.begin().await.map_err(storage)?;
    sqlx::query("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        .execute(&mut *tx)
        .await
        .map_err(storage)?;
    let exists: bool =
        sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM execution_graphs WHERE graph_id = $1)")
            .bind(graph_id)
            .fetch_one(&mut *tx)
            .await
            .map_err(storage)?;
    if !exists {
        return Err(EngineError::NotFound(format!("graph {graph_id}")));
    }
    let row = sqlx::query(
        r#"
        WITH attempts AS (
            SELECT a.node_id, a.status, n.type,
                   a.status IN ('SUCCEEDED', 'FAILED', 'CANCELLED', 'SKIPPED') AS terminal,
                   CASE WHEN a.finished_at >= a.started_at
                        THEN (EXTRACT(EPOCH FROM (a.finished_at - a.started_at)) * 1000)::bigint
                   END AS duration_ms
            FROM task_attempts a
            JOIN graph_nodes n ON n.graph_id = a.graph_id AND n.node_id = a.node_id
            WHERE a.graph_id = $1
        )
        SELECT COUNT(*) AS attempts, COUNT(DISTINCT node_id) AS activated_nodes,
               COUNT(*) FILTER (WHERE terminal) AS terminal_attempts,
               COUNT(duration_ms) FILTER (WHERE terminal) AS timed_terminal_attempts,
               COALESCE(SUM(duration_ms) FILTER (WHERE terminal), 0)::bigint AS terminal_duration_ms,
               COALESCE(SUM(duration_ms) FILTER (WHERE status = 'SUCCEEDED' AND type <> 'review'), 0)::bigint AS useful_duration_ms,
               COALESCE(SUM(duration_ms) FILTER (WHERE terminal AND type = 'review'), 0)::bigint AS review_duration_ms
        FROM attempts
        "#,
    )
    .bind(graph_id)
    .fetch_one(&mut *tx)
    .await
    .map_err(storage)?;
    let usage = sqlx::query(
        r#"
        SELECT COUNT(*) AS successful_events,
               COUNT(tokens) AS token_reported_events,
               COUNT(cost) AS cost_reported_events,
               SUM(tokens)::bigint AS reported_tokens,
               SUM(cost)::text AS reported_cost_usd
        FROM execution_events WHERE graph_id = $1 AND event_type = 'node_succeeded'
        "#,
    )
    .bind(graph_id)
    .fetch_one(&mut *tx)
    .await
    .map_err(storage)?;
    let metrics = RuntimeMetrics {
        attempts: row.try_get("attempts").map_err(storage)?,
        activated_nodes: row.try_get("activated_nodes").map_err(storage)?,
        terminal_attempts: row.try_get("terminal_attempts").map_err(storage)?,
        timed_terminal_attempts: row.try_get("timed_terminal_attempts").map_err(storage)?,
        terminal_duration_ms: row.try_get("terminal_duration_ms").map_err(storage)?,
        useful_duration_ms: row.try_get("useful_duration_ms").map_err(storage)?,
        review_duration_ms: row.try_get("review_duration_ms").map_err(storage)?,
        successful_events: usage.try_get("successful_events").map_err(storage)?,
        token_reported_events: usage.try_get("token_reported_events").map_err(storage)?,
        cost_reported_events: usage.try_get("cost_reported_events").map_err(storage)?,
        reported_tokens: usage.try_get("reported_tokens").map_err(storage)?,
        reported_cost_usd: usage.try_get("reported_cost_usd").map_err(storage)?,
    };
    tx.commit().await.map_err(storage)?;
    Ok(metrics.report(graph_id))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_and_incompletely_timed_graphs_have_no_time_ratios() {
        let empty = RuntimeMetrics::default().report("empty");
        assert_eq!(empty.useful_work_ratio, None);
        assert_eq!(empty.coordination_ratio, None);
        assert_eq!(empty.activation_inflation, None);
        let partial = RuntimeMetrics {
            attempts: 2,
            activated_nodes: 1,
            terminal_attempts: 2,
            timed_terminal_attempts: 1,
            terminal_duration_ms: 60,
            useful_duration_ms: 60,
            ..Default::default()
        }
        .report("partial");
        assert_eq!(partial.useful_work_ratio, None);
        assert_eq!(partial.coordination_ratio, None);
        assert_eq!(partial.activation_inflation, Some(2.0));
        assert_eq!(
            partial.recommendations,
            vec![
                "collect_missing_attempt_timings",
                "inspect_repeated_activations"
            ]
        );
    }

    #[test]
    fn measured_zero_is_distinct_from_absent_usage() {
        let report = RuntimeMetrics {
            terminal_attempts: 1,
            timed_terminal_attempts: 1,
            successful_events: 1,
            token_reported_events: 1,
            cost_reported_events: 1,
            reported_tokens: Some(0),
            reported_cost_usd: Some("0.000000".into()),
            ..Default::default()
        }
        .report("zero");
        assert_eq!(report.useful_work_ratio, None);
        assert_eq!(report.metrics.reported_tokens, Some(0));
        assert_eq!(
            report.metrics.reported_cost_usd.as_deref(),
            Some("0.000000")
        );
        assert!(report.recommendations.is_empty());
        let json = serde_json::to_value(RuntimeMetrics::default().report("empty"))
            .expect("report serializes");
        assert!(json["useful_work_ratio"].is_null());
        assert!(json["metrics"]["reported_tokens"].is_null());
    }

    #[test]
    fn worked_graph_exposes_ratios_and_partial_measurements() {
        let report = RuntimeMetrics {
            attempts: 3,
            activated_nodes: 2,
            terminal_attempts: 3,
            timed_terminal_attempts: 3,
            terminal_duration_ms: 100,
            useful_duration_ms: 60,
            review_duration_ms: 20,
            ..Default::default()
        }
        .report("g");
        assert_eq!(report.useful_work_ratio, Some(0.6));
        assert_eq!(report.coordination_ratio, Some(0.2));
        assert_eq!(report.activation_inflation, Some(1.5));
        assert_eq!(report.metrics.reported_tokens, None);
        assert_eq!(report.metrics.reported_cost_usd, None);
    }
}
