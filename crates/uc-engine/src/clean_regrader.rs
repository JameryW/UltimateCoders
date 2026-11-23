//! SWE-Bench Pro V2 clean-container re-grading (#42).
//!
//! The clean re-grader is intentionally boring and deterministic: it accepts
//! only a non-empty UTF-8 patch (the plain-text `git diff`), refuses Git
//! metadata channels that can leak a golden patch, rejects non-whitelisted
//! sandbox egress, and requires a fresh container with no source workspace
//! mounted. The actual container is owned by the caller; this module produces
//! the admission decision and a stable patch digest.

use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Identifier recorded by the clean re-grade reports.
pub const CLEAN_REGRADER_ID: &str = "swe-bench-pro-v2";
/// Default upper bound for a submitted plain-text patch.
pub const DEFAULT_MAX_PATCH_BYTES: usize = 4 * 1024 * 1024;

/// Egress allowlist for the grading container.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SandboxEgressPolicy {
    allowed_hosts: BTreeSet<String>,
}

impl SandboxEgressPolicy {
    /// Build a policy from hostnames; values are trimmed and lower-cased.
    pub fn new(
        hosts: impl IntoIterator<Item = impl Into<String>>,
    ) -> Result<Self, RegradeConfigError> {
        let mut allowed_hosts = BTreeSet::new();
        for host in hosts {
            let host = host.into().trim().to_ascii_lowercase();
            if host.is_empty() {
                return Err(RegradeConfigError::EmptyHost);
            }
            allowed_hosts.insert(host);
        }
        if allowed_hosts.is_empty() {
            return Err(RegradeConfigError::EmptyPolicy);
        }
        Ok(Self { allowed_hosts })
    }

    /// Build a policy that only permits the named inference endpoints.
    pub fn inference_only(
        endpoints: impl IntoIterator<Item = impl Into<String>>,
    ) -> Result<Self, RegradeConfigError> {
        Self::new(endpoints)
    }

    /// Whether a host is explicitly allowed.
    pub fn allows(&self, host: &str) -> bool {
        self.allowed_hosts
            .contains(&host.trim().to_ascii_lowercase())
    }

    /// Bounded sorted host list for reporting.
    pub fn hosts(&self) -> Vec<&str> {
        self.allowed_hosts.iter().map(String::as_str).collect()
    }
}

/// Configuration validation errors.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum RegradeConfigError {
    #[error("egress host must not be empty")]
    EmptyHost,
    #[error("egress policy must allow at least one host")]
    EmptyPolicy,
}

/// A request to re-grade one task in a fresh container.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CleanRegradeRequest {
    pub task_id: String,
    /// Plain-text `git diff` only.
    pub patch: String,
    /// Hosts the sandbox may egress to during grading.
    pub network_hosts: Vec<String>,
    /// The container must be created from the clean base image.
    pub container_fresh: bool,
    /// The source workspace must not be mounted into the grading container.
    pub source_workspace_mounted: bool,
}

impl CleanRegradeRequest {
    /// Create a request; admission checks happen in {@link CleanRegrader}.
    pub fn new(
        task_id: impl Into<String>,
        patch: impl Into<String>,
        network_hosts: impl IntoIterator<Item = impl Into<String>>,
        container_fresh: bool,
        source_workspace_mounted: bool,
    ) -> Self {
        Self {
            task_id: task_id.into(),
            patch: patch.into(),
            network_hosts: network_hosts.into_iter().map(Into::into).collect(),
            container_fresh,
            source_workspace_mounted,
        }
    }
}

/// One admission violation.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", tag = "kind", content = "detail")]
pub enum RegradeViolation {
    EmptyTask,
    EmptyPatch,
    PatchTooLarge { bytes: usize, limit: usize },
    NonTextPatch,
    GitMetadataReference(String),
    NetworkEgressDenied(String),
    DirtyContainer,
    SourceWorkspaceMounted,
}

impl RegradeViolation {
    /// Stable machine-readable reason code.
    pub fn code(&self) -> &'static str {
        match self {
            Self::EmptyTask => "empty_task",
            Self::EmptyPatch => "empty_patch",
            Self::PatchTooLarge { .. } => "patch_too_large",
            Self::NonTextPatch => "non_text_patch",
            Self::GitMetadataReference(_) => "git_metadata_reference",
            Self::NetworkEgressDenied(_) => "network_egress_denied",
            Self::DirtyContainer => "dirty_container",
            Self::SourceWorkspaceMounted => "source_workspace_mounted",
        }
    }
}

/// Deterministic admission report for one clean re-grade.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CleanRegradeReport {
    pub grader_id: String,
    pub task_id: String,
    pub accepted: bool,
    pub patch_digest: String,
    pub violations: Vec<RegradeViolation>,
}

impl CleanRegradeReport {
    /// True when admission passed and the patch can be re-graded.
    pub fn is_clean(&self) -> bool {
        self.accepted && self.violations.is_empty()
    }
}

/// Pure clean-container admission gate.
#[derive(Debug, Clone)]
pub struct CleanRegrader {
    policy: SandboxEgressPolicy,
    max_patch_bytes: usize,
}

impl CleanRegrader {
    /// Build a re-grader with the default patch bound.
    pub fn new(policy: SandboxEgressPolicy) -> Self {
        Self {
            policy,
            max_patch_bytes: DEFAULT_MAX_PATCH_BYTES,
        }
    }

    /// Replace the maximum accepted patch size.
    #[must_use]
    pub fn with_max_patch_bytes(mut self, max_patch_bytes: usize) -> Self {
        self.max_patch_bytes = max_patch_bytes.max(1);
        self
    }

    /// The configured egress policy.
    pub fn policy(&self) -> &SandboxEgressPolicy {
        &self.policy
    }

    /// Check one request; never executes anything.
    pub fn regrade(&self, request: &CleanRegradeRequest) -> CleanRegradeReport {
        let mut violations = Vec::new();
        if request.task_id.trim().is_empty() {
            violations.push(RegradeViolation::EmptyTask);
        }
        let patch = request.patch.as_str();
        if patch.trim().is_empty() {
            violations.push(RegradeViolation::EmptyPatch);
        } else {
            if patch.len() > self.max_patch_bytes {
                violations.push(RegradeViolation::PatchTooLarge {
                    bytes: patch.len(),
                    limit: self.max_patch_bytes,
                });
            }
            if !is_plain_text(patch) {
                violations.push(RegradeViolation::NonTextPatch);
            }
            if let Some(marker) = git_metadata_marker(patch) {
                violations.push(RegradeViolation::GitMetadataReference(marker));
            }
        }
        for host in &request.network_hosts {
            if !self.policy.allows(host) {
                violations.push(RegradeViolation::NetworkEgressDenied(host.clone()));
            }
        }
        if !request.container_fresh {
            violations.push(RegradeViolation::DirtyContainer);
        }
        if request.source_workspace_mounted {
            violations.push(RegradeViolation::SourceWorkspaceMounted);
        }
        CleanRegradeReport {
            grader_id: CLEAN_REGRADER_ID.to_string(),
            task_id: request.task_id.clone(),
            accepted: violations.is_empty(),
            patch_digest: patch_digest(patch),
            violations,
        }
    }
}

fn patch_digest(patch: &str) -> String {
    blake3::hash(patch.as_bytes()).to_hex().to_string()
}

fn is_plain_text(patch: &str) -> bool {
    !patch
        .chars()
        .any(|ch| ch == '\u{0}' || (ch.is_control() && !matches!(ch, '\n' | '\r' | '\t')))
}

fn git_metadata_marker(patch: &str) -> Option<String> {
    let lower = patch.to_ascii_lowercase();
    for marker in [
        ".git/",
        "refs/tags/",
        "refs/heads/",
        "packed-refs",
        "git log",
        "git show",
        "git reflog",
    ] {
        if lower.contains(marker) {
            return Some(marker.to_string());
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    const PATCH: &str =
        "diff --git a/src/lib.rs b/src/lib.rs\n--- a/src/lib.rs\n+++ b/src/lib.rs\n@@ -1 +1 @@\n-old\n+new\n";

    fn grader() -> CleanRegrader {
        CleanRegrader::new(
            SandboxEgressPolicy::inference_only(["api.anthropic.com", "api.openai.com"])
                .expect("policy"),
        )
    }

    fn request() -> CleanRegradeRequest {
        CleanRegradeRequest::new("task-1", PATCH, ["api.openai.com"], true, false)
    }

    #[test]
    fn accepts_a_clean_plain_text_patch() {
        let report = grader().regrade(&request());
        assert!(report.accepted);
        assert!(report.is_clean());
        assert_eq!(report.grader_id, CLEAN_REGRADER_ID);
        assert!(report.violations.is_empty());
    }

    #[test]
    fn rejects_empty_task_and_patch() {
        let request = CleanRegradeRequest::new(" ", "", ["api.openai.com"], true, false);
        let report = grader().regrade(&request);
        assert!(!report.accepted);
        let codes: Vec<&str> = report
            .violations
            .iter()
            .map(RegradeViolation::code)
            .collect();
        assert!(codes.contains(&"empty_task"));
        assert!(codes.contains(&"empty_patch"));
    }

    #[test]
    fn rejects_unknown_network_egress() {
        let request = CleanRegradeRequest::new(
            "task-1",
            PATCH,
            ["api.openai.com", "evil.example"],
            true,
            false,
        );
        let report = grader().regrade(&request);
        assert_eq!(
            report.violations,
            vec![RegradeViolation::NetworkEgressDenied("evil.example".into())]
        );
    }

    #[test]
    fn rejects_a_dirty_container_and_mounted_source() {
        let request = CleanRegradeRequest::new("task-1", PATCH, ["api.openai.com"], false, true);
        let report = grader().regrade(&request);
        assert!(!report.accepted);
        assert!(report
            .violations
            .contains(&RegradeViolation::DirtyContainer));
        assert!(report
            .violations
            .contains(&RegradeViolation::SourceWorkspaceMounted));
    }

    #[test]
    fn rejects_binary_and_nul_patches() {
        let mut request = request();
        request.patch = "diff --git a/x b/x\n+\u{0}binary".into();
        let report = grader().regrade(&request);
        assert!(report.violations.contains(&RegradeViolation::NonTextPatch));
    }

    #[test]
    fn rejects_git_metadata_references() {
        for patch in [
            "diff --git a/x b/x\n+see .git/refs/heads/main",
            "diff --git a/x b/x\n+git log --all",
            "diff --git a/x b/x\n+refs/tags/gold",
        ] {
            let mut request = request();
            request.patch = patch.into();
            let report = grader().regrade(&request);
            assert!(report
                .violations
                .iter()
                .any(|violation| matches!(violation, RegradeViolation::GitMetadataReference(_))));
        }
    }

    #[test]
    fn enforces_the_patch_byte_limit() {
        let grader = grader().with_max_patch_bytes(8);
        let report = grader.regrade(&request());
        assert!(matches!(
            report.violations.first(),
            Some(RegradeViolation::PatchTooLarge { limit: 8, .. })
        ));
    }

    #[test]
    fn patch_digest_is_stable_and_changes_with_content() {
        let first = grader().regrade(&request());
        let second = grader().regrade(&request());
        assert_eq!(first.patch_digest, second.patch_digest);

        let mut changed = request();
        changed.patch = format!("{PATCH}+// changed\n");
        let third = grader().regrade(&changed);
        assert_ne!(first.patch_digest, third.patch_digest);
    }

    #[test]
    fn policy_normalises_hosts_and_rejects_empty_config() {
        let policy = SandboxEgressPolicy::new([" API.OpenAI.com "]).expect("policy");
        assert!(policy.allows("api.openai.com"));
        assert!(!policy.allows("other.example"));
        assert!(SandboxEgressPolicy::new(std::iter::empty::<&str>()).is_err());
        assert!(SandboxEgressPolicy::new(["  "]).is_err());
    }
}
