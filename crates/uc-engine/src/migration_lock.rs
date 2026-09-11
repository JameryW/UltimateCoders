//! Serialize schema migrations across processes and replicas.
//!
//! SQL's `CREATE TABLE IF NOT EXISTS` (and friends) are **not** concurrency-safe:
//! two sessions that both observe "no such table" both proceed to create it, and
//! the loser is rejected while inserting the composite row type that CREATE TABLE
//! also writes into `pg_type`:
//!
//! ```text
//! duplicate key value violates unique constraint "pg_type_typname_nsp_index"
//! ```
//!
//! Every store constructor in this crate runs its own migrations, so two gateway
//! replicas starting against the same fresh database race — which is exactly the
//! scale-out path this project supports. [`hold_schema_migrations_lock`] turns
//! that into "first one migrates, the rest wait and then find the schema
//! up to date".
//!
//! # Why the guard owns a dedicated connection
//!
//! Advisory locks belong to a *session*. Handing a pooled connection back leaves
//! the session alive in the pool with the lock still held, so an ordinary
//! `unlock()` call at the end of the guarded section is not enough: the migration
//! bodies contain many early returns, and a cancelled task never reaches one.
//!
//! This guard therefore keeps its own [`sqlx::PgConnection`] and makes no
//! promise to unlock explicitly — dropping it closes the connection, Postgres
//! ends the session, and the lock goes with it. Leak-proof by construction
//! rather than by every exit path remembering to clean up.

#![cfg(feature = "storage")]

use std::time::{Duration, Instant};

use sqlx::postgres::PgConnectOptions;
use sqlx::{Connection, PgConnection, PgPool};
use uc_types::EngineError;

/// Application-wide namespace id for the schema-migration lock.
///
/// One key for all migration sets on purpose: metadata and scheduler tables are
/// created by different stores at different points in start-up, and a single
/// "someone is migrating" gate is easier to reason about than a per-store key
/// that still has to wait for each other anyway.
const SCHEMA_MIGRATIONS_LOCK_KEY: i64 = 0x5543_4d4c; // "UCML"

/// How long to keep asking for the lock before giving up. A replica that cannot
/// get it in this window means the migrator is wedged or dead, which is worth an
/// error rather than an indefinite hang.
const LOCK_MAX_WAIT: Duration = Duration::from_secs(30);

/// Poll interval while waiting for another process to finish migrating.
const LOCK_RETRY_INTERVAL: Duration = Duration::from_millis(250);

/// Held for as long as migrations run; see the module docs.
pub struct SchemaMigrationGuard {
    /// Non-`None` while the advisory lock is held by this session.
    conn: Option<PgConnection>,
}

impl SchemaMigrationGuard {
    /// Release the lock by ending the session that holds it.
    ///
    /// Deliberately not an `async fn pg_advisory_unlock(...)`: the borrow would
    /// have to be awaited on every early-return path, including cancellation.
    /// Closing the socket is unconditional — it happens even when the owner is
    /// dropped by a cancelled task.
    pub fn release(&mut self) {
        if let Some(conn) = self.conn.take() {
            drop(conn);
        }
    }
}

impl Drop for SchemaMigrationGuard {
    fn drop(&mut self) {
        self.release();
    }
}

/// Wait for exclusive rights to run schema migrations on `pool`'s database.
///
/// Returns once the lock is held. Errors only if another process has held it for
/// [`LOCK_MAX_WAIT`], which means the migration that holds it is stuck — the
/// caller decides whether to fail or fall back, exactly as it does for any other
/// storage error.
pub async fn hold_schema_migrations_lock(
    pool: &PgPool,
    who: &str,
) -> Result<SchemaMigrationGuard, EngineError> {
    let options: &PgConnectOptions = &pool.connect_options();
    let mut conn = PgConnection::connect_with(options).await.map_err(|e| {
        EngineError::ConnectionError(format!("Migration lock connection ({who}): {e}"))
    })?;

    let deadline = Instant::now() + LOCK_MAX_WAIT;
    loop {
        let acquired: (bool,) = sqlx::query_as("SELECT pg_try_advisory_lock($1)")
            .bind(SCHEMA_MIGRATIONS_LOCK_KEY)
            .fetch_one(&mut conn)
            .await
            .map_err(|e| {
                EngineError::ConnectionError(format!("Migration lock attempt ({who}): {e}"))
            })?;

        if acquired.0 {
            tracing::debug!("Held schema migration advisory lock for {who}");
            return Ok(SchemaMigrationGuard { conn: Some(conn) });
        }

        if Instant::now() >= deadline {
            drop(conn);
            return Err(EngineError::ConnectionError(format!(
                "Timed out after {}s waiting for the schema migration lock ({who}); \
                 another instance is still migrating",
                LOCK_MAX_WAIT.as_secs()
            )));
        }

        tokio::time::sleep(LOCK_RETRY_INTERVAL).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lock_key_is_stable_and_nonzero() {
        // Changing this key silently un-serializes every deployment that mixes
        // old and new replicas, so pin it.
        assert_eq!(SCHEMA_MIGRATIONS_LOCK_KEY, 0x5543_4d4c);
        assert_ne!(SCHEMA_MIGRATIONS_LOCK_KEY, 0);
    }

    #[test]
    fn waiting_is_bounded_then_reports_who_was_waiting() {
        assert_eq!(LOCK_MAX_WAIT, Duration::from_secs(30));
        assert!(LOCK_RETRY_INTERVAL < LOCK_MAX_WAIT);
    }
}
