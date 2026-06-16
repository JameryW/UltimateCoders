//! Local engine — in-process implementation of EngineApi.
//!
//! Directly calls core components (indexer, memory, scheduler, search)
//! without any network overhead. Used in single-machine deployments
//! via PyO3 FFI.
//!
//! Also includes fault tolerance components:
//! - `CheckpointManager` for event sourcing + snapshot recovery
//! - `ConflictDetector` for intent-based conflict detection
//! - `LlmRateLimiter` for dual-dimension API rate limiting
//! - `CircuitBreaker` for protecting against cascading failures

use uc_types::{
    async_trait, EngineApi, EngineError, HealthStatus, IndexRequest, IndexResponse, MemoryEntry,
    MemoryKey, MemoryReadRequest, MemorySearchRequest, MemorySearchResponse, MemoryWriteRequest,
    RepoIndexState, SearchQuery, SearchResult,
};

use crate::checkpoint::{CheckpointConfig, CheckpointManager};
use crate::circuit_breaker::CircuitBreaker;
use crate::config::EngineConfig;
use crate::conflict::{ConflictDetector, ConflictResult, EditIntent};
use crate::events::{AgentEventType, InMemoryEventStore, TaskSnapshot};
use crate::indexer::semantic::EmbeddingService;
use crate::indexer::IndexPipeline;
use crate::memory::long_term::LongTermMemory;
use crate::memory::short_term::ShortTermMemory;
use crate::memory::MemoryStore;
use crate::metadata::postgres::PostgresMetadataStore;
use crate::rate_limiter::LlmRateLimiter;
use crate::sandbox::subprocess::SubprocessSandbox;
use crate::sandbox::{ExecRequest, ExecResult, Sandbox, SandboxConfig, SandboxHandle};
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
    /// Checkpoint manager for event sourcing + snapshot recovery.
    checkpoint_manager: Arc<CheckpointManager>,
    /// Conflict detector for intent-based conflict detection.
    conflict_detector: Arc<ConflictDetector>,
    /// Rate limiter for LLM API calls.
    rate_limiter: Arc<LlmRateLimiter>,
    /// Circuit breaker for LLM API fault tolerance.
    circuit_breaker: Arc<CircuitBreaker>,
    /// Sandbox for executing coding agents in isolated environments.
    sandbox: Arc<dyn Sandbox>,
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
        let metadata_store = Arc::new(PostgresMetadataStore::new(&config.storage.pg_url).await?);

        // Create embedding service
        let embedding_service = Arc::new(EmbeddingService::new(config.embedding.clone()));

        // Create unified memory store
        let memory_store = Arc::new(MemoryStore::new(
            short_term,
            long_term.clone(),
            embedding_service.clone(),
            config.memory.clone(),
        ));

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

        // Create fault tolerance components
        let event_store = Arc::new(InMemoryEventStore::new());
        let checkpoint_manager = Arc::new(CheckpointManager::new(
            event_store,
            CheckpointConfig::default(),
        ));
        let conflict_detector = Arc::new(ConflictDetector::new());
        let rate_limiter = Arc::new(LlmRateLimiter::with_defaults());
        let circuit_breaker = Arc::new(CircuitBreaker::with_defaults());

        Ok(Self {
            memory_store,
            metadata_store,
            index_pipeline,
            search_engine,
            checkpoint_manager,
            conflict_detector,
            rate_limiter,
            circuit_breaker,
            sandbox: Arc::new(SubprocessSandbox::new()),
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

        let embedding_service = Arc::new(EmbeddingService::new_fallback());

        let memory_store = Arc::new(MemoryStore::new_with_default_config(
            short_term,
            long_term.clone(),
            embedding_service.clone(),
        ));

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

        // Fault tolerance components
        let event_store = Arc::new(InMemoryEventStore::new());
        let checkpoint_manager = Arc::new(CheckpointManager::new(
            event_store,
            CheckpointConfig::default(),
        ));
        let conflict_detector = Arc::new(ConflictDetector::new());
        let rate_limiter = Arc::new(LlmRateLimiter::with_defaults());
        let circuit_breaker = Arc::new(CircuitBreaker::with_defaults());

        Self {
            memory_store,
            metadata_store,
            index_pipeline,
            search_engine,
            checkpoint_manager,
            conflict_detector,
            rate_limiter,
            circuit_breaker,
            sandbox: Arc::new(SubprocessSandbox::new()),
            config: EngineConfig::default(),
            start_time: Instant::now(),
        }
    }

    /// Create a new local engine for testing with all in-memory fallbacks and custom config.
    #[cfg(not(feature = "storage"))]
    fn new_fallback_with_config(config: EngineConfig) -> Self {
        let short_term = Arc::new(ShortTermMemory::new_fallback(
            config.memory.task_ttl_seconds,
        ));
        let long_term = Arc::new(LongTermMemory::new_fallback());
        let metadata_store = Arc::new(PostgresMetadataStore::new_fallback());

        let embedding_service = Arc::new(EmbeddingService::new(config.embedding.clone()));

        let memory_store = Arc::new(MemoryStore::new(
            short_term,
            long_term.clone(),
            embedding_service.clone(),
            config.memory.clone(),
        ));

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

        // Fault tolerance components
        let event_store = Arc::new(InMemoryEventStore::new());
        let checkpoint_manager = Arc::new(CheckpointManager::new(
            event_store,
            CheckpointConfig::default(),
        ));
        let conflict_detector = Arc::new(ConflictDetector::new());
        let rate_limiter = Arc::new(LlmRateLimiter::with_defaults());
        let circuit_breaker = Arc::new(CircuitBreaker::with_defaults());

        Self {
            memory_store,
            metadata_store,
            index_pipeline,
            search_engine,
            checkpoint_manager,
            conflict_detector,
            rate_limiter,
            circuit_breaker,
            sandbox: Arc::new(SubprocessSandbox::new()),
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

    /// Get the checkpoint manager (for direct access).
    pub fn checkpoint_manager(&self) -> &Arc<CheckpointManager> {
        &self.checkpoint_manager
    }

    /// Get the conflict detector (for direct access).
    pub fn conflict_detector(&self) -> &Arc<ConflictDetector> {
        &self.conflict_detector
    }

    /// Get the rate limiter (for direct access).
    pub fn rate_limiter(&self) -> &Arc<LlmRateLimiter> {
        &self.rate_limiter
    }

    /// Get the circuit breaker (for direct access).
    pub fn circuit_breaker(&self) -> &Arc<CircuitBreaker> {
        &self.circuit_breaker
    }

    // ── Fault Tolerance Operations ──────────────────────────────

    /// Record an event to the event sourcing system.
    ///
    /// Events are appended to the event store. If the event count
    /// reaches the snapshot interval, a checkpoint is automatically created.
    pub async fn record_event(
        &self,
        subject: &str,
        event: AgentEventType,
    ) -> Result<u64, EngineError> {
        self.checkpoint_manager.record_event(subject, event).await
    }

    /// Create a checkpoint (snapshot) of a task's current state.
    pub async fn checkpoint_task(&self, task_id: &str) -> Result<String, EngineError> {
        self.checkpoint_manager.create_snapshot(task_id).await
    }

    /// Recover a task from the latest checkpoint + event replay.
    pub async fn recover_task(&self, task_id: &str) -> Result<TaskSnapshot, EngineError> {
        self.checkpoint_manager.recover(task_id).await
    }

    /// Declare an edit intent for conflict detection.
    ///
    /// Workers should call this before modifying a file to declare
    /// their intent. The conflict detector will check for overlapping
    /// edits and return a conflict result.
    pub fn declare_edit_intent(&self, intent: EditIntent) -> ConflictResult {
        self.conflict_detector.declare_intent(intent)
    }

    /// Check for conflicts without declaring an intent.
    pub fn check_conflict(
        &self,
        file_path: &str,
        worker_id: &str,
        regions: &[crate::events::LineRange],
    ) -> ConflictResult {
        self.conflict_detector
            .check_conflict(file_path, worker_id, regions)
    }

    /// Resolve a conflict using three-way merge.
    pub fn resolve_conflict(
        &self,
        base: &str,
        ours: &str,
        theirs: &str,
    ) -> crate::conflict::MergeResult {
        crate::conflict::merger::three_way_merge(base, ours, theirs)
    }

    /// Try to acquire LLM rate limit capacity.
    ///
    /// Call before making an LLM API request. Returns Ok if capacity
    /// is available, Err(RateLimited) otherwise.
    pub fn acquire_rate_limit(&self, estimated_tokens: f64) -> Result<(), EngineError> {
        self.rate_limiter.try_acquire(estimated_tokens)
    }

    /// Release rate limit capacity after a request completes.
    pub fn release_rate_limit(&self) {
        self.rate_limiter.release();
    }

    /// Check if the circuit breaker allows a request.
    pub fn check_circuit_breaker(&self) -> Result<(), EngineError> {
        self.circuit_breaker.allow_request()
    }

    /// Record a successful LLM API call (for circuit breaker).
    pub fn record_llm_success(&self) {
        self.circuit_breaker.record_success();
    }

    /// Record a failed LLM API call (for circuit breaker).
    pub fn record_llm_failure(&self) {
        self.circuit_breaker.record_failure();
    }

    // ── Sandbox Operations ──────────────────────────────────────

    /// Create a sandbox environment.
    ///
    /// Returns a handle to the created sandbox that can be used
    /// for executing commands.
    pub async fn create_sandbox(
        &self,
        config: &SandboxConfig,
    ) -> Result<SandboxHandle, EngineError> {
        self.sandbox.create(config).await
    }

    /// Execute a command in a sandbox.
    ///
    /// The command runs in the isolated environment specified by
    /// the sandbox handle, with resource limits enforced.
    pub async fn execute_in_sandbox(
        &self,
        handle: &SandboxHandle,
        request: ExecRequest,
    ) -> Result<ExecResult, EngineError> {
        self.sandbox.execute(handle, request).await
    }

    /// Stop a sandbox environment.
    ///
    /// Cleans up any resources (containers, processes) associated
    /// with the sandbox.
    pub async fn stop_sandbox(&self, handle: &SandboxHandle) -> Result<(), EngineError> {
        self.sandbox.stop(handle).await
    }

    /// Get the sandbox implementation (for direct access).
    pub fn sandbox(&self) -> &Arc<dyn Sandbox> {
        &self.sandbox
    }

    /// Replace the sandbox implementation.
    ///
    /// Useful for switching from subprocess to Docker mode at runtime.
    pub fn set_sandbox(&mut self, sandbox: Arc<dyn Sandbox>) {
        self.sandbox = sandbox;
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
                // u64 -> u32 truncation is safe: no repo has >4 billion files/symbols/chunks
                files_count: state.files_count as u32,
                symbols_count: state.symbols_count as u32,
                chunks_count: state.chunks_count as u32,
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

    async fn read_memory(
        &self,
        request: MemoryReadRequest,
    ) -> Result<Option<MemoryEntry>, EngineError> {
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

        // Map "fallback" to "degraded" for AC5 compliance (healthy/degraded/unavailable)
        let memory_components: Vec<uc_types::engine::ComponentHealth> = memory_health
            .into_iter()
            .map(|c| {
                let status = match c.status.as_str() {
                    "ok" => "healthy",
                    "fallback" => "degraded",
                    other => other, // keep "error", "disabled", etc.
                };
                uc_types::engine::ComponentHealth {
                    name: c.name,
                    status: status.into(),
                    details: c.details,
                }
            })
            .collect();

        // Metadata store status
        let metadata_status = if self.metadata_store.is_connected() {
            "healthy"
        } else {
            "degraded"
        };
        let metadata_details = if self.metadata_store.is_connected() {
            Some("PostgreSQL connected".into())
        } else {
            Some("Using in-memory fallback".into())
        };

        // Index pipeline: check if available
        let index_pipeline_status = "healthy";
        let index_pipeline_details = Some("Index pipeline ready".into());

        // Search engine: check if hybrid engine has semantic support
        let search_status = if self.index_pipeline.semantic_indexer().is_some() {
            "healthy"
        } else {
            "degraded"
        };
        let search_details = Some(if self.index_pipeline.semantic_indexer().is_some() {
            "Hybrid search (text + semantic) available".into()
        } else {
            "Text search only (semantic unavailable)".into()
        });

        let overall_status = if memory_components.iter().all(|c| c.status == "healthy")
            && metadata_status == "healthy"
            && search_status == "healthy"
        {
            "healthy"
        } else if memory_components.iter().any(|c| c.status == "degraded")
            || metadata_status == "degraded"
            || search_status == "degraded"
        {
            "degraded"
        } else {
            "error"
        };

        let mut components = memory_components;
        components.push(uc_types::engine::ComponentHealth {
            name: "metadata_store".into(),
            status: metadata_status.into(),
            details: metadata_details,
        });
        components.push(uc_types::engine::ComponentHealth {
            name: "index_pipeline".into(),
            status: index_pipeline_status.into(),
            details: index_pipeline_details,
        });
        components.push(uc_types::engine::ComponentHealth {
            name: "search_engine".into(),
            status: search_status.into(),
            details: search_details,
        });
        components.push(uc_types::engine::ComponentHealth {
            name: "embedding_service".into(),
            status: if self.index_pipeline.semantic_indexer().is_some() {
                "healthy"
            } else {
                "unavailable"
            }
            .into(),
            details: Some(if self.index_pipeline.semantic_indexer().is_some() {
                "Embedding service configured".into()
            } else {
                "Embedding service not configured".into()
            }),
        });
        components.push(uc_types::engine::ComponentHealth {
            name: "checkpoint_manager".into(),
            status: "healthy".into(),
            details: Some("Event sourcing + checkpoint ready".into()),
        });
        components.push(uc_types::engine::ComponentHealth {
            name: "conflict_detector".into(),
            status: "healthy".into(),
            details: Some("Intent-based conflict detection ready".into()),
        });
        components.push(uc_types::engine::ComponentHealth {
            name: "rate_limiter".into(),
            status: "healthy".into(),
            details: Some(format!(
                "RPM: {:.0} available, TPM: {:.0} available",
                self.rate_limiter.rpm_available(),
                self.rate_limiter.tpm_available(),
            )),
        });
        components.push(uc_types::engine::ComponentHealth {
            name: "circuit_breaker".into(),
            status: match self.circuit_breaker.state() {
                crate::circuit_breaker::CircuitState::Closed => "healthy",
                crate::circuit_breaker::CircuitState::HalfOpen => "degraded",
                crate::circuit_breaker::CircuitState::Open => "unavailable",
            }
            .into(),
            details: Some(format!(
                "State: {:?}, failures: {}",
                self.circuit_breaker.state(),
                self.circuit_breaker.failure_count(),
            )),
        });
        components.push(uc_types::engine::ComponentHealth {
            name: "sandbox".into(),
            status: "healthy".into(),
            details: Some("Sandbox execution ready".into()),
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
                                               // Now has 11 components: short_term, long_term, metadata, index_pipeline,
                                               // search_engine, embedding_service, checkpoint_manager, conflict_detector,
                                               // rate_limiter, circuit_breaker, sandbox
        assert_eq!(health.components.len(), 11);
    }

    #[tokio::test]
    async fn local_engine_fallback_health_check() {
        let engine = LocalEngine::new_fallback();
        let health = engine.health().await.unwrap();

        assert_eq!(health.components[0].name, "short_term_memory");
        assert_eq!(health.components[0].status, "degraded");
        assert_eq!(health.components[1].name, "long_term_memory");
        assert_eq!(health.components[1].status, "degraded");
        assert_eq!(health.components[2].name, "metadata_store");
        assert_eq!(health.components[2].status, "degraded");
        assert_eq!(health.components[3].name, "index_pipeline");
        assert_eq!(health.components[3].status, "healthy");
        assert_eq!(health.components[4].name, "search_engine");
        assert_eq!(health.components[4].status, "healthy");
        assert_eq!(health.components[5].name, "embedding_service");
        assert_eq!(health.components[5].status, "healthy");
        // Fault tolerance components
        assert_eq!(health.components[6].name, "checkpoint_manager");
        assert_eq!(health.components[6].status, "healthy");
        assert_eq!(health.components[7].name, "conflict_detector");
        assert_eq!(health.components[7].status, "healthy");
        assert_eq!(health.components[8].name, "rate_limiter");
        assert_eq!(health.components[8].status, "healthy");
        assert_eq!(health.components[9].name, "circuit_breaker");
        assert_eq!(health.components[9].status, "healthy");
        // Sandbox component
        assert_eq!(health.components[10].name, "sandbox");
        assert_eq!(health.components[10].status, "healthy");
    }

    #[tokio::test]
    async fn local_engine_fallback_memory_write_read() {
        let engine = LocalEngine::new_fallback();

        let key = MemoryKey::Task {
            task_id: "test-task".to_string(),
            key: "decisions".to_string(),
        };

        let write_result = engine
            .write_memory(MemoryWriteRequest {
                key: key.clone(),
                content: MemoryContent::Text("Use PostgreSQL for metadata".to_string()),
                metadata: MemoryMetadata {
                    source_agent: "test".to_string(),
                    importance: 0.5,
                    tags: vec!["test".to_string()],
                    embedding: None,
                },
            })
            .await
            .unwrap();

        assert_eq!(write_result.key, key);

        let read_result = engine
            .read_memory(MemoryReadRequest {
                key: key.clone(),
                include_semantic: false,
            })
            .await
            .unwrap();

        assert!(read_result.is_some());
        let entry = read_result.unwrap();
        assert_eq!(entry.id, write_result.id);
    }

    #[tokio::test]
    async fn local_engine_fallback_memory_delete() {
        let engine = LocalEngine::new_fallback();

        let key = MemoryKey::Global {
            key: "config".to_string(),
        };

        engine
            .write_memory(MemoryWriteRequest {
                key: key.clone(),
                content: MemoryContent::Text("v1".to_string()),
                metadata: MemoryMetadata {
                    source_agent: "test".to_string(),
                    importance: 0.5,
                    tags: vec!["test".to_string()],
                    embedding: None,
                },
            })
            .await
            .unwrap();

        engine.delete_memory(&key).await.unwrap();

        let read_result = engine
            .read_memory(MemoryReadRequest {
                key: key.clone(),
                include_semantic: false,
            })
            .await
            .unwrap();
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
        assert!(
            !result.items.is_empty(),
            "semantic search should return results for exact content match"
        );

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
        assert!(
            !hybrid_result.items.is_empty(),
            "hybrid search should return results"
        );

        let _ = std::fs::remove_dir_all(&temp_dir);
    }

    #[tokio::test]
    async fn local_engine_event_sourcing_and_recovery() {
        let engine = LocalEngine::new_fallback();
        let task_id = "ft-test-task-1";

        // Record events
        engine
            .record_event(
                &format!("agent.events.{}", task_id),
                AgentEventType::TaskCreated {
                    task_id: uc_types::TaskId(task_id.to_string()),
                    description: "Fault tolerance test".to_string(),
                },
            )
            .await
            .unwrap();

        engine
            .record_event(
                &format!("agent.events.{}", task_id),
                AgentEventType::SubtaskAssigned {
                    task_id: uc_types::TaskId::new(), // TODO: derive task_id from context
                    subtask_id: uc_types::TaskId::new(),
                    worker_id: uc_types::WorkerId::new(),
                },
            )
            .await
            .unwrap();

        // Create checkpoint
        let snapshot_id = engine.checkpoint_task(task_id).await.unwrap();
        assert!(!snapshot_id.is_empty());

        // Recover from checkpoint
        let state = engine.recover_task(task_id).await.unwrap();
        assert_eq!(state.task_id, task_id);
    }

    #[test]
    fn local_engine_conflict_detection() {
        let engine = LocalEngine::new_fallback();

        // Declare first intent
        let intent1 = EditIntent::new(
            "worker-1".to_string(),
            "src/main.rs".to_string(),
            crate::conflict::EditType::Modify,
            vec![crate::events::LineRange::new(1, 20)],
        );
        let result1 = engine.declare_edit_intent(intent1);
        assert!(matches!(result1, ConflictResult::NoConflict));

        // Declare overlapping intent
        let intent2 = EditIntent::new(
            "worker-2".to_string(),
            "src/main.rs".to_string(),
            crate::conflict::EditType::Modify,
            vec![crate::events::LineRange::new(10, 30)],
        );
        let result2 = engine.declare_edit_intent(intent2);
        assert!(matches!(result2, ConflictResult::Conflicting { .. }));
    }

    #[test]
    fn local_engine_three_way_merge() {
        let engine = LocalEngine::new_fallback();

        let base = "line1\nline2\nline3";
        let ours = "line1-modified\nline2\nline3";
        let theirs = "line1\nline2\nline3-modified";

        let result = engine.resolve_conflict(base, ours, theirs);
        assert!(result.success);
        let merged = result.merged.as_ref().unwrap();
        assert!(merged.contains("line1-modified"));
        assert!(merged.contains("line3-modified"));
    }

    #[test]
    fn local_engine_rate_limiting() {
        let engine = LocalEngine::new_fallback();

        // Should be able to acquire with reasonable token estimate
        assert!(engine.acquire_rate_limit(1000.0).is_ok());

        // Release after use
        engine.release_rate_limit();
    }

    #[test]
    fn local_engine_circuit_breaker() {
        let engine = LocalEngine::new_fallback();

        // Circuit should be closed initially
        assert!(engine.check_circuit_breaker().is_ok());

        // Record some failures
        for _ in 0..5 {
            engine.record_llm_failure();
        }

        // Circuit should now be open
        assert!(engine.check_circuit_breaker().is_err());

        // Force close for cleanup
        engine
            .circuit_breaker()
            .force_state(crate::circuit_breaker::CircuitState::Closed);
    }

    #[tokio::test]
    async fn local_engine_sandbox_create_and_execute() {
        let engine = LocalEngine::new_fallback();

        let config = crate::sandbox::SandboxConfig {
            project_path: "/tmp".to_string(),
            working_dir: "/tmp".to_string(),
            ..Default::default()
        };

        let handle = engine.create_sandbox(&config).await.unwrap();
        assert_eq!(handle.status, crate::sandbox::SandboxStatus::Ready);

        let request = crate::sandbox::ExecRequest::new("echo", vec!["hello sandbox".to_string()]);
        let result = engine.execute_in_sandbox(&handle, request).await.unwrap();
        assert_eq!(result.exit_code, 0);
        assert!(result.stdout.contains("hello sandbox"));

        engine.stop_sandbox(&handle).await.unwrap();
    }

    #[tokio::test]
    async fn local_engine_sandbox_execute_timeout() {
        let engine = LocalEngine::new_fallback();

        let config = crate::sandbox::SandboxConfig {
            project_path: "/tmp".to_string(),
            working_dir: "/tmp".to_string(),
            ..Default::default()
        };

        let handle = engine.create_sandbox(&config).await.unwrap();

        let request = crate::sandbox::ExecRequest {
            command: "sleep".to_string(),
            args: vec!["60".to_string()],
            timeout_secs: 1,
            ..Default::default()
        };

        let result = engine.execute_in_sandbox(&handle, request).await.unwrap();
        assert!(result.timed_out);

        engine.stop_sandbox(&handle).await.unwrap();
    }

    /// AC2: get_index_state returns real counts
    #[cfg(feature = "indexing")]
    #[tokio::test]
    async fn local_engine_get_index_state_returns_real_counts() {
        let engine = LocalEngine::new_fallback();

        // Create a temp directory with test files
        let temp_dir = std::env::temp_dir().join("uc-test-get-index-state-counts");
        let _ = std::fs::remove_dir_all(&temp_dir);
        std::fs::create_dir_all(&temp_dir).unwrap();

        std::fs::write(
            temp_dir.join("main.rs"),
            r#"fn main() {
    let config = Config::new();
    println!("Hello!");
}"#,
        )
        .unwrap();

        // Index the repo
        let request = IndexRequest {
            repo: RepoSpec {
                repo_id: "test-counts-repo".to_string(),
                remote_url: String::new(),
                default_branch: "main".to_string(),
                local_path: Some(temp_dir.to_string_lossy().to_string()),
            },
            force_full: true,
        };

        let response = engine.index_repo(request).await.unwrap();
        assert!(response.files_indexed >= 1);

        // get_index_state should return indexed=true with counts
        let state = engine.get_index_state("test-counts-repo").await.unwrap();
        assert_eq!(state.repo_id, "test-counts-repo");
        assert!(state.indexed);
        assert!(
            state.files_count > 0,
            "files_count should be > 0 after indexing, got {}",
            state.files_count
        );

        let _ = std::fs::remove_dir_all(&temp_dir);
    }

    // -- AC1: NotFound error variant tests --

    #[test]
    fn engine_error_not_found_is_not_found() {
        let err = EngineError::NotFound("repo xyz".into());
        assert!(err.is_not_found());
        assert!(!err.is_retryable());
        assert!(!err.should_fallback());
    }

    #[test]
    fn engine_error_other_variants_are_not_not_found() {
        assert!(!EngineError::SearchError("x".into()).is_not_found());
        assert!(!EngineError::IndexError("x".into()).is_not_found());
        assert!(!EngineError::MemoryReadError("x".into()).is_not_found());
        assert!(!EngineError::ConnectionError("x".into()).is_not_found());
        assert!(!EngineError::TimeoutError("x".into()).is_not_found());
        assert!(!EngineError::RateLimited(5).is_not_found());
    }

    #[tokio::test]
    async fn local_engine_health_returns_component_statuses() {
        let engine = LocalEngine::new_fallback();
        let health = engine.health().await.unwrap();

        // Verify key components exist with correct status values
        let component_names: Vec<&str> =
            health.components.iter().map(|c| c.name.as_str()).collect();
        assert!(
            component_names.contains(&"short_term_memory"),
            "missing short_term_memory"
        );
        assert!(
            component_names.contains(&"long_term_memory"),
            "missing long_term_memory"
        );
        assert!(
            component_names.contains(&"search_engine"),
            "missing search_engine"
        );
        assert!(
            component_names.contains(&"index_pipeline"),
            "missing index_pipeline"
        );

        // All statuses should be one of: healthy, degraded, unavailable
        for c in &health.components {
            assert!(
                matches!(
                    c.status.as_str(),
                    "healthy" | "degraded" | "unavailable" | "disabled"
                ),
                "component {} has unexpected status: {}",
                c.name,
                c.status
            );
        }

        // In-memory fallback should report degraded for memory components
        let short_term = health
            .components
            .iter()
            .find(|c| c.name == "short_term_memory")
            .unwrap();
        assert_eq!(short_term.status, "degraded");
        let long_term = health
            .components
            .iter()
            .find(|c| c.name == "long_term_memory")
            .unwrap();
        assert_eq!(long_term.status, "degraded");
    }

    #[tokio::test]
    async fn local_engine_health_overall_status_degraded_with_fallback() {
        let engine = LocalEngine::new_fallback();
        let health = engine.health().await.unwrap();
        // Using in-memory fallbacks, overall should be degraded
        assert_eq!(health.status, "degraded");
    }

    #[tokio::test]
    async fn local_engine_health_component_details_populated() {
        let engine = LocalEngine::new_fallback();
        let health = engine.health().await.unwrap();

        // All components should have details set
        for c in &health.components {
            assert!(c.details.is_some(), "component {} missing details", c.name);
        }
    }
}
