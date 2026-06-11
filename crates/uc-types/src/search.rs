//! Search types for the hybrid retrieval system (Text + Semantic + AST).

use serde::{Deserialize, Serialize};

/// A search query against the code index.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SearchQuery {
    /// The search text (natural language or code pattern).
    pub query: String,
    /// Which search modes to use.
    pub modes: Vec<SearchMode>,
    /// Scope filter: restrict to specific repositories.
    pub repo_ids: Vec<String>,
    /// Scope filter: restrict to specific languages.
    pub languages: Vec<String>,
    /// Scope filter: restrict to specific file path patterns.
    pub path_patterns: Vec<String>,
    /// Maximum number of results.
    pub max_results: u32,
}

/// Search mode selector.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Default)]
pub enum SearchMode {
    /// Keyword/regex exact matching (like ripgrep).
    Text,
    /// Semantic vector similarity search.
    Semantic,
    /// Structural AST-based query (symbols, references, call chains).
    Ast,
    /// Combine all modes with relevance-weighted scoring.
    #[default]
    Hybrid,
}

/// A search result item.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SearchResult {
    pub items: Vec<SearchResultItem>,
}

/// A single item in the search results.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SearchResultItem {
    pub repo_id: String,
    pub file_path: String,
    pub start_line: u32,
    pub end_line: u32,
    /// The matching code snippet.
    pub content_snippet: String,
    /// Which search mode produced this result.
    pub match_type: SearchMode,
    /// Relevance score (0.0-1.0).
    pub score: f32,
    /// AST metadata (if match_type is Ast or Hybrid).
    pub symbol_name: Option<String>,
    pub symbol_kind: Option<String>,
    pub parent_symbol: Option<String>,
}

/// AST-specific query types.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum AstQuery {
    /// Find symbol definitions by name.
    SymbolSearch {
        name: String,
        kind: Option<SymbolKind>,
    },
    /// Find all references to a symbol.
    References {
        symbol_name: String,
        repo_id: Option<String>,
    },
    /// Find all call sites of a function.
    CallChain { function_name: String },
    /// Find all implementations of an interface/trait.
    Implementations { interface_name: String },
    /// Find imports in a repository.
    Imports { repo_id: String },
}

/// Kind of a code symbol.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum SymbolKind {
    Function,
    Method,
    Class,
    Struct,
    Interface,
    Trait,
    Enum,
    Variable,
    Constant,
    Type,
    Module,
    Import,
}
