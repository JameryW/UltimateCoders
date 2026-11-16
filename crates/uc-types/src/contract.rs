//! Boundary contracts (#7) — per-task file boundaries and evidence-gated
//! challenges.
//!
//! A [`BoundaryContract`] classifies every path a task may touch into one of
//! four roles. The entry map is ordered (`BTreeMap`), so iteration and
//! serialization are deterministic across runs — a contract revision is
//! reproducible byte-for-byte.
//!
//! Only [`BoundaryRole::Editable`] paths may be written. A stricter role can
//! only be relaxed through [`BoundaryContract::challenge`], which requires the
//! challenge to name an explicitly covered file and carry a non-empty evidence
//! line. Every approved challenge returns a **new revision** (`version + 1`)
//! and leaves the input contract untouched; rejected challenges return the
//! unchanged contract with an explanatory decision. [`BoundaryContract::narrow`]
//! is the opposite direction: it may only move a path to a strictly stricter
//! role.
//!
//! Paths that are not listed resolve to [`BoundaryRole::InspectOnly`] through
//! [`BoundaryContract::role_for`] — absence of an entry is never "editable".

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// The role a path plays inside a task's boundary.
#[derive(
    Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize, Default,
)]
#[serde(rename_all = "snake_case")]
pub enum BoundaryRole {
    /// The task may modify this path.
    Editable,
    /// The task may read/inspect this path but must not modify it. This is the
    /// fail-closed default for unlisted paths.
    #[default]
    InspectOnly,
    /// The path is required by the task contract (its presence/content is
    /// depended upon) and may not be freely changed.
    Required,
    /// The path is protected; changes require an approved boundary challenge.
    Protected,
}

impl BoundaryRole {
    /// Stable wire/storage token.
    pub fn as_str(self) -> &'static str {
        match self {
            BoundaryRole::Editable => "editable",
            BoundaryRole::InspectOnly => "inspect_only",
            BoundaryRole::Required => "required",
            BoundaryRole::Protected => "protected",
        }
    }

    /// Whether the task may write this path under the current contract.
    pub fn is_editable(self) -> bool {
        matches!(self, BoundaryRole::Editable)
    }

    /// Only these roles may be relaxed to `Editable` through a challenge.
    pub fn challengeable(self) -> bool {
        matches!(self, BoundaryRole::InspectOnly | BoundaryRole::Protected)
    }

    /// Strictness rank used by [`BoundaryContract::narrow`]: higher is stricter.
    pub fn strictness(self) -> u8 {
        match self {
            BoundaryRole::Editable => 0,
            BoundaryRole::InspectOnly => 1,
            BoundaryRole::Required => 2,
            BoundaryRole::Protected => 3,
        }
    }

    /// Whether narrowing from `self` to `next` is allowed (strictly stricter).
    pub fn can_narrow_to(self, next: BoundaryRole) -> bool {
        next.strictness() > self.strictness()
    }
}

impl std::fmt::Display for BoundaryRole {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Lifecycle of a boundary challenge against one path.
///
/// `Unchallenged` is the default; a [`BoundaryChallengeDecision`] reports
/// `Approved`/`Rejected`, while `Challenged` covers an in-flight request that
/// has been recorded but not yet adjudicated (e.g. a dashboard queue row).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum BoundaryChallengeState {
    #[default]
    Unchallenged,
    Challenged,
    Approved,
    Rejected,
}

/// Why a boundary contract operation was refused.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum BoundaryContractError {
    #[error("boundary contract contains an empty path")]
    EmptyPath,
    #[error("duplicate boundary path `{path}`")]
    DuplicatePath { path: String },
    #[error("path `{path}` is not part of boundary contract `{contract_id}`")]
    UnknownPath { contract_id: String, path: String },
    #[error("role transition for `{path}` from {from:?} to {to:?} is not a narrowing")]
    NotNarrowing {
        path: String,
        from: BoundaryRole,
        to: BoundaryRole,
    },
}

/// Role classification produced by [`BoundaryContract::validate`].
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct BoundaryContractValidation {
    pub editable: BTreeSet<String>,
    pub inspect_only: BTreeSet<String>,
    pub required: BTreeSet<String>,
    pub protected: BTreeSet<String>,
}

impl BoundaryContractValidation {
    fn classify(entries: &BTreeMap<String, BoundaryRole>) -> Self {
        let mut validation = BoundaryContractValidation::default();
        for (path, role) in entries {
            match role {
                BoundaryRole::Editable => validation.editable.insert(path.clone()),
                BoundaryRole::InspectOnly => validation.inspect_only.insert(path.clone()),
                BoundaryRole::Required => validation.required.insert(path.clone()),
                BoundaryRole::Protected => validation.protected.insert(path.clone()),
            };
        }
        validation
    }

    /// Total number of classified entries.
    pub fn total_entries(&self) -> usize {
        self.editable.len() + self.inspect_only.len() + self.required.len() + self.protected.len()
    }
}

/// The boundary contract attached to one task.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BoundaryContract {
    pub contract_id: String,
    pub task_id: String,
    /// Path → role, ordered for deterministic iteration/serialization.
    pub entries: BTreeMap<String, BoundaryRole>,
    /// Monotonic revision; bumped by every accepted mutation.
    pub version: u64,
}

impl BoundaryContract {
    /// Build a fresh contract at version 1 from an ordered entry map.
    pub fn new(
        contract_id: impl Into<String>,
        task_id: impl Into<String>,
        entries: BTreeMap<String, BoundaryRole>,
    ) -> Self {
        Self {
            contract_id: contract_id.into(),
            task_id: task_id.into(),
            entries,
            version: 1,
        }
    }

    /// Build a contract from an entry iterator, explicating rejecting empty
    /// and duplicate path keys (a `BTreeMap` cannot represent duplicates, but
    /// untrusted wire input can).
    pub fn from_entries<I>(
        contract_id: impl Into<String>,
        task_id: impl Into<String>,
        entries: I,
    ) -> Result<Self, BoundaryContractError>
    where
        I: IntoIterator<Item = (String, BoundaryRole)>,
    {
        let mut map = BTreeMap::new();
        for (path, role) in entries {
            if path.trim().is_empty() {
                return Err(BoundaryContractError::EmptyPath);
            }
            if map.insert(path.clone(), role).is_some() {
                return Err(BoundaryContractError::DuplicatePath { path });
            }
        }
        Ok(Self::new(contract_id, task_id, map))
    }

    /// Resolve the role of `path`. Unlisted paths are `InspectOnly`
    /// (fail-closed); use [`BoundaryContract::explicit_role`] to distinguish
    /// "absent" from "explicitly inspect-only".
    pub fn role_for(&self, path: &str) -> BoundaryRole {
        self.entries
            .get(path)
            .copied()
            .unwrap_or(BoundaryRole::InspectOnly)
    }

    /// The explicit entry for `path`, if the contract names it.
    pub fn explicit_role(&self, path: &str) -> Option<BoundaryRole> {
        self.entries.get(path).copied()
    }

    /// Insert or replace one path. Returns the previous role, if any.
    ///
    /// This is the low-level setter: it does not bump [`Self::version`].
    /// Use [`BoundaryContract::update`] or [`BoundaryContract::challenge`] for
    /// auditable, versioned changes.
    pub fn set_role(
        &mut self,
        path: impl Into<String>,
        role: BoundaryRole,
    ) -> Option<BoundaryRole> {
        self.entries.insert(path.into(), role)
    }

    /// The ordered entry map.
    pub fn entries(&self) -> &BTreeMap<String, BoundaryRole> {
        &self.entries
    }

    /// Validate and classify the contract. Rejects empty path keys; the
    /// returned sets classify every path by role in deterministic order.
    pub fn validate(&self) -> Result<BoundaryContractValidation, BoundaryContractError> {
        for path in self.entries.keys() {
            if path.trim().is_empty() {
                return Err(BoundaryContractError::EmptyPath);
            }
        }
        Ok(BoundaryContractValidation::classify(&self.entries))
    }

    /// Apply a batch of role updates and bump the revision. Returns the new
    /// version. Unlisted paths may be introduced by an update.
    pub fn update<I>(&mut self, entries: I) -> u64
    where
        I: IntoIterator<Item = (String, BoundaryRole)>,
    {
        for (path, role) in entries {
            self.entries.insert(path, role);
        }
        self.version = self.version.saturating_add(1);
        self.version
    }

    /// Narrow one path to a strictly stricter role, returning a new revision.
    /// Widening (or a no-op change) is refused.
    pub fn narrow(mut self, path: &str, role: BoundaryRole) -> Result<Self, BoundaryContractError> {
        let previous =
            self.explicit_role(path)
                .ok_or_else(|| BoundaryContractError::UnknownPath {
                    contract_id: self.contract_id.clone(),
                    path: path.to_string(),
                })?;
        if !previous.can_narrow_to(role) {
            return Err(BoundaryContractError::NotNarrowing {
                path: path.to_string(),
                from: previous,
                to: role,
            });
        }
        self.entries.insert(path.to_string(), role);
        self.version = self.version.saturating_add(1);
        Ok(self)
    }

    /// Adjudicate an evidence-backed challenge against one path.
    ///
    /// Approval requires **all** of:
    /// 1. `target_file` is an explicit entry of this contract;
    /// 2. its current role is `Protected` or `InspectOnly`;
    /// 3. `evidence_line` is non-empty after trimming.
    ///
    /// An approved challenge returns a clone at `version + 1` with the target
    /// promoted to `Editable`; a rejected challenge returns the unchanged
    /// contract and a decision whose `new_role` equals `previous_role`.
    /// The operation is pure and deterministic.
    pub fn challenge(&self, request: &BoundaryChallengeRequest) -> BoundaryChallengeOutcome {
        let target = request.target_file.trim();
        let Some(previous) = self.explicit_role(target) else {
            return BoundaryChallengeOutcome::rejected(
                self,
                BoundaryRole::InspectOnly,
                format!(
                    "path `{target}` is not covered by boundary contract `{}`",
                    self.contract_id
                ),
            );
        };
        if !previous.challengeable() {
            return BoundaryChallengeOutcome::rejected(
                self,
                previous,
                format!(
                    "role `{}` cannot be challenged to editable",
                    previous.as_str()
                ),
            );
        }
        if request.evidence_line.trim().is_empty() {
            return BoundaryChallengeOutcome::rejected(
                self,
                previous,
                "challenge requires a non-empty evidence_line",
            );
        }

        let mut updated = self.clone();
        updated
            .entries
            .insert(target.to_string(), BoundaryRole::Editable);
        updated.version = self.version.saturating_add(1);
        let reason = if request.reason.trim().is_empty() {
            format!(
                "approved boundary challenge for `{target}`; evidence: {}",
                request.evidence_line.trim()
            )
        } else {
            request.reason.trim().to_string()
        };
        BoundaryChallengeOutcome {
            contract: updated,
            decision: BoundaryChallengeDecision {
                approved: true,
                previous_role: previous,
                new_role: BoundaryRole::Editable,
                reason,
            },
        }
    }
}

/// A request to relax a boundary for one file/symbol.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BoundaryChallengeRequest {
    pub target_file: String,
    /// AST symbol the challenge is about (e.g. `impl Foo::bar`), if known.
    pub ast_symbol: String,
    /// Concrete evidence that editing is necessary (log line, failing test,
    /// stack frame, …). Must be non-empty for approval.
    pub evidence_line: String,
    pub reason: String,
}

/// The adjudication of one [`BoundaryChallengeRequest`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BoundaryChallengeDecision {
    pub approved: bool,
    pub previous_role: BoundaryRole,
    pub new_role: BoundaryRole,
    pub reason: String,
}

impl BoundaryChallengeDecision {
    /// The terminal state this decision represents.
    pub fn state(&self) -> BoundaryChallengeState {
        if self.approved {
            BoundaryChallengeState::Approved
        } else {
            BoundaryChallengeState::Rejected
        }
    }
}

/// Result of [`BoundaryContract::challenge`]: the (possibly updated) contract
/// plus the decision that produced it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BoundaryChallengeOutcome {
    pub contract: BoundaryContract,
    pub decision: BoundaryChallengeDecision,
}

impl BoundaryChallengeOutcome {
    fn rejected(
        contract: &BoundaryContract,
        previous_role: BoundaryRole,
        reason: impl Into<String>,
    ) -> Self {
        Self {
            contract: contract.clone(),
            decision: BoundaryChallengeDecision {
                approved: false,
                previous_role,
                new_role: previous_role,
                reason: reason.into(),
            },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn contract() -> BoundaryContract {
        BoundaryContract::new(
            "bc-1",
            "task-1",
            BTreeMap::from([
                (
                    "crates/uc-types/src/lib.rs".to_string(),
                    BoundaryRole::Protected,
                ),
                (
                    "crates/uc-types/src/graph.rs".to_string(),
                    BoundaryRole::InspectOnly,
                ),
                (
                    "crates/uc-types/src/agent.rs".to_string(),
                    BoundaryRole::Required,
                ),
                (
                    "crates/uc-types/src/contract.rs".to_string(),
                    BoundaryRole::Editable,
                ),
            ]),
        )
    }

    fn request(target: &str, evidence: &str) -> BoundaryChallengeRequest {
        BoundaryChallengeRequest {
            target_file: target.to_string(),
            ast_symbol: "impl BoundaryContract".to_string(),
            evidence_line: evidence.to_string(),
            reason: "required by failing test".to_string(),
        }
    }

    #[test]
    fn roles_serialize_to_snake_case_and_default_fail_closed() {
        assert_eq!(
            serde_json::to_string(&BoundaryRole::InspectOnly).unwrap(),
            "\"inspect_only\""
        );
        assert_eq!(
            serde_json::to_string(&BoundaryRole::Protected).unwrap(),
            "\"protected\""
        );
        assert_eq!(BoundaryRole::default(), BoundaryRole::InspectOnly);
        assert!(BoundaryRole::Editable.is_editable());
        assert!(!BoundaryRole::Required.is_editable());
        for role in [
            BoundaryRole::Editable,
            BoundaryRole::InspectOnly,
            BoundaryRole::Required,
            BoundaryRole::Protected,
        ] {
            assert_eq!(role.to_string(), role.as_str());
        }
    }

    #[test]
    fn role_for_defaults_unlisted_paths_to_inspect_only() {
        let contract = contract();
        assert_eq!(
            contract.role_for("crates/uc-types/src/lib.rs"),
            BoundaryRole::Protected
        );
        assert_eq!(
            contract.role_for("crates/uc-types/src/unknown.rs"),
            BoundaryRole::InspectOnly
        );
        assert_eq!(
            contract.explicit_role("crates/uc-types/src/unknown.rs"),
            None
        );
    }

    #[test]
    fn set_role_reports_previous_and_entries_stay_sorted() {
        let mut contract = contract();
        let previous = contract.set_role("crates/uc-types/src/graph.rs", BoundaryRole::Editable);
        assert_eq!(previous, Some(BoundaryRole::InspectOnly));
        assert_eq!(
            contract.role_for("crates/uc-types/src/graph.rs"),
            BoundaryRole::Editable
        );
        let paths: Vec<&str> = contract.entries().keys().map(String::as_str).collect();
        let mut sorted = paths.clone();
        sorted.sort_unstable();
        assert_eq!(paths, sorted, "BTreeMap iteration must be deterministic");
    }

    #[test]
    fn validate_classifies_every_role_and_rejects_empty_paths() {
        let validation = contract().validate().unwrap();
        assert_eq!(validation.total_entries(), 4);
        assert_eq!(validation.editable.len(), 1);
        assert_eq!(validation.inspect_only.len(), 1);
        assert_eq!(validation.required.len(), 1);
        assert_eq!(validation.protected.len(), 1);
        assert!(validation.protected.contains("crates/uc-types/src/lib.rs"));

        let mut invalid = contract();
        invalid
            .entries
            .insert("   ".to_string(), BoundaryRole::Editable);
        assert_eq!(invalid.validate(), Err(BoundaryContractError::EmptyPath));
    }

    #[test]
    fn from_entries_rejects_duplicate_paths() {
        let duplicate = vec![
            ("src/lib.rs".to_string(), BoundaryRole::Editable),
            ("src/lib.rs".to_string(), BoundaryRole::Protected),
        ];
        assert_eq!(
            BoundaryContract::from_entries("bc", "task", duplicate),
            Err(BoundaryContractError::DuplicatePath {
                path: "src/lib.rs".to_string()
            })
        );
    }

    #[test]
    fn challenge_approves_protected_and_inspect_only_with_evidence() {
        let contract = contract();
        let protected = contract.challenge(&request(
            "crates/uc-types/src/lib.rs",
            "error[E0308]: mismatched types",
        ));
        assert!(protected.decision.approved);
        assert_eq!(protected.decision.previous_role, BoundaryRole::Protected);
        assert_eq!(protected.decision.new_role, BoundaryRole::Editable);
        assert_eq!(protected.contract.version, contract.version + 1);
        assert_eq!(
            protected.contract.role_for("crates/uc-types/src/lib.rs"),
            BoundaryRole::Editable
        );
        // The input contract is untouched (pure operation).
        assert_eq!(
            contract.role_for("crates/uc-types/src/lib.rs"),
            BoundaryRole::Protected
        );
        assert_eq!(contract.version, 1);

        let inspect_only = contract.challenge(&request(
            "crates/uc-types/src/graph.rs",
            "test `transition_table_is_exhaustive` fails",
        ));
        assert!(inspect_only.decision.approved);
        assert_eq!(inspect_only.contract.version, 2);
        assert_eq!(
            inspect_only.decision.state(),
            BoundaryChallengeState::Approved
        );
    }

    #[test]
    fn challenge_is_deterministic() {
        let contract = contract();
        let request = request("crates/uc-types/src/lib.rs", "evidence");
        let first = contract.challenge(&request);
        let second = contract.challenge(&request);
        assert_eq!(first, second);
        assert_eq!(first.contract.entries, second.contract.entries);
    }

    #[test]
    fn challenge_rejects_editable_required_and_unlisted_paths() {
        let contract = contract();
        for target in [
            "crates/uc-types/src/contract.rs", // already Editable
            "crates/uc-types/src/agent.rs",    // Required
            "crates/uc-types/src/missing.rs",  // not covered
        ] {
            let outcome = contract.challenge(&request(target, "some evidence"));
            assert!(!outcome.decision.approved, "{target} must be rejected");
            assert_eq!(outcome.contract, contract, "{target} must be unchanged");
            assert_eq!(outcome.decision.state(), BoundaryChallengeState::Rejected);
        }
    }

    #[test]
    fn challenge_rejects_empty_evidence() {
        let contract = contract();
        let outcome = contract.challenge(&request("crates/uc-types/src/lib.rs", "   \n\t"));
        assert!(!outcome.decision.approved);
        assert!(outcome.decision.reason.contains("evidence_line"));
        assert_eq!(
            outcome.decision.previous_role, outcome.decision.new_role,
            "rejection must not change the role"
        );
        assert_eq!(outcome.contract, contract);
    }

    #[test]
    fn narrow_only_moves_to_stricter_roles_and_bumps_version() {
        let narrowed = contract()
            .narrow("crates/uc-types/src/contract.rs", BoundaryRole::Protected)
            .unwrap();
        assert_eq!(narrowed.version, 2);
        assert_eq!(
            narrowed.role_for("crates/uc-types/src/contract.rs"),
            BoundaryRole::Protected
        );

        let widening = contract().narrow("crates/uc-types/src/lib.rs", BoundaryRole::Editable);
        assert!(matches!(
            widening,
            Err(BoundaryContractError::NotNarrowing { .. })
        ));

        let missing = contract().narrow("crates/uc-types/src/nope.rs", BoundaryRole::Protected);
        assert!(matches!(
            missing,
            Err(BoundaryContractError::UnknownPath { .. })
        ));
    }

    #[test]
    fn update_bumps_version_and_applies_batch() {
        let mut contract = contract();
        let version = contract.update([
            (
                "crates/uc-types/src/agent.rs".to_string(),
                BoundaryRole::Protected,
            ),
            (
                "crates/uc-types/src/new.rs".to_string(),
                BoundaryRole::Editable,
            ),
        ]);
        assert_eq!(version, 2);
        assert_eq!(contract.version, 2);
        assert_eq!(
            contract.role_for("crates/uc-types/src/agent.rs"),
            BoundaryRole::Protected
        );
        assert_eq!(
            contract.role_for("crates/uc-types/src/new.rs"),
            BoundaryRole::Editable
        );
    }
}
