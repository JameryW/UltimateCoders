//! Indexer module — orchestrates code indexing pipeline.
//!
//! The pipeline:
//! 1. Git clone/fetch repository
//! 2. Walk files in working tree
//! 3. Text index: language-aware tokenization + inverted index
//! 4. AST parse: tree-sitter -> symbols/references -> PostgreSQL
//! 5. Semantic index: AST-aware chunking -> embedding -> Qdrant
//!
//! Supports both full reindex and incremental indexing via git diff.

pub mod ast;
pub mod semantic;
pub mod text;

use uc_types::error::EngineError;
use uc_types::index::{IndexHealth, IndexRequest, IndexResponse, IndexState, RepoSpec};
use uc_types::search::SearchQuery;
use uc_types::search::SearchResult;

use crate::indexer::semantic::{EmbeddingService, SemanticIndexer};
use crate::memory::long_term::LongTermMemory;
use crate::metadata::postgres::PostgresMetadataStore;

use std::sync::Arc;
use std::time::Instant;

/// Index pipeline — coordinates the full indexing workflow.
pub struct IndexPipeline {
    text_index: Arc<tokio::sync::RwLock<text::TextSearchIndex>>,
    ast_indexer: Arc<ast::AstIndexer>,
    metadata: Arc<PostgresMetadataStore>,
    /// Semantic indexer (optional — only present when embedding is configured).
    semantic_indexer: Option<Arc<SemanticIndexer>>,
    /// Long-term memory for Qdrant (optional — only present when semantic indexing is enabled).
    long_term_memory: Option<Arc<LongTermMemory>>,
}

impl IndexPipeline {
    /// Create a new index pipeline.
    pub fn new(metadata: Arc<PostgresMetadataStore>) -> Self {
        Self {
            text_index: Arc::new(tokio::sync::RwLock::new(text::TextSearchIndex::new())),
            ast_indexer: Arc::new(ast::AstIndexer::new()),
            metadata,
            semantic_indexer: None,
            long_term_memory: None,
        }
    }

    /// Create a new index pipeline with semantic indexing support.
    pub fn with_semantic(
        metadata: Arc<PostgresMetadataStore>,
        embedding_service: Arc<EmbeddingService>,
        long_term_memory: Arc<LongTermMemory>,
    ) -> Self {
        Self {
            text_index: Arc::new(tokio::sync::RwLock::new(text::TextSearchIndex::new())),
            ast_indexer: Arc::new(ast::AstIndexer::new()),
            metadata,
            semantic_indexer: Some(Arc::new(SemanticIndexer::new(embedding_service))),
            long_term_memory: Some(long_term_memory),
        }
    }

    /// Set the semantic indexer and long-term memory for embedding support.
    pub fn set_semantic(
        &mut self,
        embedding_service: Arc<EmbeddingService>,
        long_term_memory: Arc<LongTermMemory>,
    ) {
        self.semantic_indexer = Some(Arc::new(SemanticIndexer::new(embedding_service)));
        self.long_term_memory = Some(long_term_memory);
    }

    /// Index a repository (full or incremental).
    pub async fn index_repo(&self, request: &IndexRequest) -> Result<IndexResponse, EngineError> {
        let start = Instant::now();
        let repo_spec = &request.repo;

        // Register the repository in metadata
        self.metadata.register_repo(repo_spec).await?;

        // Check existing index state for incremental indexing
        let existing_state = self.metadata.get_index_state(&repo_spec.repo_id).await?;
        let is_incremental = existing_state.is_some() && !request.force_full;

        if is_incremental {
            self.incremental_index(repo_spec, existing_state.as_ref().unwrap())
                .await
        } else {
            self.full_index(repo_spec).await
        }
        .map(|response| {
            let duration = start.elapsed();
            IndexResponse {
                repo_id: repo_spec.repo_id.clone(),
                files_indexed: response.files_indexed,
                symbols_extracted: response.symbols_extracted,
                chunks_embedded: response.chunks_embedded,
                duration_ms: duration.as_millis() as u64,
            }
        })
    }

    /// Full index: walk all files and index everything.
    async fn full_index(&self, repo_spec: &RepoSpec) -> Result<IndexResponse, EngineError> {
        let mut files_indexed: u32 = 0;
        let mut symbols_extracted: u32 = 0;
        let mut chunks_embedded: u32 = 0;

        // Set index state to Indexing
        let now = chrono::Utc::now();
        self.metadata
            .update_index_state(&IndexState {
                repo_id: repo_spec.repo_id.clone(),
                last_indexed_sha: String::new(),
                last_indexed_at: now,
                last_full_reindex: now,
                index_version: CURRENT_INDEX_VERSION,
                health: IndexHealth::Indexing,
                files_count: 0,
                symbols_count: 0,
                chunks_count: 0,
            })
            .await?;

        // If the repo has a local path, index files from there
        if let Some(local_path) = &repo_spec.local_path {
            let path = std::path::Path::new(local_path);
            if path.exists() {
                // Clear existing data for this repo before full reindex
                {
                    let mut text_idx = self.text_index.write().await;
                    text_idx.remove_repo(&repo_spec.repo_id);
                }
                self.metadata
                    .delete_symbols_for_repo(&repo_spec.repo_id)
                    .await?;

                // Also clear existing semantic embeddings for this repo
                if let Some(semantic) = &self.semantic_indexer {
                    semantic.remove_repo(&repo_spec.repo_id).await?;
                }

                let result = self.index_directory(&repo_spec.repo_id, path).await?;
                files_indexed = result.files_indexed;
                symbols_extracted = result.symbols_extracted;
                chunks_embedded = result.chunks_embedded;
            }
        }

        // Get HEAD SHA for the index state (so incremental indexing knows the baseline)
        let head_sha = self.get_head_sha(repo_spec).await.unwrap_or_default();

        // Update index state to Healthy
        let now = chrono::Utc::now();
        self.metadata
            .update_index_state(&IndexState {
                repo_id: repo_spec.repo_id.clone(),
                last_indexed_sha: head_sha,
                last_indexed_at: now,
                last_full_reindex: now,
                index_version: CURRENT_INDEX_VERSION,
                health: IndexHealth::Healthy,
                files_count: files_indexed as u64,
                symbols_count: symbols_extracted as u64,
                chunks_count: chunks_embedded as u64,
            })
            .await?;

        Ok(IndexResponse {
            repo_id: repo_spec.repo_id.clone(),
            files_indexed,
            symbols_extracted,
            chunks_embedded,
            duration_ms: 0,
        })
    }

    /// Incremental index: compute diff and only process changed files.
    async fn incremental_index(
        &self,
        repo_spec: &RepoSpec,
        existing_state: &IndexState,
    ) -> Result<IndexResponse, EngineError> {
        let local_path = repo_spec.local_path.as_deref().ok_or_else(|| {
            EngineError::IndexingError("incremental indexing requires a local path".into())
        })?;

        let repo_dir = std::path::Path::new(local_path);
        if !repo_dir.exists() {
            return Err(EngineError::IndexingError(format!(
                "path not found: {}",
                local_path
            )));
        }

        // 1. Open the git repo and get current HEAD SHA
        let head_sha = self.get_head_sha(repo_spec).await?;

        // If HEAD hasn't changed, skip indexing
        if head_sha == existing_state.last_indexed_sha {
            return Ok(IndexResponse {
                repo_id: repo_spec.repo_id.clone(),
                files_indexed: 0,
                symbols_extracted: 0,
                chunks_embedded: 0,
                duration_ms: 0,
            });
        }

        // 2. Compute diff between last indexed SHA and HEAD
        let diffs = self
            .compute_diff(repo_spec, &existing_state.last_indexed_sha, &head_sha)
            .await?;

        // If diff computation returns empty (e.g., old SHA is not ancestor of new),
        // fall back to full index
        if diffs.is_empty() && !existing_state.last_indexed_sha.is_empty() {
            tracing::warn!(
                "Could not compute incremental diff for repo {}; falling back to full index",
                repo_spec.repo_id
            );
            return self.full_index(repo_spec).await;
        }

        // 3. Process each changed file
        let mut files_indexed: u32 = 0;
        let mut symbols_extracted: u32 = 0;
        let mut chunks_embedded: u32 = 0;

        // Set index state to Indexing
        self.metadata
            .update_index_state(&IndexState {
                repo_id: repo_spec.repo_id.clone(),
                last_indexed_sha: existing_state.last_indexed_sha.clone(),
                last_indexed_at: chrono::Utc::now(),
                last_full_reindex: existing_state.last_full_reindex,
                index_version: existing_state.index_version,
                health: IndexHealth::Indexing,
                files_count: existing_state.files_count,
                symbols_count: existing_state.symbols_count,
                chunks_count: existing_state.chunks_count,
            })
            .await?;

        for diff in &diffs {
            match diff.kind {
                crate::git::repo_manager::DiffKind::Deleted => {
                    // Remove from all indexes
                    self.remove_file_from_index(&repo_spec.repo_id, &diff.path)
                        .await?;
                }
                crate::git::repo_manager::DiffKind::Added
                | crate::git::repo_manager::DiffKind::Modified => {
                    // Re-index just this file
                    let result = self
                        .index_single_file(&repo_spec.repo_id, repo_dir, &diff.path)
                        .await?;
                    files_indexed += result.files_indexed;
                    symbols_extracted += result.symbols_extracted;
                    chunks_embedded += result.chunks_embedded;
                }
                crate::git::repo_manager::DiffKind::Renamed => {
                    // For renamed, we need the old path too.
                    // Since FileDiff only has the new path, treat as add.
                    // The old path content will be stale but gets cleaned up
                    // on the next full reindex.
                    let result = self
                        .index_single_file(&repo_spec.repo_id, repo_dir, &diff.path)
                        .await?;
                    files_indexed += result.files_indexed;
                    symbols_extracted += result.symbols_extracted;
                    chunks_embedded += result.chunks_embedded;
                }
            }
        }

        // 4. Update index state with new SHA and updated counts.
        // NOTE: counts are additive here — deleted files are removed from the
        // index but not decremented from the counts. Counts become accurate
        // again after the next full reindex (which resets to absolute values).
        self.metadata
            .update_index_state(&IndexState {
                repo_id: repo_spec.repo_id.clone(),
                last_indexed_sha: head_sha,
                last_indexed_at: chrono::Utc::now(),
                last_full_reindex: existing_state.last_full_reindex,
                index_version: CURRENT_INDEX_VERSION,
                health: IndexHealth::Healthy,
                files_count: existing_state.files_count + files_indexed as u64,
                symbols_count: existing_state.symbols_count + symbols_extracted as u64,
                chunks_count: existing_state.chunks_count + chunks_embedded as u64,
            })
            .await?;

        Ok(IndexResponse {
            repo_id: repo_spec.repo_id.clone(),
            files_indexed,
            symbols_extracted,
            chunks_embedded,
            duration_ms: 0,
        })
    }

    /// Remove a single file from all indexes.
    async fn remove_file_from_index(
        &self,
        repo_id: &str,
        file_path: &str,
    ) -> Result<(), EngineError> {
        // Remove from text index
        {
            let mut text_idx = self.text_index.write().await;
            text_idx.remove_file(repo_id, file_path);
        }

        // Remove AST symbols for this file
        self.metadata
            .delete_symbols_for_file(repo_id, file_path)
            .await?;

        // Remove semantic embeddings for this file
        if let Some(semantic) = &self.semantic_indexer {
            semantic.remove_file(repo_id, file_path).await?;
        }

        Ok(())
    }

    /// Index a single file (text + AST + semantic).
    async fn index_single_file(
        &self,
        repo_id: &str,
        base_dir: &std::path::Path,
        file_path: &str,
    ) -> Result<IndexResponse, EngineError> {
        let mut symbols_extracted: u32 = 0;
        let mut chunks_embedded: u32 = 0;

        let full_path = base_dir.join(file_path);
        let language = crate::git::detect_language(file_path);
        let content = match std::fs::read_to_string(&full_path) {
            Ok(c) => c,
            Err(_) => {
                return Ok(IndexResponse {
                    repo_id: repo_id.to_string(),
                    files_indexed: 0,
                    symbols_extracted: 0,
                    chunks_embedded: 0,
                    duration_ms: 0,
                });
            }
        };

        let lang = language.unwrap_or("unknown");

        // Remove old data for this file first (in case it's a modification)
        self.remove_file_from_index(repo_id, file_path).await?;

        // Text index
        {
            let mut text_idx = self.text_index.write().await;
            text_idx.index_file(repo_id, file_path, lang, &content)?;
        }

        // AST index
        if ast::should_parse(file_path) {
            let content_hash = blake3::hash(content.as_bytes()).to_hex().to_string();
            let result = self
                .ast_indexer
                .index_file(
                    &self.metadata,
                    repo_id,
                    file_path,
                    &content,
                    lang,
                    &content_hash,
                )
                .await?;
            symbols_extracted += result.symbols.len() as u32;

            // Semantic index
            if let (Some(semantic), Some(ltm)) = (&self.semantic_indexer, &self.long_term_memory) {
                let chunks = semantic::create_chunks_from_ast(
                    repo_id,
                    file_path,
                    lang,
                    &content,
                    &content_hash,
                    &result.symbols,
                );
                if !chunks.is_empty() {
                    let count = semantic.index_chunks(&chunks, ltm).await?;
                    chunks_embedded += count;
                }
            }
        }

        Ok(IndexResponse {
            repo_id: repo_id.to_string(),
            files_indexed: 1,
            symbols_extracted,
            chunks_embedded,
            duration_ms: 0,
        })
    }

    /// Get the HEAD SHA for a repository, if git operations are available.
    async fn get_head_sha(&self, repo_spec: &RepoSpec) -> Result<String, EngineError> {
        #[cfg(feature = "indexing")]
        {
            let local_path = repo_spec.local_path.as_deref().ok_or_else(|| {
                EngineError::IndexingError("local path required for HEAD SHA".into())
            })?;
            let repo_dir = std::path::Path::new(local_path);
            let repo_manager = crate::git::repo_manager::RepoManager::new(repo_dir.to_path_buf());
            let repo = repo_manager
                .clone_or_open(repo_spec)
                .map_err(|e| EngineError::IndexingError(format!("failed to open repo: {}", e)))?;
            repo_manager
                .head_sha(&repo)
                .map_err(|e| EngineError::IndexingError(format!("failed to get HEAD SHA: {}", e)))
        }
        #[cfg(not(feature = "indexing"))]
        {
            let _ = repo_spec;
            Err(EngineError::IndexingError(
                "Indexing feature is disabled, cannot get HEAD SHA".into(),
            ))
        }
    }

    /// Compute the diff between two SHAs for a repository.
    async fn compute_diff(
        &self,
        repo_spec: &RepoSpec,
        old_sha: &str,
        new_sha: &str,
    ) -> Result<Vec<crate::git::repo_manager::FileDiff>, EngineError> {
        #[cfg(feature = "indexing")]
        {
            let local_path = repo_spec.local_path.as_deref().ok_or_else(|| {
                EngineError::IndexingError("local path required for diff computation".into())
            })?;
            let repo_dir = std::path::Path::new(local_path);
            let repo_manager = crate::git::repo_manager::RepoManager::new(repo_dir.to_path_buf());
            let repo = repo_manager
                .clone_or_open(repo_spec)
                .map_err(|e| EngineError::IndexingError(format!("failed to open repo: {}", e)))?;
            repo_manager
                .diff_between(&repo, old_sha, new_sha)
                .map_err(|e| EngineError::IndexingError(format!("failed to compute diff: {}", e)))
        }
        #[cfg(not(feature = "indexing"))]
        {
            let _ = (repo_spec, old_sha, new_sha);
            Ok(vec![])
        }
    }

    /// Index all files in a directory.
    ///
    /// Note: callers are responsible for clearing existing data before calling
    /// this method. `full_index` clears symbols/embeddings before calling this;
    /// `index_single_file` removes data for the specific file first.
    async fn index_directory(
        &self,
        repo_id: &str,
        dir: &std::path::Path,
    ) -> Result<IndexResponse, EngineError> {
        let mut files_indexed: u32 = 0;
        let mut symbols_extracted: u32 = 0;
        let mut chunks_embedded: u32 = 0;

        // Walk and index files
        let entries = walk_directory(dir)?;
        for entry in entries {
            let file_path = entry.path.clone();
            let language = crate::git::detect_language(&file_path);

            // Read file content
            let full_path = dir.join(&file_path);
            let content = match std::fs::read_to_string(&full_path) {
                Ok(c) => c,
                Err(_) => continue, // Skip files that can't be read as UTF-8
            };

            let lang = language.unwrap_or("unknown");

            // Text index
            {
                let mut text_idx = self.text_index.write().await;
                text_idx.index_file(repo_id, &file_path, lang, &content)?;
            }

            // AST index (only for supported languages)
            if ast::should_parse(&file_path) {
                let content_hash = blake3::hash(content.as_bytes()).to_hex().to_string();
                let result = self
                    .ast_indexer
                    .index_file(
                        &self.metadata,
                        repo_id,
                        &file_path,
                        &content,
                        lang,
                        &content_hash,
                    )
                    .await?;
                symbols_extracted += result.symbols.len() as u32;

                // Semantic index: create AST-aware chunks and embed them
                if let (Some(semantic), Some(ltm)) =
                    (&self.semantic_indexer, &self.long_term_memory)
                {
                    let chunks = semantic::create_chunks_from_ast(
                        repo_id,
                        &file_path,
                        lang,
                        &content,
                        &content_hash,
                        &result.symbols,
                    );

                    if !chunks.is_empty() {
                        let count = semantic.index_chunks(&chunks, ltm).await?;
                        chunks_embedded += count;
                    }
                }
            }

            files_indexed += 1;
        }

        Ok(IndexResponse {
            repo_id: repo_id.to_string(),
            files_indexed,
            symbols_extracted,
            chunks_embedded,
            duration_ms: 0,
        })
    }

    /// Search the text index.
    pub async fn search_text(&self, query: &SearchQuery) -> Result<SearchResult, EngineError> {
        let text_idx = self.text_index.read().await;
        text_idx.search(query)
    }

    /// Remove a repository's index.
    pub async fn remove_index(&self, repo_id: &str) -> Result<(), EngineError> {
        // Remove from text index
        {
            let mut text_idx = self.text_index.write().await;
            text_idx.remove_repo(repo_id);
        }

        // Remove from semantic index
        if let Some(semantic) = &self.semantic_indexer {
            semantic.remove_repo(repo_id).await?;
        }

        // Remove from metadata store
        self.metadata.delete_repo(repo_id).await?;

        Ok(())
    }

    /// Get the index state for a repository.
    pub async fn get_index_state(&self, repo_id: &str) -> Result<Option<IndexState>, EngineError> {
        self.metadata.get_index_state(repo_id).await
    }

    /// Get the text search index (for direct access).
    pub fn text_index(&self) -> &Arc<tokio::sync::RwLock<text::TextSearchIndex>> {
        &self.text_index
    }

    /// Get the AST indexer (for direct access).
    pub fn ast_indexer(&self) -> &Arc<ast::AstIndexer> {
        &self.ast_indexer
    }

    /// Get the metadata store (for direct access).
    pub fn metadata_store(&self) -> &Arc<PostgresMetadataStore> {
        &self.metadata
    }

    /// Get the semantic indexer (for direct access).
    pub fn semantic_indexer(&self) -> Option<&Arc<SemanticIndexer>> {
        self.semantic_indexer.as_ref()
    }

    /// Get the long-term memory (for direct access).
    pub fn long_term_memory(&self) -> Option<&Arc<LongTermMemory>> {
        self.long_term_memory.as_ref()
    }
}

/// Current index schema version.
const CURRENT_INDEX_VERSION: u32 = 1;

/// Directory walking result.
struct DirEntry {
    path: String,
}

/// Walk a directory recursively, returning file paths relative to the base.
fn walk_directory(dir: &std::path::Path) -> Result<Vec<DirEntry>, EngineError> {
    let mut entries = Vec::new();
    walk_dir_recursive(dir, dir, &mut entries)?;
    Ok(entries)
}

fn walk_dir_recursive(
    dir: &std::path::Path,
    base: &std::path::Path,
    entries: &mut Vec<DirEntry>,
) -> Result<(), EngineError> {
    let read_dir = std::fs::read_dir(dir)
        .map_err(|e| EngineError::IndexingError(format!("Failed to read directory: {}", e)))?;

    for entry in read_dir {
        let entry =
            entry.map_err(|e| EngineError::IndexingError(format!("Dir entry error: {}", e)))?;

        let file_name = entry.file_name();
        let file_name_str = file_name.to_string_lossy();

        // Skip hidden files and directories
        if file_name_str.starts_with('.') {
            continue;
        }

        // Skip common non-code directories
        if file_name_str == "target"
            || file_name_str == "node_modules"
            || file_name_str == "__pycache__"
        {
            continue;
        }

        let path = entry.path();
        let metadata = entry
            .metadata()
            .map_err(|e| EngineError::IndexingError(format!("Metadata error: {}", e)))?;

        if metadata.is_dir() {
            walk_dir_recursive(&path, base, entries)?;
        } else if metadata.is_file() {
            let relative = path
                .strip_prefix(base)
                .unwrap_or(&path)
                .to_string_lossy()
                .to_string();

            entries.push(DirEntry { path: relative });
        }
    }

    Ok(())
}

#[cfg(feature = "indexing")]
#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_index_pipeline_with_local_directory() {
        // Create a temp directory with some files
        let temp_dir = std::env::temp_dir().join("uc-test-index-pipeline");
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

        std::fs::write(
            temp_dir.join("app.py"),
            r#"class App:
    def run(self):
        print("running")
"#,
        )
        .unwrap();

        let metadata = Arc::new(PostgresMetadataStore::new_fallback());
        let pipeline = IndexPipeline::new(metadata);

        let request = IndexRequest {
            repo: RepoSpec {
                repo_id: "test-repo".to_string(),
                remote_url: "https://github.com/test/repo".to_string(),
                default_branch: "main".to_string(),
                local_path: Some(temp_dir.to_string_lossy().to_string()),
            },
            force_full: true,
        };

        let response = pipeline.index_repo(&request).await.unwrap();
        assert_eq!(response.repo_id, "test-repo");
        assert!(response.files_indexed >= 2);

        // Search should find results
        let query = SearchQuery {
            query: "config".to_string(),
            modes: vec![uc_types::search::SearchMode::Text],
            repo_ids: vec![],
            languages: vec![],
            path_patterns: vec![],
            max_results: 10,
        };

        let result = pipeline.search_text(&query).await.unwrap();
        assert!(!result.items.is_empty());

        // Clean up
        let _ = std::fs::remove_dir_all(&temp_dir);
    }

    #[tokio::test]
    async fn test_remove_index() {
        let temp_dir = std::env::temp_dir().join("uc-test-remove-index");
        let _ = std::fs::remove_dir_all(&temp_dir);
        std::fs::create_dir_all(&temp_dir).unwrap();

        std::fs::write(temp_dir.join("test.rs"), "fn unique_func() {}").unwrap();

        let metadata = Arc::new(PostgresMetadataStore::new_fallback());
        let pipeline = IndexPipeline::new(metadata);

        let request = IndexRequest {
            repo: RepoSpec {
                repo_id: "test-remove".to_string(),
                remote_url: String::new(),
                default_branch: "main".to_string(),
                local_path: Some(temp_dir.to_string_lossy().to_string()),
            },
            force_full: true,
        };

        pipeline.index_repo(&request).await.unwrap();
        pipeline.remove_index("test-remove").await.unwrap();

        // After removal, search should return no results
        let query = SearchQuery {
            query: "unique_func".to_string(),
            modes: vec![uc_types::search::SearchMode::Text],
            repo_ids: vec![],
            languages: vec![],
            path_patterns: vec![],
            max_results: 10,
        };

        let result = pipeline.search_text(&query).await.unwrap();
        assert!(result.items.is_empty());

        let _ = std::fs::remove_dir_all(&temp_dir);
    }

    #[tokio::test]
    async fn test_index_pipeline_with_semantic() {
        let temp_dir = std::env::temp_dir().join("uc-test-index-semantic");
        let _ = std::fs::remove_dir_all(&temp_dir);
        std::fs::create_dir_all(&temp_dir).unwrap();

        std::fs::write(
            temp_dir.join("main.rs"),
            r#"fn search_handler(query: &str) -> Vec<String> {
    let results = database.search(query);
    results.into_iter().map(|r| r.name).collect()
}

struct Database {
    url: String,
}

impl Database {
    fn search(&self, query: &str) -> Vec<Record> {
        vec![]
    }
}"#,
        )
        .unwrap();

        let metadata = Arc::new(PostgresMetadataStore::new_fallback());
        let embedding_service = Arc::new(EmbeddingService::new_fallback());
        let long_term_memory = Arc::new(LongTermMemory::new_fallback());

        let pipeline = IndexPipeline::with_semantic(metadata, embedding_service, long_term_memory);

        let request = IndexRequest {
            repo: RepoSpec {
                repo_id: "test-semantic".to_string(),
                remote_url: String::new(),
                default_branch: "main".to_string(),
                local_path: Some(temp_dir.to_string_lossy().to_string()),
            },
            force_full: true,
        };

        let response = pipeline.index_repo(&request).await.unwrap();
        assert_eq!(response.repo_id, "test-semantic");
        assert!(response.files_indexed >= 1);
        // Should have embedded some chunks (symbols + file chunk)
        assert!(response.chunks_embedded > 0);

        let _ = std::fs::remove_dir_all(&temp_dir);
    }

    /// Helper: create a git repo in a temp directory with an initial commit.
    fn create_git_repo(temp_dir: &std::path::Path) -> git2::Repository {
        let repo = git2::Repository::init(temp_dir).unwrap();

        // Configure user for commits
        let mut config = repo.config().unwrap();
        config.set_str("user.name", "Test User").unwrap();
        config.set_str("user.email", "test@example.com").unwrap();

        repo
    }

    /// Helper: write a file and commit it to the repo.
    fn commit_file(
        repo: &git2::Repository,
        repo_dir: &std::path::Path,
        file_name: &str,
        content: &str,
        message: &str,
    ) -> String {
        let file_path = repo_dir.join(file_name);
        if let Some(parent) = file_path.parent() {
            std::fs::create_dir_all(parent).unwrap();
        }
        std::fs::write(&file_path, content).unwrap();

        let mut index = repo.index().unwrap();
        index.add_path(std::path::Path::new(file_name)).unwrap();
        index.write().unwrap();

        let tree_id = index.write_tree().unwrap();
        let tree = repo.find_tree(tree_id).unwrap();

        let sig = repo.signature().unwrap();
        let head = repo.head().ok();
        let parent_commit = head.as_ref().and_then(|h| h.peel_to_commit().ok());

        let parents: Vec<&git2::Commit> = parent_commit.as_ref().map(|c| c).into_iter().collect();

        let commit_id = repo
            .commit(Some("HEAD"), &sig, &sig, message, &tree, parents.as_slice())
            .unwrap();

        commit_id.to_string()
    }

    /// Helper: delete a file and commit the deletion.
    fn commit_delete(
        repo: &git2::Repository,
        repo_dir: &std::path::Path,
        file_name: &str,
        message: &str,
    ) -> String {
        let file_path = repo_dir.join(file_name);
        std::fs::remove_file(&file_path).unwrap();

        let mut index = repo.index().unwrap();
        index.remove_path(std::path::Path::new(file_name)).unwrap();
        index.write().unwrap();

        let tree_id = index.write_tree().unwrap();
        let tree = repo.find_tree(tree_id).unwrap();

        let sig = repo.signature().unwrap();
        let head = repo.head().unwrap();
        let parent_commit = head.peel_to_commit().unwrap();

        let commit_id = repo
            .commit(Some("HEAD"), &sig, &sig, message, &tree, &[&parent_commit])
            .unwrap();

        commit_id.to_string()
    }

    #[tokio::test]
    async fn test_incremental_index_no_changes() {
        let temp_dir = std::env::temp_dir().join("uc-test-incremental-no-changes");
        let _ = std::fs::remove_dir_all(&temp_dir);
        std::fs::create_dir_all(&temp_dir).unwrap();

        let repo = create_git_repo(&temp_dir);
        let sha1 = commit_file(&repo, &temp_dir, "main.rs", "fn hello() {}", "initial");

        let metadata = Arc::new(PostgresMetadataStore::new_fallback());
        let pipeline = IndexPipeline::new(Arc::clone(&metadata));

        let repo_spec = RepoSpec {
            repo_id: "test-inc-nochange".to_string(),
            remote_url: String::new(),
            default_branch: "main".to_string(),
            local_path: Some(temp_dir.to_string_lossy().to_string()),
        };

        // Full index first
        pipeline
            .index_repo(&IndexRequest {
                repo: repo_spec.clone(),
                force_full: true,
            })
            .await
            .unwrap();

        // Verify the SHA was stored
        let state = metadata
            .get_index_state("test-inc-nochange")
            .await
            .unwrap()
            .unwrap();
        assert_eq!(state.last_indexed_sha, sha1);

        // Incremental index with no changes — should report 0 files indexed
        let response = pipeline
            .index_repo(&IndexRequest {
                repo: repo_spec,
                force_full: false,
            })
            .await
            .unwrap();

        assert_eq!(response.files_indexed, 0);

        let _ = std::fs::remove_dir_all(&temp_dir);
    }

    #[tokio::test]
    async fn test_incremental_index_added_file() {
        let temp_dir = std::env::temp_dir().join("uc-test-incremental-added");
        let _ = std::fs::remove_dir_all(&temp_dir);
        std::fs::create_dir_all(&temp_dir).unwrap();

        let repo = create_git_repo(&temp_dir);
        commit_file(&repo, &temp_dir, "main.rs", "fn hello() {}", "initial");

        let metadata = Arc::new(PostgresMetadataStore::new_fallback());
        let pipeline = IndexPipeline::new(Arc::clone(&metadata));

        let repo_spec = RepoSpec {
            repo_id: "test-inc-added".to_string(),
            remote_url: String::new(),
            default_branch: "main".to_string(),
            local_path: Some(temp_dir.to_string_lossy().to_string()),
        };

        // Full index first
        pipeline
            .index_repo(&IndexRequest {
                repo: repo_spec.clone(),
                force_full: true,
            })
            .await
            .unwrap();

        // Add a new file and commit
        commit_file(&repo, &temp_dir, "lib.rs", "fn helper() {}", "add lib");

        // Incremental index should pick up the new file
        let response = pipeline
            .index_repo(&IndexRequest {
                repo: repo_spec,
                force_full: false,
            })
            .await
            .unwrap();

        assert_eq!(response.files_indexed, 1);

        // Verify the new file is searchable
        let query = SearchQuery {
            query: "helper".to_string(),
            modes: vec![uc_types::search::SearchMode::Text],
            repo_ids: vec![],
            languages: vec![],
            path_patterns: vec![],
            max_results: 10,
        };
        let result = pipeline.search_text(&query).await.unwrap();
        assert!(result.items.iter().any(|i| i.file_path == "lib.rs"));

        let _ = std::fs::remove_dir_all(&temp_dir);
    }

    #[tokio::test]
    async fn test_incremental_index_modified_file() {
        let temp_dir = std::env::temp_dir().join("uc-test-incremental-modified");
        let _ = std::fs::remove_dir_all(&temp_dir);
        std::fs::create_dir_all(&temp_dir).unwrap();

        let repo = create_git_repo(&temp_dir);
        commit_file(
            &repo,
            &temp_dir,
            "main.rs",
            "fn original_routine() {}",
            "initial",
        );

        let metadata = Arc::new(PostgresMetadataStore::new_fallback());
        let pipeline = IndexPipeline::new(Arc::clone(&metadata));

        let repo_spec = RepoSpec {
            repo_id: "test-inc-modified".to_string(),
            remote_url: String::new(),
            default_branch: "main".to_string(),
            local_path: Some(temp_dir.to_string_lossy().to_string()),
        };

        // Full index first
        pipeline
            .index_repo(&IndexRequest {
                repo: repo_spec.clone(),
                force_full: true,
            })
            .await
            .unwrap();

        // Modify the file and commit
        commit_file(
            &repo,
            &temp_dir,
            "main.rs",
            "fn updated_handler() {}",
            "modify main",
        );

        // Incremental index should re-index the modified file
        let response = pipeline
            .index_repo(&IndexRequest {
                repo: repo_spec.clone(),
                force_full: false,
            })
            .await
            .unwrap();

        assert_eq!(response.files_indexed, 1);

        // Verify the old content is gone: "original_routine" should not be found
        // (tokenizes to "original" + "routine", neither of which appear in "updated_handler")
        let query_old = SearchQuery {
            query: "original_routine".to_string(),
            modes: vec![uc_types::search::SearchMode::Text],
            repo_ids: vec![],
            languages: vec![],
            path_patterns: vec![],
            max_results: 10,
        };
        let result_old = pipeline.search_text(&query_old).await.unwrap();
        assert!(result_old.items.is_empty());

        // Verify the new content is searchable
        let query_new = SearchQuery {
            query: "updated_handler".to_string(),
            modes: vec![uc_types::search::SearchMode::Text],
            repo_ids: vec![],
            languages: vec![],
            path_patterns: vec![],
            max_results: 10,
        };
        let result_new = pipeline.search_text(&query_new).await.unwrap();
        assert!(!result_new.items.is_empty());

        let _ = std::fs::remove_dir_all(&temp_dir);
    }

    #[tokio::test]
    async fn test_incremental_index_deleted_file() {
        let temp_dir = std::env::temp_dir().join("uc-test-incremental-deleted");
        let _ = std::fs::remove_dir_all(&temp_dir);
        std::fs::create_dir_all(&temp_dir).unwrap();

        let repo = create_git_repo(&temp_dir);
        commit_file(&repo, &temp_dir, "main.rs", "fn hello() {}", "initial");
        commit_file(
            &repo,
            &temp_dir,
            "removeme.rs",
            "fn will_be_deleted() {}",
            "add removeme",
        );

        let metadata = Arc::new(PostgresMetadataStore::new_fallback());
        let pipeline = IndexPipeline::new(Arc::clone(&metadata));

        let repo_spec = RepoSpec {
            repo_id: "test-inc-deleted".to_string(),
            remote_url: String::new(),
            default_branch: "main".to_string(),
            local_path: Some(temp_dir.to_string_lossy().to_string()),
        };

        // Full index first
        pipeline
            .index_repo(&IndexRequest {
                repo: repo_spec.clone(),
                force_full: true,
            })
            .await
            .unwrap();

        // Verify the file is searchable
        let query_before = SearchQuery {
            query: "will_be_deleted".to_string(),
            modes: vec![uc_types::search::SearchMode::Text],
            repo_ids: vec![],
            languages: vec![],
            path_patterns: vec![],
            max_results: 10,
        };
        let result_before = pipeline.search_text(&query_before).await.unwrap();
        assert!(!result_before.items.is_empty());

        // Delete the file and commit
        commit_delete(&repo, &temp_dir, "removeme.rs", "delete removeme");

        // Incremental index should remove the deleted file
        let response = pipeline
            .index_repo(&IndexRequest {
                repo: repo_spec,
                force_full: false,
            })
            .await
            .unwrap();

        assert_eq!(response.files_indexed, 0); // No new files indexed

        // Verify the deleted file is no longer searchable
        let query_after = SearchQuery {
            query: "will_be_deleted".to_string(),
            modes: vec![uc_types::search::SearchMode::Text],
            repo_ids: vec![],
            languages: vec![],
            path_patterns: vec![],
            max_results: 10,
        };
        let result_after = pipeline.search_text(&query_after).await.unwrap();
        assert!(result_after.items.is_empty());

        let _ = std::fs::remove_dir_all(&temp_dir);
    }

    #[tokio::test]
    async fn test_remove_file_from_index() {
        let temp_dir = std::env::temp_dir().join("uc-test-remove-file");
        let _ = std::fs::remove_dir_all(&temp_dir);
        std::fs::create_dir_all(&temp_dir).unwrap();

        std::fs::write(temp_dir.join("file1.rs"), "fn keep_me() {}").unwrap();
        std::fs::write(temp_dir.join("file2.rs"), "fn remove_me() {}").unwrap();

        let metadata = Arc::new(PostgresMetadataStore::new_fallback());
        let pipeline = IndexPipeline::new(Arc::clone(&metadata));

        let repo_spec = RepoSpec {
            repo_id: "test-rm-file".to_string(),
            remote_url: String::new(),
            default_branch: "main".to_string(),
            local_path: Some(temp_dir.to_string_lossy().to_string()),
        };

        // Full index
        pipeline
            .index_repo(&IndexRequest {
                repo: repo_spec,
                force_full: true,
            })
            .await
            .unwrap();

        // Remove a single file
        pipeline
            .remove_file_from_index("test-rm-file", "file2.rs")
            .await
            .unwrap();

        // file2 content should be gone
        let query = SearchQuery {
            query: "remove_me".to_string(),
            modes: vec![uc_types::search::SearchMode::Text],
            repo_ids: vec![],
            languages: vec![],
            path_patterns: vec![],
            max_results: 10,
        };
        let result = pipeline.search_text(&query).await.unwrap();
        assert!(result.items.is_empty());

        // file1 content should still be there
        let query2 = SearchQuery {
            query: "keep_me".to_string(),
            modes: vec![uc_types::search::SearchMode::Text],
            repo_ids: vec![],
            languages: vec![],
            path_patterns: vec![],
            max_results: 10,
        };
        let result2 = pipeline.search_text(&query2).await.unwrap();
        assert!(!result2.items.is_empty());

        let _ = std::fs::remove_dir_all(&temp_dir);
    }

    #[tokio::test]
    async fn test_index_single_file() {
        let temp_dir = std::env::temp_dir().join("uc-test-single-file");
        let _ = std::fs::remove_dir_all(&temp_dir);
        std::fs::create_dir_all(&temp_dir).unwrap();

        std::fs::write(temp_dir.join("solo.rs"), "fn solo_function() -> i32 { 42 }").unwrap();

        let metadata = Arc::new(PostgresMetadataStore::new_fallback());
        let pipeline = IndexPipeline::new(metadata);

        let result = pipeline
            .index_single_file("test-single", &temp_dir, "solo.rs")
            .await
            .unwrap();

        assert_eq!(result.files_indexed, 1);
        assert_eq!(result.repo_id, "test-single");

        // Verify text search finds it
        let query = SearchQuery {
            query: "solo_function".to_string(),
            modes: vec![uc_types::search::SearchMode::Text],
            repo_ids: vec![],
            languages: vec![],
            path_patterns: vec![],
            max_results: 10,
        };
        let search_result = pipeline.search_text(&query).await.unwrap();
        assert!(!search_result.items.is_empty());
        assert_eq!(search_result.items[0].file_path, "solo.rs");

        let _ = std::fs::remove_dir_all(&temp_dir);
    }

    #[tokio::test]
    async fn test_full_index_saves_head_sha() {
        let temp_dir = std::env::temp_dir().join("uc-test-full-saves-sha");
        let _ = std::fs::remove_dir_all(&temp_dir);
        std::fs::create_dir_all(&temp_dir).unwrap();

        let repo = create_git_repo(&temp_dir);
        let sha = commit_file(&repo, &temp_dir, "main.rs", "fn main() {}", "initial");

        let metadata = Arc::new(PostgresMetadataStore::new_fallback());
        let pipeline = IndexPipeline::new(Arc::clone(&metadata));

        let repo_spec = RepoSpec {
            repo_id: "test-sha-save".to_string(),
            remote_url: String::new(),
            default_branch: "main".to_string(),
            local_path: Some(temp_dir.to_string_lossy().to_string()),
        };

        pipeline
            .index_repo(&IndexRequest {
                repo: repo_spec,
                force_full: true,
            })
            .await
            .unwrap();

        // Verify the HEAD SHA was stored
        let state = metadata
            .get_index_state("test-sha-save")
            .await
            .unwrap()
            .unwrap();
        assert_eq!(state.last_indexed_sha, sha);
        assert_eq!(state.health, IndexHealth::Healthy);

        let _ = std::fs::remove_dir_all(&temp_dir);
    }

    #[tokio::test]
    async fn test_incremental_index_updates_sha() {
        let temp_dir = std::env::temp_dir().join("uc-test-incremental-updates-sha");
        let _ = std::fs::remove_dir_all(&temp_dir);
        std::fs::create_dir_all(&temp_dir).unwrap();

        let repo = create_git_repo(&temp_dir);
        commit_file(&repo, &temp_dir, "main.rs", "fn hello() {}", "initial");

        let metadata = Arc::new(PostgresMetadataStore::new_fallback());
        let pipeline = IndexPipeline::new(Arc::clone(&metadata));

        let repo_spec = RepoSpec {
            repo_id: "test-inc-sha".to_string(),
            remote_url: String::new(),
            default_branch: "main".to_string(),
            local_path: Some(temp_dir.to_string_lossy().to_string()),
        };

        // Full index first
        pipeline
            .index_repo(&IndexRequest {
                repo: repo_spec.clone(),
                force_full: true,
            })
            .await
            .unwrap();

        let state_after_full = metadata
            .get_index_state("test-inc-sha")
            .await
            .unwrap()
            .unwrap();
        let sha_after_full = state_after_full.last_indexed_sha.clone();
        assert!(!sha_after_full.is_empty());

        // Add a file and commit
        let sha2 = commit_file(&repo, &temp_dir, "lib.rs", "fn world() {}", "add lib");

        // Incremental index
        pipeline
            .index_repo(&IndexRequest {
                repo: repo_spec,
                force_full: false,
            })
            .await
            .unwrap();

        // Verify the SHA was updated
        let state_after_inc = metadata
            .get_index_state("test-inc-sha")
            .await
            .unwrap()
            .unwrap();
        assert_eq!(state_after_inc.last_indexed_sha, sha2);
        assert_ne!(state_after_inc.last_indexed_sha, sha_after_full);

        let _ = std::fs::remove_dir_all(&temp_dir);
    }

    #[tokio::test]
    async fn test_index_state_stores_counts_after_full_index() {
        let temp_dir = std::env::temp_dir().join("uc-test-index-state-counts");
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

        std::fs::write(
            temp_dir.join("lib.rs"),
            r#"struct Database { url: String }
impl Database { fn connect(&self) -> Self { self.clone() } fn query(&self, q: &str) {} }"#,
        )
        .unwrap();

        let metadata = Arc::new(PostgresMetadataStore::new_fallback());
        let pipeline = IndexPipeline::new(Arc::clone(&metadata));

        let request = IndexRequest {
            repo: RepoSpec {
                repo_id: "test-state-counts".to_string(),
                remote_url: "https://github.com/test/repo".to_string(),
                default_branch: "main".to_string(),
                local_path: Some(temp_dir.to_string_lossy().to_string()),
            },
            force_full: true,
        };

        let response = pipeline.index_repo(&request).await.unwrap();
        assert_eq!(response.repo_id, "test-state-counts");
        assert!(response.files_indexed >= 2);

        // Verify counts are stored in IndexState
        let state = metadata
            .get_index_state("test-state-counts")
            .await
            .unwrap()
            .unwrap();
        assert_eq!(state.health, IndexHealth::Healthy);
        assert!(
            state.files_count > 0,
            "files_count should be > 0, got {}",
            state.files_count
        );
        assert!(
            state.files_count >= 2,
            "files_count should be >= 2 (indexed at least 2 files), got {}",
            state.files_count
        );

        let _ = std::fs::remove_dir_all(&temp_dir);
    }
}
