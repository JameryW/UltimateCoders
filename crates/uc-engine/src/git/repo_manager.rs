//! Repository manager — clone, fetch, diff computation.
//!
//! Uses `git2` (libgit2 bindings) for git operations. When the `indexing`
//! feature is disabled, all methods return `EngineError::IndexingError`.

use uc_types::error::EngineError;
use uc_types::index::RepoSpec;

use std::path::{Path, PathBuf};

/// Information about a file change between two commits.
#[derive(Debug, Clone)]
pub struct FileDiff {
    /// File path relative to repo root.
    pub path: String,
    /// Kind of change.
    pub kind: DiffKind,
}

/// Kind of file change.
#[derive(Debug, Clone, PartialEq)]
pub enum DiffKind {
    Added,
    Modified,
    Deleted,
    Renamed,
}

/// Result of walking the working tree.
#[derive(Debug, Clone)]
pub struct FileEntry {
    /// File path relative to repo root.
    pub path: String,
    /// File size in bytes.
    pub size: u64,
}

/// Repository manager — handles clone, fetch, diff, and tree walk operations.
pub struct RepoManager {
    /// Base directory where repositories are cloned.
    clone_base_dir: PathBuf,
}

impl RepoManager {
    /// Create a new repo manager with the given base directory for clones.
    pub fn new(clone_base_dir: impl Into<PathBuf>) -> Self {
        Self {
            clone_base_dir: clone_base_dir.into(),
        }
    }

    /// Create with a default clone directory (system temp dir + "uc-repos").
    pub fn new_default() -> Self {
        let base = std::env::temp_dir().join("uc-repos");
        Self::new(base)
    }

    /// Get the local path for a repository.
    pub fn repo_path(&self, repo_id: &str) -> PathBuf {
        self.clone_base_dir.join(repo_id)
    }

    /// Clone a repository (shallow clone with `--depth=1`).
    ///
    /// If the repository is already cloned, opens it instead.
    /// If a `local_path` is provided in the spec, uses that path directly.
    pub fn clone_or_open(&self, spec: &RepoSpec) -> Result<git2::Repository, EngineError> {
        let local_path = spec
            .local_path
            .as_ref()
            .map(PathBuf::from)
            .unwrap_or_else(|| self.repo_path(&spec.repo_id));

        // If already cloned, open it
        if local_path.exists() && local_path.join(".git").exists() {
            tracing::info!("Opening existing repository at {:?}", local_path);
            return git2::Repository::open(&local_path)
                .map_err(|e| EngineError::IndexingError(format!("Failed to open repo: {}", e)));
        }

        // Ensure parent directory exists
        if let Some(parent) = local_path.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|e| EngineError::IndexingError(format!("Failed to create directory: {}", e)))?;
        }

        // Shallow clone
        tracing::info!("Cloning {} (shallow) to {:?}", spec.remote_url, local_path);
        let mut builder = git2::build::RepoBuilder::new();
        let mut fetch_options = git2::FetchOptions::new();
        fetch_options.depth(1);
        builder.fetch_options(fetch_options);

        builder.clone(&spec.remote_url, &local_path)
            .map_err(|e| EngineError::IndexingError(format!("Failed to clone repo: {}", e)))
    }

    /// Fetch latest changes for a repository.
    pub fn fetch(&self, repo: &mut git2::Repository) -> Result<(), EngineError> {
        tracing::info!("Fetching latest changes");
        repo.find_remote("origin")
            .and_then(|mut remote| {
                let mut fetch_options = git2::FetchOptions::new();
                fetch_options.depth(1);
                remote.fetch(&["refs/heads/*:refs/heads/*"], Some(&mut fetch_options), None)
            })
            .map_err(|e| EngineError::IndexingError(format!("Failed to fetch: {}", e)))?;

        Ok(())
    }

    /// Get the current HEAD SHA of the repository.
    pub fn head_sha(&self, repo: &git2::Repository) -> Result<String, EngineError> {
        let head = repo
            .head()
            .map_err(|e| EngineError::IndexingError(format!("Failed to get HEAD: {}", e)))?;

        let commit = head
            .peel_to_commit()
            .map_err(|e| EngineError::IndexingError(format!("Failed to peel HEAD to commit: {}", e)))?;

        Ok(commit.id().to_string())
    }

    /// Compute the diff between two commits.
    ///
    /// Returns a list of file changes. If `old_sha` is empty or the commit
    /// cannot be found, returns an empty diff (indicating full reindex needed).
    pub fn diff_between(
        &self,
        repo: &git2::Repository,
        old_sha: &str,
        new_sha: &str,
    ) -> Result<Vec<FileDiff>, EngineError> {
        if old_sha.is_empty() {
            return Ok(vec![]);
        }

        let old_oid = git2::Oid::from_str(old_sha)
            .map_err(|e| EngineError::IndexingError(format!("Invalid old SHA '{}': {}", old_sha, e)))?;
        let new_oid = git2::Oid::from_str(new_sha)
            .map_err(|e| EngineError::IndexingError(format!("Invalid new SHA '{}': {}", new_sha, e)))?;

        // Check if old is ancestor of new (for incremental)
        let old_commit = repo.find_commit(old_oid)
            .map_err(|e| EngineError::IndexingError(format!("Old commit not found: {}", e)))?;
        let new_commit = repo.find_commit(new_oid)
            .map_err(|e| EngineError::IndexingError(format!("New commit not found: {}", e)))?;

        // If old is not an ancestor of new, we need a full reindex
        if repo.merge_base(old_oid, new_oid).map(|base| base != old_oid).unwrap_or(true) {
            tracing::warn!(
                "Old SHA {} is not an ancestor of new SHA {}; full reindex recommended",
                old_sha, new_sha
            );
            return Ok(vec![]);
        }

        let old_tree = old_commit.tree()
            .map_err(|e| EngineError::IndexingError(format!("Failed to get old tree: {}", e)))?;
        let new_tree = new_commit.tree()
            .map_err(|e| EngineError::IndexingError(format!("Failed to get new tree: {}", e)))?;

        let diff = repo.diff_tree_to_tree(Some(&old_tree), Some(&new_tree), None)
            .map_err(|e| EngineError::IndexingError(format!("Failed to compute diff: {}", e)))?;

        let mut result = Vec::new();
        for delta in diff.deltas() {
            let kind = match delta.status() {
                git2::Delta::Added => DiffKind::Added,
                git2::Delta::Deleted => DiffKind::Deleted,
                git2::Delta::Modified => DiffKind::Modified,
                git2::Delta::Renamed => DiffKind::Renamed,
                _ => continue, // Skip copied, typechange, etc.
            };

            let path = delta
                .new_file()
                .path()
                .or_else(|| delta.old_file().path())
                .map(|p| p.to_string_lossy().to_string())
                .unwrap_or_default();

            result.push(FileDiff { path, kind });
        }

        Ok(result)
    }

    /// Walk all files in the working tree of the repository.
    ///
    /// Returns file paths and sizes. Skips `.git` directory.
    pub fn walk_files(&self, repo: &git2::Repository) -> Result<Vec<FileEntry>, EngineError> {
        let workdir = repo
            .workdir()
            .ok_or_else(|| EngineError::IndexingError("Repository has no working directory".into()))?;

        let mut entries = Vec::new();
        walk_dir_recursive(workdir, workdir, &mut entries)?;

        Ok(entries)
    }

    /// Read the content of a file from the repository.
    pub fn read_file(
        &self,
        repo: &git2::Repository,
        file_path: &str,
    ) -> Result<String, EngineError> {
        let workdir = repo
            .workdir()
            .ok_or_else(|| EngineError::IndexingError("Repository has no working directory".into()))?;

        let full_path = workdir.join(file_path);
        std::fs::read_to_string(&full_path)
            .map_err(|e| EngineError::IndexingError(format!("Failed to read file {}: {}", file_path, e)))
    }

    /// Read the content of a file at a specific commit.
    pub fn read_file_at_commit(
        &self,
        repo: &git2::Repository,
        file_path: &str,
        sha: &str,
    ) -> Result<String, EngineError> {
        let oid = git2::Oid::from_str(sha)
            .map_err(|e| EngineError::IndexingError(format!("Invalid SHA '{}': {}", sha, e)))?;

        let commit = repo.find_commit(oid)
            .map_err(|e| EngineError::IndexingError(format!("Commit not found: {}", e)))?;

        let tree = commit.tree()
            .map_err(|e| EngineError::IndexingError(format!("Failed to get tree: {}", e)))?;

        let entry = tree
            .get_path(Path::new(file_path))
            .map_err(|e| EngineError::IndexingError(format!("File not found in tree: {}", e)))?;

        let blob = repo.find_blob(entry.id())
            .map_err(|e| EngineError::IndexingError(format!("Failed to read blob: {}", e)))?;

        let content = std::str::from_utf8(blob.content())
            .map_err(|e| EngineError::IndexingError(format!("File is not valid UTF-8: {}", e)))?;

        Ok(content.to_string())
    }
}

/// Recursively walk a directory, collecting file entries relative to the base.
fn walk_dir_recursive(
    dir: &Path,
    base: &Path,
    entries: &mut Vec<FileEntry>,
) -> Result<(), EngineError> {
    let read_dir = std::fs::read_dir(dir)
        .map_err(|e| EngineError::IndexingError(format!("Failed to read directory: {}", e)))?;

    for entry in read_dir {
        let entry = entry
            .map_err(|e| EngineError::IndexingError(format!("Failed to read dir entry: {}", e)))?;

        let file_name = entry.file_name();
        let file_name_str = file_name.to_string_lossy();

        // Skip .git and hidden directories
        if file_name_str.starts_with('.') {
            continue;
        }

        let path = entry.path();
        let metadata = entry.metadata()
            .map_err(|e| EngineError::IndexingError(format!("Failed to read metadata: {}", e)))?;

        if metadata.is_dir() {
            walk_dir_recursive(&path, base, entries)?;
        } else if metadata.is_file() {
            let relative = path
                .strip_prefix(base)
                .unwrap_or(&path)
                .to_string_lossy()
                .to_string();

            entries.push(FileEntry {
                path: relative,
                size: metadata.len(),
            });
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_repo_manager_new_default() {
        let manager = RepoManager::new_default();
        assert!(manager.clone_base_dir.to_string_lossy().contains("uc-repos"));
    }

    #[test]
    fn test_repo_path() {
        let manager = RepoManager::new("/tmp/test-repos");
        let path = manager.repo_path("my-repo");
        assert_eq!(path, PathBuf::from("/tmp/test-repos/my-repo"));
    }

    #[test]
    fn test_diff_kind_equality() {
        assert_eq!(DiffKind::Added, DiffKind::Added);
        assert_ne!(DiffKind::Added, DiffKind::Modified);
    }
}
