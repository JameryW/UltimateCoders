//! File change tracking using git diff.
//!
//! Tracks file modifications made by a sandboxed agent execution
//! by creating a baseline commit before execution and computing
//! the diff afterward.

use std::process::Stdio;
use tokio::io::AsyncReadExt;
use tokio::process::Command;
use uc_types::{ChangeType, EngineError, FileChange};

/// File change tracker using git operations.
///
/// Workflow:
/// 1. Before execution: `git add -A && git commit -m "baseline"` in the working directory
/// 2. After execution: `git diff HEAD~1` -> parse unified diff -> `Vec<FileChange>`
/// 3. Cleanup: `git reset --soft HEAD~1` (keep changes but remove the baseline commit)
pub struct FileTracker;

impl FileTracker {
    /// Create a baseline commit before agent execution.
    ///
    /// Stages all files and creates a commit that serves as the
    /// reference point for computing changes after execution.
    ///
    /// # Arguments
    /// * `working_dir` - The git repository working directory.
    ///
    /// # Errors
    /// Returns `EngineError::SandboxError` if git operations fail.
    pub async fn create_baseline(working_dir: &str) -> Result<(), EngineError> {
        // git add -A
        let add_output = Command::new("git")
            .args(["add", "-A"])
            .current_dir(working_dir)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .status()
            .await
            .map_err(|e| EngineError::SandboxError(format!("git add failed: {}", e)))?;

        if !add_output.success() {
            return Err(EngineError::SandboxError(
                "git add -A failed (not a git repo?)".to_string(),
            ));
        }

        // git commit --allow-empty (in case nothing to commit)
        let commit_output = Command::new("git")
            .args(["commit", "--allow-empty", "-m", "sandbox-baseline"])
            .current_dir(working_dir)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .status()
            .await
            .map_err(|e| EngineError::SandboxError(format!("git commit failed: {}", e)))?;

        if !commit_output.success() {
            return Err(EngineError::SandboxError(
                "git commit baseline failed".to_string(),
            ));
        }

        tracing::info!(
            working_dir = %working_dir,
            "Created baseline commit for file tracking"
        );

        Ok(())
    }

    /// Get file changes since the baseline commit.
    ///
    /// Runs `git diff HEAD~1 --unified=0` and parses the output
    /// into a list of `FileChange` objects.
    ///
    /// # Arguments
    /// * `working_dir` - The git repository working directory.
    pub async fn get_changes(working_dir: &str) -> Result<Vec<FileChange>, EngineError> {
        // git diff HEAD~1 --unified=0
        let mut child = Command::new("git")
            .args(["diff", "HEAD~1", "--unified=0"])
            .current_dir(working_dir)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| EngineError::SandboxError(format!("git diff spawn failed: {}", e)))?;

        let mut stdout = Vec::new();
        if let Some(mut pipe) = child.stdout.take() {
            pipe.read_to_end(&mut stdout)
                .await
                .map_err(|e| EngineError::SandboxError(format!("git diff read failed: {}", e)))?;
        }

        let status = child
            .wait()
            .await
            .map_err(|e| EngineError::SandboxError(format!("git diff wait failed: {}", e)))?;

        if !status.success() {
            return Err(EngineError::SandboxError(
                "git diff HEAD~1 failed".to_string(),
            ));
        }

        let diff_text = String::from_utf8_lossy(&stdout);
        let changes = parse_unified_diff(&diff_text);

        tracing::info!(
            working_dir = %working_dir,
            changes_count = changes.len(),
            "Extracted file changes from git diff"
        );

        Ok(changes)
    }

    /// Reset to the baseline commit, keeping the working directory changes.
    ///
    /// This removes the baseline commit marker but preserves any
    /// file modifications made by the agent.
    ///
    /// # Arguments
    /// * `working_dir` - The git repository working directory.
    pub async fn reset_baseline(working_dir: &str) -> Result<(), EngineError> {
        let output = Command::new("git")
            .args(["reset", "--soft", "HEAD~1"])
            .current_dir(working_dir)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .status()
            .await
            .map_err(|e| EngineError::SandboxError(format!("git reset failed: {}", e)))?;

        if !output.success() {
            return Err(EngineError::SandboxError(
                "git reset --soft HEAD~1 failed".to_string(),
            ));
        }

        tracing::info!(
            working_dir = %working_dir,
            "Reset baseline commit"
        );

        Ok(())
    }

    /// Full workflow: create baseline, then get changes, then reset.
    ///
    /// This is a convenience method that runs the baseline creation
    /// (for pre-execution), or the get-changes + reset (for post-execution).
    pub async fn get_changes_and_reset(working_dir: &str) -> Result<Vec<FileChange>, EngineError> {
        let changes = Self::get_changes(working_dir).await?;
        let _ = Self::reset_baseline(working_dir).await; // Best-effort reset
        Ok(changes)
    }
}

/// Parse a unified diff output into file changes.
///
/// Handles the standard git diff format:
/// ```text
/// diff --git a/path b/path
/// --- a/path
/// +++ b/path
/// @@ ... @@
/// +added line
/// -removed line
/// ```
fn parse_unified_diff(diff: &str) -> Vec<FileChange> {
    let mut changes = Vec::new();
    let mut current_path: Option<String> = None;
    let mut current_diff_lines: Vec<String> = Vec::new();
    let mut is_new_file = false;
    let mut is_deleted_file = false;

    for line in diff.lines() {
        // New diff header
        if line.starts_with("diff --git ") {
            // Flush the previous file
            if let Some(path) = current_path.take() {
                let change_type = if is_new_file {
                    ChangeType::Created
                } else if is_deleted_file {
                    ChangeType::Deleted
                } else {
                    ChangeType::Modified
                };
                changes.push(FileChange {
                    file_path: path,
                    change_type,
                    diff: current_diff_lines.join("\n"),
                });
                current_diff_lines.clear();
            }

            is_new_file = false;
            is_deleted_file = false;

            // Extract path from "diff --git a/path b/path"
            if let Some(path) = extract_path_from_diff_header(line) {
                current_path = Some(path);
            }
        }

        // New file marker
        if line.starts_with("new file") {
            is_new_file = true;
        }

        // Deleted file marker
        if line.starts_with("deleted file") {
            is_deleted_file = true;
        }

        // Accumulate diff lines
        if current_path.is_some() {
            current_diff_lines.push(line.to_string());
        }
    }

    // Flush the last file
    if let Some(path) = current_path {
        let change_type = if is_new_file {
            ChangeType::Created
        } else if is_deleted_file {
            ChangeType::Deleted
        } else {
            ChangeType::Modified
        };
        changes.push(FileChange {
            file_path: path,
            change_type,
            diff: current_diff_lines.join("\n"),
        });
    }

    changes
}

/// Extract the file path from a diff header line.
///
/// Input: "diff --git a/src/main.rs b/src/main.rs"
/// Output: "src/main.rs"
fn extract_path_from_diff_header(line: &str) -> Option<String> {
    let parts: Vec<&str> = line.splitn(4, ' ').collect();
    if parts.len() >= 4 {
        // Use the "b/path" side (destination)
        let b_path = parts[3];
        if let Some(path) = b_path.strip_prefix("b/") {
            return Some(path.to_string());
        }
        // Fallback: use as-is
        return Some(b_path.to_string());
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_diff_single_modified_file() {
        let diff = r#"diff --git a/src/main.rs b/src/main.rs
--- a/src/main.rs
+++ b/src/main.rs
@@ -10,0 +11 @@
+fn new_function() {}
"#;
        let changes = parse_unified_diff(diff);
        assert_eq!(changes.len(), 1);
        assert_eq!(changes[0].file_path, "src/main.rs");
        assert_eq!(changes[0].change_type, ChangeType::Modified);
        assert!(changes[0].diff.contains("fn new_function"));
    }

    #[test]
    fn parse_diff_new_file() {
        let diff = r#"diff --git a/src/new.rs b/src/new.rs
new file mode 100644
--- /dev/null
+++ b/src/new.rs
@@ -0,0 +1,3 @@
+pub fn new() -> Self {
+    Self::default()
+}
"#;
        let changes = parse_unified_diff(diff);
        assert_eq!(changes.len(), 1);
        assert_eq!(changes[0].file_path, "src/new.rs");
        assert_eq!(changes[0].change_type, ChangeType::Created);
    }

    #[test]
    fn parse_diff_deleted_file() {
        let diff = r#"diff --git a/src/old.rs b/src/old.rs
deleted file mode 100644
--- a/src/old.rs
+++ /dev/null
@@ -1,3 +0,0 @@
-pub fn old() {}
"#;
        let changes = parse_unified_diff(diff);
        assert_eq!(changes.len(), 1);
        assert_eq!(changes[0].file_path, "src/old.rs");
        assert_eq!(changes[0].change_type, ChangeType::Deleted);
    }

    #[test]
    fn parse_diff_multiple_files() {
        let diff = r#"diff --git a/src/main.rs b/src/main.rs
--- a/src/main.rs
+++ b/src/main.rs
@@ -1 +1 @@
-fn old() {}
+fn new() {}
diff --git a/src/lib.rs b/src/lib.rs
--- a/src/lib.rs
+++ b/src/lib.rs
@@ -5,0 +6 @@
+pub mod extra;
"#;
        let changes = parse_unified_diff(diff);
        assert_eq!(changes.len(), 2);
        assert_eq!(changes[0].file_path, "src/main.rs");
        assert_eq!(changes[0].change_type, ChangeType::Modified);
        assert_eq!(changes[1].file_path, "src/lib.rs");
        assert_eq!(changes[1].change_type, ChangeType::Modified);
    }

    #[test]
    fn parse_diff_empty() {
        let changes = parse_unified_diff("");
        assert!(changes.is_empty());
    }

    #[test]
    fn extract_path_from_header() {
        let path = extract_path_from_diff_header("diff --git a/src/main.rs b/src/main.rs");
        assert_eq!(path, Some("src/main.rs".to_string()));
    }

    #[test]
    fn extract_path_from_header_no_prefix() {
        let path = extract_path_from_diff_header("diff --git foo bar");
        assert_eq!(path, Some("bar".to_string()));
    }

    // Integration tests that require a git repo are skipped in CI
    // since they need a real git repository.

    #[tokio::test]
    async fn file_tracker_create_baseline_not_git_repo() {
        let result = FileTracker::create_baseline("/tmp/nonexistent_dir_12345").await;
        // Should fail because the directory doesn't exist or isn't a git repo
        assert!(result.is_err());
    }
}
