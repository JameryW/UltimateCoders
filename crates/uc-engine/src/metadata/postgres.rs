//! PostgreSQL metadata store — structured metadata for repos, index state, symbols, references.
//!
//! Uses `sqlx` for async PostgreSQL access with compile-time checked queries
//! when possible. For dynamic queries (AST search), uses runtime query building.
//!
//! Migrations are embedded and run at startup.
//!
//! When the `storage` feature is disabled, all methods operate on an in-memory
//! fallback store, and PostgreSQL-related code is not compiled.

use uc_types::error::EngineError;
use uc_types::index::{IndexHealth, IndexState, RepoSpec};
use uc_types::search::{SymbolKind};

#[cfg(feature = "storage")]
use sqlx::postgres::{PgPool, PgPoolOptions};

use std::sync::Arc;
#[cfg(feature = "storage")]
use std::time::Duration;

/// PostgreSQL metadata store.
pub struct PostgresMetadataStore {
    #[cfg(feature = "storage")]
    pool: Option<Arc<PgPool>>,
    /// In-memory fallback when PostgreSQL is not available.
    fallback: Arc<tokio::sync::RwLock<FallbackData>>,
}

/// In-memory fallback data structure for when PostgreSQL is unavailable.
#[derive(Default)]
struct FallbackData {
    repos: Vec<RepoSpec>,
    index_states: Vec<IndexState>,
    symbols: Vec<SymbolRecord>,
    references: Vec<ReferenceRecord>,
}

/// Internal symbol record matching the PostgreSQL schema.
#[derive(Debug, Clone)]
#[allow(dead_code)]
struct SymbolRecord {
    id: i64,
    repo_id: String,
    file_path: String,
    name: String,
    kind: String,
    start_line: i32,
    start_col: i32,
    end_line: i32,
    end_col: i32,
    parent_symbol_id: Option<i64>,
    language: String,
    content_hash: String,
}

/// Internal reference record matching the PostgreSQL schema.
#[derive(Debug, Clone)]
#[allow(dead_code)]
struct ReferenceRecord {
    id: i64,
    repo_id: String,
    file_path: String,
    source_symbol_id: Option<i64>,
    target_name: String,
    reference_kind: String,
    start_line: i32,
    start_col: i32,
    language: String,
    content_hash: String,
}

impl PostgresMetadataStore {
    /// Create a new PostgreSQL metadata store, connecting to the database.
    #[cfg(feature = "storage")]
    pub async fn new(database_url: &str) -> Result<Self, EngineError> {
        match PgPoolOptions::new()
            .max_connections(10)
            .acquire_timeout(Duration::from_secs(10))
            .connect(database_url)
            .await
        {
            Ok(pool) => {
                let store = Self {
                    pool: Some(Arc::new(pool)),
                    fallback: Arc::new(tokio::sync::RwLock::new(FallbackData::default())),
                };

                // Run migrations
                store.run_migrations().await?;

                tracing::info!("Connected to PostgreSQL for metadata storage");
                Ok(store)
            }
            Err(e) => {
                tracing::warn!(
                    "PostgreSQL unavailable, using in-memory fallback for metadata: {}",
                    e
                );
                Ok(Self {
                    pool: None,
                    fallback: Arc::new(tokio::sync::RwLock::new(FallbackData::default())),
                })
            }
        }
    }

    /// Create a new PostgreSQL metadata store (fallback-only when storage feature is disabled).
    #[cfg(not(feature = "storage"))]
    pub async fn new(_database_url: &str) -> Result<Self, EngineError> {
        tracing::info!("Storage feature disabled, using in-memory fallback for metadata");
        Ok(Self::new_fallback())
    }

    /// Create with an existing connection pool (for testing / dependency injection).
    #[cfg(feature = "storage")]
    pub fn with_pool(pool: Arc<PgPool>) -> Self {
        Self {
            pool: Some(pool),
            fallback: Arc::new(tokio::sync::RwLock::new(FallbackData::default())),
        }
    }

    /// Create with in-memory fallback only (for testing).
    pub fn new_fallback() -> Self {
        Self {
            #[cfg(feature = "storage")]
            pool: None,
            fallback: Arc::new(tokio::sync::RwLock::new(FallbackData::default())),
        }
    }

    /// Run database migrations to create required tables.
    #[cfg(feature = "storage")]
    pub async fn run_migrations(&self) -> Result<(), EngineError> {
        let pool = self.pool.as_ref().ok_or_else(|| {
            EngineError::ConnectionError("PostgreSQL pool not available".into())
        })?;

        // Create repos table
        sqlx::query(
            r#"
            CREATE TABLE IF NOT EXISTS repos (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                repo_id TEXT NOT NULL UNIQUE,
                remote_url TEXT NOT NULL,
                default_branch TEXT NOT NULL DEFAULT 'main',
                local_path TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            "#,
        )
        .execute(pool.as_ref())
        .await
        .map_err(|e| EngineError::ConnectionError(format!("Migration error (repos): {}", e)))?;

        // Create index_state table
        sqlx::query(
            r#"
            CREATE TABLE IF NOT EXISTS index_state (
                id BIGSERIAL PRIMARY KEY,
                repo_id TEXT NOT NULL REFERENCES repos(repo_id),
                last_indexed_sha TEXT NOT NULL DEFAULT '',
                last_indexed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_full_reindex TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                index_version INT NOT NULL DEFAULT 1,
                health TEXT NOT NULL DEFAULT 'healthy',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            "#,
        )
        .execute(pool.as_ref())
        .await
        .map_err(|e| EngineError::ConnectionError(format!("Migration error (index_state): {}", e)))?;

        // Create symbols table
        sqlx::query(
            r#"
            CREATE TABLE IF NOT EXISTS symbols (
                id BIGSERIAL PRIMARY KEY,
                repo_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                start_line INT NOT NULL,
                start_col INT NOT NULL,
                end_line INT NOT NULL,
                end_col INT NOT NULL,
                parent_symbol_id BIGINT REFERENCES symbols(id),
                language TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            "#,
        )
        .execute(pool.as_ref())
        .await
        .map_err(|e| EngineError::ConnectionError(format!("Migration error (symbols): {}", e)))?;

        // Create references table
        sqlx::query(
            r#"
            CREATE TABLE IF NOT EXISTS references (
                id BIGSERIAL PRIMARY KEY,
                repo_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                source_symbol_id BIGINT REFERENCES symbols(id),
                target_name TEXT NOT NULL,
                reference_kind TEXT NOT NULL,
                start_line INT NOT NULL,
                start_col INT NOT NULL,
                language TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            "#,
        )
        .execute(pool.as_ref())
        .await
        .map_err(|e| EngineError::ConnectionError(format!("Migration error (references): {}", e)))?;

        // Create indexes
        let indexes = [
            "CREATE INDEX IF NOT EXISTS idx_symbols_repo_id ON symbols(repo_id)",
            "CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name)",
            "CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind)",
            "CREATE INDEX IF NOT EXISTS idx_symbols_file_path ON symbols(file_path)",
            "CREATE INDEX IF NOT EXISTS idx_references_repo_id ON references(repo_id)",
            "CREATE INDEX IF NOT EXISTS idx_references_target_name ON references(target_name)",
            "CREATE INDEX IF NOT EXISTS idx_references_kind ON references(reference_kind)",
            "CREATE INDEX IF NOT EXISTS idx_index_state_repo_id ON index_state(repo_id)",
        ];

        for idx_sql in &indexes {
            sqlx::query(idx_sql)
                .execute(pool.as_ref())
                .await
                .map_err(|e| EngineError::ConnectionError(format!("Index creation error: {}", e)))?;
        }

        tracing::info!("PostgreSQL migrations completed");
        Ok(())
    }

    /// Run database migrations (no-op when storage feature is disabled).
    #[cfg(not(feature = "storage"))]
    pub async fn run_migrations(&self) -> Result<(), EngineError> {
        Ok(())
    }

    // ── Repository CRUD ──────────────────────────────────────

    /// Register a repository in the metadata store.
    pub async fn register_repo(&self, spec: &RepoSpec) -> Result<(), EngineError> {
        #[cfg(feature = "storage")]
        if let Some(pool) = &self.pool {
            sqlx::query(
                r#"
                INSERT INTO repos (repo_id, remote_url, default_branch, local_path)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (repo_id) DO UPDATE SET
                    remote_url = EXCLUDED.remote_url,
                    default_branch = EXCLUDED.default_branch,
                    local_path = EXCLUDED.local_path,
                    updated_at = NOW()
                "#,
            )
            .bind(&spec.repo_id)
            .bind(&spec.remote_url)
            .bind(&spec.default_branch)
            .bind(&spec.local_path)
            .execute(pool.as_ref())
            .await
            .map_err(|e| EngineError::ConnectionError(format!("Repo insert error: {}", e)))?;
        } else {
            let mut fallback = self.fallback.write().await;
            if let Some(existing) = fallback.repos.iter_mut().find(|r| r.repo_id == spec.repo_id) {
                *existing = spec.clone();
            } else {
                fallback.repos.push(spec.clone());
            }
        }

        #[cfg(not(feature = "storage"))]
        {
            let mut fallback = self.fallback.write().await;
            if let Some(existing) = fallback.repos.iter_mut().find(|r| r.repo_id == spec.repo_id) {
                *existing = spec.clone();
            } else {
                fallback.repos.push(spec.clone());
            }
        }

        Ok(())
    }

    /// Get a repository by its ID.
    pub async fn get_repo(&self, repo_id: &str) -> Result<Option<RepoSpec>, EngineError> {
        #[cfg(feature = "storage")]
        if let Some(pool) = &self.pool {
            let row = sqlx::query_as::<_, (String, String, String, Option<String>)>(
                "SELECT repo_id, remote_url, default_branch, local_path FROM repos WHERE repo_id = $1",
            )
            .bind(repo_id)
            .fetch_optional(pool.as_ref())
            .await
            .map_err(|e| EngineError::ConnectionError(format!("Repo fetch error: {}", e)))?;

            Ok(row.map(|(repo_id, remote_url, default_branch, local_path)| RepoSpec {
                repo_id,
                remote_url,
                default_branch,
                local_path,
            }))
        } else {
            let fallback = self.fallback.read().await;
            Ok(fallback.repos.iter().find(|r| r.repo_id == repo_id).cloned())
        }

        #[cfg(not(feature = "storage"))]
        {
            let fallback = self.fallback.read().await;
            Ok(fallback.repos.iter().find(|r| r.repo_id == repo_id).cloned())
        }
    }

    /// List all registered repositories.
    pub async fn list_repos(&self) -> Result<Vec<RepoSpec>, EngineError> {
        #[cfg(feature = "storage")]
        if let Some(pool) = &self.pool {
            let rows = sqlx::query_as::<_, (String, String, String, Option<String>)>(
                "SELECT repo_id, remote_url, default_branch, local_path FROM repos ORDER BY repo_id",
            )
            .fetch_all(pool.as_ref())
            .await
            .map_err(|e| EngineError::ConnectionError(format!("Repo list error: {}", e)))?;

            Ok(rows
                .into_iter()
                .map(|(repo_id, remote_url, default_branch, local_path)| RepoSpec {
                    repo_id,
                    remote_url,
                    default_branch,
                    local_path,
                })
                .collect())
        } else {
            let fallback = self.fallback.read().await;
            Ok(fallback.repos.clone())
        }

        #[cfg(not(feature = "storage"))]
        {
            let fallback = self.fallback.read().await;
            Ok(fallback.repos.clone())
        }
    }

    /// Delete a repository and its associated index state, symbols, and references.
    pub async fn delete_repo(&self, repo_id: &str) -> Result<(), EngineError> {
        #[cfg(feature = "storage")]
        if let Some(pool) = &self.pool {
            // Delete in order of foreign key dependencies
            sqlx::query("DELETE FROM references WHERE repo_id = $1")
                .bind(repo_id)
                .execute(pool.as_ref())
                .await
                .map_err(|e| EngineError::ConnectionError(format!("References delete error: {}", e)))?;

            sqlx::query("DELETE FROM symbols WHERE repo_id = $1")
                .bind(repo_id)
                .execute(pool.as_ref())
                .await
                .map_err(|e| EngineError::ConnectionError(format!("Symbols delete error: {}", e)))?;

            sqlx::query("DELETE FROM index_state WHERE repo_id = $1")
                .bind(repo_id)
                .execute(pool.as_ref())
                .await
                .map_err(|e| EngineError::ConnectionError(format!("Index state delete error: {}", e)))?;

            sqlx::query("DELETE FROM repos WHERE repo_id = $1")
                .bind(repo_id)
                .execute(pool.as_ref())
                .await
                .map_err(|e| EngineError::ConnectionError(format!("Repo delete error: {}", e)))?;
        } else {
            let mut fallback = self.fallback.write().await;
            fallback.references.retain(|r| r.repo_id != repo_id);
            fallback.symbols.retain(|s| s.repo_id != repo_id);
            fallback.index_states.retain(|i| i.repo_id != repo_id);
            fallback.repos.retain(|r| r.repo_id != repo_id);
        }

        #[cfg(not(feature = "storage"))]
        {
            let mut fallback = self.fallback.write().await;
            fallback.references.retain(|r| r.repo_id != repo_id);
            fallback.symbols.retain(|s| s.repo_id != repo_id);
            fallback.index_states.retain(|i| i.repo_id != repo_id);
            fallback.repos.retain(|r| r.repo_id != repo_id);
        }

        Ok(())
    }

    // ── Index State ──────────────────────────────────────────

    /// Get the index state for a repository.
    pub async fn get_index_state(&self, repo_id: &str) -> Result<Option<IndexState>, EngineError> {
        #[cfg(feature = "storage")]
        if let Some(pool) = &self.pool {
            let row = sqlx::query_as::<_, (String, String, chrono::DateTime<chrono::Utc>, chrono::DateTime<chrono::Utc>, i32, String)>(
                "SELECT repo_id, last_indexed_sha, last_indexed_at, last_full_reindex, index_version, health FROM index_state WHERE repo_id = $1",
            )
            .bind(repo_id)
            .fetch_optional(pool.as_ref())
            .await
            .map_err(|e| EngineError::ConnectionError(format!("Index state fetch error: {}", e)))?;

            Ok(row.map(|(repo_id, last_indexed_sha, last_indexed_at, last_full_reindex, index_version, health)| IndexState {
                repo_id,
                last_indexed_sha,
                last_indexed_at,
                last_full_reindex,
                index_version: index_version as u32,
                health: parse_health(&health),
            }))
        } else {
            let fallback = self.fallback.read().await;
            Ok(fallback.index_states.iter().find(|i| i.repo_id == repo_id).cloned())
        }

        #[cfg(not(feature = "storage"))]
        {
            let fallback = self.fallback.read().await;
            Ok(fallback.index_states.iter().find(|i| i.repo_id == repo_id).cloned())
        }
    }

    /// Update the index state for a repository.
    pub async fn update_index_state(&self, state: &IndexState) -> Result<(), EngineError> {
        let health_str = format_health(&state.health);

        #[cfg(feature = "storage")]
        if let Some(pool) = &self.pool {
            sqlx::query(
                r#"
                INSERT INTO index_state (repo_id, last_indexed_sha, last_indexed_at, last_full_reindex, index_version, health)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (repo_id) DO UPDATE SET
                    last_indexed_sha = EXCLUDED.last_indexed_sha,
                    last_indexed_at = EXCLUDED.last_indexed_at,
                    last_full_reindex = EXCLUDED.last_full_reindex,
                    index_version = EXCLUDED.index_version,
                    health = EXCLUDED.health
                "#,
            )
            .bind(&state.repo_id)
            .bind(&state.last_indexed_sha)
            .bind(state.last_indexed_at)
            .bind(state.last_full_reindex)
            .bind(state.index_version as i32)
            .bind(health_str)
            .execute(pool.as_ref())
            .await
            .map_err(|e| EngineError::ConnectionError(format!("Index state upsert error: {}", e)))?;
        } else {
            let mut fallback = self.fallback.write().await;
            if let Some(existing) = fallback.index_states.iter_mut().find(|i| i.repo_id == state.repo_id) {
                *existing = state.clone();
            } else {
                fallback.index_states.push(state.clone());
            }
        }

        #[cfg(not(feature = "storage"))]
        {
            let _ = health_str; // suppress unused warning
            let mut fallback = self.fallback.write().await;
            if let Some(existing) = fallback.index_states.iter_mut().find(|i| i.repo_id == state.repo_id) {
                *existing = state.clone();
            } else {
                fallback.index_states.push(state.clone());
            }
        }

        Ok(())
    }

    // ── Symbol Search ────────────────────────────────────────

    /// Search for symbols by name.
    pub async fn search_symbols(
        &self,
        name: &str,
        repo_id: Option<&str>,
        kind: Option<&SymbolKind>,
        limit: u32,
    ) -> Result<Vec<SymbolSearchResult>, EngineError> {
        #[cfg(feature = "storage")]
        if let Some(pool) = &self.pool {
            let kind_str = kind.map(symbol_kind_to_str);

            let rows = if let (Some(rid), Some(k)) = (repo_id, kind_str) {
                sqlx::query_as::<_, (String, String, String, String, i32, i32, i32, i32, Option<i64>, String)>(
                    "SELECT repo_id, file_path, name, kind, start_line, start_col, end_line, end_col, parent_symbol_id, language FROM symbols WHERE name ILIKE $1 AND repo_id = $2 AND kind = $3 LIMIT $4",
                )
                .bind(format!("%{}%", name))
                .bind(rid)
                .bind(k)
                .bind(limit as i64)
                .fetch_all(pool.as_ref())
                .await
            } else if let Some(rid) = repo_id {
                sqlx::query_as::<_, (String, String, String, String, i32, i32, i32, i32, Option<i64>, String)>(
                    "SELECT repo_id, file_path, name, kind, start_line, start_col, end_line, end_col, parent_symbol_id, language FROM symbols WHERE name ILIKE $1 AND repo_id = $2 LIMIT $3",
                )
                .bind(format!("%{}%", name))
                .bind(rid)
                .bind(limit as i64)
                .fetch_all(pool.as_ref())
                .await
            } else {
                sqlx::query_as::<_, (String, String, String, String, i32, i32, i32, i32, Option<i64>, String)>(
                    "SELECT repo_id, file_path, name, kind, start_line, start_col, end_line, end_col, parent_symbol_id, language FROM symbols WHERE name ILIKE $1 LIMIT $2",
                )
                .bind(format!("%{}%", name))
                .bind(limit as i64)
                .fetch_all(pool.as_ref())
                .await
            }
            .map_err(|e| EngineError::SearchError(format!("Symbol search error: {}", e)))?;

            Ok(rows
                .into_iter()
                .map(|(repo_id, file_path, name, kind, start_line, start_col, end_line, end_col, _parent, language)| {
                    SymbolSearchResult {
                        repo_id,
                        file_path,
                        name,
                        kind: parse_symbol_kind(&kind),
                        start_line: start_line as u32,
                        start_col: start_col as u32,
                        end_line: end_line as u32,
                        end_col: end_col as u32,
                        language,
                    }
                })
                .collect())
        } else {
            // Fallback: simple linear scan
            let fallback = self.fallback.read().await;
            let pattern = name.to_lowercase();
            let results: Vec<SymbolSearchResult> = fallback
                .symbols
                .iter()
                .filter(|s| {
                    s.name.to_lowercase().contains(&pattern)
                        && repo_id.is_none_or(|rid| s.repo_id == rid)
                        && kind.is_none_or(|k| s.kind == symbol_kind_to_str(k))
                })
                .take(limit as usize)
                .map(|s| SymbolSearchResult {
                    repo_id: s.repo_id.clone(),
                    file_path: s.file_path.clone(),
                    name: s.name.clone(),
                    kind: parse_symbol_kind(&s.kind),
                    start_line: s.start_line as u32,
                    start_col: s.start_col as u32,
                    end_line: s.end_line as u32,
                    end_col: s.end_col as u32,
                    language: s.language.clone(),
                })
                .collect();
            Ok(results)
        }

        #[cfg(not(feature = "storage"))]
        {
            // Fallback: simple linear scan
            let fallback = self.fallback.read().await;
            let pattern = name.to_lowercase();
            let results: Vec<SymbolSearchResult> = fallback
                .symbols
                .iter()
                .filter(|s| {
                    s.name.to_lowercase().contains(&pattern)
                        && repo_id.is_none_or(|rid| s.repo_id == rid)
                        && kind.is_none_or(|k| s.kind == symbol_kind_to_str(k))
                })
                .take(limit as usize)
                .map(|s| SymbolSearchResult {
                    repo_id: s.repo_id.clone(),
                    file_path: s.file_path.clone(),
                    name: s.name.clone(),
                    kind: parse_symbol_kind(&s.kind),
                    start_line: s.start_line as u32,
                    start_col: s.start_col as u32,
                    end_line: s.end_line as u32,
                    end_col: s.end_col as u32,
                    language: s.language.clone(),
                })
                .collect();
            Ok(results)
        }
    }

    /// Search for references to a symbol.
    pub async fn search_references(
        &self,
        target_name: &str,
        repo_id: Option<&str>,
        limit: u32,
    ) -> Result<Vec<ReferenceSearchResult>, EngineError> {
        #[cfg(feature = "storage")]
        if let Some(pool) = &self.pool {
            let rows = if let Some(rid) = repo_id {
                sqlx::query_as::<_, (String, String, Option<i64>, String, String, i32, i32, String)>(
                    "SELECT repo_id, file_path, source_symbol_id, target_name, reference_kind, start_line, start_col, language FROM references WHERE target_name = $1 AND repo_id = $2 LIMIT $3",
                )
                .bind(target_name)
                .bind(rid)
                .bind(limit as i64)
                .fetch_all(pool.as_ref())
                .await
            } else {
                sqlx::query_as::<_, (String, String, Option<i64>, String, String, i32, i32, String)>(
                    "SELECT repo_id, file_path, source_symbol_id, target_name, reference_kind, start_line, start_col, language FROM references WHERE target_name = $1 LIMIT $2",
                )
                .bind(target_name)
                .bind(limit as i64)
                .fetch_all(pool.as_ref())
                .await
            }
            .map_err(|e| EngineError::SearchError(format!("Reference search error: {}", e)))?;

            Ok(rows
                .into_iter()
                .map(|(repo_id, file_path, source_symbol_id, target_name, reference_kind, start_line, start_col, language)| {
                    ReferenceSearchResult {
                        repo_id,
                        file_path,
                        source_symbol_id,
                        target_name,
                        reference_kind,
                        start_line: start_line as u32,
                        start_col: start_col as u32,
                        language,
                    }
                })
                .collect())
        } else {
            let fallback = self.fallback.read().await;
            let results: Vec<ReferenceSearchResult> = fallback
                .references
                .iter()
                .filter(|r| {
                    r.target_name == target_name
                        && repo_id.is_none_or(|rid| r.repo_id == rid)
                })
                .take(limit as usize)
                .map(|r| ReferenceSearchResult {
                    repo_id: r.repo_id.clone(),
                    file_path: r.file_path.clone(),
                    source_symbol_id: r.source_symbol_id,
                    target_name: r.target_name.clone(),
                    reference_kind: r.reference_kind.clone(),
                    start_line: r.start_line as u32,
                    start_col: r.start_col as u32,
                    language: r.language.clone(),
                })
                .collect();
            Ok(results)
        }

        #[cfg(not(feature = "storage"))]
        {
            let fallback = self.fallback.read().await;
            let results: Vec<ReferenceSearchResult> = fallback
                .references
                .iter()
                .filter(|r| {
                    r.target_name == target_name
                        && repo_id.is_none_or(|rid| r.repo_id == rid)
                })
                .take(limit as usize)
                .map(|r| ReferenceSearchResult {
                    repo_id: r.repo_id.clone(),
                    file_path: r.file_path.clone(),
                    source_symbol_id: r.source_symbol_id,
                    target_name: r.target_name.clone(),
                    reference_kind: r.reference_kind.clone(),
                    start_line: r.start_line as u32,
                    start_col: r.start_col as u32,
                    language: r.language.clone(),
                })
                .collect();
            Ok(results)
        }
    }

    /// Insert symbols for a repository (batch).
    pub async fn insert_symbols(
        &self,
        repo_id: &str,
        symbols: Vec<SymbolInsert>,
    ) -> Result<u32, EngineError> {
        let count = symbols.len() as u32;

        #[cfg(feature = "storage")]
        if let Some(pool) = &self.pool {
            for symbol in &symbols {
                sqlx::query(
                    r#"
                    INSERT INTO symbols (repo_id, file_path, name, kind, start_line, start_col, end_line, end_col, language, content_hash)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    "#,
                )
                .bind(repo_id)
                .bind(&symbol.file_path)
                .bind(&symbol.name)
                .bind(symbol_kind_to_str(&symbol.kind))
                .bind(symbol.start_line as i32)
                .bind(symbol.start_col as i32)
                .bind(symbol.end_line as i32)
                .bind(symbol.end_col as i32)
                .bind(&symbol.language)
                .bind(&symbol.content_hash)
                .execute(pool.as_ref())
                .await
                .map_err(|e| EngineError::ConnectionError(format!("Symbol insert error: {}", e)))?;
            }
        } else {
            let mut fallback = self.fallback.write().await;
            let start_id = fallback.symbols.last().map(|s| s.id + 1).unwrap_or(1);
            for (i, symbol) in symbols.into_iter().enumerate() {
                fallback.symbols.push(SymbolRecord {
                    id: start_id + i as i64,
                    repo_id: repo_id.to_string(),
                    file_path: symbol.file_path,
                    name: symbol.name,
                    kind: symbol_kind_to_str(&symbol.kind).to_string(),
                    start_line: symbol.start_line as i32,
                    start_col: symbol.start_col as i32,
                    end_line: symbol.end_line as i32,
                    end_col: symbol.end_col as i32,
                    parent_symbol_id: None,
                    language: symbol.language,
                    content_hash: symbol.content_hash,
                });
            }
        }

        #[cfg(not(feature = "storage"))]
        {
            let mut fallback = self.fallback.write().await;
            let start_id = fallback.symbols.last().map(|s| s.id + 1).unwrap_or(1);
            for (i, symbol) in symbols.into_iter().enumerate() {
                fallback.symbols.push(SymbolRecord {
                    id: start_id + i as i64,
                    repo_id: repo_id.to_string(),
                    file_path: symbol.file_path,
                    name: symbol.name,
                    kind: symbol_kind_to_str(&symbol.kind).to_string(),
                    start_line: symbol.start_line as i32,
                    start_col: symbol.start_col as i32,
                    end_line: symbol.end_line as i32,
                    end_col: symbol.end_col as i32,
                    parent_symbol_id: None,
                    language: symbol.language,
                    content_hash: symbol.content_hash,
                });
            }
        }

        Ok(count)
    }

    /// Delete all symbols and references for a specific file within a repository.
    pub async fn delete_symbols_for_file(&self, repo_id: &str, file_path: &str) -> Result<(), EngineError> {
        #[cfg(feature = "storage")]
        if let Some(pool) = &self.pool {
            // Delete references first (they reference symbols)
            sqlx::query("DELETE FROM references WHERE repo_id = $1 AND file_path = $2")
                .bind(repo_id)
                .bind(file_path)
                .execute(pool.as_ref())
                .await
                .map_err(|e| EngineError::ConnectionError(format!("References delete for file error: {}", e)))?;

            sqlx::query("DELETE FROM symbols WHERE repo_id = $1 AND file_path = $2")
                .bind(repo_id)
                .bind(file_path)
                .execute(pool.as_ref())
                .await
                .map_err(|e| EngineError::ConnectionError(format!("Symbols delete for file error: {}", e)))?;
        } else {
            let mut fallback = self.fallback.write().await;
            fallback.references.retain(|r| !(r.repo_id == repo_id && r.file_path == file_path));
            fallback.symbols.retain(|s| !(s.repo_id == repo_id && s.file_path == file_path));
        }

        #[cfg(not(feature = "storage"))]
        {
            let mut fallback = self.fallback.write().await;
            fallback.references.retain(|r| !(r.repo_id == repo_id && r.file_path == file_path));
            fallback.symbols.retain(|s| !(s.repo_id == repo_id && s.file_path == file_path));
        }

        Ok(())
    }

    /// Delete all symbols for a repository (used during reindex).
    pub async fn delete_symbols_for_repo(&self, repo_id: &str) -> Result<(), EngineError> {
        #[cfg(feature = "storage")]
        if let Some(pool) = &self.pool {
            sqlx::query("DELETE FROM symbols WHERE repo_id = $1")
                .bind(repo_id)
                .execute(pool.as_ref())
                .await
                .map_err(|e| EngineError::ConnectionError(format!("Symbol delete error: {}", e)))?;
        } else {
            let mut fallback = self.fallback.write().await;
            fallback.symbols.retain(|s| s.repo_id != repo_id);
        }

        #[cfg(not(feature = "storage"))]
        {
            let mut fallback = self.fallback.write().await;
            fallback.symbols.retain(|s| s.repo_id != repo_id);
        }

        Ok(())
    }

    /// Check if PostgreSQL is available.
    pub fn is_connected(&self) -> bool {
        #[cfg(feature = "storage")]
        {
            self.pool.is_some()
        }
        #[cfg(not(feature = "storage"))]
        {
            false
        }
    }
}

/// Result from a symbol search.
#[derive(Debug, Clone)]
pub struct SymbolSearchResult {
    pub repo_id: String,
    pub file_path: String,
    pub name: String,
    pub kind: SymbolKind,
    pub start_line: u32,
    pub start_col: u32,
    pub end_line: u32,
    pub end_col: u32,
    pub language: String,
}

/// Result from a reference search.
#[derive(Debug, Clone)]
pub struct ReferenceSearchResult {
    pub repo_id: String,
    pub file_path: String,
    pub source_symbol_id: Option<i64>,
    pub target_name: String,
    pub reference_kind: String,
    pub start_line: u32,
    pub start_col: u32,
    pub language: String,
}

/// Input for inserting a symbol.
#[derive(Debug, Clone)]
pub struct SymbolInsert {
    pub file_path: String,
    pub name: String,
    pub kind: SymbolKind,
    pub start_line: u32,
    pub start_col: u32,
    pub end_line: u32,
    pub end_col: u32,
    pub language: String,
    pub content_hash: String,
}

// ── Helpers ──────────────────────────────────────────────────

fn format_health(health: &IndexHealth) -> &'static str {
    match health {
        IndexHealth::Healthy => "healthy",
        IndexHealth::Indexing => "indexing",
        IndexHealth::Stale => "stale",
        IndexHealth::Corrupted => "corrupted",
    }
}

#[cfg(feature = "storage")]
fn parse_health(s: &str) -> IndexHealth {
    match s {
        "healthy" => IndexHealth::Healthy,
        "indexing" => IndexHealth::Indexing,
        "stale" => IndexHealth::Stale,
        "corrupted" => IndexHealth::Corrupted,
        _ => IndexHealth::Healthy,
    }
}

fn symbol_kind_to_str(kind: &SymbolKind) -> &'static str {
    match kind {
        SymbolKind::Function => "function",
        SymbolKind::Method => "method",
        SymbolKind::Class => "class",
        SymbolKind::Struct => "struct",
        SymbolKind::Interface => "interface",
        SymbolKind::Trait => "trait",
        SymbolKind::Enum => "enum",
        SymbolKind::Variable => "variable",
        SymbolKind::Constant => "constant",
        SymbolKind::Type => "type",
        SymbolKind::Module => "module",
        SymbolKind::Import => "import",
    }
}

fn parse_symbol_kind(s: &str) -> SymbolKind {
    match s {
        "function" => SymbolKind::Function,
        "method" => SymbolKind::Method,
        "class" => SymbolKind::Class,
        "struct" => SymbolKind::Struct,
        "interface" => SymbolKind::Interface,
        "trait" => SymbolKind::Trait,
        "enum" => SymbolKind::Enum,
        "variable" => SymbolKind::Variable,
        "constant" => SymbolKind::Constant,
        "type" => SymbolKind::Type,
        "module" => SymbolKind::Module,
        "import" => SymbolKind::Import,
        _ => SymbolKind::Function,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(feature = "storage")]
    #[test]
    fn test_format_parse_health() {
        assert_eq!(format_health(&IndexHealth::Healthy), "healthy");
        assert_eq!(format_health(&IndexHealth::Indexing), "indexing");
        assert_eq!(format_health(&IndexHealth::Stale), "stale");
        assert_eq!(format_health(&IndexHealth::Corrupted), "corrupted");

        assert_eq!(parse_health("healthy"), IndexHealth::Healthy);
        assert_eq!(parse_health("indexing"), IndexHealth::Indexing);
        assert_eq!(parse_health("stale"), IndexHealth::Stale);
        assert_eq!(parse_health("corrupted"), IndexHealth::Corrupted);
    }

    #[test]
    fn test_symbol_kind_roundtrip() {
        let kinds = [
            SymbolKind::Function,
            SymbolKind::Method,
            SymbolKind::Class,
            SymbolKind::Struct,
            SymbolKind::Interface,
            SymbolKind::Trait,
            SymbolKind::Enum,
            SymbolKind::Variable,
            SymbolKind::Constant,
            SymbolKind::Type,
            SymbolKind::Module,
            SymbolKind::Import,
        ];

        for kind in &kinds {
            let s = symbol_kind_to_str(kind);
            let parsed = parse_symbol_kind(s);
            assert_eq!(*kind, parsed, "Failed roundtrip for {:?}", kind);
        }
    }

    #[tokio::test]
    async fn test_fallback_repo_crud() {
        let store = PostgresMetadataStore::new_fallback();

        let spec = RepoSpec {
            repo_id: "test-repo".to_string(),
            remote_url: "https://github.com/test/repo".to_string(),
            default_branch: "main".to_string(),
            local_path: Some("/tmp/repo".to_string()),
        };

        // Register
        store.register_repo(&spec).await.unwrap();

        // Get
        let result = store.get_repo("test-repo").await.unwrap();
        assert!(result.is_some());
        let fetched = result.unwrap();
        assert_eq!(fetched.repo_id, "test-repo");
        assert_eq!(fetched.remote_url, "https://github.com/test/repo");

        // List
        let repos = store.list_repos().await.unwrap();
        assert_eq!(repos.len(), 1);

        // Delete
        store.delete_repo("test-repo").await.unwrap();
        let result = store.get_repo("test-repo").await.unwrap();
        assert!(result.is_none());
    }

    #[tokio::test]
    async fn test_fallback_index_state() {
        let store = PostgresMetadataStore::new_fallback();

        let state = IndexState {
            repo_id: "test-repo".to_string(),
            last_indexed_sha: "abc123".to_string(),
            last_indexed_at: chrono::Utc::now(),
            last_full_reindex: chrono::Utc::now(),
            index_version: 1,
            health: IndexHealth::Healthy,
        };

        store.update_index_state(&state).await.unwrap();

        let result = store.get_index_state("test-repo").await.unwrap();
        assert!(result.is_some());
        assert_eq!(result.unwrap().last_indexed_sha, "abc123");
    }

    #[tokio::test]
    async fn test_fallback_symbol_search() {
        let store = PostgresMetadataStore::new_fallback();

        let symbols = vec![
            SymbolInsert {
                file_path: "src/main.rs".to_string(),
                name: "main".to_string(),
                kind: SymbolKind::Function,
                start_line: 1,
                start_col: 0,
                end_line: 10,
                end_col: 1,
                language: "rust".to_string(),
                content_hash: "abc".to_string(),
            },
            SymbolInsert {
                file_path: "src/lib.rs".to_string(),
                name: "Config".to_string(),
                kind: SymbolKind::Struct,
                start_line: 5,
                start_col: 0,
                end_line: 20,
                end_col: 1,
                language: "rust".to_string(),
                content_hash: "def".to_string(),
            },
        ];

        store.insert_symbols("test-repo", symbols).await.unwrap();

        let results = store
            .search_symbols("main", Some("test-repo"), None, 10)
            .await
            .unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].name, "main");

        let results = store
            .search_symbols("Config", None, Some(&SymbolKind::Struct), 10)
            .await
            .unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].name, "Config");
    }

    #[tokio::test]
    async fn test_fallback_delete_symbols_for_file() {
        let store = PostgresMetadataStore::new_fallback();

        let symbols = vec![
            SymbolInsert {
                file_path: "src/main.rs".to_string(),
                name: "main".to_string(),
                kind: SymbolKind::Function,
                start_line: 1,
                start_col: 0,
                end_line: 10,
                end_col: 1,
                language: "rust".to_string(),
                content_hash: "abc".to_string(),
            },
            SymbolInsert {
                file_path: "src/lib.rs".to_string(),
                name: "Config".to_string(),
                kind: SymbolKind::Struct,
                start_line: 5,
                start_col: 0,
                end_line: 20,
                end_col: 1,
                language: "rust".to_string(),
                content_hash: "def".to_string(),
            },
        ];

        store.insert_symbols("test-repo", symbols).await.unwrap();

        // Delete symbols for just main.rs
        store.delete_symbols_for_file("test-repo", "src/main.rs").await.unwrap();

        // main.rs symbols should be gone
        let results_main = store
            .search_symbols("main", Some("test-repo"), None, 10)
            .await
            .unwrap();
        assert!(results_main.is_empty());

        // lib.rs symbols should remain
        let results_lib = store
            .search_symbols("Config", Some("test-repo"), None, 10)
            .await
            .unwrap();
        assert_eq!(results_lib.len(), 1);
        assert_eq!(results_lib[0].file_path, "src/lib.rs");
    }
}
