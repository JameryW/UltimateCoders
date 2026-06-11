//! Git module — repository management (clone, fetch, diff).
//!
//! When the `indexing` feature is disabled, provides fallback stubs
//! for git operations. The `detect_language` function is always available
//! since it only inspects file extensions.

#[cfg(feature = "indexing")]
pub mod repo_manager;

#[cfg(not(feature = "indexing"))]
pub mod repo_manager {
    //! Fallback stubs for repo_manager when indexing feature is disabled.

    use uc_types::error::EngineError;
    use uc_types::index::RepoSpec;

    /// Placeholder FileDiff — no data when indexing is disabled.
    #[derive(Debug, Clone)]
    pub struct FileDiff {
        pub path: String,
        pub kind: DiffKind,
    }

    /// Placeholder DiffKind.
    #[derive(Debug, Clone, PartialEq)]
    pub enum DiffKind {
        Added,
        Modified,
        Deleted,
        Renamed,
    }

    /// Placeholder FileEntry.
    #[derive(Debug, Clone)]
    pub struct FileEntry {
        pub path: String,
        pub size: u64,
    }

    /// Placeholder RepoManager — returns errors when indexing is disabled.
    pub struct RepoManager {
        _private: (),
    }

    impl RepoManager {
        pub fn new(_clone_base_dir: impl Into<std::path::PathBuf>) -> Self {
            Self { _private: () }
        }

        pub fn new_default() -> Self {
            Self { _private: () }
        }

        pub fn repo_path(&self, _repo_id: &str) -> std::path::PathBuf {
            std::path::PathBuf::new()
        }

        pub fn clone_or_open(&self, _spec: &RepoSpec) -> Result<(), EngineError> {
            Err(EngineError::IndexingError(
                "Indexing feature is disabled".into(),
            ))
        }

        pub fn head_sha(&self, _repo: &()) -> Result<String, EngineError> {
            Err(EngineError::IndexingError(
                "Indexing feature is disabled".into(),
            ))
        }

        pub fn diff_between(
            &self,
            _repo: &(),
            _old_sha: &str,
            _new_sha: &str,
        ) -> Result<Vec<FileDiff>, EngineError> {
            Err(EngineError::IndexingError(
                "Indexing feature is disabled".into(),
            ))
        }

        pub fn walk_files(&self, _repo: &()) -> Result<Vec<FileEntry>, EngineError> {
            Err(EngineError::IndexingError(
                "Indexing feature is disabled".into(),
            ))
        }

        pub fn read_file(&self, _repo: &(), _file_path: &str) -> Result<String, EngineError> {
            Err(EngineError::IndexingError(
                "Indexing feature is disabled".into(),
            ))
        }

        pub fn read_file_at_commit(
            &self,
            _repo: &(),
            _file_path: &str,
            _sha: &str,
        ) -> Result<String, EngineError> {
            Err(EngineError::IndexingError(
                "Indexing feature is disabled".into(),
            ))
        }
    }
}

/// Detect the programming language from a file extension.
///
/// This function is always available regardless of feature flags,
/// since it only inspects the file extension string.
pub fn detect_language(file_path: &str) -> Option<&'static str> {
    let ext = file_path.rsplit('.').next()?;
    match ext {
        "rs" => Some("rust"),
        "py" => Some("python"),
        "js" => Some("javascript"),
        "ts" => Some("typescript"),
        "tsx" => Some("tsx"),
        "jsx" => Some("jsx"),
        "go" => Some("go"),
        "java" => Some("java"),
        "c" | "h" => Some("c"),
        "cpp" | "cc" | "cxx" | "hpp" => Some("cpp"),
        "rb" => Some("ruby"),
        "swift" => Some("swift"),
        "kt" => Some("kotlin"),
        "scala" => Some("scala"),
        "sh" | "bash" => Some("bash"),
        "sql" => Some("sql"),
        "html" => Some("html"),
        "css" => Some("css"),
        "json" => Some("json"),
        "yaml" | "yml" => Some("yaml"),
        "toml" => Some("toml"),
        "md" => Some("markdown"),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_detect_language() {
        assert_eq!(detect_language("src/main.rs"), Some("rust"));
        assert_eq!(detect_language("app.py"), Some("python"));
        assert_eq!(detect_language("index.js"), Some("javascript"));
        assert_eq!(detect_language("main.go"), Some("go"));
        assert_eq!(detect_language("Makefile"), None);
        assert_eq!(detect_language("README.md"), Some("markdown"));
        assert_eq!(detect_language("config.toml"), Some("toml"));
        assert_eq!(detect_language("app.tsx"), Some("tsx"));
    }
}
