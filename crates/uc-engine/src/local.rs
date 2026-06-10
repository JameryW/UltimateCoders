//! Local engine — in-process implementation of EngineApi.
//!
//! Directly calls core components (indexer, memory, scheduler, search)
//! without any network overhead. Used in single-machine deployments
//! via PyO3 FFI.

use uc_types::{
    async_trait, EngineApi, EngineError, HealthStatus,
    IndexRequest, IndexResponse,
    MemoryEntry, MemoryKey, MemoryReadRequest, MemorySearchRequest,
    MemorySearchResponse, MemoryWriteRequest,
    RepoIndexState, SearchResult, SearchQuery,
};

use crate::config::EngineConfig;
use crate::memory::short_term::ShortTermMemory;
use crate::memory::long_term::LongTermMemory;
use crate::memory::MemoryStore;
use crate::metadata::postgres::PostgresMetadataStore;
use crate::indexer::IndexPipeline;
use crate::indexer::semantic::EmbeddingService;
use crate::search::HybridSearchEngine;
use crate::search::SemanticSearchEngine;

use std::sync::Arc;
use std::time::Instant;

/// Local engine that runs all components in-process.
pub struct LocalEngine {
    memory_store: Arc<MemoryStore>,
    metadata_store: Arc<PostgresMetadataStore>,
    index_pipeline: Arc<IndexPipeline>,
    search_engine: Arc<HybridSearchEngine>,
    #[allow(dead_code)]
    config: EngineConfig,
    start_time: Instant,
}

impl LocalEngine {
    /// Create a new local engine with explicit configuration.
    ///
    /// Connects to TiKV, Qdrant, and PostgreSQL using the configured endpoints.
    /// If any storage backend is unavailable, falls back to in-memory storage.
    #[cfg(feature = "storage")]
    pub async fn new(config: EngineConfig) -> Result<Self, EngineError> {
        // Initialize short-term memory (TiKV)
        let short_term = Arc::new(
            ShortTermMemory::new(
                config.storage.tikv_pd_endpoints.clone(),
                config.memory.task_ttl_seconds,
            )
            .await?,
        );

        // Initialize long-term memory (Qdrant)
        let long_term = Arc::new(
            LongTermMemory::new(
                &config.storage.qdrant_url,
                config.storage.qdrant_api_key.as_deref(),
            )
            .await?,
        );

        // Initialize metadata store (PostgreSQL)
        let metadata_store = Arc::new(
            PostgresMetadataStore::new(&config.storage.pg_url).await?,
        );

        // Create unified memory store
        let memory_store = Arc::new(MemoryStore::new(
            short_term,
            long_term.clone(),
            config.memory.clone(),
        ));

        // Create embedding service
        let embedding_service = Arc::new(EmbeddingService::new(config.embedding.clone()));

        // Create index pipeline with semantic support
        let index_pipeline = Arc::new(IndexPipeline::with_semantic(
            metadata_store.clone(),
            embedding_service.clone(),
            long_term.clone(),
        ));

        // Create semantic search engine
        let semantic_search = Arc::new(SemanticSearchEngine::new(
            index_pipeline.semantic_indexer().unwrap().clone(),
            long_term,
        ));

        // Create hybrid search engine with semantic
        let search_engine = Arc::new(HybridSearchEngine::with_semantic(
            index_pipeline.clone(),
            semantic_search,
        ));

        Ok(Self {
            memory_store,
            metadata_store,
            index_pipeline,
            search_engine,
            config,
            start_time: Instant::now(),
        })
    }

    /// Create a new local engine with explicit configuration (fallback-only when storage feature is disabled).
    #[cfg(not(feature = "storage"))]
    pub async fn new(config: EngineConfig) -> Result<Self, EngineError> {
        Ok(Self::new_fallback_with_config(config))
    }

    /// Create a new local engine with default configuration (from env vars).
    pub async fn from_env() -> Result<Self, EngineError> {
        Self::new(EngineConfig::from_env()).await
    }

    /// Create a new local engine for testing with all in-memory fallbacks.
    pub fn new_fallback() -> Self {
        let short_term = Arc::new(ShortTermMemory::new_fallback(3600));
        let long_term = Arc::new(LongTermMemory::new_fallback());
        let metadata_store = Arc::new(PostgresMetadataStore::new_fallback());

        let memory_store = Arc::new(MemoryStore::new_with_default_config(
            short_term,
            long_term.clone(),
        ));

        let embedding_service = Arc::new(EmbeddingService::new_fallback());

        let index_pipeline = Arc::new(IndexPipeline::with_semantic(
            metadata_store.clone(),
            embedding_service.clone(),
            long_term.clone(),
        ));

        let semantic_search = Arc::new(SemanticSearchEngine::new(
            index_pipeline.semantic_indexer().unwrap().clone(),
            long_term,
        ));

        let search_engine = Arc::new(HybridSearchEngine::with_semantic(
            index_pipeline.clone(),
            semantic_search,
        ));

        Self {
            memory_store,
            metadata_store,
            index_pipeline,
            search_engine,
            config: EngineConfig::default(),
            start_time: Instant::now(),
        }
    }

    /// Create a new local engine for testing with all in-memory fallbacks and custom config.
    #[cfg(not(feature = "storage"))]
    fn new_fallback_with_config(config: EngineConfig) -> Self {
        let short_term = Arc::new(ShortTermMemory::new_fallback(config.memory.task_ttl_seconds));
        let long_term = Arc::new(LongTermMemory::new_fallback());
        let metadata_store = Arc::new(PostgresMetadataStore::new_fallback());

        let memory_store = Arc::new(MemoryStore::new(
            short_term,
            long_term.clone(),
            config.memory.clone(),
        ));

        let embedding_service = Arc::new(EmbeddingService::new(config.embedding.clone()));

        let index_pipeline = Arc::new(IndexPipeline::with_semantic(
            metadata_store.clone(),
            embedding_service.clone(),
            long_term.clone(),
        ));

        let semantic_search = Arc::new(SemanticSearchEngine::new(
            index_pipeline.semantic_indexer().unwrap().clone(),
            long_term,
        ));

        let search_engine = Arc::new(HybridSearchEngine::with_semantic(
            index_pipeline.clone(),
            semantic_search,
        ));

        Self {
            memory_store,
            metadata_store,
            index_pipeline,
            search_engine,
            config,
            start_time: Instant::now(),
        }
    }

    /// Get the memory store (for direct access from tests or other components).
    pub fn memory_store(&self) -> &Arc<MemoryStore> {
        &self.memory_store
    }

    /// Get the metadata store (for direct access).
    pub fn metadata_store(&self) -> &Arc<PostgresMetadataStore> {
        &self.metadata_store
    }

    /// Get the index pipeline (for direct access).
    pub fn index_pipeline(&self) -> &Arc<IndexPipeline> {
        &self.index_pipeline
    }

    /// Get the search engine (for direct access).
    pub fn search_engine(&self) -> &Arc<HybridSearchEngine> {
        &self.search_engine
    }
}

#[async_trait]
impl EngineApi for LocalEngine {
    async fn search(&self, query: SearchQuery) -> Result<SearchResult, EngineError> {
        self.search_engine.search(&query).await
    }

    async fn index_repo(&self, request: IndexRequest) -> Result<IndexResponse, EngineError> {
        self.index_pipeline.index_repo(&request).await
    }

    async fn get_index_state(&self, repo_id: &str) -> Result<RepoIndexState, EngineError> {
        let result = self.index_pipeline.get_index_state(repo_id).await?;
        match result {
            Some(state) => Ok(RepoIndexState {
                repo_id: state.repo_id,
                indexed: true,
                last_indexed_sha: Some(state.last_indexed_sha),
                files_count: 0,
                symbols_count: 0,
                chunks_count: 0,
            }),
            None => Ok(RepoIndexState {
                repo_id: repo_id.to_string(),
                indexed: false,
                last_indexed_sha: None,
                files_count: 0,
                symbols_count: 0,
                chunks_count: 0,
            }),
        }
    }

    async fn remove_index(&self, repo_id: &str) -> Result<(), EngineError> {
        self.index_pipeline.remove_index(repo_id).await
    }

    async fn read_memory(&self, request: MemoryReadRequest) -> Result<Option<MemoryEntry>, EngineError> {
        self.memory_store.read(request).await
    }

    async fn write_memory(&self, request: MemoryWriteRequest) -> Result<MemoryEntry, EngineError> {
        self.memory_store.write(request).await
    }

    async fn delete_memory(&self, key: &MemoryKey) -> Result<(), EngineError> {
        self.memory_store.delete(key).await
    }

    async fn search_memory(
        &self,
        request: MemorySearchRequest,
    ) -> Result<MemorySearchResponse, EngineError> {
        self.memory_store.search(request).await
    }

    async fn health(&self) -> Result<HealthStatus, EngineError> {
        let memory_health = self.memory_store.health();
        let metadata_status = if self.metadata_store.is_connected() {
            "ok"
        } else {
            "fallback"
        };
        let metadata_details = if self.metadata_store.is_connected() {
            Some("PostgreSQL connected".into())
        } else {
            Some("Using in-memory fallback".into())
        };

        let overall_status = if memory_health.iter().all(|c| c.status == "ok")
            && metadata_status == "ok"
        {
            "ok"
        } else if memory_health.iter().any(|c| c.status == "fallback")
            || metadata_status == "fallback"
        {
            "degraded"
        } else {
            "error"
        };

        let mut components = memory_health;
        components.push(uc_types::engine::ComponentHealth {
            name: "metadata_store".into(),
            status: metadata_status.into(),
            details: metadata_details,
        });
        components.push(uc_types::engine::ComponentHealth {
            name: "index_pipeline".into(),
            status: "ok".into(),
            details: Some("Index pipeline ready".into()),
        });
        components.push(uc_types::engine::ComponentHealth {
            name: "search_engine".into(),
            status: "ok".into(),
            details: Some("Hybrid search engine ready".into()),
        });
        components.push(uc_types::engine::ComponentHealth {
            name: "embedding_service".into(),
            status: if self.index_pipeline.semantic_indexer().is_some() {
                "ok"
            } else {
                "disabled"
            }
            .into(),
            details: Some(if self.index_pipeline.semantic_indexer().is_some() {
                "Embedding service configured".into()
            } else {
                "Embedding service not configured".into()
            }),
        });

        Ok(HealthStatus {
            status: overall_status.into(),
            version: env!("CARGO_PKG_VERSION").into(),
            uptime_seconds: self.start_time.elapsed().as_secs(),
            components,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use uc_types::memory::{MemoryContent, MemoryMetadata, MemoryReadRequest};

    #[cfg(feature = "indexing")]
    use uc_types::index::{IndexRequest, RepoSpec};
    #[cfg(feature = "indexing")]
    use uc_types::search::SearchMode;

    #[tokio::test]
    async fn local_engine_fallback_creates() {
        let engine = LocalEngine::new_fallback();
        let health = engine.health().await.unwrap();
        assert_eq!(health.status, "degraded"); // Using fallbacks
        // Now has 6 components: short_term, long_term, metadata, index_pipeline, search_engine, embedding_service
        assert_eq!(health.components.len(), 6);
    }

    #[tokio::test]
    async fn local_engine_fallback_health_check() {
        let engine = LocalEngine::new_fallback();
        let health = engine.health().await.unwrap();

        assert_eq!(health.components[0].name, "short_term_memory");
        assert_eq!(health.components[0].status, "fallback");
        assert_eq!(health.components[1].name, "long_term_memory");
        assert_eq!(health.components[1].status, "fallback");
        assert_eq!(health.components[2].name, "metadata_store");
        assert_eq!(health.components[2].status, "fallback");
        assert_eq!(health.components[3].name, "index_pipeline");
        assert_eq!(health.components[3].status, "ok");
        assert_eq!(health.components[4].name, "search_engine");
        assert_eq!(health.components[4].status, "ok");
        assert_eq!(health.components[5].name, "embedding_service");
        assert_eq!(health.components[5].status, "ok");
    }

    #[tokio::test]
    async fn local_engine_fallback_memory_write_read() {
        let engine = LocalEngine::new_fallback();

        let key = MemoryKey::Task {
            task_id: "test-task".to_string(),
            key: "decisions".to_string(),
        };

        let write_result = engine.write_memory(MemoryWriteRequest {
            key: key.clone(),
            content: MemoryContent::Text("Use PostgreSQL for metadata".to_string()),
            metadata: MemoryMetadata {
                source_agent: "test".to_string(),
                importance: 0.5,
                tags: vec!["test".to_string()],
                embedding: None,
            },
        }).await.unwrap();

        assert_eq!(write_result.key, key);

        let read_result = engine.read_memory(MemoryReadRequest {
            key: key.clone(),
            include_semantic: false,
        }).await.unwrap();

        assert!(read_result.is_some());
        let entry = read_result.unwrap();
        assert_eq!(entry.id, write_result.id);
    }

    #[tokio::test]
    async fn local_engine_fallback_memory_delete() {
        let engine = LocalEngine::new_fallback();

        let key = MemoryKey::Global { key: "config".to_string() };

        engine.write_memory(MemoryWriteRequest {
            key: key.clone(),
            content: MemoryContent::Text("v1".to_string()),
            metadata: MemoryMetadata {
                source_agent: "test".to_string(),
                importance: 0.5,
                tags: vec!["test".to_string()],
                embedding: None,
            },
        }).await.unwrap();

        engine.delete_memory(&key).await.unwrap();

        let read_result = engine.read_memory(MemoryReadRequest {
            key: key.clone(),
            include_semantic: false,
        }).await.unwrap();
        assert!(read_result.is_none());
    }

    #[cfg(feature = "indexing")]
    #[tokio::test]
    async fn local_engine_index_and_search() {
        let engine = LocalEngine::new_fallback();

        // Create a temp directory with test files
        let temp_dir = std::env::temp_dir().join("uc-test-local-engine-search");
        let _ = std::fs::remove_dir_all(&temp_dir);
        std::fs::create_dir_all(&temp_dir).unwrap();

        std::fs::write(
            temp_dir.join("main.rs"),
            r#"fn main() {
    let database = Database::connect();
    database.query("SELECT * FROM users");
}"#,
        )
        .unwrap();

        // Index
        let request = IndexRequest {
            repo: RepoSpec {
                repo_id: "test-local".to_string(),
                remote_url: String::new(),
                default_branch: "main".to_string(),
                local_path: Some(temp_dir.to_string_lossy().to_string()),
            },
            force_full: true,
        };

        let response = engine.index_repo(request).await.unwrap();
        assert_eq!(response.repo_id, "test-local");
        assert!(response.files_indexed >= 1);

        // Search
        let query = SearchQuery {
            query: "database".to_string(),
            modes: vec![SearchMode::Text],
            repo_ids: vec![],
            languages: vec![],
            path_patterns: vec![],
            max_results: 10,
        };

        let result = engine.search(query).await.unwrap();
        assert!(!result.items.is_empty());
        assert!(result.items[0].file_path.contains("main.rs"));

        // Get index state
        let state = engine.get_index_state("test-local").await.unwrap();
        assert_eq!(state.repo_id, "test-local");
        assert!(state.indexed);

        // Remove index
        engine.remove_index("test-local").await.unwrap();

        // After removal, search should return no results
        let query2 = SearchQuery {
            query: "database".to_string(),
            modes: vec![SearchMode::Text],
            repo_ids: vec!["test-local".to_string()],
            languages: vec![],
            path_patterns: vec![],
            max_results: 10,
        };
        let result2 = engine.search(query2).await.unwrap();
        assert!(result2.items.is_empty());

        let _ = std::fs::remove_dir_all(&temp_dir);
    }

    #[cfg(feature = "indexing")]
    #[tokio::test]
    async fn local_engine_semantic_search() {
        let engine = LocalEngine::new_fallback();

        let temp_dir = std::env::temp_dir().join("uc-test-local-engine-semantic");
        let _ = std::fs::remove_dir_all(&temp_dir);
        std::fs::create_dir_all(&temp_dir).unwrap();

        // Use a simple single-line function so the chunk content matches exactly
        std::fs::write(
            temp_dir.join("search.rs"),
            r#"fn search_by_keyword(query: &str) -> Vec<String> { let index = load_index(); index.lookup(query) }

fn load_index() -> Index { Index::new() }"#,
        )
        .unwrap();

        // Index
        let request = IndexRequest {
            repo: RepoSpec {
                repo_id: "test-semantic-local".to_string(),
                remote_url: String::new(),
                default_branch: "main".to_string(),
                local_path: Some(temp_dir.to_string_lossy().to_string()),
            },
            force_full: true,
        };

        let response = engine.index_repo(request).await.unwrap();
        assert!(response.files_indexed >= 1);
        // Should have embedded chunks
        assert!(response.chunks_embedded > 0);

        // Test semantic search — with BLAKE3 fallback embeddings,
        // we search for the exact chunk content to verify the pipeline works.
        // In production, Voyage Code 3 would provide true semantic matching.
        //
        // The AST indexer extracts the full function node as content.
        // We query with the same text that the AST would extract.
        let query = SearchQuery {
            query: "fn search_by_keyword(query: &str) -> Vec<String> { let index = load_index(); index.lookup(query) }".to_string(),
            modes: vec![SearchMode::Semantic],
            repo_ids: vec!["test-semantic-local".to_string()],
            languages: vec![],
            path_patterns: vec![],
            max_results: 10,
        };

        let result = engine.search(query).await.unwrap();
        // Semantic search should find results (exact content match with BLAKE3 fallback)
        assert!(!result.items.is_empty(), "semantic search should return results for exact content match");

        // Hybrid search should include results from all modes
        let hybrid_query = SearchQuery {
            query: "search_by_keyword".to_string(),
            modes: vec![SearchMode::Hybrid],
            repo_ids: vec!["test-semantic-local".to_string()],
            languages: vec![],
            path_patterns: vec![],
            max_results: 10,
        };

        let hybrid_result = engine.search(hybrid_query).await.unwrap();
        assert!(!hybrid_result.items.is_empty(), "hybrid search should return results");

        let _ = std::fs::remove_dir_all(&temp_dir);
    }
}
