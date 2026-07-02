//! Repos config — YAML-based multi-repo configuration + auto-discovery (Rust port).
//!
//! Mirrors `python/ultimate_coders/repo_config.py`: parses `uc.repos.yaml`,
//! scans `scan_dirs` for git repos, and yields `RepoSpec`s ready for
//! `LocalEngine::index_repo`.
//!
//! Config file priority:
//!   1. `UC_REPOS_CONFIG` environment variable
//!   2. `./uc.repos.yaml` (CWD)
//!   3. `./uc.repos.yml` (CWD)
//!   4. Skip (return `None`, no error)
//!
//! # ponytail
//! - Remote-only entries (remote_url, no local_path) are skipped with a log
//!   line — Rust gateway MVP is local-only. Remote clone is handled by the
//!   Python worker mode.
//! - scan_dirs discovery derives repo_id from the directory name only (no
//!   `git remote get-url` subprocess) to keep startup fast and dependency-free.
//! - No hot-reload (MVP: load once at startup).

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use tracing::{debug, info, warn};

use uc_types::{IndexRequest, RepoSpec};

/// A single repository declaration from `uc.repos.yaml`.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct RepoEntry {
    #[serde(default)]
    pub repo_id: String,
    #[serde(default)]
    pub local_path: String,
    #[serde(default)]
    pub remote_url: String,
    #[serde(default = "default_branch")]
    pub default_branch: String,
    #[serde(default)]
    pub tags: Vec<String>,
}

fn default_branch() -> String {
    "main".to_string()
}

/// Full `uc.repos.yaml` configuration.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RepoConfig {
    #[serde(default)]
    pub repos: Vec<RepoEntry>,
    #[serde(default)]
    pub scan_dirs: Vec<String>,
    #[serde(default = "default_scan_depth")]
    pub scan_depth: usize,
    #[serde(default = "default_workspace_id")]
    pub workspace_id: String,
}

fn default_scan_depth() -> usize {
    3
}

fn default_workspace_id() -> String {
    "default".to_string()
}

impl Default for RepoConfig {
    fn default() -> Self {
        Self {
            repos: Vec::new(),
            scan_dirs: Vec::new(),
            scan_depth: default_scan_depth(),
            workspace_id: default_workspace_id(),
        }
    }
}

/// Resolve the config file path following priority rules.
///
/// Order: explicit `path` arg > `UC_REPOS_CONFIG` env > `./uc.repos.yaml` >
/// `./uc.repos.yml` > None. If `path` is `None` and no env/cwd file is found,
/// returns `None` (no error).
pub fn resolve_config_path(path: Option<&Path>) -> Option<PathBuf> {
    if let Some(p) = path {
        if p.exists() {
            return Some(p.to_path_buf());
        }
        warn!("repos config path {:?} does not exist", p);
        return None;
    }

    if let Ok(env_path) = std::env::var("UC_REPOS_CONFIG") {
        let p = PathBuf::from(&env_path);
        if p.exists() {
            return Some(p);
        }
        warn!("UC_REPOS_CONFIG path {:?} does not exist", env_path);
        return None;
    }

    for name in ["uc.repos.yaml", "uc.repos.yml"] {
        let p = PathBuf::from(name);
        if p.exists() {
            return Some(p);
        }
    }

    None
}

/// Load and parse `uc.repos.yaml`. Returns `None` if no config file is found
/// (no error — startup proceeds with an empty engine). Parse failures log a
/// warning and return `None` (tolerant, mirrors Python behavior).
pub fn load_repos_config(path: Option<&Path>) -> Option<RepoConfig> {
    let resolved = resolve_config_path(path)?;
    debug!(path = ?resolved, "loading repos config");
    let content = match std::fs::read_to_string(&resolved) {
        Ok(s) => s,
        Err(e) => {
            warn!("Failed to read repos config {:?}: {}", resolved, e);
            return None;
        }
    };
    parse_repos_yaml(&content)
}

/// Parse yaml text into `RepoConfig`. Empty/whitespace → default config.
pub fn parse_repos_yaml(content: &str) -> Option<RepoConfig> {
    // Empty file → empty config (not an error).
    let trimmed = content.trim();
    if trimmed.is_empty() {
        return Some(RepoConfig::default());
    }
    let cfg: RepoConfig = match serde_yaml::from_str(trimmed) {
        Ok(c) => c,
        Err(e) => {
            warn!("Failed to parse repos yaml: {}", e);
            return None;
        }
    };
    // Validate: repo_id required; at least one of local_path/remote_url required.
    // Remote-only entries (no local_path) are skipped here (MVP: local-only).
    let repos: Vec<RepoEntry> = cfg
        .repos
        .into_iter()
        .filter(|r| {
            if r.repo_id.is_empty() || (r.local_path.is_empty() && r.remote_url.is_empty()) {
                return false;
            }
            if r.local_path.is_empty() {
                // ponytail: remote-only entries skipped on Rust gateway side.
                info!(
                    repo_id = %r.repo_id,
                    remote_url = %r.remote_url,
                    "Skipping remote-only repo entry (handled by Python worker mode)"
                );
                return false;
            }
            true
        })
        .collect();
    Some(RepoConfig { repos, ..cfg })
}

/// Directories skipped during scan_dirs walk (mirrors Python `RepoScanner._SKIP_DIRS`).
pub const SKIP_DIRS: &[&str] = &[
    "node_modules",
    "__pycache__",
    ".tox",
    ".venv",
    "venv",
    "env",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".cargo",
    "target",
];

/// Scan `scan_dirs` up to `scan_depth` levels for subdirectories containing a
/// `.git` directory. Returns deduplicated `RepoEntry`s (repo_id = dir name,
/// local_path = absolute path). Skips hidden dirs and `SKIP_DIRS`.
pub fn discover_scan_dirs(
    scan_dirs: &[String],
    scan_depth: usize,
    exclude_repo_ids: &std::collections::HashSet<String>,
) -> Vec<RepoEntry> {
    let mut seen: std::collections::HashSet<String> = exclude_repo_ids.clone();
    let mut found = Vec::new();
    for dir_path in scan_dirs {
        let root = PathBuf::from(dir_path);
        if !root.is_dir() {
            debug!("scan_dirs entry does not exist: {:?}", dir_path);
            continue;
        }
        walk_for_git(&root, scan_depth as i64, &mut seen, &mut found);
    }
    found
}

/// Recursive walk: append discovered git repos to `found`.
///
/// `depth` follows Python `RepoScanner._walk` semantics: a dir at `depth >= 0`
/// is checked for `.git`; recursion into children happens at `depth - 1` (so
/// `depth == 0` checks the current dir but does not descend).
fn walk_for_git(
    dir: &Path,
    depth: i64,
    seen: &mut std::collections::HashSet<String>,
    found: &mut Vec<RepoEntry>,
) {
    if depth < 0 {
        return;
    }
    let name = dir.file_name().and_then(|s| s.to_str()).unwrap_or("");
    // Skip hidden directories and common non-project dirs.
    if name.starts_with('.') || SKIP_DIRS.contains(&name) {
        return;
    }
    let git_dir = dir.join(".git");
    if git_dir.is_dir() {
        let repo_id = name.to_string();
        if !seen.contains(&repo_id) {
            seen.insert(repo_id.clone());
            found.push(RepoEntry {
                repo_id,
                local_path: dir.to_string_lossy().into_owned(),
                remote_url: String::new(),
                default_branch: default_branch(),
                tags: Vec::new(),
            });
        }
        // Don't recurse into git repos.
        return;
    }
    let children = match std::fs::read_dir(dir) {
        Ok(rd) => rd,
        Err(e) => {
            debug!("Permission/IO error scanning {:?}: {}", dir, e);
            return;
        }
    };
    for child in children.flatten() {
        let p = child.path();
        if p.is_dir() {
            walk_for_git(&p, depth - 1, seen, found);
        }
    }
}

/// Build `IndexRequest`s for all repos that should be indexed at startup:
/// explicit `repos` entries with `local_path` (deduplicated by repo_id) +
/// scan_dirs discoveries (excluding already-declared repo_ids).
///
/// Each `IndexRequest` carries the config's `workspace_id` and `force_full=false`.
pub fn build_index_requests(cfg: &RepoConfig) -> Vec<IndexRequest> {
    let mut requests = Vec::new();
    let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();

    // Explicit repos first.
    for r in &cfg.repos {
        if seen.contains(&r.repo_id) {
            continue;
        }
        seen.insert(r.repo_id.clone());
        requests.push(IndexRequest {
            repo: RepoSpec {
                repo_id: r.repo_id.clone(),
                remote_url: r.remote_url.clone(),
                default_branch: r.default_branch.clone(),
                local_path: Some(r.local_path.clone()),
                workspace_id: cfg.workspace_id.clone(),
            },
            force_full: false,
        });
    }

    // scan_dirs discoveries (exclude declared repo_ids).
    let discovered = discover_scan_dirs(&cfg.scan_dirs, cfg.scan_depth, &seen);
    for r in discovered {
        requests.push(IndexRequest {
            repo: RepoSpec {
                repo_id: r.repo_id.clone(),
                remote_url: r.remote_url.clone(),
                default_branch: r.default_branch.clone(),
                local_path: Some(r.local_path.clone()),
                workspace_id: cfg.workspace_id.clone(),
            },
            force_full: false,
        });
    }

    requests
}

#[cfg(test)]
mod tests {
    use super::*;

    fn yaml(s: &str) -> Option<RepoConfig> {
        parse_repos_yaml(s)
    }

    #[test]
    fn parse_empty_yaml_returns_default() {
        let cfg = yaml("").unwrap();
        assert!(cfg.repos.is_empty());
        assert_eq!(cfg.workspace_id, "default");
        assert_eq!(cfg.scan_depth, 3);
    }

    #[test]
    fn parse_workspace_id_and_repos() {
        let cfg = yaml(
            r#"
workspace_id: aiworks
repos:
  - repo_id: foo
    local_path: /tmp/foo
    default_branch: main
  - repo_id: bar
    local_path: /tmp/bar
    tags: [rust]
"#,
        )
        .unwrap();
        assert_eq!(cfg.workspace_id, "aiworks");
        assert_eq!(cfg.repos.len(), 2);
        assert_eq!(cfg.repos[0].repo_id, "foo");
        assert_eq!(cfg.repos[1].tags, vec!["rust".to_string()]);
    }

    #[test]
    fn parse_skips_remote_only_entries() {
        // ponytail: Rust gateway MVP is local-only.
        let cfg = yaml(
            r#"
workspace_id: ws
repos:
  - repo_id: local1
    local_path: /tmp/local1
  - repo_id: remote1
    remote_url: https://github.com/x/remote1
  - repo_id: empty
"#,
        )
        .unwrap();
        // Only local1 survives; remote1 (no local_path) and empty (no paths) dropped.
        assert_eq!(cfg.repos.len(), 1);
        assert_eq!(cfg.repos[0].repo_id, "local1");
    }

    #[test]
    fn parse_invalid_yaml_returns_none() {
        assert!(yaml("workspace_id: [unclosed").is_none());
    }

    #[test]
    fn build_index_requests_carries_workspace_id() {
        let cfg = yaml(
            r#"
workspace_id: myws
repos:
  - repo_id: foo
    local_path: /tmp/foo
scan_dirs: []
"#,
        )
        .unwrap();
        let reqs = build_index_requests(&cfg);
        assert_eq!(reqs.len(), 1);
        assert_eq!(reqs[0].repo.repo_id, "foo");
        assert_eq!(reqs[0].repo.workspace_id, "myws");
        assert_eq!(reqs[0].repo.local_path.as_deref(), Some("/tmp/foo"));
        assert!(!reqs[0].force_full);
    }

    #[test]
    fn build_index_requests_dedups_by_repo_id() {
        let cfg = yaml(
            r#"
repos:
  - repo_id: dup
    local_path: /tmp/a
  - repo_id: dup
    local_path: /tmp/b
"#,
        )
        .unwrap();
        let reqs = build_index_requests(&cfg);
        assert_eq!(reqs.len(), 1);
    }

    #[test]
    fn discover_scan_dirs_finds_git_repos() {
        use std::fs;
        let tmp = std::env::temp_dir().join("uc_repos_config_test_git");
        let _ = fs::remove_dir_all(&tmp);
        // Two git repos.
        let r1 = tmp.join("repo-alpha");
        let r2 = tmp.join("nested").join("repo-beta");
        fs::create_dir_all(r1.join(".git")).unwrap();
        fs::create_dir_all(r2.join(".git")).unwrap();
        // A skip-list dir containing .git should be ignored.
        let skip = tmp.join("node_modules").join("hidden");
        fs::create_dir_all(skip.join(".git")).unwrap();
        // A hidden dir containing .git should be ignored.
        let hidden = tmp.join(".hidden-repo");
        fs::create_dir_all(hidden.join(".git")).unwrap();

        let scan_dirs = vec![tmp.to_string_lossy().into_owned()];
        let found = discover_scan_dirs(&scan_dirs, 3, &Default::default());
        let ids: Vec<_> = found.iter().map(|r| r.repo_id.clone()).collect();
        assert!(ids.contains(&"repo-alpha".to_string()));
        assert!(ids.contains(&"repo-beta".to_string()));
        assert!(!ids.contains(&"hidden".to_string()));
        assert_eq!(found.len(), 2);

        let _ = fs::remove_dir_all(&tmp);
    }

    #[test]
    fn discover_scan_dirs_respects_depth() {
        use std::fs;
        let tmp = std::env::temp_dir().join("uc_repos_config_test_depth");
        let _ = fs::remove_dir_all(&tmp);
        // tmp/a/b/c/deep (.git) — depth 3 from tmp reaches a(1)→b(2)→c(3) but c's .git
        // is only seen if we descend INTO c, which needs depth>3. With depth=3 the
        // .git at c is found (walk_for_git(c, depth>=1) checks .git). Build path so
        // depth=2 finds it, depth=1 does not.
        let deep = tmp.join("a").join("b").join("c");
        fs::create_dir_all(deep.join(".git")).unwrap();

        let scan = vec![tmp.to_string_lossy().into_owned()];
        // depth=1 → only immediate children of tmp (a, no .git) → none found.
        let found = discover_scan_dirs(&scan, 1, &Default::default());
        assert!(found.is_empty(), "depth=1 should find nothing: {:?}", found);

        // depth=3 → a(1)→b(2)→c(3): walk_for_git(c, depth>=1) finds .git.
        let found = discover_scan_dirs(&scan, 3, &Default::default());
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].repo_id, "c");

        let _ = fs::remove_dir_all(&tmp);
    }

    #[test]
    fn discover_scan_dirs_excludes_declared_ids() {
        use std::fs;
        let tmp = std::env::temp_dir().join("uc_repos_config_test_excl");
        let _ = fs::remove_dir_all(&tmp);
        let r1 = tmp.join("declared");
        fs::create_dir_all(r1.join(".git")).unwrap();
        let r2 = tmp.join("other");
        fs::create_dir_all(r2.join(".git")).unwrap();

        let mut exclude = std::collections::HashSet::new();
        exclude.insert("declared".to_string());
        let scan = vec![tmp.to_string_lossy().into_owned()];
        let found = discover_scan_dirs(&scan, 2, &exclude);
        let ids: Vec<_> = found.iter().map(|r| r.repo_id.clone()).collect();
        assert!(!ids.contains(&"declared".to_string()));
        assert!(ids.contains(&"other".to_string()));

        let _ = fs::remove_dir_all(&tmp);
    }

    #[test]
    fn build_index_requests_combines_explicit_and_scan() {
        use std::fs;
        let tmp = std::env::temp_dir().join("uc_repos_config_test_combo");
        let _ = fs::remove_dir_all(&tmp);
        let r1 = tmp.join("scanned-repo");
        fs::create_dir_all(r1.join(".git")).unwrap();

        let cfg = yaml(&format!(
            r#"
workspace_id: combo-ws
repos:
  - repo_id: explicit-repo
    local_path: {explicit}
scan_dirs:
  - {scan}
scan_depth: 2
"#,
            explicit = "/tmp/explicit-repo",
            scan = tmp.to_string_lossy(),
        ))
        .unwrap();
        let reqs = build_index_requests(&cfg);
        let ids: Vec<_> = reqs.iter().map(|r| r.repo.repo_id.clone()).collect();
        assert!(ids.contains(&"explicit-repo".to_string()));
        assert!(ids.contains(&"scanned-repo".to_string()));
        // scan discovery excluded the declared id.
        assert!(!ids.contains(&"tmp".to_string()));
        for r in &reqs {
            assert_eq!(r.repo.workspace_id, "combo-ws");
        }

        let _ = fs::remove_dir_all(&tmp);
    }
}
