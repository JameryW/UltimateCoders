//! Semantic search engine using Qdrant for vector similarity search.
//!
//! Embeds a query text via `EmbeddingService`, then searches Qdrant
//! (or the in-memory fallback store) for similar code embeddings.

use uc_types::error::EngineError;
use uc_types::search::{SearchMode, SearchQuery, SearchResult, SearchResultItem};

use crate::indexer::semantic::SemanticIndexer;
use crate::memory::long_term::LongTermMemory;

use std::sync::Arc;

/// Semantic search engine backed by Qdrant vector similarity.
///
/// Uses the `EmbeddingService` to compute query embeddings, then searches
/// the shared `memory_embeddings` collection in Qdrant (key prefix `code_embedding:`) or the fallback store.
pub struct SemanticSearchEngine {
    semantic_indexer: Arc<SemanticIndexer>,
    long_term_memory: Arc<LongTermMemory>,
}

impl SemanticSearchEngine {
    /// Create a new semantic search engine.
    pub fn new(
        semantic_indexer: Arc<SemanticIndexer>,
        long_term_memory: Arc<LongTermMemory>,
    ) -> Self {
        Self {
            semantic_indexer,
            long_term_memory,
        }
    }

    /// Execute a semantic search query.
    ///
    /// Embeds the query text, then searches for similar code embeddings
    /// filtered by repo_ids and languages.
    pub async fn search(&self, query: &SearchQuery) -> Result<SearchResult, EngineError> {
        let max_results = if query.max_results > 0 {
            query.max_results
        } else {
            20
        };

        let items = self
            .semantic_indexer
            .search(
                &query.query,
                &query.repo_ids,
                &query.languages,
                max_results,
                &self.long_term_memory,
            )
            .await?;

        Ok(SearchResult { items })
    }

    /// Search with a pre-computed embedding vector (for callers who
    /// already have the embedding).
    pub async fn search_with_embedding(
        &self,
        embedding: Vec<f32>,
        repo_ids: &[String],
        languages: &[String],
        max_results: u32,
    ) -> Result<Vec<SearchResultItem>, EngineError> {
        // Use LongTermMemory directly for the search
        let scope = if repo_ids.len() == 1 {
            uc_types::memory::MemorySearchScope::Project {
                project_id: repo_ids[0].clone(),
            }
        } else {
            uc_types::memory::MemorySearchScope::All
        };

        let memory_results = self
            .long_term_memory
            .search(embedding, &scope, max_results, 0.5)
            .await?;

        let mut results: Vec<SearchResultItem> = memory_results
            .into_iter()
            .filter_map(|mr| {
                if !mr
                    .entry
                    .metadata
                    .tags
                    .contains(&"code_embedding".to_string())
                {
                    return None;
                }

                let (file_path, start_line, end_line) =
                    crate::indexer::semantic::parse_embedding_key(&mr.entry.key)?;

                if !languages.is_empty() {
                    let lang_match = mr
                        .entry
                        .metadata
                        .tags
                        .iter()
                        .any(|tag| languages.iter().any(|lang| tag == lang));
                    if !lang_match {
                        return None;
                    }
                }

                Some(SearchResultItem {
                    repo_id: match &mr.entry.key {
                        uc_types::memory::MemoryKey::Project { project_id, .. } => {
                            project_id.clone()
                        }
                        _ => return None,
                    },
                    file_path,
                    start_line,
                    end_line,
                    content_snippet: match &mr.entry.content {
                        uc_types::memory::MemoryContent::Text(t) => {
                            if t.len() > 200 {
                                // char-safe truncation: &t[..200] panics if byte
                                // 200 splits a multi-byte char (non-ASCII code).
                                format!("{}...", crate::sandbox::truncate_str(t, 200))
                            } else {
                                t.clone()
                            }
                        }
                        uc_types::memory::MemoryContent::Code { code, .. } => {
                            if code.len() > 200 {
                                format!("{}...", crate::sandbox::truncate_str(code, 200))
                            } else {
                                code.clone()
                            }
                        }
                        _ => String::new(),
                    },
                    match_type: SearchMode::Semantic,
                    score: mr.score,
                    symbol_name: None,
                    symbol_kind: None,
                    parent_symbol: None,
                })
            })
            .collect();

        results.sort_by(|a, b| {
            b.score
                .partial_cmp(&a.score)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        results.truncate(max_results as usize);

        Ok(results)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::EmbeddingConfig;
    use crate::indexer::semantic::{EmbeddingService, SemanticIndexer};
    use uc_types::index::{ChunkType, CodeChunk};

    #[tokio::test]
    async fn test_semantic_search_engine_fallback() {
        let embedding_service = Arc::new(EmbeddingService::new(EmbeddingConfig::default()));
        let semantic_indexer = Arc::new(SemanticIndexer::new(embedding_service));
        let long_term = Arc::new(LongTermMemory::new_fallback());
        let engine = SemanticSearchEngine::new(semantic_indexer, long_term);

        // Index some chunks first
        let chunks = vec![CodeChunk {
            id: "test-chunk-1".to_string(),
            repo_id: "sem-test".to_string(),
            file_path: "src/search.rs".to_string(),
            start_line: 1,
            end_line: 10,
            content: "fn search_by_keyword(query: &str) -> Vec<Result>".to_string(),
            language: "rust".to_string(),
            symbol_name: Some("search_by_keyword".to_string()),
            symbol_kind: Some("function".to_string()),
            parent_symbol: None,
            chunk_type: ChunkType::Symbol,
            content_hash: "h1".to_string(),
        }];

        engine
            .semantic_indexer
            .index_chunks(&chunks, &engine.long_term_memory)
            .await
            .unwrap();

        // Search with the same text (BLAKE3 fallback produces identical vectors for identical text)
        let query = SearchQuery {
            query: "fn search_by_keyword(query: &str) -> Vec<Result>".to_string(),
            modes: vec![SearchMode::Semantic],
            repo_ids: vec!["sem-test".to_string()],
            languages: vec!["rust".to_string()],
            path_patterns: vec![],
            max_results: 10,
        };

        let result = engine.search(&query).await.unwrap();
        assert!(!result.items.is_empty());
        assert_eq!(result.items[0].match_type, SearchMode::Semantic);
    }
}
