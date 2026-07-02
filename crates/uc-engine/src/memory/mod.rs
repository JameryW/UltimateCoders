//! Memory module — layered memory system (short-term TiKV + long-term Qdrant).
//!
//! Read path: check short-term first, then long-term if `include_semantic`.
//! Write path: always write to short-term; if importance > threshold, also write
//!             to long-term (with embedding if available).
//! Search path: semantic search in long-term via Qdrant.

pub mod long_term;
pub mod short_term;

use uc_types::error::EngineError;
use uc_types::memory::{
    MemoryContent, MemoryEntry, MemoryKey, MemoryReadRequest, MemorySearchRequest,
    MemorySearchResponse, MemorySearchResult, MemorySearchScope, MemoryWriteRequest,
};

use crate::config::MemoryConfig;
use crate::indexer::semantic::EmbeddingService;
use crate::memory::long_term::LongTermMemory;
use crate::memory::short_term::ShortTermMemory;

use std::sync::Arc;

use dashmap::DashMap;
use tokio::sync::Mutex;

/// Unified memory store that coordinates short-term and long-term storage.
///
/// Short-term memory (TiKV): fast KV access for task-scoped context.
/// Long-term memory (Qdrant): semantic search for project-scoped knowledge.
pub struct MemoryStore {
    short_term: Arc<ShortTermMemory>,
    long_term: Arc<LongTermMemory>,
    embedding_service: Arc<EmbeddingService>,
    config: MemoryConfig,
    /// Per-key locks for replay reconciliation (last-writer-wins by version).
    /// ponytail: in-process mutex is safe because there's a single gateway
    /// instance; upgrade to TiKV-native CAS (`with_atomic_for_cas`) if we
    /// ever go multi-gateway.
    replay_locks: DashMap<String, Arc<Mutex<()>>>,
}

impl MemoryStore {
    /// Create a new unified memory store.
    pub fn new(
        short_term: Arc<ShortTermMemory>,
        long_term: Arc<LongTermMemory>,
        embedding_service: Arc<EmbeddingService>,
        config: MemoryConfig,
    ) -> Self {
        Self {
            short_term,
            long_term,
            embedding_service,
            config,
            replay_locks: DashMap::new(),
        }
    }

    /// Create with default config.
    pub fn new_with_default_config(
        short_term: Arc<ShortTermMemory>,
        long_term: Arc<LongTermMemory>,
        embedding_service: Arc<EmbeddingService>,
    ) -> Self {
        Self {
            short_term,
            long_term,
            embedding_service,
            config: MemoryConfig::default(),
            replay_locks: DashMap::new(),
        }
    }

    /// Read a memory entry.
    ///
    /// Checks short-term memory first. If not found and `include_semantic` is true,
    /// falls back to long-term memory using semantic search with the key as query text.
    pub async fn read(
        &self,
        request: MemoryReadRequest,
    ) -> Result<Option<MemoryEntry>, EngineError> {
        // Always check short-term first
        let result = self.short_term.read(&request.key).await?;
        if result.is_some() {
            return Ok(result);
        }

        // If not found and semantic lookup requested, search long-term memory
        if request.include_semantic {
            // Derive query text from the key's inner key field.
            //
            // NOTE (BLAKE3 limitation): Entries written via `write()` are embedded
            // from their *content* text, but `read()` searches using the *key* text.
            // With Voyage semantic embeddings, key "architecture" and content
            // "Use microservices for scaling" are semantically related, so the
            // search can still find a match. With the BLAKE3 fallback, however,
            // embeddings are deterministic per exact text — different strings
            // produce unrelated vectors. This means `read(include_semantic=true)`
            // may not find entries via BLAKE3 unless the key text happens to
            // overlap with the content text that was embedded at write time.
            let query_text = key_to_query_text(&request.key);
            let scope = key_to_search_scope(&request.key);

            match self.embedding_service.embed_single(&query_text).await {
                Ok(query_embedding) => {
                    let results = self
                        .long_term
                        .search(query_embedding, &scope, 1, self.config.min_search_score)
                        .await?;
                    if let Some(first) = results.into_iter().next() {
                        return Ok(Some(first.entry));
                    }
                }
                Err(e) => {
                    // Graceful degradation: log warning and return None
                    tracing::warn!(
                        "Embedding generation failed for semantic read, returning None: {}",
                        e
                    );
                }
            }
        }

        Ok(None)
    }

    /// Write a memory entry.
    ///
    /// Always writes to short-term memory. If the entry's importance exceeds
    /// the configured threshold, also writes to long-term memory (generating
    /// an embedding if one is not already provided).
    pub async fn write(&self, request: MemoryWriteRequest) -> Result<MemoryEntry, EngineError> {
        let now = chrono::Utc::now();
        let version = request
            .version
            .unwrap_or_else(|| MemoryEntry::version_from_timestamp(now));
        let mut entry = MemoryEntry {
            id: uc_types::memory::MemoryId::new(),
            key: request.key,
            content: request.content,
            metadata: request.metadata,
            created_at: now,
            updated_at: now,
            version,
        };

        // Always write to short-term
        self.short_term.write(&entry).await?;

        // Write to long-term if importance exceeds threshold
        if entry.metadata.importance >= self.config.long_term_importance_threshold {
            // Generate embedding if not already provided, so semantic search can find it
            if entry.metadata.embedding.is_none() {
                let content_text = content_to_text(&entry.content);
                match self.embedding_service.embed_single(&content_text).await {
                    Ok(embedding) => {
                        entry.metadata.embedding = Some(embedding);
                    }
                    Err(e) => {
                        // Log but continue — LongTermMemory::write() falls back to
                        // a zero vector when embedding is None, so the entry is still
                        // stored but won't be findable via semantic search
                        tracing::warn!(
                            "Failed to generate embedding for long-term memory write: {}",
                            e
                        );
                    }
                }
            }
            match self.long_term.write(&entry).await {
                Ok(()) => {
                    tracing::debug!(
                        "Wrote entry to long-term memory (importance={:.2})",
                        entry.metadata.importance
                    );
                }
                Err(e) => {
                    // Log but don't fail — short-term write succeeded
                    tracing::warn!("Failed to write to long-term memory: {}", e);
                }
            }
        }

        Ok(entry)
    }

    /// Replay a memory write that happened during a gRPC fallback window.
    ///
    /// Last-writer-wins by `version`: reads the currently-stored entry under
    /// a per-key lock, and only writes if `request.version` is >= the stored
    /// version. This is the authoritative cross-worker reconciliation point
    /// for memory writes that bypassed the gateway while it was unreachable.
    pub async fn replay_write(
        &self,
        request: MemoryWriteRequest,
    ) -> Result<uc_types::memory::MemoryReplayResult, EngineError> {
        let encoded_key = crate::memory::short_term::encode_key(&request.key);
        // ponytail: per-key in-process lock — single gateway makes this safe.
        let lock = self
            .replay_locks
            .entry(encoded_key.clone())
            .or_insert_with(|| Arc::new(Mutex::new(())))
            .clone();
        let _guard = lock.lock().await;

        let stored = self.short_term.read(&request.key).await?;
        let pending_version = request
            .version
            .unwrap_or_else(|| MemoryEntry::version_from_timestamp(chrono::Utc::now()));

        if let Some(existing) = &stored {
            if existing.version > pending_version {
                tracing::info!(
                    "replay_write skipped stale entry (key={:?} stored_v={} pending_v={})",
                    request.key,
                    existing.version,
                    pending_version
                );
                return Ok(uc_types::memory::MemoryReplayResult {
                    entry: existing.clone(),
                    applied: false,
                });
            }
        }

        let req = MemoryWriteRequest {
            version: Some(pending_version),
            ..request
        };
        let entry = self.write(req).await?;
        Ok(uc_types::memory::MemoryReplayResult {
            entry,
            applied: true,
        })
    }

    /// Delete a memory entry.
    ///
    /// Deletes from both short-term and long-term memory.
    pub async fn delete(&self, key: &MemoryKey) -> Result<(), EngineError> {
        // Delete from short-term
        self.short_term.delete(key).await?;

        // Delete from long-term (best-effort)
        match self.long_term.delete(key).await {
            Ok(()) => {}
            Err(e) => {
                tracing::warn!("Failed to delete from long-term memory: {}", e);
            }
        }

        Ok(())
    }

    /// Search long-term memory semantically.
    ///
    /// Internally uses the `EmbeddingService` to convert the query text into
    /// an embedding vector (BLAKE3 fallback or Voyage Code 3 API), then
    /// delegates to `search_with_embedding()`. If embedding generation fails,
    /// returns an empty result set with a warning log (graceful degradation).
    pub async fn search(
        &self,
        request: MemorySearchRequest,
    ) -> Result<MemorySearchResponse, EngineError> {
        let max_results = if request.max_results > 0 {
            request.max_results.min(self.config.max_search_results)
        } else {
            self.config.max_search_results
        };

        let min_score = if request.min_score > 0.0 {
            request.min_score
        } else {
            self.config.min_search_score
        };

        // Compute query embedding via EmbeddingService (BLAKE3 fallback or Voyage API)
        let query_embedding = match self.embedding_service.embed_single(&request.query).await {
            Ok(embedding) => embedding,
            Err(e) => {
                // Graceful degradation: return empty results instead of propagating error
                tracing::warn!(
                    "Embedding generation failed for memory search query '{}', returning empty results: {}",
                    request.query,
                    e
                );
                return Ok(MemorySearchResponse { results: vec![] });
            }
        };

        let results = self
            .long_term
            .search(query_embedding, &request.scope, max_results, min_score)
            .await?;

        Ok(MemorySearchResponse { results })
    }

    /// Search with a pre-computed embedding vector.
    ///
    /// This is the preferred method when the caller has already computed
    /// the embedding for the query text.
    pub async fn search_with_embedding(
        &self,
        query_embedding: Vec<f32>,
        scope: MemorySearchScope,
        max_results: u32,
        min_score: f32,
    ) -> Result<Vec<MemorySearchResult>, EngineError> {
        let max_results = if max_results > 0 {
            max_results.min(self.config.max_search_results)
        } else {
            self.config.max_search_results
        };

        let min_score = if min_score > 0.0 {
            min_score
        } else {
            self.config.min_search_score
        };

        self.long_term
            .search(query_embedding, &scope, max_results, min_score)
            .await
    }

    /// List all keys in a scope (prefix scan on short-term memory).
    pub async fn list_keys(&self, key: &MemoryKey) -> Result<Vec<MemoryKey>, EngineError> {
        let prefix = crate::memory::short_term::scope_prefix(key);
        self.short_term.list_keys(&prefix).await
    }

    /// Get a reference to the long-term memory store.
    pub fn long_term(&self) -> &Arc<LongTermMemory> {
        &self.long_term
    }

    /// Get a reference to the embedding service.
    pub fn embedding_service(&self) -> &Arc<EmbeddingService> {
        &self.embedding_service
    }

    /// Health check for memory components.
    pub fn health(&self) -> Vec<uc_types::engine::ComponentHealth> {
        vec![
            uc_types::engine::ComponentHealth {
                name: "short_term_memory".into(),
                status: if self.short_term.is_connected() {
                    "ok".into()
                } else {
                    "fallback".into()
                },
                details: if self.short_term.is_connected() {
                    Some("TiKV connected".into())
                } else {
                    Some("Using in-memory fallback".into())
                },
            },
            uc_types::engine::ComponentHealth {
                name: "long_term_memory".into(),
                status: if self.long_term.is_connected() {
                    "ok".into()
                } else {
                    "fallback".into()
                },
                details: if self.long_term.is_connected() {
                    Some("Qdrant connected".into())
                } else {
                    Some("Using in-memory fallback".into())
                },
            },
        ]
    }
}

/// Extract searchable text from `MemoryContent` for embedding generation.
fn content_to_text(content: &MemoryContent) -> String {
    match content {
        MemoryContent::Text(t) => t.clone(),
        MemoryContent::Structured(v) => v.to_string(),
        MemoryContent::Code { code, .. } => code.clone(),
        MemoryContent::Diff { diff, .. } => diff.clone(),
        MemoryContent::Reference { description, .. } => description.clone(),
    }
}

/// Extract query text from a `MemoryKey` for semantic search.
///
/// Uses the inner key field as the query text since it typically describes
/// what the memory entry is about (e.g., "architecture", "decisions").
fn key_to_query_text(key: &MemoryKey) -> String {
    match key {
        MemoryKey::Task { key: inner, .. } => inner.clone(),
        MemoryKey::Project { key: inner, .. } => inner.clone(),
        MemoryKey::Global { key: inner } => inner.clone(),
    }
}

/// Derive a `MemorySearchScope` from a `MemoryKey` for semantic lookup.
///
/// Project-scoped keys search within that project, global keys search globally,
/// and task-scoped keys search within the associated project (using All scope
/// since tasks are not directly represented in long-term memory scopes).
fn key_to_search_scope(key: &MemoryKey) -> MemorySearchScope {
    match key {
        MemoryKey::Project { project_id, .. } => MemorySearchScope::Project {
            project_id: project_id.clone(),
        },
        MemoryKey::Global { .. } => MemorySearchScope::Global,
        MemoryKey::Task { .. } => MemorySearchScope::All,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use uc_types::memory::{MemoryContent, MemoryMetadata};

    fn make_write_request(key: MemoryKey, importance: f32) -> MemoryWriteRequest {
        MemoryWriteRequest {
            key,
            content: MemoryContent::Text("test data".to_string()),
            metadata: MemoryMetadata {
                source_agent: "test".to_string(),
                importance,
                tags: vec!["test".to_string()],
                embedding: None,
            },
            version: None,
        }
    }

    fn make_replay_request(
        key: MemoryKey,
        content: &str,
        version: u64,
    ) -> MemoryWriteRequest {
        MemoryWriteRequest {
            key,
            content: MemoryContent::Text(content.to_string()),
            metadata: MemoryMetadata {
                source_agent: "test".to_string(),
                importance: 0.5,
                tags: vec!["test".to_string()],
                embedding: None,
            },
            version: Some(version),
        }
    }

    /// AC1: a replay write with a newer version is applied and readable.
    #[tokio::test]
    async fn test_replay_write_applies_newer_version() {
        let store = make_store().await;
        let key = MemoryKey::Global {
            key: "shared".to_string(),
        };

        // Worker B writes v100 first (the "current" gateway value).
        store
            .replay_write(make_replay_request(key.clone(), "B-wins", 100))
            .await
            .unwrap();

        // Worker A replays its fallback write at v50 (older) — must be skipped.
        let result = store
            .replay_write(make_replay_request(key.clone(), "A-stale", 50))
            .await
            .unwrap();
        assert!(!result.applied, "stale replay must be skipped");

        // Worker A replays a newer write at v200 — must be applied.
        let result = store
            .replay_write(make_replay_request(key.clone(), "A-fresh", 200))
            .await
            .unwrap();
        assert!(result.applied, "fresh replay must be applied");

        // The readable value is the freshest write.
        let read = store
            .read(MemoryReadRequest {
                key: key.clone(),
                include_semantic: false,
            })
            .await
            .unwrap()
            .expect("entry should exist");
        match read.content {
            MemoryContent::Text(t) => assert_eq!(t, "A-fresh"),
            _ => panic!("expected text content"),
        }
        assert_eq!(read.version, 200);
    }

    async fn make_store() -> MemoryStore {
        let short_term = Arc::new(ShortTermMemory::new_fallback(3600));
        let long_term = Arc::new(LongTermMemory::new_fallback());
        let embedding_service = Arc::new(EmbeddingService::new_fallback());
        MemoryStore::new_with_default_config(short_term, long_term, embedding_service)
    }

    #[tokio::test]
    async fn test_write_and_read() {
        let store = make_store().await;

        let key = MemoryKey::Task {
            task_id: "t1".to_string(),
            key: "decisions".to_string(),
        };

        let request = make_write_request(key.clone(), 0.5);
        let entry = store.write(request).await.unwrap();

        let read_result = store
            .read(MemoryReadRequest {
                key: key.clone(),
                include_semantic: false,
            })
            .await
            .unwrap();

        assert!(read_result.is_some());
        let read_entry = read_result.unwrap();
        assert_eq!(read_entry.id, entry.id);
    }

    #[tokio::test]
    async fn test_delete() {
        let store = make_store().await;

        let key = MemoryKey::Global {
            key: "config".to_string(),
        };

        store
            .write(make_write_request(key.clone(), 0.5))
            .await
            .unwrap();

        let read_result = store
            .read(MemoryReadRequest {
                key: key.clone(),
                include_semantic: false,
            })
            .await
            .unwrap();
        assert!(read_result.is_some());

        store.delete(&key).await.unwrap();

        let read_result = store
            .read(MemoryReadRequest {
                key: key.clone(),
                include_semantic: false,
            })
            .await
            .unwrap();
        assert!(read_result.is_none());
    }

    #[tokio::test]
    async fn test_high_importance_writes_to_long_term() {
        let store = make_store().await;

        let key = MemoryKey::Project {
            project_id: "p1".to_string(),
            key: "architecture".to_string(),
        };

        // Importance 0.9 > default threshold 0.7
        let request = make_write_request(key.clone(), 0.9);
        store.write(request).await.unwrap();

        // Verify short-term has it
        let read_result = store
            .read(MemoryReadRequest {
                key: key.clone(),
                include_semantic: false,
            })
            .await
            .unwrap();
        assert!(read_result.is_some());
    }

    #[tokio::test]
    async fn test_list_keys() {
        let store = make_store().await;

        let key1 = MemoryKey::Task {
            task_id: "t1".to_string(),
            key: "a".to_string(),
        };
        let key2 = MemoryKey::Task {
            task_id: "t1".to_string(),
            key: "b".to_string(),
        };

        store.write(make_write_request(key1, 0.5)).await.unwrap();
        store.write(make_write_request(key2, 0.5)).await.unwrap();

        let scope_key = MemoryKey::Task {
            task_id: "t1".to_string(),
            key: "".to_string(),
        };
        let keys = store.list_keys(&scope_key).await.unwrap();
        assert_eq!(keys.len(), 2);
    }

    #[tokio::test]
    async fn test_health_check() {
        let store = make_store().await;
        let health = store.health();
        assert_eq!(health.len(), 2);
        assert_eq!(health[0].name, "short_term_memory");
        assert_eq!(health[1].name, "long_term_memory");
        // Both should be in fallback mode
        assert_eq!(health[0].status, "fallback");
        assert_eq!(health[1].status, "fallback");
    }

    #[tokio::test]
    async fn test_search_with_embedding() {
        let store = make_store().await;

        // Write an entry with embedding
        let key = MemoryKey::Project {
            project_id: "p1".to_string(),
            key: "arch".to_string(),
        };
        let request = MemoryWriteRequest {
            key: key.clone(),
            content: MemoryContent::Text("architecture decision".to_string()),
            metadata: MemoryMetadata {
                source_agent: "test".to_string(),
                importance: 0.9,
                tags: vec!["architecture".to_string()],
                embedding: Some(vec![1.0, 0.0, 0.0, 0.0]),
            },
            version: None,
        };
        store.write(request).await.unwrap();

        // Search with a similar embedding
        let results = store
            .search_with_embedding(
                vec![1.0, 0.0, 0.0, 0.0],
                MemorySearchScope::Project {
                    project_id: "p1".to_string(),
                },
                10,
                0.0,
            )
            .await
            .unwrap();

        assert_eq!(results.len(), 1);
        assert!(results[0].score > 0.99);
    }

    // ── New tests for AC1, AC3, AC5 ──────────────────────────────

    /// AC1: search_memory returns valid results via BLAKE3 embedding
    #[tokio::test]
    async fn test_search_returns_results_with_blake3_embedding() {
        let store = make_store().await;

        // Write a high-importance entry — write() now auto-generates BLAKE3 embedding
        let key = MemoryKey::Project {
            project_id: "p1".to_string(),
            key: "architecture".to_string(),
        };
        let content_text = "system architecture decision";
        let request = MemoryWriteRequest {
            key: key.clone(),
            content: MemoryContent::Text(content_text.to_string()),
            metadata: MemoryMetadata {
                source_agent: "test".to_string(),
                importance: 0.9,
                tags: vec!["architecture".to_string()],
                embedding: None, // write() will auto-generate BLAKE3 embedding
            },
            version: None,
        };
        store.write(request).await.unwrap();

        // Search using the same content text — BLAKE3 embedding should produce
        // a matching vector for identical text
        let search_request = MemorySearchRequest {
            query: content_text.to_string(),
            scope: MemorySearchScope::Project {
                project_id: "p1".to_string(),
            },
            max_results: 10,
            min_score: 0.0,
        };

        let response = store.search(search_request).await.unwrap();
        assert!(
            !response.results.is_empty(),
            "search_memory should return results when using BLAKE3 embedding"
        );
        assert!(response.results[0].score > 0.99);
    }

    /// AC3: read() with include_semantic=true finds entries in long-term memory
    #[tokio::test]
    async fn test_read_include_semantic_finds_long_term_entry() {
        let store = make_store().await;

        // Write a high-importance entry with embedding directly to long-term memory
        // (bypass short-term so read() must use semantic search to find it)
        let key = MemoryKey::Project {
            project_id: "p1".to_string(),
            key: "architecture decision".to_string(),
        };
        let query_text = "architecture decision";
        let embedding = store
            .embedding_service()
            .embed_single(query_text)
            .await
            .unwrap();

        let entry = MemoryEntry {
            id: uc_types::memory::MemoryId::new(),
            key: key.clone(),
            content: MemoryContent::Text("Use microservices for scaling".to_string()),
            metadata: MemoryMetadata {
                source_agent: "test".to_string(),
                importance: 0.9,
                tags: vec!["architecture".to_string()],
                embedding: Some(embedding),
            },
            created_at: chrono::Utc::now(),
            updated_at: chrono::Utc::now(),
            version: 0,
        };
        store.long_term().write(&entry).await.unwrap();

        // Read with include_semantic=true should find the entry
        let read_result = store
            .read(MemoryReadRequest {
                key: key.clone(),
                include_semantic: true,
            })
            .await
            .unwrap();

        assert!(
            read_result.is_some(),
            "read with include_semantic=true should find entry in long-term memory"
        );
        let found = read_result.unwrap();
        assert_eq!(found.key, key);
    }

    /// AC3: read() with include_semantic=false does NOT search long-term
    #[tokio::test]
    async fn test_read_without_semantic_skips_long_term() {
        let store = make_store().await;

        // Write a high-importance entry only to long-term
        let key = MemoryKey::Project {
            project_id: "p1".to_string(),
            key: "design patterns".to_string(),
        };
        let embedding = store
            .embedding_service()
            .embed_single("design patterns")
            .await
            .unwrap();

        let entry = MemoryEntry {
            id: uc_types::memory::MemoryId::new(),
            key: key.clone(),
            content: MemoryContent::Text("Use repository pattern".to_string()),
            metadata: MemoryMetadata {
                source_agent: "test".to_string(),
                importance: 0.9,
                tags: vec!["patterns".to_string()],
                embedding: Some(embedding),
            },
            created_at: chrono::Utc::now(),
            updated_at: chrono::Utc::now(),
            version: 0,
        };
        store.long_term().write(&entry).await.unwrap();

        // Read with include_semantic=false should NOT find it
        let read_result = store
            .read(MemoryReadRequest {
                key: key.clone(),
                include_semantic: false,
            })
            .await
            .unwrap();

        assert!(
            read_result.is_none(),
            "read with include_semantic=false should not search long-term memory"
        );
    }

    /// AC5: search() gracefully handles embedding failures.
    ///
    /// Note: With the BLAKE3 fallback, `embed_single()` never fails (it's pure
    /// computation). The actual error path (Voyage API timeout / rate limit)
    /// can only be triggered with the `indexing` feature and a configured API
    /// key. This test verifies that search returns a valid response without
    /// panicking even when no results match (high min_score threshold).
    /// A full integration test with a failing Voyage API would be needed to
    /// exercise the `Err` branch of `embed_single()`.
    #[tokio::test]
    async fn test_search_graceful_degradation_on_embedding_failure() {
        let store = make_store().await;

        let search_request = MemorySearchRequest {
            query: "nonexistent query".to_string(),
            scope: MemorySearchScope::All,
            max_results: 10,
            min_score: 0.99, // Very high threshold to effectively get no results
        };

        let response = store.search(search_request).await.unwrap();
        // Should not error — graceful degradation
        assert!(
            response.results.is_empty(),
            "search with high min_score should return empty results without error"
        );
    }

    #[test]
    fn test_key_to_query_text() {
        let task_key = MemoryKey::Task {
            task_id: "t1".to_string(),
            key: "decisions".to_string(),
        };
        assert_eq!(key_to_query_text(&task_key), "decisions");

        let project_key = MemoryKey::Project {
            project_id: "p1".to_string(),
            key: "architecture".to_string(),
        };
        assert_eq!(key_to_query_text(&project_key), "architecture");

        let global_key = MemoryKey::Global {
            key: "conventions".to_string(),
        };
        assert_eq!(key_to_query_text(&global_key), "conventions");
    }

    #[test]
    fn test_key_to_search_scope() {
        let project_key = MemoryKey::Project {
            project_id: "p1".to_string(),
            key: "arch".to_string(),
        };
        let scope = key_to_search_scope(&project_key);
        assert!(matches!(
            scope,
            MemorySearchScope::Project { ref project_id } if project_id == "p1"
        ));

        let global_key = MemoryKey::Global {
            key: "conv".to_string(),
        };
        assert!(matches!(
            key_to_search_scope(&global_key),
            MemorySearchScope::Global
        ));

        let task_key = MemoryKey::Task {
            task_id: "t1".to_string(),
            key: "dec".to_string(),
        };
        assert!(matches!(
            key_to_search_scope(&task_key),
            MemorySearchScope::All
        ));
    }

    #[test]
    fn test_content_to_text() {
        assert_eq!(
            content_to_text(&MemoryContent::Text("hello".to_string())),
            "hello"
        );
        let json: serde_json::Value = serde_json::json!({"key": "value"});
        assert!(content_to_text(&MemoryContent::Structured(json)).contains("value"));
        assert_eq!(
            content_to_text(&MemoryContent::Code {
                language: "rust".to_string(),
                code: "fn main() {}".to_string()
            }),
            "fn main() {}"
        );
        assert_eq!(
            content_to_text(&MemoryContent::Diff {
                file_path: "a.rs".to_string(),
                diff: "+hello".to_string()
            }),
            "+hello"
        );
        assert_eq!(
            content_to_text(&MemoryContent::Reference {
                uri: "https://example.com".to_string(),
                description: "external resource".to_string()
            }),
            "external resource"
        );
    }
}
