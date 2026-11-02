//! Namespaced, versioned record store — the durability base for the governance
//! plane.
//!
//! ## Why one generic store instead of a table per entity
//!
//! The repo already has three persistence mechanisms (`tasks` table, scheduler
//! table, graph rows) plus the Python `runtime_state.py` document store. Adding
//! six more tables for submissions, outbox entries, cases, mandates, triggers and
//! proposals would add six migrations, six dialects of boilerplate, and six
//! chances for the two halves of the system to disagree about durability
//! semantics.
//!
//! Instead every governance entity is a **JSON document with a namespace, a key
//! and a CAS version**, which is exactly the shape `python/ultimate_coders/
//! runtime_state.py` uses (SQLite and PostgreSQL interchangeable). The stores in
//! this module layer typed access on top; the SQL is one table.
//!
//! ## The CAS contract
//!
//! * `expected_version = None` — unconditional upsert (create-or-replace).
//! * `expected_version = Some(0)` — create only; fails if the key exists.
//! * `expected_version = Some(v)` — update only if the stored version is exactly
//!   `v`; otherwise [`RecordError::Conflict`] carries the actual version.
//!
//! Every successful write bumps `version`. Readers get the version back, which is
//! what lets `submit_once` and case transitions be compare-and-swap rather than
//! read-modify-write races.

use std::collections::HashMap;
use std::sync::RwLock;

use async_trait::async_trait;
use chrono::{DateTime, Utc};
use serde::de::DeserializeOwned;
use serde::Serialize;

/// Table backing the PostgreSQL implementation.
pub const GOVERNANCE_RECORDS_TABLE: &str = "governance_records";

/// One stored document.
#[derive(Debug, Clone, PartialEq)]
pub struct Record {
    pub namespace: String,
    pub key: String,
    /// Monotonic CAS token, starting at 1 for the first write.
    pub version: u64,
    pub value: serde_json::Value,
    pub updated_at: DateTime<Utc>,
}

/// Failure modes of the record store.
#[derive(Debug, Clone, PartialEq)]
pub enum RecordError {
    /// The compare-and-swap precondition failed.
    Conflict {
        expected: u64,
        /// `None` means no record exists (e.g. "create only" against an absent
        /// key would be `Some(0)` expected; the actual side is always `Some`
        /// here because a conflict means a record existed).
        actual: Option<u64>,
    },
    /// The backing store refused or failed the operation.
    Backend(String),
    /// A stored document could not be decoded into the requested type.
    Decode { key: String, detail: String },
    /// A value could not be encoded.
    Encode { key: String, detail: String },
}

impl std::fmt::Display for RecordError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RecordError::Conflict { expected, actual } => match actual {
                Some(actual) => write!(
                    f,
                    "record version conflict: expected v{expected}, found v{actual}"
                ),
                None => write!(f, "record version conflict: expected v{expected}, found nothing"),
            },
            RecordError::Backend(detail) => write!(f, "record store backend error: {detail}"),
            RecordError::Decode { key, detail } => {
                write!(f, "cannot decode record {key}: {detail}")
            }
            RecordError::Encode { key, detail } => write!(f, "cannot encode record {key}: {detail}"),
        }
    }
}

impl std::error::Error for RecordError {}

/// Durable, namespaced JSON documents with compare-and-swap versions.
#[async_trait]
pub trait RecordStore: Send + Sync {
    /// Whether records survive a process restart. The in-memory implementation
    /// reports `false` rather than pretending; callers that need durability must
    /// surface this to the operator.
    fn is_durable(&self) -> bool;

    async fn get(&self, namespace: &str, key: &str) -> Result<Option<Record>, RecordError>;

    /// Write a document with CAS semantics. See the module docs for the three
    /// `expected_version` modes.
    async fn put(
        &self,
        namespace: &str,
        key: &str,
        value: serde_json::Value,
        expected_version: Option<u64>,
    ) -> Result<Record, RecordError>;

    /// List a namespace in key order, bounded by `limit`.
    async fn list(&self, namespace: &str, limit: usize) -> Result<Vec<Record>, RecordError>;

    async fn delete(&self, namespace: &str, key: &str) -> Result<bool, RecordError>;
}

impl std::fmt::Debug for dyn RecordStore {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("RecordStore")
            .field("durable", &self.is_durable())
            .finish_non_exhaustive()
    }
}

// ── In-memory implementation ────────────────────────────────────────────

/// Process-local store. Used by tests and by gateway runs without a database.
///
/// A `std::sync::RwLock` (not tokio) is deliberate: every operation is a short
/// map access with no `await` inside the critical section, so the lock is never
/// held across a suspension point.
#[derive(Debug, Default)]
pub struct InMemoryRecordStore {
    inner: RwLock<HashMap<(String, String), Record>>,
}

impl InMemoryRecordStore {
    pub fn new() -> Self {
        Self::default()
    }

    /// Number of stored documents (diagnostics/tests).
    pub fn len(&self) -> usize {
        self.inner.read().map(|map| map.len()).unwrap_or_default()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

#[async_trait]
impl RecordStore for InMemoryRecordStore {
    fn is_durable(&self) -> bool {
        false
    }

    async fn get(&self, namespace: &str, key: &str) -> Result<Option<Record>, RecordError> {
        let guard = self
            .inner
            .read()
            .map_err(|_| RecordError::Backend("record store lock poisoned".to_string()))?;
        Ok(guard
            .get(&(namespace.to_string(), key.to_string()))
            .cloned())
    }

    async fn put(
        &self,
        namespace: &str,
        key: &str,
        value: serde_json::Value,
        expected_version: Option<u64>,
    ) -> Result<Record, RecordError> {
        let mut guard = self
            .inner
            .write()
            .map_err(|_| RecordError::Backend("record store lock poisoned".to_string()))?;
        let map_key = (namespace.to_string(), key.to_string());
        let existing = guard.get(&map_key);
        match (expected_version, existing) {
            (Some(expected), Some(current)) if current.version != expected => {
                return Err(RecordError::Conflict {
                    expected,
                    actual: Some(current.version),
                });
            }
            (Some(expected), None) if expected != 0 => {
                return Err(RecordError::Conflict {
                    expected,
                    actual: None,
                });
            }
            _ => {}
        }
        let version = existing.map(|record| record.version + 1).unwrap_or(1);
        let record = Record {
            namespace: namespace.to_string(),
            key: key.to_string(),
            version,
            value,
            updated_at: Utc::now(),
        };
        guard.insert(map_key, record.clone());
        Ok(record)
    }

    async fn list(&self, namespace: &str, limit: usize) -> Result<Vec<Record>, RecordError> {
        let guard = self
            .inner
            .read()
            .map_err(|_| RecordError::Backend("record store lock poisoned".to_string()))?;
        let mut records: Vec<Record> = guard
            .values()
            .filter(|record| record.namespace == namespace)
            .cloned()
            .collect();
        records.sort_by(|a, b| a.key.cmp(&b.key));
        records.truncate(limit);
        Ok(records)
    }

    async fn delete(&self, namespace: &str, key: &str) -> Result<bool, RecordError> {
        let mut guard = self
            .inner
            .write()
            .map_err(|_| RecordError::Backend("record store lock poisoned".to_string()))?;
        Ok(guard
            .remove(&(namespace.to_string(), key.to_string()))
            .is_some())
    }
}

// ── PostgreSQL implementation ───────────────────────────────────────────

/// DDL for the single governance table. `IF NOT EXISTS` keeps boot idempotent
/// alongside the existing gateway migrations.
#[cfg(feature = "storage")]
pub const GOVERNANCE_RECORDS_DDL: &str = r#"
CREATE TABLE IF NOT EXISTS governance_records (
    namespace  TEXT        NOT NULL,
    key        TEXT        NOT NULL,
    version    BIGINT      NOT NULL,
    payload    JSONB       NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace, key)
);
"#;

/// PostgreSQL-backed store (feature `storage`).
///
/// One table, one index (the primary key), no cross-row transactions needed:
/// every write is a single `INSERT ... ON CONFLICT ... WHERE version = $expected`
/// whose `RETURNING` clause doubles as the CAS verdict.
#[cfg(feature = "storage")]
pub struct PostgresRecordStore {
    pool: std::sync::Arc<sqlx::PgPool>,
    schema: tokio::sync::OnceCell<()>,
}

#[cfg(feature = "storage")]
impl PostgresRecordStore {
    /// Connect using the gateway's `UC_DATABASE_URL`.
    pub async fn connect(url: &str, max_connections: u32) -> Result<Self, RecordError> {
        let pool = sqlx::postgres::PgPoolOptions::new()
            .max_connections(max_connections.max(1))
            .connect(url)
            .await
            .map_err(|error| RecordError::Backend(format!("postgres connect: {error}")))?;
        Ok(Self::with_pool(std::sync::Arc::new(pool)))
    }

    pub fn with_pool(pool: std::sync::Arc<sqlx::PgPool>) -> Self {
        Self {
            pool,
            schema: tokio::sync::OnceCell::new(),
        }
    }

    async fn ensure_schema(&self) -> Result<(), RecordError> {
        self.schema
            .get_or_try_init(|| async {
                sqlx::query(GOVERNANCE_RECORDS_DDL)
                    .execute(self.pool.as_ref())
                    .await
                    .map(|_| ())
                    .map_err(|error| RecordError::Backend(format!("governance DDL: {error}")))
            })
            .await
            .map(|_| ())
    }
}

#[cfg(feature = "storage")]
#[async_trait]
impl RecordStore for PostgresRecordStore {
    fn is_durable(&self) -> bool {
        true
    }

    async fn get(&self, namespace: &str, key: &str) -> Result<Option<Record>, RecordError> {
        self.ensure_schema().await?;
        let row = sqlx::query_as::<_, (i64, sqlx::types::Json<serde_json::Value>, DateTime<Utc>)>(
            "SELECT version, payload, updated_at FROM governance_records \
             WHERE namespace = $1 AND key = $2",
        )
        .bind(namespace)
        .bind(key)
        .fetch_optional(self.pool.as_ref())
        .await
        .map_err(|error| RecordError::Backend(format!("postgres get: {error}")))?;
        Ok(row.map(|(version, payload, updated_at)| Record {
            namespace: namespace.to_string(),
            key: key.to_string(),
            version: version as u64,
            value: payload.0,
            updated_at,
        }))
    }

    async fn put(
        &self,
        namespace: &str,
        key: &str,
        value: serde_json::Value,
        expected_version: Option<u64>,
    ) -> Result<Record, RecordError> {
        self.ensure_schema().await?;
        let row = sqlx::query_as::<_, (i64, DateTime<Utc>)>(
            "INSERT INTO governance_records (namespace, key, version, payload, updated_at) \
             VALUES ($1, $2, 1, $3, now()) \
             ON CONFLICT (namespace, key) DO UPDATE \
               SET version = governance_records.version + 1, \
                   payload = EXCLUDED.payload, \
                   updated_at = now() \
               WHERE $4::bigint IS NULL OR governance_records.version = $4::bigint \
             RETURNING version, updated_at",
        )
        .bind(namespace)
        .bind(key)
        .bind(sqlx::types::Json(value.clone()))
        .bind(expected_version.map(|version| version as i64))
        .fetch_optional(self.pool.as_ref())
        .await
        .map_err(|error| RecordError::Backend(format!("postgres put: {error}")))?;
        match row {
            Some((version, updated_at)) => Ok(Record {
                namespace: namespace.to_string(),
                key: key.to_string(),
                version: version as u64,
                value,
                updated_at,
            }),
            None => {
                // The `WHERE` guard suppressed the update: fetch the winner so
                // the caller can see what it lost to.
                let actual = self.get(namespace, key).await?;
                Err(RecordError::Conflict {
                    expected: expected_version.unwrap_or(0),
                    actual: actual.map(|record| record.version),
                })
            }
        }
    }

    async fn list(&self, namespace: &str, limit: usize) -> Result<Vec<Record>, RecordError> {
        self.ensure_schema().await?;
        let rows = sqlx::query_as::<_, (String, i64, sqlx::types::Json<serde_json::Value>, DateTime<Utc>)>(
            "SELECT key, version, payload, updated_at FROM governance_records \
             WHERE namespace = $1 ORDER BY key ASC LIMIT $2",
        )
        .bind(namespace)
        .bind(limit as i64)
        .fetch_all(self.pool.as_ref())
        .await
        .map_err(|error| RecordError::Backend(format!("postgres list: {error}")))?;
        Ok(rows
            .into_iter()
            .map(|(key, version, payload, updated_at)| Record {
                namespace: namespace.to_string(),
                key,
                version: version as u64,
                value: payload.0,
                updated_at,
            })
            .collect())
    }

    async fn delete(&self, namespace: &str, key: &str) -> Result<bool, RecordError> {
        self.ensure_schema().await?;
        let result = sqlx::query("DELETE FROM governance_records WHERE namespace = $1 AND key = $2")
            .bind(namespace)
            .bind(key)
            .execute(self.pool.as_ref())
            .await
            .map_err(|error| RecordError::Backend(format!("postgres delete: {error}")))?;
        Ok(result.rows_affected() > 0)
    }
}

// ── Typed namespace view ────────────────────────────────────────────────

/// A typed view over one namespace, so stores do not repeat serde plumbing.
#[derive(Clone)]
pub struct NamespacedStore {
    store: std::sync::Arc<dyn RecordStore>,
    namespace: &'static str,
}

impl std::fmt::Debug for NamespacedStore {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("NamespacedStore")
            .field("namespace", &self.namespace)
            .finish_non_exhaustive()
    }
}

impl NamespacedStore {
    pub fn new(store: std::sync::Arc<dyn RecordStore>, namespace: &'static str) -> Self {
        Self { store, namespace }
    }

    pub fn namespace(&self) -> &'static str {
        self.namespace
    }

    pub fn is_durable(&self) -> bool {
        self.store.is_durable()
    }

    pub fn raw(&self) -> &std::sync::Arc<dyn RecordStore> {
        &self.store
    }

    pub async fn get<T: DeserializeOwned>(
        &self,
        key: &str,
    ) -> Result<Option<(T, u64)>, RecordError> {
        let Some(record) = self.store.get(self.namespace, key).await? else {
            return Ok(None);
        };
        let value = serde_json::from_value(record.value).map_err(|error| RecordError::Decode {
            key: key.to_string(),
            detail: error.to_string(),
        })?;
        Ok(Some((value, record.version)))
    }

    pub async fn put<T: Serialize>(
        &self,
        key: &str,
        value: &T,
        expected_version: Option<u64>,
    ) -> Result<Record, RecordError> {
        let encoded = serde_json::to_value(value).map_err(|error| RecordError::Encode {
            key: key.to_string(),
            detail: error.to_string(),
        })?;
        self.store
            .put(self.namespace, key, encoded, expected_version)
            .await
    }

    pub async fn list<T: DeserializeOwned>(
        &self,
        limit: usize,
    ) -> Result<Vec<(T, u64)>, RecordError> {
        let records = self.store.list(self.namespace, limit).await?;
        let mut decoded = Vec::with_capacity(records.len());
        for record in records {
            let value =
                serde_json::from_value(record.value).map_err(|error| RecordError::Decode {
                    key: record.key.clone(),
                    detail: error.to_string(),
                })?;
            decoded.push((value, record.version));
        }
        Ok(decoded)
    }

    pub async fn delete(&self, key: &str) -> Result<bool, RecordError> {
        self.store.delete(self.namespace, key).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn create_then_update_bumps_version() {
        let store = InMemoryRecordStore::new();
        let created = store
            .put("ns", "k", serde_json::json!({"a": 1}), Some(0))
            .await
            .expect("create");
        assert_eq!(created.version, 1);
        let updated = store
            .put("ns", "k", serde_json::json!({"a": 2}), Some(1))
            .await
            .expect("update");
        assert_eq!(updated.version, 2);
        let stored = store.get("ns", "k").await.unwrap().unwrap();
        assert_eq!(stored.value, serde_json::json!({"a": 2}));
    }

    #[tokio::test]
    async fn create_only_fails_when_the_key_exists() {
        let store = InMemoryRecordStore::new();
        store
            .put("ns", "k", serde_json::json!({}), Some(0))
            .await
            .expect("first create");
        let second = store.put("ns", "k", serde_json::json!({}), Some(0)).await;
        assert_eq!(
            second,
            Err(RecordError::Conflict {
                expected: 0,
                actual: Some(1)
            })
        );
    }

    #[tokio::test]
    async fn stale_cas_token_is_rejected() {
        let store = InMemoryRecordStore::new();
        store
            .put("ns", "k", serde_json::json!({"a": 1}), Some(0))
            .await
            .unwrap();
        store
            .put("ns", "k", serde_json::json!({"a": 2}), Some(1))
            .await
            .unwrap();
        // A writer that still believes the version is 1 must lose.
        let stale = store
            .put("ns", "k", serde_json::json!({"a": 3}), Some(1))
            .await;
        assert_eq!(
            stale,
            Err(RecordError::Conflict {
                expected: 1,
                actual: Some(2)
            })
        );
        let stored = store.get("ns", "k").await.unwrap().unwrap();
        assert_eq!(stored.value, serde_json::json!({"a": 2}));
    }

    #[tokio::test]
    async fn unconditional_put_upserts() {
        let store = InMemoryRecordStore::new();
        store.put("ns", "k", serde_json::json!({"a": 1}), None).await.unwrap();
        let second = store.put("ns", "k", serde_json::json!({"a": 2}), None).await.unwrap();
        assert_eq!(second.version, 2);
    }

    #[tokio::test]
    async fn update_of_a_missing_key_with_nonzero_expectation_conflicts() {
        let store = InMemoryRecordStore::new();
        let result = store.put("ns", "missing", serde_json::json!({}), Some(3)).await;
        assert_eq!(
            result,
            Err(RecordError::Conflict {
                expected: 3,
                actual: None
            })
        );
    }

    #[tokio::test]
    async fn namespaces_are_isolated_and_listing_is_key_ordered() {
        let store: std::sync::Arc<dyn RecordStore> = std::sync::Arc::new(InMemoryRecordStore::new());
        store.put("a", "z", serde_json::json!(1), None).await.unwrap();
        store.put("a", "m", serde_json::json!(2), None).await.unwrap();
        store.put("b", "m", serde_json::json!(3), None).await.unwrap();
        let listed = store.list("a", 10).await.unwrap();
        assert_eq!(
            listed.iter().map(|record| record.key.as_str()).collect::<Vec<_>>(),
            vec!["m", "z"]
        );
        let bound = store.list("a", 1).await.unwrap();
        assert_eq!(bound.len(), 1);
    }

    #[tokio::test]
    async fn typed_view_round_trips_a_document() {
        #[derive(serde::Serialize, serde::Deserialize, PartialEq, Debug)]
        struct Doc {
            name: String,
            count: u32,
        }
        let store: std::sync::Arc<dyn RecordStore> = std::sync::Arc::new(InMemoryRecordStore::new());
        let view = NamespacedStore::new(store, "docs");
        let doc = Doc {
            name: "alpha".to_string(),
            count: 7,
        };
        view.put("d1", &doc, Some(0)).await.expect("put");
        let (read, version) = view.get::<Doc>("d1").await.unwrap().expect("present");
        assert_eq!(read, doc);
        assert_eq!(version, 1);
        assert!(view.delete("d1").await.unwrap());
        assert!(view.get::<Doc>("d1").await.unwrap().is_none());
    }

    #[tokio::test]
    async fn decode_failure_names_the_key() {
        let store: std::sync::Arc<dyn RecordStore> = std::sync::Arc::new(InMemoryRecordStore::new());
        store
            .put("docs", "bad", serde_json::json!({"unexpected": true}), Some(0))
            .await
            .unwrap();
        let view = NamespacedStore::new(store, "docs");
        #[derive(serde::Deserialize, Debug)]
        #[allow(dead_code)]
        struct Doc {
            name: String,
        }
        let error = view.get::<Doc>("bad").await.expect_err("decode must fail");
        assert!(matches!(error, RecordError::Decode { .. }));
        assert!(error.to_string().contains("bad"));
    }

    #[tokio::test]
    async fn in_memory_store_is_explicitly_not_durable() {
        let store = InMemoryRecordStore::new();
        assert!(!store.is_durable());
        let all: std::sync::Arc<dyn RecordStore> = std::sync::Arc::new(store);
        assert!(!all.is_durable());
    }
}

