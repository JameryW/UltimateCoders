//! Text-level indexing (trigram/ngram + language-aware tokenization).
//!
//! Provides an in-memory inverted index for keyword and regex search across
//! indexed repositories. Language-aware tokenization splits camelCase and
//! snake_case identifiers into searchable tokens.
//!
//! When the `indexing` feature is disabled, provides a minimal stub that
//! always returns errors.

use uc_types::error::EngineError;
use uc_types::search::{SearchQuery, SearchResult};

// ── Full implementation (indexing feature enabled) ────────────

#[cfg(feature = "indexing")]
use std::collections::{BTreeMap, HashMap, HashSet};

#[cfg(feature = "indexing")]
use regex;

/// In-memory text search index.
pub struct TextSearchIndex {
    #[cfg(feature = "indexing")]
    inverted_index: HashMap<String, Vec<Posting>>,
    #[cfg(feature = "indexing")]
    documents: HashMap<(String, String), DocumentMeta>,
    #[cfg(feature = "indexing")]
    doc_count: u64,
}

#[cfg(feature = "indexing")]
/// A posting entry: which file and line contains a token.
#[derive(Debug, Clone)]
struct Posting {
    repo_id: String,
    file_path: String,
    line_number: u32,
    /// Term frequency in this document.
    tf: u32,
}

#[cfg(feature = "indexing")]
/// Document metadata for ranking.
#[derive(Debug, Clone)]
#[allow(dead_code)]
struct DocumentMeta {
    repo_id: String,
    file_path: String,
    language: String,
    total_tokens: u32,
}

impl TextSearchIndex {
    /// Create a new empty text search index.
    pub fn new() -> Self {
        #[cfg(feature = "indexing")]
        {
            Self {
                inverted_index: HashMap::new(),
                documents: HashMap::new(),
                doc_count: 0,
            }
        }
        #[cfg(not(feature = "indexing"))]
        {
            Self {}
        }
    }

    /// Index a file's content.
    pub fn index_file(
        &mut self,
        repo_id: &str,
        file_path: &str,
        language: &str,
        content: &str,
    ) -> Result<(), EngineError> {
        #[cfg(feature = "indexing")]
        {
            let doc_key = (repo_id.to_string(), file_path.to_string());

            // Remove old postings for this document if it was previously indexed
            self.remove_file(repo_id, file_path);

            // Tokenize
            let tokens = tokenize_content(content, language);
            let total_tokens = tokens.len() as u32;

            // Count term frequencies per line
            let mut line_token_counts: BTreeMap<u32, HashMap<String, u32>> = BTreeMap::new();
            for (line_num, token) in &tokens {
                let line_map = line_token_counts.entry(*line_num).or_default();
                *line_map.entry(token.clone()).or_default() += 1;
            }

            // Build postings
            for (line_num, token_counts) in line_token_counts {
                for (token, tf) in token_counts {
                    let posting = Posting {
                        repo_id: repo_id.to_string(),
                        file_path: file_path.to_string(),
                        line_number: line_num,
                        tf,
                    };
                    self.inverted_index
                        .entry(token)
                        .or_default()
                        .push(posting);
                }
            }

            // Store document metadata
            self.documents.insert(
                doc_key,
                DocumentMeta {
                    repo_id: repo_id.to_string(),
                    file_path: file_path.to_string(),
                    language: language.to_string(),
                    total_tokens,
                },
            );
            self.doc_count += 1;

            Ok(())
        }
        #[cfg(not(feature = "indexing"))]
        {
            let _ = (repo_id, file_path, language, content);
            Err(EngineError::IndexingError("Indexing feature is disabled".into()))
        }
    }

    /// Remove a file from the index.
    pub fn remove_file(&mut self, repo_id: &str, file_path: &str) {
        #[cfg(feature = "indexing")]
        {
            let doc_key = (repo_id.to_string(), file_path.to_string());

            if self.documents.remove(&doc_key).is_some() {
                self.doc_count = self.doc_count.saturating_sub(1);

                // Remove postings for this document
                for postings in self.inverted_index.values_mut() {
                    postings.retain(|p| p.repo_id != repo_id || p.file_path != file_path);
                }

                // Clean up empty posting lists
                self.inverted_index.retain(|_, v| !v.is_empty());
            }
        }
        #[cfg(not(feature = "indexing"))]
        {
            let _ = (repo_id, file_path);
        }
    }

    /// Remove all files for a repository.
    pub fn remove_repo(&mut self, repo_id: &str) {
        #[cfg(feature = "indexing")]
        {
            let files_to_remove: Vec<String> = self
                .documents
                .keys()
                .filter(|(rid, _)| rid == repo_id)
                .map(|(_, fp)| fp.clone())
                .collect();

            for file_path in &files_to_remove {
                self.remove_file(repo_id, file_path);
            }
        }
        #[cfg(not(feature = "indexing"))]
        {
            let _ = repo_id;
        }
    }

    /// Search the text index.
    pub fn search(&self, query: &SearchQuery) -> Result<SearchResult, EngineError> {
        #[cfg(feature = "indexing")]
        {
            let query_text = query.query.trim();
            if query_text.is_empty() {
                return Ok(SearchResult { items: vec![] });
            }

            let is_regex = query_text.starts_with('/') && query_text.ends_with('/')
                || query_text.contains('|')
                || query_text.contains('*')
                || query_text.contains('+')
                || query_text.contains('[')
                || query_text.contains('(');

            let results = if is_regex {
                self.search_regex(query)?
            } else {
                self.search_keywords(query)?
            };

            Ok(results)
        }
        #[cfg(not(feature = "indexing"))]
        {
            let _ = query;
            Err(EngineError::SearchError("Indexing feature is disabled".into()))
        }
    }

    /// Get the number of indexed documents.
    pub fn doc_count(&self) -> u64 {
        #[cfg(feature = "indexing")]
        {
            self.doc_count
        }
        #[cfg(not(feature = "indexing"))]
        {
            0
        }
    }

    /// Get the number of unique tokens in the index.
    pub fn token_count(&self) -> usize {
        #[cfg(feature = "indexing")]
        {
            self.inverted_index.len()
        }
        #[cfg(not(feature = "indexing"))]
        {
            0
        }
    }
}

impl Default for TextSearchIndex {
    fn default() -> Self {
        Self::new()
    }
}

// ── Search implementations (indexing feature only) ────────────

#[cfg(feature = "indexing")]
impl TextSearchIndex {
    /// Keyword search: tokenize the query and find matching documents.
    fn search_keywords(&self, query: &SearchQuery) -> Result<SearchResult, EngineError> {
        let query_tokens = tokenize_query(&query.query);
        if query_tokens.is_empty() {
            return Ok(SearchResult { items: vec![] });
        }

        let mut doc_scores: HashMap<(String, String), f32> = HashMap::new();
        let mut doc_lines: HashMap<(String, String), HashSet<u32>> = HashMap::new();

        for token in &query_tokens {
            let token_lower = token.to_lowercase();
            if let Some(postings) = self.inverted_index.get(&token_lower) {
                let idf = compute_idf(self.doc_count, postings.len() as u64);
                for posting in postings {
                    if !passes_filters(posting, query, &self.documents) {
                        continue;
                    }

                    let doc_key = (posting.repo_id.clone(), posting.file_path.clone());
                    let tf = posting.tf as f32;
                    let score = tf * idf;
                    *doc_scores.entry(doc_key.clone()).or_default() += score;
                    doc_lines
                        .entry(doc_key)
                        .or_default()
                        .insert(posting.line_number);
                }
            }
        }

        let mut items = build_result_items(doc_scores, doc_lines, uc_types::search::SearchMode::Text);
        // Normalize by query length for keyword search
        for item in &mut items {
            item.score /= query_tokens.len() as f32;
        }

        items.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal));
        items.truncate(query.max_results as usize);

        Ok(SearchResult { items })
    }

    /// Regex search: match the query pattern against indexed tokens.
    fn search_regex(&self, query: &SearchQuery) -> Result<SearchResult, EngineError> {
        let pattern = if query.query.starts_with('/') && query.query.ends_with('/') {
            &query.query[1..query.query.len() - 1]
        } else {
            &query.query
        };

        let regex = regex::Regex::new(pattern)
            .map_err(|e| EngineError::SearchError(format!("Invalid regex pattern: {}", e)))?;

        let mut doc_scores: HashMap<(String, String), f32> = HashMap::new();
        let mut doc_lines: HashMap<(String, String), HashSet<u32>> = HashMap::new();

        for (token, postings) in &self.inverted_index {
            if !regex.is_match(token) {
                continue;
            }

            let idf = compute_idf(self.doc_count, postings.len() as u64);
            for posting in postings {
                if !passes_filters(posting, query, &self.documents) {
                    continue;
                }

                let doc_key = (posting.repo_id.clone(), posting.file_path.clone());
                let tf = posting.tf as f32;
                let score = tf * idf;
                *doc_scores.entry(doc_key.clone()).or_default() += score;
                doc_lines
                    .entry(doc_key)
                    .or_default()
                    .insert(posting.line_number);
            }
        }

        let mut items = build_result_items(doc_scores, doc_lines, uc_types::search::SearchMode::Text);
        items.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal));
        items.truncate(query.max_results as usize);

        Ok(SearchResult { items })
    }
}

// ── Helper functions (indexing feature only) ──────────────────

#[cfg(feature = "indexing")]
fn passes_filters(
    posting: &Posting,
    query: &SearchQuery,
    documents: &HashMap<(String, String), DocumentMeta>,
) -> bool {
    if !query.repo_ids.is_empty() && !query.repo_ids.contains(&posting.repo_id) {
        return false;
    }
    if !query.languages.is_empty() {
        let doc_key = (posting.repo_id.clone(), posting.file_path.clone());
        if let Some(meta) = documents.get(&doc_key) {
            if !query.languages.contains(&meta.language) {
                return false;
            }
        }
    }
    if !query.path_patterns.is_empty() {
        let matches_path = query
            .path_patterns
            .iter()
            .any(|pattern| glob_match(pattern, &posting.file_path));
        if !matches_path {
            return false;
        }
    }
    true
}

#[cfg(feature = "indexing")]
fn build_result_items(
    doc_scores: HashMap<(String, String), f32>,
    doc_lines: HashMap<(String, String), HashSet<u32>>,
    match_type: uc_types::search::SearchMode,
) -> Vec<uc_types::search::SearchResultItem> {
    use uc_types::search::SearchResultItem;

    doc_scores
        .into_iter()
        .map(|(doc_key, score)| {
            let lines = doc_lines.get(&doc_key).cloned().unwrap_or_default();
            let min_line = lines.iter().min().copied().unwrap_or(1);
            let max_line = lines.iter().max().copied().unwrap_or(min_line);

            SearchResultItem {
                repo_id: doc_key.0.clone(),
                file_path: doc_key.1.clone(),
                start_line: min_line,
                end_line: max_line,
                content_snippet: String::new(),
                match_type: match_type.clone(),
                score,
                symbol_name: None,
                symbol_kind: None,
                parent_symbol: None,
            }
        })
        .collect()
}

// ── Tokenization (always available) ───────────────────────────

/// Tokenize file content with language-aware splitting.
///
/// Returns a list of (line_number, token) pairs. Tokens are lowercased.
/// Language-aware splitting handles:
/// - camelCase -> camel, case
/// - PascalCase -> pascal, case
/// - snake_case -> snake, case
/// - SCREAMING_SNAKE -> screaming, snake
/// - kebab-case -> kebab, case
#[cfg(feature = "indexing")]
pub fn tokenize_content(content: &str, _language: &str) -> Vec<(u32, String)> {
    let mut tokens = Vec::new();

    for (line_idx, line) in content.lines().enumerate() {
        let line_num = (line_idx + 1) as u32;
        let line_tokens = tokenize_line(line);
        for token in line_tokens {
            tokens.push((line_num, token));
        }
    }

    tokens
}

/// Tokenize a single line of code.
fn tokenize_line(line: &str) -> Vec<String> {
    let mut tokens = Vec::new();
    let mut current = String::new();
    let mut prev_char_type = CharType::Other;

    for ch in line.chars() {
        let char_type = classify_char(ch);

        match (prev_char_type, char_type) {
            (CharType::Lower, CharType::Lower) => current.push(ch),
            (CharType::Upper, CharType::Upper) => current.push(ch),
            (CharType::Digit, CharType::Digit) => current.push(ch),

            (CharType::Lower, CharType::Upper) => {
                if !current.is_empty() {
                    tokens.push(current.to_lowercase());
                }
                current = ch.to_string();
            }

            (CharType::Upper, CharType::Lower) => {
                if current.len() > 1 {
                    let boundary = current.len() - 1;
                    tokens.push(current[..boundary].to_lowercase());
                    current = format!("{}{}", &current[boundary..], ch);
                } else {
                    current.push(ch);
                }
            }

            (CharType::Lower | CharType::Upper, CharType::Digit) => {
                if !current.is_empty() {
                    tokens.push(current.to_lowercase());
                }
                current = ch.to_string();
            }
            (CharType::Digit, CharType::Lower | CharType::Upper) => {
                if !current.is_empty() {
                    tokens.push(current.to_lowercase());
                }
                current = ch.to_string();
            }

            (CharType::Lower | CharType::Upper | CharType::Digit, CharType::Separator) => {
                if !current.is_empty() {
                    tokens.push(current.to_lowercase());
                    current.clear();
                }
            }

            (_, CharType::Other) => {
                if !current.is_empty() {
                    tokens.push(current.to_lowercase());
                    current.clear();
                }
            }

            (CharType::Separator | CharType::Other, CharType::Lower | CharType::Upper | CharType::Digit) => {
                current = ch.to_string();
            }

            (CharType::Separator, CharType::Separator) => {}

            (CharType::Other, CharType::Separator) => {
                // Transition from other to separator — no action needed
            }
        }

        prev_char_type = char_type;
    }

    if !current.is_empty() {
        tokens.push(current.to_lowercase());
    }

    tokens.retain(|t| t.len() >= 2 && !is_stop_word(t));

    tokens
}

/// Tokenize a search query.
pub fn tokenize_query(query: &str) -> Vec<String> {
    let mut tokens = Vec::new();

    for word in query.split(|c: char| c.is_whitespace() || c == ':' || c == '"' || c == '\'') {
        if word.is_empty() {
            continue;
        }
        let sub_tokens = tokenize_line(word);
        tokens.extend(sub_tokens);
    }

    tokens
}

/// Character classification for tokenization.
#[derive(Clone, Copy, PartialEq)]
enum CharType {
    Lower,
    Upper,
    Digit,
    Separator,
    Other,
}

fn classify_char(ch: char) -> CharType {
    if ch.is_ascii_lowercase() {
        CharType::Lower
    } else if ch.is_ascii_uppercase() {
        CharType::Upper
    } else if ch.is_ascii_digit() {
        CharType::Digit
    } else if ch == '_' || ch == '-' {
        CharType::Separator
    } else {
        CharType::Other
    }
}

/// Common stop words to skip in indexing.
fn is_stop_word(token: &str) -> bool {
    matches!(
        token,
        "if" | "in" | "of" | "on" | "to" | "or" | "as" | "at"
            | "by" | "do" | "go" | "no" | "so" | "up" | "an"
            | "be" | "is" | "it" | "we" | "he" | "me"
            | "fn" | "let" | "var" | "for" | "and" | "not" | "but"
            | "the" | "was" | "are" | "has" | "had" | "use" | "pub"
            | "mod" | "ref" | "mut" | "out" | "nil"
    )
}

// ── Scoring helpers ───────────────────────────────────────────

/// Compute inverse document frequency.
#[cfg(feature = "indexing")]
fn compute_idf(total_docs: u64, docs_with_term: u64) -> f32 {
    if docs_with_term == 0 {
        return 0.0;
    }
    ((total_docs as f32 + 1.0) / (docs_with_term as f32 + 1.0)).ln() + 1.0
}

/// Simple glob matching for path patterns.
#[cfg(feature = "indexing")]
fn glob_match(pattern: &str, text: &str) -> bool {
    let pattern_lower = pattern.to_lowercase();
    let text_lower = text.to_lowercase();

    if !pattern_lower.contains('*') && !pattern_lower.contains('?') {
        return text_lower.contains(&pattern_lower);
    }

    let parts: Vec<&str> = pattern_lower.split('*').collect();
    if parts.is_empty() {
        return true;
    }

    let mut pos = 0;
    for (i, part) in parts.iter().enumerate() {
        if part.is_empty() {
            continue;
        }
        match text_lower[pos..].find(part) {
            Some(idx) => {
                if i == 0 && idx != 0 && !pattern_lower.starts_with('*') {
                    return false;
                }
                pos += idx + part.len();
            }
            None => return false,
        }
    }

    if !pattern_lower.ends_with('*') && !text_lower.ends_with(parts.last().unwrap()) {
        return pos >= text_lower.len();
    }

    true
}

#[cfg(test)]
mod tests {
    #[cfg(feature = "indexing")]
    use super::*;
    #[cfg(feature = "indexing")]
    use uc_types::search::SearchMode;

    #[cfg(feature = "indexing")]
    #[test]
    fn test_tokenize_camel_case() {
        let tokens = tokenize_line("getUserById");
        assert!(tokens.contains(&"get".to_string()));
        assert!(tokens.contains(&"user".to_string()));
        assert!(tokens.contains(&"id".to_string()));
    }

    #[cfg(feature = "indexing")]
    #[test]
    fn test_tokenize_snake_case() {
        let tokens = tokenize_line("my_variable_name");
        assert!(tokens.contains(&"my".to_string()));
        assert!(tokens.contains(&"variable".to_string()));
        assert!(tokens.contains(&"name".to_string()));
    }

    #[cfg(feature = "indexing")]
    #[test]
    fn test_tokenize_pascal_case() {
        let tokens = tokenize_line("MyClass");
        assert!(tokens.contains(&"my".to_string()));
        assert!(tokens.contains(&"class".to_string()));
    }

    #[cfg(feature = "indexing")]
    #[test]
    fn test_tokenize_screaming_snake() {
        let tokens = tokenize_line("MAX_RETRIES");
        assert!(tokens.contains(&"max".to_string()));
        assert!(tokens.contains(&"retries".to_string()));
    }

    #[cfg(feature = "indexing")]
    #[test]
    fn test_tokenize_kebab_case() {
        let tokens = tokenize_line("my-component-name");
        assert!(tokens.contains(&"my".to_string()));
        assert!(tokens.contains(&"component".to_string()));
        assert!(tokens.contains(&"name".to_string()));
    }

    #[cfg(feature = "indexing")]
    #[test]
    fn test_tokenize_mixed() {
        let tokens = tokenize_line("fn getUserById(user_id: i32) -> Result<User>");
        assert!(tokens.contains(&"user".to_string()));
        assert!(tokens.contains(&"result".to_string()));
    }

    #[cfg(feature = "indexing")]
    #[test]
    fn test_tokenize_query() {
        let tokens = tokenize_query("getUserById");
        assert!(tokens.contains(&"get".to_string()));
        assert!(tokens.contains(&"user".to_string()));
    }

    #[cfg(feature = "indexing")]
    #[test]
    fn test_stop_words() {
        assert!(is_stop_word("fn"));
        assert!(is_stop_word("let"));
        assert!(!is_stop_word("function"));
        assert!(!is_stop_word("config"));
        assert!(!is_stop_word("get"));
        assert!(!is_stop_word("new"));
    }

    #[cfg(feature = "indexing")]
    #[test]
    fn test_index_and_search() {
        let mut index = TextSearchIndex::new();

        index
            .index_file(
                "repo1",
                "src/main.rs",
                "rust",
                r#"fn main() {
    let config = Config::new();
    println!("Hello, world!");
}"#,
            )
            .unwrap();

        index
            .index_file(
                "repo1",
                "src/lib.rs",
                "rust",
                r#"pub struct Config {
    name: String,
    value: i32,
}

impl Config {
    pub fn new() -> Self {
        Config { name: "default".into(), value: 0 }
    }
}"#,
            )
            .unwrap();

        let query = SearchQuery {
            query: "config".to_string(),
            modes: vec![SearchMode::Text],
            repo_ids: vec![],
            languages: vec![],
            path_patterns: vec![],
            max_results: 10,
        };

        let result = index.search(&query).unwrap();
        assert!(!result.items.is_empty());
        assert!(result.items.len() >= 2);
    }

    #[cfg(feature = "indexing")]
    #[test]
    fn test_search_with_repo_filter() {
        let mut index = TextSearchIndex::new();

        index.index_file("repo1", "main.rs", "rust", "fn hello() {}").unwrap();
        index.index_file("repo2", "main.rs", "rust", "fn hello() {}").unwrap();

        let query = SearchQuery {
            query: "hello".to_string(),
            modes: vec![SearchMode::Text],
            repo_ids: vec!["repo1".to_string()],
            languages: vec![],
            path_patterns: vec![],
            max_results: 10,
        };

        let result = index.search(&query).unwrap();
        assert_eq!(result.items.len(), 1);
        assert_eq!(result.items[0].repo_id, "repo1");
    }

    #[cfg(feature = "indexing")]
    #[test]
    fn test_search_with_language_filter() {
        let mut index = TextSearchIndex::new();

        index.index_file("repo1", "main.rs", "rust", "fn process_data() {}").unwrap();
        index.index_file("repo1", "main.py", "python", "def process_data(): pass").unwrap();

        let query = SearchQuery {
            query: "process data".to_string(),
            modes: vec![SearchMode::Text],
            repo_ids: vec![],
            languages: vec!["python".to_string()],
            path_patterns: vec![],
            max_results: 10,
        };

        let result = index.search(&query).unwrap();
        assert_eq!(result.items.len(), 1);
        assert_eq!(result.items[0].file_path, "main.py");
    }

    #[cfg(feature = "indexing")]
    #[test]
    fn test_search_with_path_filter() {
        let mut index = TextSearchIndex::new();

        index.index_file("repo1", "src/main.rs", "rust", "fn database_connect() {}").unwrap();
        index.index_file("repo1", "tests/main.rs", "rust", "fn database_connect() {}").unwrap();

        let query = SearchQuery {
            query: "database connect".to_string(),
            modes: vec![SearchMode::Text],
            repo_ids: vec![],
            languages: vec![],
            path_patterns: vec!["src/*".to_string()],
            max_results: 10,
        };

        let result = index.search(&query).unwrap();
        assert_eq!(result.items.len(), 1);
        assert!(result.items[0].file_path.starts_with("src/"));
    }

    #[cfg(feature = "indexing")]
    #[test]
    fn test_remove_file() {
        let mut index = TextSearchIndex::new();

        index.index_file("repo1", "main.rs", "rust", "fn unique_function() {}").unwrap();
        assert_eq!(index.doc_count(), 1);

        index.remove_file("repo1", "main.rs");
        assert_eq!(index.doc_count(), 0);

        let query = SearchQuery {
            query: "unique function".to_string(),
            modes: vec![SearchMode::Text],
            repo_ids: vec![],
            languages: vec![],
            path_patterns: vec![],
            max_results: 10,
        };
        let result = index.search(&query).unwrap();
        assert!(result.items.is_empty());
    }

    #[cfg(feature = "indexing")]
    #[test]
    fn test_remove_repo() {
        let mut index = TextSearchIndex::new();

        index.index_file("repo1", "a.rs", "rust", "fn alpha() {}").unwrap();
        index.index_file("repo1", "b.rs", "rust", "fn beta() {}").unwrap();
        index.index_file("repo2", "c.rs", "rust", "fn gamma() {}").unwrap();

        index.remove_repo("repo1");
        assert_eq!(index.doc_count(), 1);
    }

    #[cfg(feature = "indexing")]
    #[test]
    fn test_regex_search() {
        let mut index = TextSearchIndex::new();

        index.index_file("repo1", "main.rs", "rust", "fn get_user() {} fn get_item() {} fn set_value() {}").unwrap();

        // Regex search matches against individual tokens in the inverted index.
        // Tokens are: "get", "user", "item", "set", "value", etc.
        // So "get.*" won't match any single token; use a pattern that matches tokens.
        let query = SearchQuery {
            query: "get|set".to_string(),
            modes: vec![SearchMode::Text],
            repo_ids: vec![],
            languages: vec![],
            path_patterns: vec![],
            max_results: 10,
        };

        let result = index.search(&query).unwrap();
        assert!(!result.items.is_empty());
    }

    #[cfg(feature = "indexing")]
    #[test]
    fn test_empty_query() {
        let index = TextSearchIndex::new();
        let query = SearchQuery {
            query: "".to_string(),
            modes: vec![],
            repo_ids: vec![],
            languages: vec![],
            path_patterns: vec![],
            max_results: 10,
        };
        let result = index.search(&query).unwrap();
        assert!(result.items.is_empty());
    }

    #[cfg(feature = "indexing")]
    #[test]
    fn test_glob_match() {
        assert!(glob_match("src/*", "src/main.rs"));
        assert!(glob_match("*.rs", "main.rs"));
        assert!(glob_match("test*", "test_foo"));
        assert!(!glob_match("src/*", "lib/main.rs"));
        assert!(glob_match("main", "src/main.rs"));
    }

    #[cfg(feature = "indexing")]
    #[test]
    fn test_compute_idf() {
        let idf_rare = compute_idf(100, 1);
        let idf_common = compute_idf(100, 50);
        assert!(idf_rare > idf_common);
    }

    #[cfg(feature = "indexing")]
    #[test]
    fn test_tokenize_content_line_numbers() {
        let content = "line one\nline two\nline three";
        let tokens = tokenize_content(content, "text");
        assert!(tokens.iter().any(|(line, _)| *line == 1));
        assert!(tokens.iter().any(|(line, _)| *line == 2));
        assert!(tokens.iter().any(|(line, _)| *line == 3));
    }
}
