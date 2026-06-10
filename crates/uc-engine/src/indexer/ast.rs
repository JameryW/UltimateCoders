//! AST-level indexing (tree-sitter -> PostgreSQL symbols/references).
//!
//! Uses tree-sitter to parse source files, extract symbol definitions
//! (function, class, method, struct, trait, enum, variable, constant,
//! type, import) and symbol references (call sites, imports, type usages).
//!
//! When the `indexing` feature is disabled, all methods return errors.

use uc_types::error::EngineError;
use uc_types::search::{AstQuery, SearchMode, SearchResultItem, SymbolKind};

use crate::metadata::postgres::{PostgresMetadataStore, SymbolInsert};

/// Result of parsing a single file.
#[derive(Debug, Clone)]
pub struct AstParseResult {
    pub file_path: String,
    pub language: String,
    pub symbols: Vec<ExtractedSymbol>,
    pub references: Vec<ExtractedReference>,
}

/// A symbol extracted from an AST.
#[derive(Debug, Clone)]
pub struct ExtractedSymbol {
    pub name: String,
    pub kind: SymbolKind,
    pub start_line: u32,
    pub start_col: u32,
    pub end_line: u32,
    pub end_col: u32,
    pub parent_symbol: Option<String>,
    pub content: String,
}

/// A reference extracted from an AST.
#[derive(Debug, Clone)]
pub struct ExtractedReference {
    pub target_name: String,
    pub reference_kind: String,
    pub start_line: u32,
    pub start_col: u32,
    pub source_symbol: Option<String>,
}

/// AST indexer — parses source files and extracts symbols/references.
pub struct AstIndexer {
    #[cfg(feature = "indexing")]
    supported_languages: Vec<&'static str>,
}

impl AstIndexer {
    /// Create a new AST indexer.
    pub fn new() -> Self {
        #[cfg(feature = "indexing")]
        {
            Self {
                supported_languages: vec!["rust", "python"],
            }
        }
        #[cfg(not(feature = "indexing"))]
        {
            Self {}
        }
    }

    /// Check if a language is supported.
    pub fn supports_language(&self, language: &str) -> bool {
        #[cfg(feature = "indexing")]
        {
            self.supported_languages.contains(&language)
        }
        #[cfg(not(feature = "indexing"))]
        {
            let _ = language;
            false
        }
    }

    /// Parse a single file and extract symbols and references.
    pub fn parse_file(
        &self,
        file_path: &str,
        content: &str,
        language: &str,
    ) -> Result<AstParseResult, EngineError> {
        #[cfg(feature = "indexing")]
        {
            if !self.supports_language(language) {
                return Ok(AstParseResult {
                    file_path: file_path.to_string(),
                    language: language.to_string(),
                    symbols: vec![],
                    references: vec![],
                });
            }

            let mut parser = tree_sitter::Parser::new();

            match language {
                "rust" => parser
                    .set_language(&tree_sitter_rust::LANGUAGE.into())
                    .map_err(|e| EngineError::IndexingError(format!("Rust grammar error: {}", e)))?,
                "python" => parser
                    .set_language(&tree_sitter_python::LANGUAGE.into())
                    .map_err(|e| EngineError::IndexingError(format!("Python grammar error: {}", e)))?,
                _ => {
                    return Ok(AstParseResult {
                        file_path: file_path.to_string(),
                        language: language.to_string(),
                        symbols: vec![],
                        references: vec![],
                    });
                }
            }

            let tree = parser
                .parse(content, None)
                .ok_or_else(|| EngineError::IndexingError("Parse failed".into()))?;

            let (symbols, references) = match language {
                "rust" => extract_rust_symbols(&tree, content),
                "python" => extract_python_symbols(&tree, content),
                _ => (vec![], vec![]),
            };

            Ok(AstParseResult {
                file_path: file_path.to_string(),
                language: language.to_string(),
                symbols,
                references,
            })
        }
        #[cfg(not(feature = "indexing"))]
        {
            let _ = (file_path, content, language);
            Err(EngineError::IndexingError("Indexing feature is disabled".into()))
        }
    }

    /// Parse a file and store results in the metadata store.
    pub async fn index_file(
        &self,
        metadata: &PostgresMetadataStore,
        repo_id: &str,
        file_path: &str,
        content: &str,
        language: &str,
        content_hash: &str,
    ) -> Result<AstParseResult, EngineError> {
        let result = self.parse_file(file_path, content, language)?;

        // Convert extracted symbols to insert format and store
        let symbol_inserts: Vec<SymbolInsert> = result
            .symbols
            .iter()
            .map(|s| SymbolInsert {
                file_path: file_path.to_string(),
                name: s.name.clone(),
                kind: s.kind.clone(),
                start_line: s.start_line,
                start_col: s.start_col,
                end_line: s.end_line,
                end_col: s.end_col,
                language: language.to_string(),
                content_hash: content_hash.to_string(),
            })
            .collect();

        if !symbol_inserts.is_empty() {
            metadata.insert_symbols(repo_id, symbol_inserts).await?;
        }

        Ok(result)
    }

    /// Search for symbols using an AST query.
    pub async fn search(
        &self,
        metadata: &PostgresMetadataStore,
        query: &AstQuery,
        max_results: u32,
    ) -> Result<Vec<SearchResultItem>, EngineError> {
        match query {
            AstQuery::SymbolSearch { name, kind } => {
                let results = metadata
                    .search_symbols(name, None, kind.as_ref(), max_results)
                    .await?;

                Ok(results
                    .into_iter()
                    .map(|s| SearchResultItem {
                        repo_id: s.repo_id,
                        file_path: s.file_path,
                        start_line: s.start_line,
                        end_line: s.end_line,
                        content_snippet: String::new(),
                        match_type: SearchMode::Ast,
                        score: 1.0,
                        symbol_name: Some(s.name),
                        symbol_kind: Some(format!("{:?}", s.kind).to_lowercase()),
                        parent_symbol: None,
                    })
                    .collect())
            }

            AstQuery::References { symbol_name, repo_id } => {
                let results = metadata
                    .search_references(symbol_name, repo_id.as_deref(), max_results)
                    .await?;

                Ok(results
                    .into_iter()
                    .map(|r| SearchResultItem {
                        repo_id: r.repo_id,
                        file_path: r.file_path,
                        start_line: r.start_line,
                        end_line: r.start_line,
                        content_snippet: String::new(),
                        match_type: SearchMode::Ast,
                        score: 0.9,
                        symbol_name: Some(r.target_name),
                        symbol_kind: Some(r.reference_kind),
                        parent_symbol: None,
                    })
                    .collect())
            }

            AstQuery::CallChain { function_name } => {
                let results = metadata
                    .search_references(function_name, None, max_results)
                    .await?;

                Ok(results
                    .into_iter()
                    .filter(|r| r.reference_kind == "call")
                    .map(|r| SearchResultItem {
                        repo_id: r.repo_id,
                        file_path: r.file_path,
                        start_line: r.start_line,
                        end_line: r.start_line,
                        content_snippet: String::new(),
                        match_type: SearchMode::Ast,
                        score: 0.85,
                        symbol_name: Some(r.target_name),
                        symbol_kind: Some("call".to_string()),
                        parent_symbol: None,
                    })
                    .collect())
            }

            AstQuery::Implementations { interface_name } => {
                let results = metadata
                    .search_symbols(interface_name, None, None, max_results)
                    .await?;

                Ok(results
                    .into_iter()
                    .map(|s| SearchResultItem {
                        repo_id: s.repo_id,
                        file_path: s.file_path,
                        start_line: s.start_line,
                        end_line: s.end_line,
                        content_snippet: String::new(),
                        match_type: SearchMode::Ast,
                        score: 0.8,
                        symbol_name: Some(s.name),
                        symbol_kind: Some(format!("{:?}", s.kind).to_lowercase()),
                        parent_symbol: None,
                    })
                    .collect())
            }

            AstQuery::Imports { repo_id } => {
                let results = metadata
                    .search_symbols("import", Some(repo_id), Some(&SymbolKind::Import), max_results)
                    .await?;

                Ok(results
                    .into_iter()
                    .map(|s| SearchResultItem {
                        repo_id: s.repo_id,
                        file_path: s.file_path,
                        start_line: s.start_line,
                        end_line: s.end_line,
                        content_snippet: String::new(),
                        match_type: SearchMode::Ast,
                        score: 0.7,
                        symbol_name: Some(s.name),
                        symbol_kind: Some("import".to_string()),
                        parent_symbol: None,
                    })
                    .collect())
            }
        }
    }
}

impl Default for AstIndexer {
    fn default() -> Self {
        Self::new()
    }
}

/// Detect whether a file should be parsed by the AST indexer based on language.
pub fn should_parse(file_path: &str) -> bool {
    crate::git::detect_language(file_path)
        .map(|lang| matches!(lang, "rust" | "python"))
        .unwrap_or(false)
}

// ── Rust AST Extraction (indexing feature only) ───────────────

#[cfg(feature = "indexing")]
fn extract_rust_symbols(
    tree: &tree_sitter::Tree,
    source: &str,
) -> (Vec<ExtractedSymbol>, Vec<ExtractedReference>) {
    let mut symbols = Vec::new();
    let mut references = Vec::new();
    let mut parent_stack: Vec<String> = Vec::new();

    walk_rust_node(
        tree.root_node(),
        source,
        &mut symbols,
        &mut references,
        &mut parent_stack,
    );

    (symbols, references)
}

#[cfg(feature = "indexing")]
fn walk_rust_node(
    node: tree_sitter::Node,
    source: &str,
    symbols: &mut Vec<ExtractedSymbol>,
    references: &mut Vec<ExtractedReference>,
    parent_stack: &mut Vec<String>,
) {
    let kind = node.kind();

    match kind {
        "function_item" | "function_signature_item" => {
            if let Some(name_node) = node.child_by_field_name("name") {
                let name = node_text(&name_node, source);

                symbols.push(ExtractedSymbol {
                    name: name.clone(),
                    kind: if parent_stack.last().map(|p| !p.is_empty()).unwrap_or(false) {
                        SymbolKind::Method
                    } else {
                        SymbolKind::Function
                    },
                    start_line: node.start_position().row as u32 + 1,
                    start_col: node.start_position().column as u32,
                    end_line: node.end_position().row as u32 + 1,
                    end_col: node.end_position().column as u32,
                    parent_symbol: parent_stack.last().cloned(),
                    content: node_text(&node, source),
                });

                parent_stack.push(name);
                for child in node_children(&node) {
                    walk_rust_node(child, source, symbols, references, parent_stack);
                }
                parent_stack.pop();
                return;
            }
        }
        "struct_item" => {
            if let Some(name_node) = node.child_by_field_name("name") {
                let name = node_text(&name_node, source);

                symbols.push(ExtractedSymbol {
                    name: name.clone(),
                    kind: SymbolKind::Struct,
                    start_line: node.start_position().row as u32 + 1,
                    start_col: node.start_position().column as u32,
                    end_line: node.end_position().row as u32 + 1,
                    end_col: node.end_position().column as u32,
                    parent_symbol: parent_stack.last().cloned(),
                    content: node_text(&node, source),
                });

                parent_stack.push(name);
                for child in node_children(&node) {
                    walk_rust_node(child, source, symbols, references, parent_stack);
                }
                parent_stack.pop();
                return;
            }
        }
        "enum_item" => {
            if let Some(name_node) = node.child_by_field_name("name") {
                let name = node_text(&name_node, source);

                symbols.push(ExtractedSymbol {
                    name: name.clone(),
                    kind: SymbolKind::Enum,
                    start_line: node.start_position().row as u32 + 1,
                    start_col: node.start_position().column as u32,
                    end_line: node.end_position().row as u32 + 1,
                    end_col: node.end_position().column as u32,
                    parent_symbol: parent_stack.last().cloned(),
                    content: node_text(&node, source),
                });

                parent_stack.push(name);
                for child in node_children(&node) {
                    walk_rust_node(child, source, symbols, references, parent_stack);
                }
                parent_stack.pop();
                return;
            }
        }
        "trait_item" => {
            if let Some(name_node) = node.child_by_field_name("name") {
                let name = node_text(&name_node, source);

                symbols.push(ExtractedSymbol {
                    name: name.clone(),
                    kind: SymbolKind::Trait,
                    start_line: node.start_position().row as u32 + 1,
                    start_col: node.start_position().column as u32,
                    end_line: node.end_position().row as u32 + 1,
                    end_col: node.end_position().column as u32,
                    parent_symbol: parent_stack.last().cloned(),
                    content: node_text(&node, source),
                });

                parent_stack.push(name);
                for child in node_children(&node) {
                    walk_rust_node(child, source, symbols, references, parent_stack);
                }
                parent_stack.pop();
                return;
            }
        }
        "impl_item" => {
            if let Some(type_node) = node.child_by_field_name("type") {
                let type_name = node_text(&type_node, source);

                if let Some(trait_node) = node.child_by_field_name("trait") {
                    let trait_name = node_text(&trait_node, source);
                    references.push(ExtractedReference {
                        target_name: trait_name,
                        reference_kind: "implementation".to_string(),
                        start_line: node.start_position().row as u32 + 1,
                        start_col: node.start_position().column as u32,
                        source_symbol: None,
                    });
                }

                parent_stack.push(type_name);
                for child in node_children(&node) {
                    walk_rust_node(child, source, symbols, references, parent_stack);
                }
                parent_stack.pop();
                return;
            }
        }
        "type_item" => {
            if let Some(name_node) = node.child_by_field_name("name") {
                let name = node_text(&name_node, source);

                symbols.push(ExtractedSymbol {
                    name: name.clone(),
                    kind: SymbolKind::Type,
                    start_line: node.start_position().row as u32 + 1,
                    start_col: node.start_position().column as u32,
                    end_line: node.end_position().row as u32 + 1,
                    end_col: node.end_position().column as u32,
                    parent_symbol: parent_stack.last().cloned(),
                    content: node_text(&node, source),
                });
                return;
            }
        }
        "const_item" => {
            if let Some(name_node) = node.child_by_field_name("name") {
                let name = node_text(&name_node, source);

                symbols.push(ExtractedSymbol {
                    name: name.clone(),
                    kind: SymbolKind::Constant,
                    start_line: node.start_position().row as u32 + 1,
                    start_col: node.start_position().column as u32,
                    end_line: node.end_position().row as u32 + 1,
                    end_col: node.end_position().column as u32,
                    parent_symbol: parent_stack.last().cloned(),
                    content: node_text(&node, source),
                });
                return;
            }
        }
        "static_item" => {
            if let Some(name_node) = node.child_by_field_name("name") {
                let name = node_text(&name_node, source);

                symbols.push(ExtractedSymbol {
                    name: name.clone(),
                    kind: SymbolKind::Constant,
                    start_line: node.start_position().row as u32 + 1,
                    start_col: node.start_position().column as u32,
                    end_line: node.end_position().row as u32 + 1,
                    end_col: node.end_position().column as u32,
                    parent_symbol: parent_stack.last().cloned(),
                    content: node_text(&node, source),
                });
                return;
            }
        }
        "use_declaration" => {
            let mut cursor = node.walk();
            if cursor.goto_first_child() {
                loop {
                    let child = cursor.node();
                    if child.kind() == "scoped_identifier" || child.kind() == "identifier" {
                        let import_path = node_text(&child, source);
                        symbols.push(ExtractedSymbol {
                            name: import_path.clone(),
                            kind: SymbolKind::Import,
                            start_line: child.start_position().row as u32 + 1,
                            start_col: child.start_position().column as u32,
                            end_line: child.end_position().row as u32 + 1,
                            end_col: child.end_position().column as u32,
                            parent_symbol: None,
                            content: node_text(&node, source),
                        });
                        break;
                    }
                    if !cursor.goto_next_sibling() {
                        break;
                    }
                }
            }
        }
        "call_expression" => {
            if let Some(func_node) = node.child_by_field_name("function") {
                let func_name = node_text(&func_node, source);
                if !func_name.contains('.') && !func_name.contains("::") {
                    references.push(ExtractedReference {
                        target_name: func_name,
                        reference_kind: "call".to_string(),
                        start_line: func_node.start_position().row as u32 + 1,
                        start_col: func_node.start_position().column as u32,
                        source_symbol: parent_stack.last().cloned(),
                    });
                }
            }
        }
        _ => {}
    }

    for child in node_children(&node) {
        walk_rust_node(child, source, symbols, references, parent_stack);
    }
}

// ── Python AST Extraction (indexing feature only) ─────────────

#[cfg(feature = "indexing")]
fn extract_python_symbols(
    tree: &tree_sitter::Tree,
    source: &str,
) -> (Vec<ExtractedSymbol>, Vec<ExtractedReference>) {
    let mut symbols = Vec::new();
    let mut references = Vec::new();
    let mut parent_stack: Vec<String> = Vec::new();

    walk_python_node(
        tree.root_node(),
        source,
        &mut symbols,
        &mut references,
        &mut parent_stack,
    );

    (symbols, references)
}

#[cfg(feature = "indexing")]
fn walk_python_node(
    node: tree_sitter::Node,
    source: &str,
    symbols: &mut Vec<ExtractedSymbol>,
    references: &mut Vec<ExtractedReference>,
    parent_stack: &mut Vec<String>,
) {
    let kind = node.kind();

    match kind {
        "function_definition" => {
            if let Some(name_node) = node.child_by_field_name("name") {
                let name = node_text(&name_node, source);

                symbols.push(ExtractedSymbol {
                    name: name.clone(),
                    kind: if parent_stack.last().map(|p| !p.is_empty()).unwrap_or(false) {
                        SymbolKind::Method
                    } else {
                        SymbolKind::Function
                    },
                    start_line: node.start_position().row as u32 + 1,
                    start_col: node.start_position().column as u32,
                    end_line: node.end_position().row as u32 + 1,
                    end_col: node.end_position().column as u32,
                    parent_symbol: parent_stack.last().cloned(),
                    content: node_text(&node, source),
                });

                parent_stack.push(name);
                for child in node_children(&node) {
                    walk_python_node(child, source, symbols, references, parent_stack);
                }
                parent_stack.pop();
                return;
            }
        }
        "class_definition" => {
            if let Some(name_node) = node.child_by_field_name("name") {
                let name = node_text(&name_node, source);

                symbols.push(ExtractedSymbol {
                    name: name.clone(),
                    kind: SymbolKind::Class,
                    start_line: node.start_position().row as u32 + 1,
                    start_col: node.start_position().column as u32,
                    end_line: node.end_position().row as u32 + 1,
                    end_col: node.end_position().column as u32,
                    parent_symbol: parent_stack.last().cloned(),
                    content: node_text(&node, source),
                });

                // Extract base classes (inheritance references)
                let cursor = node.walk();
                let _ = cursor;
                for i in 0..node.child_count() {
                    let child = node.child(i as u32).unwrap();
                    if child.kind() == "argument_list" {
                        let mut arg_cursor = child.walk();
                        if arg_cursor.goto_first_child() {
                            loop {
                                let arg = arg_cursor.node();
                                if arg.kind() == "identifier" {
                                    references.push(ExtractedReference {
                                        target_name: node_text(&arg, source),
                                        reference_kind: "inheritance".to_string(),
                                        start_line: arg.start_position().row as u32 + 1,
                                        start_col: arg.start_position().column as u32,
                                        source_symbol: Some(name.clone()),
                                    });
                                }
                                if !arg_cursor.goto_next_sibling() {
                                    break;
                                }
                            }
                        }
                        break;
                    }
                }

                parent_stack.push(name);
                for child in node_children(&node) {
                    walk_python_node(child, source, symbols, references, parent_stack);
                }
                parent_stack.pop();
                return;
            }
        }
        "import_statement" | "import_from_statement" => {
            let mut cursor = node.walk();
            if cursor.goto_first_child() {
                loop {
                    let child = cursor.node();
                    if child.kind() == "dotted_name" || child.kind() == "aliased_import" {
                        let import_name = node_text(&child, source);
                        let short_name = import_name.split('.').next().unwrap_or(&import_name);
                        symbols.push(ExtractedSymbol {
                            name: short_name.to_string(),
                            kind: SymbolKind::Import,
                            start_line: child.start_position().row as u32 + 1,
                            start_col: child.start_position().column as u32,
                            end_line: child.end_position().row as u32 + 1,
                            end_col: child.end_position().column as u32,
                            parent_symbol: None,
                            content: node_text(&node, source),
                        });
                    }
                    if !cursor.goto_next_sibling() {
                        break;
                    }
                }
            }
        }
        "call" => {
            if let Some(func_node) = node.child_by_field_name("function") {
                let func_name = node_text(&func_node, source);
                if !func_name.contains('.') {
                    references.push(ExtractedReference {
                        target_name: func_name,
                        reference_kind: "call".to_string(),
                        start_line: func_node.start_position().row as u32 + 1,
                        start_col: func_node.start_position().column as u32,
                        source_symbol: parent_stack.last().cloned(),
                    });
                }
            }
        }
        _ => {}
    }

    for child in node_children(&node) {
        walk_python_node(child, source, symbols, references, parent_stack);
    }
}

// ── Helpers (indexing feature only) ───────────────────────────

#[cfg(feature = "indexing")]
fn node_text(node: &tree_sitter::Node, source: &str) -> String {
    node.utf8_text(source.as_bytes())
        .unwrap_or("")
        .to_string()
}

#[cfg(feature = "indexing")]
fn node_children<'a>(node: &tree_sitter::Node<'a>) -> Vec<tree_sitter::Node<'a>> {
    let mut children = Vec::new();
    let mut cursor = node.walk();
    if cursor.goto_first_child() {
        loop {
            children.push(cursor.node());
            if !cursor.goto_next_sibling() {
                break;
            }
        }
    }
    children
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_ast_indexer_supports_language() {
        let indexer = AstIndexer::new();
        #[cfg(feature = "indexing")]
        {
            assert!(indexer.supports_language("rust"));
            assert!(indexer.supports_language("python"));
            assert!(!indexer.supports_language("brainfuck"));
        }
        #[cfg(not(feature = "indexing"))]
        {
            assert!(!indexer.supports_language("rust"));
        }
    }

    #[cfg(feature = "indexing")]
    #[test]
    fn test_should_parse() {
        assert!(should_parse("main.rs"));
        assert!(should_parse("app.py"));
        assert!(!should_parse("config.toml"));
        assert!(!should_parse("README.md"));
    }

    #[cfg(feature = "indexing")]
    #[test]
    fn test_parse_rust_file() {
        let indexer = AstIndexer::new();
        let source = r#"
fn main() {
    let config = Config::new();
    println!("Hello");
}

struct Config {
    name: String,
}

impl Config {
    fn new() -> Self {
        Config { name: "default".into() }
    }
}

trait HasName {
    fn name(&self) -> &str;
}

const MAX_RETRIES: u32 = 3;
"#;

        let result = indexer.parse_file("src/main.rs", source, "rust").unwrap();

        assert!(!result.symbols.is_empty());

        let fn_names: Vec<&str> = result.symbols.iter().map(|s| s.name.as_str()).collect();
        assert!(fn_names.contains(&"main"));
        assert!(fn_names.contains(&"new"));
        assert!(fn_names.contains(&"Config"));
        assert!(fn_names.contains(&"MAX_RETRIES"));
        assert!(fn_names.contains(&"HasName"));
    }

    #[cfg(feature = "indexing")]
    #[test]
    fn test_parse_python_file() {
        let indexer = AstIndexer::new();
        let source = r#"
class MyClass(BaseClass):
    def __init__(self):
        self.value = 0

    def process(self):
        return helper(self.value)

def helper(value):
    return value * 2

import os
from collections import defaultdict
"#;

        let result = indexer
            .parse_file("src/main.py", source, "python")
            .unwrap();

        assert!(!result.symbols.is_empty());

        let names: Vec<&str> = result.symbols.iter().map(|s| s.name.as_str()).collect();
        assert!(names.contains(&"MyClass"));
        assert!(names.contains(&"__init__"));
        assert!(names.contains(&"process"));
        assert!(names.contains(&"helper"));
    }

    #[cfg(feature = "indexing")]
    #[test]
    fn test_parse_unsupported_language() {
        let indexer = AstIndexer::new();
        let result = indexer.parse_file("test.brainfuck", "+++", "brainfuck").unwrap();
        assert!(result.symbols.is_empty());
        assert!(result.references.is_empty());
    }

    #[cfg(feature = "indexing")]
    #[test]
    fn test_rust_function_line_numbers() {
        let indexer = AstIndexer::new();
        let source = "fn hello() {\n    println!(\"hi\");\n}\n";

        let result = indexer.parse_file("test.rs", source, "rust").unwrap();
        let hello = result.symbols.iter().find(|s| s.name == "hello").unwrap();
        assert_eq!(hello.start_line, 1);
        assert_eq!(hello.end_line, 3);
    }

    #[cfg(feature = "indexing")]
    #[test]
    fn test_python_method_parent() {
        let indexer = AstIndexer::new();
        let source = "class Foo:\n    def bar(self):\n        pass\n";

        let result = indexer.parse_file("test.py", source, "python").unwrap();
        let bar = result.symbols.iter().find(|s| s.name == "bar").unwrap();
        assert_eq!(bar.kind, SymbolKind::Method);
        assert_eq!(bar.parent_symbol.as_deref(), Some("Foo"));
    }
}
