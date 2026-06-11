//! Hybrid search engine (Text + Semantic + AST).
//!
//! Combines results from text search, semantic search, and AST search
//! with relevance-weighted scoring. Deduplicates by (repo_id, file_path,
//! start_line) and boosts scores for items found by multiple modes.

use uc_types::error::EngineError;
use uc_types::search::{SearchMode, SearchQuery, SearchResult, SearchResultItem};

use crate::indexer::IndexPipeline;
use crate::search::semantic::SemanticSearchEngine;

use std::sync::Arc;

/// Hybrid search engine that combines multiple search modes.
pub struct HybridSearchEngine {
    pipeline: Arc<IndexPipeline>,
    semantic_engine: Option<Arc<SemanticSearchEngine>>,
}

impl HybridSearchEngine {
    /// Create a new hybrid search engine backed by the given pipeline.
    pub fn new(pipeline: Arc<IndexPipeline>) -> Self {
        Self {
            pipeline,
            semantic_engine: None,
        }
    }

    /// Create a new hybrid search engine with semantic search support.
    pub fn with_semantic(
        pipeline: Arc<IndexPipeline>,
        semantic_engine: Arc<SemanticSearchEngine>,
    ) -> Self {
        Self {
            pipeline,
            semantic_engine: Some(semantic_engine),
        }
    }

    /// Set the semantic search engine.
    pub fn set_semantic_engine(&mut self, engine: Arc<SemanticSearchEngine>) {
        self.semantic_engine = Some(engine);
    }

    /// Execute a search query using the specified modes.
    pub async fn search(&self, query: &SearchQuery) -> Result<SearchResult, EngineError> {
        let modes = if query.modes.is_empty() {
            vec![SearchMode::Hybrid]
        } else {
            query.modes.clone()
        };

        let mut all_items: Vec<SearchResultItem> = Vec::new();

        for mode in &modes {
            match mode {
                SearchMode::Text => {
                    let text_result = self.pipeline.search_text(query).await?;
                    all_items.extend(text_result.items);
                }
                SearchMode::Semantic => {
                    if let Some(semantic) = &self.semantic_engine {
                        let semantic_result = semantic.search(query).await?;
                        all_items.extend(semantic_result.items);
                    }
                    // If no semantic engine is configured, return empty results
                }
                SearchMode::Ast => {
                    let ast_result = self.search_ast(query).await?;
                    all_items.extend(ast_result);
                }
                SearchMode::Hybrid => {
                    // Run all three search modes
                    let text_result = self.pipeline.search_text(query).await?;
                    all_items.extend(text_result.items);

                    let ast_result = self.search_ast(query).await?;
                    all_items.extend(ast_result);

                    if let Some(semantic) = &self.semantic_engine {
                        let semantic_result = semantic.search(query).await?;
                        all_items.extend(semantic_result.items);
                    }
                }
            }
        }

        // Merge and deduplicate results
        let merged = merge_results(all_items, query.max_results);

        Ok(SearchResult { items: merged })
    }

    /// Execute an AST-based search by tokenizing the query and searching symbols.
    async fn search_ast(&self, query: &SearchQuery) -> Result<Vec<SearchResultItem>, EngineError> {
        // For AST search via the generic SearchQuery, we do a symbol search
        // using the query text as the symbol name.
        let ast_query = uc_types::search::AstQuery::SymbolSearch {
            name: query.query.clone(),
            kind: None,
        };

        let metadata = self.pipeline.metadata_store();
        let ast_indexer = self.pipeline.ast_indexer();
        let max_results = if query.max_results > 0 {
            query.max_results
        } else {
            20
        };

        let mut results = ast_indexer
            .search(metadata, &ast_query, max_results)
            .await?;

        // Apply filters
        if !query.repo_ids.is_empty() {
            results.retain(|item| query.repo_ids.contains(&item.repo_id));
        }
        if !query.languages.is_empty() {
            results.retain(|item| {
                item.symbol_kind
                    .as_ref()
                    .map(|k| query.languages.contains(k))
                    .unwrap_or(false)
            });
        }

        Ok(results)
    }
}

/// Merge search results from multiple sources.
///
/// Deduplicates by (repo_id, file_path, start_line) and combines scores.
/// For duplicate items from different search modes, the score is boosted
/// to indicate higher confidence.
fn merge_results(items: Vec<SearchResultItem>, max_results: u32) -> Vec<SearchResultItem> {
    use std::collections::HashMap;

    let mut merged: HashMap<(String, String, u32), SearchResultItem> = HashMap::new();

    for item in items {
        let key = (
            item.repo_id.clone(),
            item.file_path.clone(),
            item.start_line,
        );

        match merged.get_mut(&key) {
            Some(existing) => {
                // Boost score for items found by multiple search modes
                existing.score = (existing.score + item.score) * 1.2;

                // Enrich with symbol metadata if available
                if item.symbol_name.is_some() && existing.symbol_name.is_none() {
                    existing.symbol_name = item.symbol_name;
                    existing.symbol_kind = item.symbol_kind;
                    existing.parent_symbol = item.parent_symbol;
                }

                // Use the wider range
                if item.end_line > existing.end_line {
                    existing.end_line = item.end_line;
                }

                // Mark as hybrid if found by different modes
                if existing.match_type != item.match_type {
                    existing.match_type = SearchMode::Hybrid;
                }
            }
            None => {
                merged.insert(key, item);
            }
        }
    }

    let mut result: Vec<SearchResultItem> = merged.into_values().collect();

    // Sort by score descending
    result.sort_by(|a, b| {
        b.score
            .partial_cmp(&a.score)
            .unwrap_or(std::cmp::Ordering::Equal)
    });

    // Truncate
    result.truncate(max_results as usize);

    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use uc_types::search::SearchMode;

    #[cfg(feature = "indexing")]
    use crate::metadata::postgres::PostgresMetadataStore;
    #[cfg(feature = "indexing")]
    use uc_types::index::{IndexRequest, RepoSpec};

    #[test]
    fn test_merge_results_dedup() {
        let items = vec![
            SearchResultItem {
                repo_id: "r1".into(),
                file_path: "main.rs".into(),
                start_line: 10,
                end_line: 15,
                content_snippet: "fn foo()".into(),
                match_type: SearchMode::Text,
                score: 0.8,
                symbol_name: None,
                symbol_kind: None,
                parent_symbol: None,
            },
            SearchResultItem {
                repo_id: "r1".into(),
                file_path: "main.rs".into(),
                start_line: 10,
                end_line: 20,
                content_snippet: String::new(),
                match_type: SearchMode::Ast,
                score: 1.0,
                symbol_name: Some("foo".into()),
                symbol_kind: Some("function".into()),
                parent_symbol: None,
            },
            SearchResultItem {
                repo_id: "r1".into(),
                file_path: "main.rs".into(),
                start_line: 10,
                end_line: 20,
                content_snippet: String::new(),
                match_type: SearchMode::Semantic,
                score: 0.9,
                symbol_name: None,
                symbol_kind: None,
                parent_symbol: None,
            },
            SearchResultItem {
                repo_id: "r1".into(),
                file_path: "lib.rs".into(),
                start_line: 5,
                end_line: 10,
                content_snippet: "fn bar()".into(),
                match_type: SearchMode::Text,
                score: 0.6,
                symbol_name: None,
                symbol_kind: None,
                parent_symbol: None,
            },
        ];

        let merged = merge_results(items, 10);

        // Should have 2 unique results (deduplicated)
        assert_eq!(merged.len(), 2);

        // The first result should be the merged one (highest score)
        assert_eq!(merged[0].file_path, "main.rs");
        assert!(merged[0].score > 0.8); // Boosted by multiple modes
        assert_eq!(merged[0].match_type, SearchMode::Hybrid); // Found by all three modes
        assert_eq!(merged[0].symbol_name.as_deref(), Some("foo")); // Symbol metadata from AST
        assert_eq!(merged[0].end_line, 20); // Wider range from AST result
    }

    #[test]
    fn test_merge_results_truncation() {
        let items: Vec<SearchResultItem> = (0..20)
            .map(|i| SearchResultItem {
                repo_id: "r1".into(),
                file_path: format!("file{}.rs", i),
                start_line: 1,
                end_line: 10,
                content_snippet: String::new(),
                match_type: SearchMode::Text,
                score: 0.5,
                symbol_name: None,
                symbol_kind: None,
                parent_symbol: None,
            })
            .collect();

        let merged = merge_results(items, 5);
        assert_eq!(merged.len(), 5);
    }

    #[test]
    fn test_merge_results_semantic_ast_text_triple() {
        let items = vec![
            SearchResultItem {
                repo_id: "r1".into(),
                file_path: "main.rs".into(),
                start_line: 10,
                end_line: 15,
                content_snippet: "fn foo()".into(),
                match_type: SearchMode::Text,
                score: 0.7,
                symbol_name: None,
                symbol_kind: None,
                parent_symbol: None,
            },
            SearchResultItem {
                repo_id: "r1".into(),
                file_path: "main.rs".into(),
                start_line: 10,
                end_line: 20,
                content_snippet: String::new(),
                match_type: SearchMode::Semantic,
                score: 0.85,
                symbol_name: None,
                symbol_kind: None,
                parent_symbol: None,
            },
        ];

        let merged = merge_results(items, 10);
        assert_eq!(merged.len(), 1);
        assert_eq!(merged[0].match_type, SearchMode::Hybrid);
        // Score should be boosted: (0.7 + 0.85) * 1.2 = 1.86
        assert!((merged[0].score - 1.86).abs() < 0.01);
    }

    #[cfg(feature = "indexing")]
    #[tokio::test]
    async fn test_hybrid_search_engine() {
        let temp_dir = std::env::temp_dir().join("uc-test-hybrid-search");
        let _ = std::fs::remove_dir_all(&temp_dir);
        std::fs::create_dir_all(&temp_dir).unwrap();

        std::fs::write(
            temp_dir.join("main.rs"),
            r#"fn process_data(data: &str) -> Result<String> {
    let config = Config::load();
    Ok(data.to_string())
}

struct Config {
    path: String,
}

impl Config {
    fn load() -> Self {
        Config { path: "/etc/app".into() }
    }
}"#,
        )
        .unwrap();

        let metadata = Arc::new(PostgresMetadataStore::new_fallback());
        let pipeline = Arc::new(IndexPipeline::new(metadata));

        // Index first
        let request = IndexRequest {
            repo: RepoSpec {
                repo_id: "test-hybrid".to_string(),
                remote_url: String::new(),
                default_branch: "main".to_string(),
                local_path: Some(temp_dir.to_string_lossy().to_string()),
            },
            force_full: true,
        };
        pipeline.index_repo(&request).await.unwrap();

        let engine = HybridSearchEngine::new(pipeline);

        // Text-only search
        let query = SearchQuery {
            query: "process data".to_string(),
            modes: vec![SearchMode::Text],
            repo_ids: vec![],
            languages: vec![],
            path_patterns: vec![],
            max_results: 10,
        };

        let result = engine.search(&query).await.unwrap();
        assert!(!result.items.is_empty());

        // Hybrid search (text + AST)
        let hybrid_query = SearchQuery {
            query: "process_data".to_string(),
            modes: vec![SearchMode::Hybrid],
            repo_ids: vec![],
            languages: vec![],
            path_patterns: vec![],
            max_results: 10,
        };

        let hybrid_result = engine.search(&hybrid_query).await.unwrap();
        // Should find results from both text and AST search
        assert!(!hybrid_result.items.is_empty());

        let _ = std::fs::remove_dir_all(&temp_dir);
    }
}
