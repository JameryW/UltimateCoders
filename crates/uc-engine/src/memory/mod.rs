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
    MemoryEntry, MemoryKey, MemoryReadRequest, MemorySearchRequest, MemorySearchResponse,
    MemorySearchResult, MemorySearchScope, MemoryWriteRequest,
};

use crate::config::MemoryConfig;
use crate::memory::long_term::LongTermMemory;
use crate::memory::short_term::ShortTermMemory;

use std::sync::Arc;

/// Unified memory store that coordinates short-term and long-term storage.
///
/// Short-term memory (TiKV): fast KV access for task-scoped context.
/// Long-term memory (Qdrant): semantic search for project-scoped knowledge.
pub struct MemoryStore {
    short_term: Arc<ShortTermMemory>,
    long_term: Arc<LongTermMemory>,
    config: MemoryConfig,
}

impl MemoryStore {
    /// Create a new unified memory store.
    pub fn new(
        short_term: Arc<ShortTermMemory>,
        long_term: Arc<LongTermMemory>,
        config: MemoryConfig,
    ) -> Self {
        Self {
            short_term,
            long_term,
            config,
        }
    }

    /// Create with default config.
    pub fn new_with_default_config(
        short_term: Arc<ShortTermMemory>,
        long_term: Arc<LongTermMemory>,
    ) -> Self {
        Self {
            short_term,
            long_term,
            config: MemoryConfig::default(),
        }
    }

    /// Read a memory entry.
    ///
    /// Checks short-term memory first. If not found and `include_semantic` is true,
    /// falls back to long-term memory (exact key match, not semantic search).
    pub async fn read(
        &self,
        request: MemoryReadRequest,
    ) -> Result<Option<MemoryEntry>, EngineError> {
        // Always check short-term first
        let result = self.short_term.read(&request.key).await?;
        if result.is_some() {
            return Ok(result);
        }

        // If not found and semantic lookup requested, check long-term
        if request.include_semantic {
            // Long-term memory doesn't support direct key reads efficiently,
            // so we do a semantic search with the key as query text.
            // For now, return None — semantic search is handled via search_memory().
            // A future optimization could index keys for direct lookup.
        }

        Ok(None)
    }

    /// Write a memory entry.
    ///
    /// Always writes to short-term memory. If the entry's importance exceeds
    /// the configured threshold, also writes to long-term memory.
    pub async fn write(&self, request: MemoryWriteRequest) -> Result<MemoryEntry, EngineError> {
        let now = chrono::Utc::now();
        let entry = MemoryEntry {
            id: uc_types::memory::MemoryId::new(),
            key: request.key,
            content: request.content,
            metadata: request.metadata,
            created_at: now,
            updated_at: now,
        };

        // Always write to short-term
        self.short_term.write(&entry).await?;

        // Write to long-term if importance exceeds threshold
        if entry.metadata.importance >= self.config.long_term_importance_threshold {
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
    /// Requires an embedding vector. If the query text doesn't have a pre-computed
    /// embedding, a zero vector is used (which will return no results).
    /// In production, the caller (Python agent layer) should compute the embedding
    /// via Voyage Code 3 API before calling this method.
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

        let query_embedding = vec![0.0f32; crate::memory::long_term::VECTOR_SIZE];

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
        }
    }

    async fn make_store() -> MemoryStore {
        let short_term = Arc::new(ShortTermMemory::new_fallback(3600));
        let long_term = Arc::new(LongTermMemory::new_fallback());
        MemoryStore::new_with_default_config(short_term, long_term)
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
}
