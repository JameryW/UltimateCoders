//! LEGO-RL hardening (#24): process-group isolation and state hashing.
//!
//! Reinforcement-learning rollouts are only meaningful when every run starts
//! from *the same* filesystem state and leaves no stray processes behind. This
//! module provides the pure-data building blocks for that discipline:
//!
//! * [`RlHardening::prepare_process_group`] / [`RlHardening::cleanup_process_group`]
//!   produce **plans** (typed command descriptions) for an external supervisor
//!   to execute. This module never spawns, signals or kills a process — there
//!   is no `unsafe` process manipulation here, and applying a plan is the
//!   caller's explicit responsibility.
//! * [`RlHardening::hash_state`] computes a deterministic BLAKE3 digest over a
//!   set of paths. Regular files contribute their bytes; directories,
//!   symlinks, missing and unreadable paths fall back to path + metadata so a
//!   digest can always be produced.
//! * [`RlHardening::verify_state`] turns two digests into a
//!   [`StateHashReceipt`], and [`RlHardening::mark_anomaly`] records
//!   environment problems for the rollout report.
//!
//! Paths are visited in normalized, sorted order, so the same tree hashes to
//! the same digest regardless of directory-enumeration order or input order.

use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// BLAKE3 is the digest algorithm recorded on every [`StateHashReceipt`].
pub const HASH_ALGORITHM: &str = "blake3";

/// Synthetic description of the process group a rollout should run in.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProcessGroupSpec {
    /// Process-group id the supervisor should create/attach.
    pub pgid: u32,
    /// cgroup v2 path that should contain the group.
    pub cgroup_path: String,
    /// PID namespace the group should join.
    pub pid_namespace: String,
}

impl ProcessGroupSpec {
    pub fn new(
        pgid: u32,
        cgroup_path: impl Into<String>,
        pid_namespace: impl Into<String>,
    ) -> Self {
        Self {
            pgid,
            cgroup_path: cgroup_path.into(),
            pid_namespace: pid_namespace.into(),
        }
    }
}

/// One command in a process-group plan. Purely declarative — nothing in this
/// module executes it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProcessGroupCommand {
    /// Logical program/step name understood by the supervisor.
    pub program: String,
    pub args: Vec<String>,
    /// Human-readable rationale shown in the rollout report.
    pub description: String,
}

impl ProcessGroupCommand {
    fn new(program: impl Into<String>, args: Vec<String>, description: impl Into<String>) -> Self {
        Self {
            program: program.into(),
            args,
            description: description.into(),
        }
    }
}

/// Plan returned by [`RlHardening::prepare_process_group`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProcessGroupPlan {
    pub spec: ProcessGroupSpec,
    pub commands: Vec<ProcessGroupCommand>,
    /// Always `true`: this module only ever produces a plan, never executes.
    pub dry_run: bool,
}

/// Plan returned by [`RlHardening::cleanup_process_group`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProcessGroupCleanupPlan {
    pub spec: ProcessGroupSpec,
    pub commands: Vec<ProcessGroupCommand>,
    /// Always `true`: this module only ever produces a plan, never executes.
    pub dry_run: bool,
}

/// Before/after digest pair recorded around a rollout.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StateHashReceipt {
    pub before: String,
    pub after: String,
    pub algorithm: String,
}

impl StateHashReceipt {
    /// Whether the state is unchanged.
    pub fn is_stable(&self) -> bool {
        self.before == self.after
    }

    /// Whether the receipt was produced by the expected algorithm.
    pub fn is_blake3(&self) -> bool {
        self.algorithm == HASH_ALGORITHM
    }
}

/// Environmental problems a rollout may report.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum EnvironmentAnomaly {
    /// A process group outlived its rollout.
    ProcessGroupLeak { pgid: u32 },
    /// The configured cgroup path is missing or not usable.
    CgroupUnavailable { cgroup_path: String },
    /// The observed PID namespace differs from the requested one.
    PidNamespaceMismatch { expected: String, actual: String },
    /// The filesystem state changed across the rollout.
    StateMutation { before: String, after: String },
    /// A path expected by the rollout is missing.
    MissingPath { path: String },
}

/// Errors from the pure hardening layer.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum RlHardeningError {
    #[error("process group pgid must be non-zero")]
    InvalidProcessGroup,
    #[error("cgroup path must not be empty")]
    EmptyCgroupPath,
    #[error("pid namespace must not be empty")]
    EmptyPidNamespace,
    #[error("state hash requires at least one path")]
    EmptyStatePaths,
}

/// Pure LEGO-RL hardening helper. Holds only recorded anomalies; all process
/// and filesystem interaction is either read-only hashing or plan generation.
#[derive(Debug, Clone, Default)]
pub struct RlHardening {
    anomalies: Vec<EnvironmentAnomaly>,
}

impl RlHardening {
    pub fn new() -> Self {
        Self::default()
    }

    fn validate_spec(spec: &ProcessGroupSpec) -> Result<(), RlHardeningError> {
        if spec.pgid == 0 {
            return Err(RlHardeningError::InvalidProcessGroup);
        }
        if spec.cgroup_path.trim().is_empty() {
            return Err(RlHardeningError::EmptyCgroupPath);
        }
        if spec.pid_namespace.trim().is_empty() {
            return Err(RlHardeningError::EmptyPidNamespace);
        }
        Ok(())
    }

    /// Produce the plan that prepares the isolated process group. The caller
    /// (supervisor) applies the commands; this method only validates the spec.
    pub fn prepare_process_group(
        &self,
        spec: &ProcessGroupSpec,
    ) -> Result<ProcessGroupPlan, RlHardeningError> {
        Self::validate_spec(spec)?;
        let cgroup = spec.cgroup_path.clone();
        let commands = vec![
            ProcessGroupCommand::new(
                "cgroup_create",
                vec![cgroup.clone()],
                "create the dedicated cgroup v2 subtree for the rollout",
            ),
            ProcessGroupCommand::new(
                "cgroup_attach_pgid",
                vec![spec.pgid.to_string(), cgroup.clone()],
                "move every process of the rollout process group into the cgroup",
            ),
            ProcessGroupCommand::new(
                "pid_namespace_join",
                vec![spec.pid_namespace.clone()],
                "isolate PB/vis targets inside the configured PID namespace",
            ),
            ProcessGroupCommand::new(
                "process_group_verify",
                vec![spec.pgid.to_string()],
                "verify the process group contains no foreign processes before start",
            ),
        ];
        Ok(ProcessGroupPlan {
            spec: spec.clone(),
            commands,
            dry_run: true,
        })
    }

    /// Produce the cleanup plan for a finished rollout. The plan contains
    /// command descriptions only — this function never signals or kills.
    pub fn cleanup_process_group(
        &self,
        spec: &ProcessGroupSpec,
    ) -> Result<ProcessGroupCleanupPlan, RlHardeningError> {
        Self::validate_spec(spec)?;
        let cgroup = spec.cgroup_path.clone();
        let commands = vec![
            ProcessGroupCommand::new(
                "cgroup_freeze",
                vec![cgroup.clone()],
                "freeze the cgroup to stop new work before teardown",
            ),
            ProcessGroupCommand::new(
                "cgroup_kill",
                vec![cgroup.clone()],
                "request the supervisor to terminate remaining cgroup members",
            ),
            ProcessGroupCommand::new(
                "process_group_reap",
                vec![spec.pgid.to_string()],
                "reap orphaned children of the rollout process group",
            ),
            ProcessGroupCommand::new(
                "cgroup_remove",
                vec![cgroup],
                "remove the empty cgroup subtree",
            ),
        ];
        Ok(ProcessGroupCleanupPlan {
            spec: spec.clone(),
            commands,
            dry_run: true,
        })
    }

    /// Deterministically hash a set of paths with BLAKE3.
    ///
    /// Traversal is recursive for directories and sorted by normalized path
    /// (with `\` normalized to `/`). Regular files contribute their bytes;
    /// directories, symlinks, missing and unreadable paths contribute a
    /// path+metadata fallback record so hashing never depends on read
    /// permissions alone. An empty path list is rejected.
    pub fn hash_state(&self, paths: &[PathBuf]) -> Result<String, RlHardeningError> {
        if paths.is_empty() {
            return Err(RlHardeningError::EmptyStatePaths);
        }

        let mut entries = BTreeMap::new();
        for path in paths {
            collect_entries(path, &mut entries);
        }

        let mut hasher = blake3::Hasher::new();
        hash_field(&mut hasher, b"uc-rl-state-v1");
        for (normalized, path) in entries {
            hash_field(&mut hasher, normalized.as_bytes());
            hash_entry(&mut hasher, path.as_path());
        }
        Ok(hasher.finalize().to_hex().to_string())
    }

    /// Compare two state digests, producing a [`StateHashReceipt`].
    pub fn verify_state(&self, before: &str, after: &str) -> StateHashReceipt {
        StateHashReceipt {
            before: before.to_string(),
            after: after.to_string(),
            algorithm: HASH_ALGORITHM.to_string(),
        }
    }

    /// Record one environment anomaly; returns its index in the anomaly log.
    pub fn mark_anomaly(&mut self, anomaly: EnvironmentAnomaly) -> usize {
        self.anomalies.push(anomaly);
        self.anomalies.len() - 1
    }

    /// All recorded anomalies in insertion order.
    pub fn anomalies(&self) -> &[EnvironmentAnomaly] {
        &self.anomalies
    }

    /// Whether no anomaly has been recorded.
    pub fn is_clean(&self) -> bool {
        self.anomalies.is_empty()
    }

    /// Drop all recorded anomalies.
    pub fn clear_anomalies(&mut self) {
        self.anomalies.clear();
    }
}

/// Normalize a path for hashing (stable separator across platforms).
fn normalize_path(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

/// Collect `path` and, for readable directories, all descendants into an
/// ordered map keyed by normalized path.
fn collect_entries(path: &Path, out: &mut BTreeMap<String, PathBuf>) {
    let key = normalize_path(path);
    if out.contains_key(&key) {
        return;
    }
    let is_dir = fs::symlink_metadata(path)
        .map(|metadata| metadata.is_dir())
        .unwrap_or(false);
    out.insert(key, path.to_path_buf());
    if !is_dir {
        return;
    }
    let Ok(read_dir) = fs::read_dir(path) else {
        return;
    };
    let mut children: Vec<PathBuf> = read_dir
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .collect();
    children.sort_by_key(|child| normalize_path(child));
    for child in children {
        collect_entries(&child, out);
    }
}

/// Mix one length-prefixed field into the hasher.
fn hash_field(hasher: &mut blake3::Hasher, bytes: &[u8]) {
    hasher.update(&(bytes.len() as u64).to_le_bytes());
    hasher.update(bytes);
}

/// Hash one filesystem entry. Never fails: unreadable/missing paths fall back
/// to a metadata record.
fn hash_entry(hasher: &mut blake3::Hasher, path: &Path) {
    let metadata = fs::symlink_metadata(path);
    match metadata {
        Ok(metadata) if metadata.file_type().is_symlink() => {
            hash_field(hasher, b"symlink");
            match fs::read_link(path) {
                Ok(target) => hash_field(hasher, normalize_path(&target).as_bytes()),
                Err(_) => hash_field(hasher, b"<unreadable-link>"),
            }
        }
        Ok(metadata) if metadata.is_file() => match fs::File::open(path) {
            Ok(mut file) => match io::copy(&mut file, hasher) {
                Ok(_) => {}
                Err(_) => {
                    hash_field(hasher, b"<read-error>");
                    hash_metadata(hasher, &metadata);
                }
            },
            Err(_) => {
                hash_field(hasher, b"<open-error>");
                hash_metadata(hasher, &metadata);
            }
        },
        Ok(metadata) => {
            hash_field(hasher, b"dir");
            hash_metadata(hasher, &metadata);
        }
        Err(_) => {
            hash_field(hasher, b"missing");
        }
    }
}

/// Metadata fallback record: kind, length, readonly flag and (when available)
/// modification time in nanoseconds since the Unix epoch.
fn hash_metadata(hasher: &mut blake3::Hasher, metadata: &fs::Metadata) {
    hasher.update(&[u8::from(metadata.is_dir())]);
    hasher.update(&metadata.len().to_le_bytes());
    hasher.update(&[u8::from(metadata.permissions().readonly())]);
    match metadata.modified() {
        Ok(modified) => match modified.duration_since(std::time::UNIX_EPOCH) {
            Ok(elapsed) => {
                hasher.update(&elapsed.as_nanos().to_le_bytes());
            }
            Err(_) => {
                hash_field(hasher, b"<pre-epoch-mtime>");
            }
        },
        Err(_) => {
            hash_field(hasher, b"<no-mtime>");
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicU64, Ordering};

    use super::*;

    fn temp_dir(label: &str) -> PathBuf {
        static COUNTER: AtomicU64 = AtomicU64::new(0);
        let unique = COUNTER.fetch_add(1, Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!(
            "uc-rl-hardening-{}-{}-{}",
            std::process::id(),
            label,
            unique
        ));
        fs::create_dir_all(&dir).expect("create temp dir");
        dir
    }

    fn spec() -> ProcessGroupSpec {
        ProcessGroupSpec::new(4321, "/sys/fs/cgroup/uc-rl/rollout-1", "uc-rl-ns-1")
    }

    #[test]
    fn prepare_and_cleanup_are_plans_only() {
        let hardening = RlHardening::new();
        let prepare = hardening.prepare_process_group(&spec()).unwrap();
        assert!(prepare.dry_run);
        assert!(prepare.commands.len() >= 3);
        assert!(prepare
            .commands
            .iter()
            .any(|command| command.program == "cgroup_attach_pgid"));
        assert!(prepare
            .commands
            .iter()
            .all(|command| !command.description.is_empty()));

        let cleanup = hardening.cleanup_process_group(&spec()).unwrap();
        assert!(cleanup.dry_run);
        assert!(cleanup
            .commands
            .iter()
            .any(|command| command.program == "cgroup_kill"));
        assert_eq!(cleanup.spec, spec());
    }

    #[test]
    fn invalid_process_group_specs_are_rejected() {
        let hardening = RlHardening::new();
        assert_eq!(
            hardening.prepare_process_group(&ProcessGroupSpec::new(0, "/cg", "ns")),
            Err(RlHardeningError::InvalidProcessGroup)
        );
        assert_eq!(
            hardening.prepare_process_group(&ProcessGroupSpec::new(1, "  ", "ns")),
            Err(RlHardeningError::EmptyCgroupPath)
        );
        assert_eq!(
            hardening.cleanup_process_group(&ProcessGroupSpec::new(1, "/cg", "")),
            Err(RlHardeningError::EmptyPidNamespace)
        );
    }

    #[test]
    fn hash_state_is_order_independent_and_tracks_file_bytes() {
        let dir = temp_dir("hash");
        let a = dir.join("a.txt");
        let b = dir.join("b.txt");
        fs::write(&a, b"alpha").unwrap();
        fs::write(&b, b"beta").unwrap();

        let hardening = RlHardening::new();
        let first = hardening.hash_state(&[a.clone(), b.clone()]).expect("hash");
        let second = hardening.hash_state(&[b.clone(), a.clone()]).expect("hash");
        assert_eq!(first, second, "input order must not affect the digest");
        assert_eq!(first.len(), 64, "BLAKE3 hex digest has 64 chars");

        fs::write(&a, b"alpha-changed").unwrap();
        let changed = hardening.hash_state(&[a.clone(), b.clone()]).expect("hash");
        assert_ne!(first, changed, "file bytes must affect the digest");

        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn verify_state_reports_stability() {
        let hardening = RlHardening::new();
        let receipt = hardening.verify_state("abc", "abc");
        assert!(receipt.is_stable());
        assert!(receipt.is_blake3());
        assert_eq!(receipt.algorithm, "blake3");
        assert_eq!(receipt.before, "abc");
        assert_eq!(receipt.after, "abc");

        let changed = hardening.verify_state("abc", "def");
        assert!(!changed.is_stable());
    }

    #[test]
    fn missing_paths_use_metadata_fallback_and_stay_stable() {
        let hardening = RlHardening::new();
        let missing = std::env::temp_dir().join("uc-rl-hardening-definitely-missing-path");
        let first = hardening
            .hash_state(&[missing.clone()])
            .expect("missing paths must not error");
        let second = hardening
            .hash_state(&[missing])
            .expect("missing paths must not error");
        assert_eq!(first, second);
        assert_eq!(
            hardening.hash_state(&[]),
            Err(RlHardeningError::EmptyStatePaths)
        );
    }

    #[test]
    fn mark_anomaly_records_in_order() {
        let mut hardening = RlHardening::new();
        assert!(hardening.is_clean());
        let index = hardening.mark_anomaly(EnvironmentAnomaly::ProcessGroupLeak { pgid: 4321 });
        assert_eq!(index, 0);
        assert_eq!(
            hardening.mark_anomaly(EnvironmentAnomaly::CgroupUnavailable {
                cgroup_path: "/sys/fs/cgroup/uc-rl/rollout-1".to_string()
            }),
            1
        );
        assert_eq!(hardening.anomalies().len(), 2);
        assert!(!hardening.is_clean());
        hardening.clear_anomalies();
        assert!(hardening.is_clean());
    }
}
