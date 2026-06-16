# Database / Storage Guidelines

> Storage patterns, fallback strategy, key encoding, migrations, and health checks.

---

## Overview

The system uses three storage backends, each following an identical structural pattern with in-memory fallback:

| Backend | Store | Purpose | Client Library |
|---------|-------|---------|---------------|
| TiKV | Short-term memory | Task-scoped KV (diffs, decisions, progress) | `tikv_client` (raw KV mode) |
| Qdrant | Long-term memory | Persistent knowledge + semantic search | `qdrant_client` |
| PostgreSQL | Metadata | Repos, index state, symbols, references | `sqlx` (async, runtime queries) |

All three use the `storage` Cargo feature flag to gate real client code. Without the feature, only in-memory fallback is compiled.

---

## Universal Fallback Pattern

Every storage struct follows the same three-field layout:

```rust
pub struct Store {
    #[cfg(feature = "storage")]
    client: Option<Arc<RealClient>>,       // Real client when available
    fallback: Arc<RwLock<FallbackData>>,    // In-memory fallback always present
}
```

**Real example** -- `ShortTermMemory` (`crates/uc-engine/src/memory/short_term.rs:28-35`):

```rust
pub struct ShortTermMemory {
    #[cfg(feature = "storage")]
    client: Option<Arc<tikv_client::RawClient>>,
    fallback: Arc<RwLock<Vec<(String, StoredEntry)>>>,
    default_ttl_seconds: u64,
}
```

**Real example** -- `PostgresMetadataStore` (`crates/uc-engine/src/metadata/postgres.rs:23-28`):

```rust
pub struct PostgresMetadataStore {
    #[cfg(feature = "storage")]
    pool: Option<Arc<PgPool>>,
    fallback: Arc<tokio::sync::RwLock<FallbackData>>,
}
```

---

## Three Construction Variants

Every storage struct provides exactly three constructors:

1. **`new(endpoint)`** -- Tries to connect; falls back to in-memory if unavailable
2. **`new_fallback()`** -- Always creates in-memory only (for testing / no-infra)
3. **`with_client(client)`** -- Dependency injection with an existing client

**Real example** (`crates/uc-engine/src/memory/short_term.rs:40-89`):

```rust
// Variant 1: Try connect, fall back on error
#[cfg(feature = "storage")]
pub async fn new(pd_endpoints: Vec<String>, ttl_seconds: u64) -> Result<Self, EngineError> {
    match tikv_client::RawClient::new(pd_endpoints).await {
        Ok(client) => {
            tracing::info!("Connected to TiKV for short-term memory");
            Ok(Self { client: Some(Arc::new(client)), fallback: Arc::new(RwLock::new(Vec::new())), ... })
        }
        Err(e) => {
            tracing::warn!("TiKV unavailable, using in-memory fallback: {}", e);
            Ok(Self { client: None, fallback: Arc::new(RwLock::new(Vec::new())), ... })
        }
    }
}

// Variant 2: In-memory only
pub fn new_fallback(ttl_seconds: u64) -> Self {
    Self { #[cfg(feature = "storage")] client: None, fallback: Arc::new(RwLock::new(Vec::new())), ... }
}

// Variant 3: Dependency injection
#[cfg(feature = "storage")]
pub fn with_client(client: Arc<tikv_client::RawClient>, ttl_seconds: u64) -> Self {
    Self { client: Some(client), fallback: Arc::new(RwLock::new(Vec::new())), ... }
}
```

---

## Dual-Path Read/Write

Every method uses `#[cfg(feature = "storage")]` + `if let Some(client)` to branch between real storage and fallback:

```rust
pub async fn read(&self, key: &MemoryKey) -> Result<Option<MemoryEntry>, EngineError> {
    #[cfg(feature = "storage")]
    if let Some(client) = &self.client {
        // Real storage path: use client.get(), client.scan(), etc.
    } else {
        // Fallback path: use self.fallback.read().await
    }
    #[cfg(not(feature = "storage"))]
    {
        // Fallback path (duplicated for non-storage builds)
    }
}
```

The fallback path is **duplicated** in both the `else` branch and the `#[cfg(not(feature = "storage"))]` block. This is intentional -- the Rust compiler eliminates the dead code in each build configuration.

---

## Key Encoding Pattern

TiKV keys use a structured prefix for efficient prefix scanning.

**Format**: `memory:{scope}:{scope_id}:{key}`

| Scope | Key Format | Example |
|-------|-----------|---------|
| Task | `memory:task:{task_id}:{key}` | `memory:task:abc123:decisions` |
| Project | `memory:project:{project_id}:{key}` | `memory:project:proj1:architecture` |
| Global | `memory:global:{key}` | `memory:global:conventions` |

Implementation (`crates/uc-engine/src/memory/short_term.rs:262-274`):

```rust
pub fn encode_key(key: &MemoryKey) -> String {
    match key {
        MemoryKey::Task { task_id, key: inner } => format!("memory:task:{}:{}", task_id, inner),
        MemoryKey::Project { project_id, key: inner } => format!("memory:project:{}:{}", project_id, inner),
        MemoryKey::Global { key: inner } => format!("memory:global:{}", inner),
    }
}
```

Prefix scanning uses `scope_prefix()` for listing:

```rust
pub fn scope_prefix(key: &MemoryKey) -> String {
    match key {
        MemoryKey::Task { task_id, .. } => format!("memory:task:{}:", task_id),
        MemoryKey::Project { project_id, .. } => format!("memory:project:{}:", project_id),
        MemoryKey::Global { .. } => "memory:global:".to_string(),
    }
}
```

---

## PostgreSQL Migrations

Migrations run at startup via `run_migrations()` (`crates/uc-engine/src/metadata/postgres.rs:134-244`).

**Tables**:

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `repos` | Registered repositories | `repo_id TEXT UNIQUE`, `remote_url`, `default_branch`, `local_path` |
| `index_state` | Per-repo index state | `repo_id TEXT REFERENCES repos(repo_id)`, `last_indexed_sha`, `health` |
| `symbols` | AST symbol records | `repo_id`, `file_path`, `name`, `kind`, `start_line/col`, `end_line/col`, `language` |
| `references` | Cross-file references | `repo_id`, `source_symbol_id REFERENCES symbols(id)`, `target_name`, `reference_kind` |

**Indexes** (`crates/uc-engine/src/metadata/postgres.rs:224-233`):

```rust
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
```

**Delete ordering** respects foreign keys: references -> symbols -> index_state -> repos.

**Migration additions (2026-06-16)** — IndexState count columns:

```sql
ALTER TABLE index_state ADD COLUMN IF NOT EXISTS files_count BIGINT NOT NULL DEFAULT 0;
ALTER TABLE index_state ADD COLUMN IF NOT EXISTS symbols_count BIGINT NOT NULL DEFAULT 0;
ALTER TABLE index_state ADD COLUMN IF NOT EXISTS chunks_count BIGINT NOT NULL DEFAULT 0;
```

These columns are populated by `IndexPipeline` after indexing. The `IF NOT EXISTS` + `DEFAULT 0` ensures the migration is additive and non-breaking — existing rows get 0 (matching previous hardcoded behavior).

**Upsert pattern** uses `ON CONFLICT ... DO UPDATE`:

```sql
INSERT INTO repos (repo_id, remote_url, default_branch, local_path)
VALUES ($1, $2, $3, $4)
ON CONFLICT (repo_id) DO UPDATE SET
    remote_url = EXCLUDED.remote_url,
    default_branch = EXCLUDED.default_branch,
    local_path = EXCLUDED.local_path,
    updated_at = NOW()
```

---

## Health Check Pattern

Each storage component provides `is_connected()` and contributes to a `Vec<ComponentHealth>`:

```rust
// crates/uc-engine/src/memory/mod.rs:199-228
pub fn health(&self) -> Vec<ComponentHealth> {
    vec![
        ComponentHealth {
            name: "short_term_memory".into(),
            status: if self.short_term.is_connected() { "ok".into() } else { "fallback".into() },
            details: if self.short_term.is_connected() {
                Some("TiKV connected".into())
            } else {
                Some("Using in-memory fallback".into())
            },
        },
        // ... long_term_memory similarly
    ]
}
```

---

## Common Mistakes

1. **Forgetting the `#[cfg(not(feature = "storage"))]` block** -- Without it, the code won't compile when the `storage` feature is disabled. The fallback logic must be duplicated.

2. **Not using `map_err` to wrap third-party errors** -- Storage client errors must be converted to `EngineError` variants. Never let raw `tikv_client::Error` or `sqlx::Error` leak through the API boundary.

3. **Using `unwrap()` on storage operations** -- Always use `.map_err(|e| EngineError::ConnectionError(...))` or `.map_err(|e| EngineError::MemoryReadError(...))` instead.

4. **Not suppressing unused variables in the non-storage path** -- When `value` is only used in the `storage` feature path, add `let _ = value;` in the `not(storage)` block to avoid compiler warnings.

5. **Using zero vectors for memory search queries** -- `MemoryStore::search()` previously used `vec![0.0f32; VECTOR_SIZE]` as the query embedding, which always returns empty results. The fix uses `EmbeddingService` to generate real embeddings (BLAKE3 fallback or Voyage Code 3 API). If you add a new search path that needs embeddings, always use `embedding_service.embed_single(text)` — never hardcode a zero vector.

6. **Hardcoding IndexState counts to 0** -- `LocalEngine::get_index_state()` previously returned `files_count: 0, symbols_count: 0, chunks_count: 0` regardless of actual indexed content. The fix reads these from `IndexState` (which `IndexPipeline` populates at index time). If you add new count fields, follow the same pattern: add to `IndexState`, populate in `IndexPipeline`, read in `LocalEngine`, add PostgreSQL migration with `DEFAULT 0`. Note: incremental indexing accumulates counts (adds newly indexed files/symbols/chunks to existing totals), but does not decrement for deleted files. Counts become accurate again after the next full reindex (which resets to absolute values).

7. **Not generating embeddings when writing to long-term memory** -- `MemoryStore::write()` must generate embeddings for high-importance entries before storing them in long-term memory. Without embeddings, the entry is stored with a zero vector and semantic search can never find it. The embedding generation is best-effort: on failure, log a warning and store with zero vector (graceful degradation).

8. **BLAKE3 key-vs-content embedding mismatch in semantic read** -- When `read()` uses `include_semantic=true`, it derives the query from the key text (via `key_to_query_text()`), but `write()` embeds from the content text. With Voyage semantic embeddings, key and content vectors are semantically related and will match. With BLAKE3 fallback, they produce **unrelated** hash-derived vectors — semantic read will almost never find the entry. This is a known limitation of the BLAKE3 fallback: it guarantees `search(query_text)` finds entries written with similar `query_text`, but `read(key)` → semantic lookup will miss entries written via `write()` where key ≠ content. Full semantic read requires Voyage Code 3 or another real embedding model.
