//! PostgreSQL migrations for the scheduler tables.
//!
//! Creates the `scheduled_tasks` and `execution_history` tables
//! required for schedule persistence and recovery.

#[cfg(feature = "storage")]
use sqlx::postgres::PgPool;
#[cfg(feature = "storage")]
use std::sync::Arc;
#[cfg(feature = "storage")]
use uc_types::EngineError;

/// Run scheduler-related database migrations.
///
/// Creates the `scheduled_tasks` and `execution_history` tables
/// if they do not already exist, along with supporting indexes.
#[cfg(feature = "storage")]
pub async fn run_migrations(pool: &Arc<PgPool>) -> Result<(), EngineError> {
    // Create scheduled_tasks table
    sqlx::query(
        r#"
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id UUID PRIMARY KEY,
            description TEXT NOT NULL,
            project_id TEXT,
            cron_expression TEXT,
            execute_after TIMESTAMPTZ,
            night_window_start TIME NOT NULL,
            night_window_end TIME NOT NULL,
            timezone TEXT DEFAULT 'UTC',
            enabled BOOLEAN DEFAULT TRUE,
            last_execution TIMESTAMPTZ,
            next_execution TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        "#,
    )
    .execute(pool.as_ref())
    .await
    .map_err(|e| {
        EngineError::ConnectionError(format!("Migration error (scheduled_tasks): {}", e))
    })?;

    // Create execution_history table
    sqlx::query(
        r#"
        CREATE TABLE IF NOT EXISTS execution_history (
            id UUID PRIMARY KEY,
            scheduled_task_id UUID REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
            started_at TIMESTAMPTZ NOT NULL,
            completed_at TIMESTAMPTZ,
            status TEXT NOT NULL,
            result_summary TEXT,
            deferred_reason TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        "#,
    )
    .execute(pool.as_ref())
    .await
    .map_err(|e| {
        EngineError::ConnectionError(format!("Migration error (execution_history): {}", e))
    })?;

    // Create indexes
    let indexes = [
        "CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_next ON scheduled_tasks(next_execution) WHERE enabled = TRUE",
        "CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_cron ON scheduled_tasks(cron_expression) WHERE cron_expression IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_execution_history_task ON execution_history(scheduled_task_id)",
        "CREATE INDEX IF NOT EXISTS idx_execution_history_status ON execution_history(status)",
    ];

    for idx_sql in &indexes {
        sqlx::query(idx_sql)
            .execute(pool.as_ref())
            .await
            .map_err(|e| EngineError::ConnectionError(format!("Index creation error: {}", e)))?;
    }

    tracing::info!("Scheduler database migrations completed");
    Ok(())
}

/// Run scheduler migrations (no-op when storage feature is disabled).
#[cfg(not(feature = "storage"))]
pub async fn run_migrations() -> Result<(), uc_types::EngineError> {
    Ok(())
}
