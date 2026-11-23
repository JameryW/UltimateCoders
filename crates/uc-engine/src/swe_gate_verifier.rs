//! SWE-Gate (#52): functional tests plus independently executable review
//! constraints, and the clean-room reward-hacking scrubber.
//!
//! The module is deliberately a verifier only. It never runs tests, mutates a
//! sandbox, or rewrites a patch; callers supply functional evidence, observed
//! constraint evidence, and the sandbox's exposed-artifact flags. The verifier
//! combines them into a single resolved gate and reports every blocking reason.

use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use thiserror::Error;

/// The default upper bound for one verifier's review-constraint suite.
pub const DEFAULT_MAX_CONSTRAINTS: usize = 256;

/// The engineering dimension a review constraint protects.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ConstraintKind {
    /// Module/layer direction, for example "api must not import storage".
    ArchitectureLayering,
    /// Public API shape and backwards compatibility.
    ApiCompatibility,
    /// A measured complexity ceiling (cyclomatic complexity or call depth).
    ComplexityBound,
    /// Calls that must never appear in the patch.
    ForbiddenCall,
    /// A security or data-handling invariant.
    SecurityInvariant,
}

/// One non-functional review constraint extracted from a real review comment.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReviewConstraint {
    /// Stable id used by the constraint test and the report.
    pub id: String,
    /// The engineering dimension.
    pub kind: ConstraintKind,
    /// Human-readable requirement.
    pub description: String,
    /// Optional complexity ceiling for `ComplexityBound`.
    pub max_complexity: Option<u32>,
    /// Optional text that must not appear in the patch.
    pub forbidden_pattern: Option<String>,
    /// Optional text that must appear in the patch.
    pub required_pattern: Option<String>,
}

impl ReviewConstraint {
    /// Create a constraint with no automatic patch predicate.
    pub fn new(
        id: impl Into<String>,
        kind: ConstraintKind,
        description: impl Into<String>,
    ) -> Result<Self, SweGateError> {
        let id = id.into();
        let description = description.into();
        require_non_empty("id", &id)?;
        require_non_empty("description", &description)?;
        Ok(Self {
            id,
            kind,
            description,
            max_complexity: None,
            forbidden_pattern: None,
            required_pattern: None,
        })
    }

    /// Attach a measured complexity ceiling.
    pub fn with_max_complexity(mut self, max_complexity: u32) -> Self {
        self.max_complexity = Some(max_complexity);
        self
    }

    /// Reject a patch that contains this text.
    pub fn forbids(mut self, pattern: impl Into<String>) -> Self {
        self.forbidden_pattern = Some(pattern.into());
        self
    }

    /// Require the patch to contain this text.
    pub fn requires(mut self, pattern: impl Into<String>) -> Self {
        self.required_pattern = Some(pattern.into());
        self
    }
}

/// The observed outcome for one review constraint.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ConstraintCheck {
    /// The constraint this observation belongs to.
    pub constraint_id: String,
    /// Whether the independent constraint test passed.
    pub passed: bool,
    /// Short evidence string retained for the gate report.
    pub evidence: String,
}

impl ConstraintCheck {
    /// Build an observation.
    pub fn new(
        constraint_id: impl Into<String>,
        passed: bool,
        evidence: impl Into<String>,
    ) -> Result<Self, SweGateError> {
        let constraint_id = constraint_id.into();
        let evidence = evidence.into();
        require_non_empty("constraint_id", &constraint_id)?;
        Ok(Self {
            constraint_id,
            passed,
            evidence,
        })
    }
}

/// Functional-test evidence, independent of review constraints.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct FunctionalResult {
    /// Tests that passed.
    pub passed: u32,
    /// Tests that failed.
    pub failed: u32,
}

impl FunctionalResult {
    /// A result is usable only when at least one test ran and none failed.
    pub fn clean(self) -> bool {
        self.passed > 0 && self.failed == 0
    }
}

/// Artifacts that can leak the gold patch or the tests into a sandbox.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct RewardHackSignals {
    /// `.git/logs/*` or reflog output is reachable.
    pub git_logs_exposed: bool,
    /// Remote tags or future commit refs are reachable.
    pub remote_tags_exposed: bool,
    /// A future commit object is reachable.
    pub future_commits_exposed: bool,
    /// Test files contain inline expected answers.
    pub inline_test_answers: bool,
}

impl RewardHackSignals {
    /// No leak channel is exposed.
    pub fn clean(self) -> bool {
        !(self.git_logs_exposed
            || self.remote_tags_exposed
            || self.future_commits_exposed
            || self.inline_test_answers)
    }
}

/// The final gate decision.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GateReport {
    /// Functional tests all passed.
    pub functional_passed: bool,
    /// Every observed review constraint passed.
    pub constraints_passed: bool,
    /// The patch is resolved only when both dimensions and the scrubber pass.
    pub resolved: bool,
    /// Constraint ids that failed or were missing.
    pub failed_constraints: Vec<String>,
    /// Leak channels observed in the sandbox.
    pub reward_hack: RewardHackSignals,
    /// Reasons the gate is blocked; empty when resolved.
    pub reasons: Vec<String>,
}

impl GateReport {
    /// True when neither functional nor review evidence is missing.
    pub fn dual_gate_passed(&self) -> bool {
        self.functional_passed && self.constraints_passed
    }
}

/// Errors raised while building or evaluating a gate.
#[derive(Debug, Error, PartialEq, Eq)]
pub enum SweGateError {
    /// A required field was empty.
    #[error("field `{0}` must not be empty")]
    EmptyField(&'static str),
    /// Two constraints use the same id.
    #[error("duplicate constraint id `{0}`")]
    DuplicateConstraint(String),
    /// The constraint suite is full.
    #[error("constraint capacity reached")]
    CapacityReached,
    /// A check names a constraint the verifier does not own.
    #[error("unknown constraint `{0}`")]
    UnknownConstraint(String),
}

/// A reusable dual-dimension gate over a fixed constraint suite.
#[derive(Debug, Clone)]
pub struct SweGateVerifier {
    constraints: Vec<ReviewConstraint>,
    capacity: usize,
}

impl SweGateVerifier {
    /// Build a verifier with the default capacity.
    pub fn new(constraints: Vec<ReviewConstraint>) -> Result<Self, SweGateError> {
        Self::with_capacity(constraints, DEFAULT_MAX_CONSTRAINTS)
    }

    /// Build a verifier with an explicit capacity.
    pub fn with_capacity(
        constraints: Vec<ReviewConstraint>,
        capacity: usize,
    ) -> Result<Self, SweGateError> {
        if constraints.len() > capacity {
            return Err(SweGateError::CapacityReached);
        }
        let mut ids = BTreeSet::new();
        for constraint in &constraints {
            require_non_empty("id", &constraint.id)?;
            require_non_empty("description", &constraint.description)?;
            if !ids.insert(constraint.id.clone()) {
                return Err(SweGateError::DuplicateConstraint(constraint.id.clone()));
            }
        }
        Ok(Self {
            constraints,
            capacity,
        })
    }

    /// The configured constraints.
    pub fn constraints(&self) -> &[ReviewConstraint] {
        &self.constraints
    }

    /// Add one constraint.
    pub fn register(&mut self, constraint: ReviewConstraint) -> Result<(), SweGateError> {
        require_non_empty("id", &constraint.id)?;
        require_non_empty("description", &constraint.description)?;
        if self.constraints.len() >= self.capacity {
            return Err(SweGateError::CapacityReached);
        }
        if self
            .constraints
            .iter()
            .any(|existing| existing.id == constraint.id)
        {
            return Err(SweGateError::DuplicateConstraint(constraint.id));
        }
        self.constraints.push(constraint);
        Ok(())
    }

    /// Check the automatically evaluable predicates on a patch. Complexity
    /// constraints are skipped here because they need a measured value.
    pub fn check_patch(&self, patch: &str) -> Vec<ConstraintCheck> {
        self.constraints
            .iter()
            .filter(|constraint| constraint.kind != ConstraintKind::ComplexityBound)
            .filter_map(|constraint| {
                let mut passed = true;
                let mut evidence = String::new();
                if let Some(pattern) = &constraint.forbidden_pattern {
                    if !pattern.is_empty() && patch.contains(pattern) {
                        passed = false;
                        evidence = format!("forbidden pattern `{pattern}` present");
                    }
                }
                if let Some(pattern) = &constraint.required_pattern {
                    if !pattern.is_empty() && !patch.contains(pattern) {
                        passed = false;
                        if !evidence.is_empty() {
                            evidence.push_str("; ");
                        }
                        evidence.push_str(&format!("required pattern `{pattern}` missing"));
                    }
                }
                ConstraintCheck::new(constraint.id.clone(), passed, evidence).ok()
            })
            .collect()
    }

    /// Evaluate a measured complexity value for one constraint.
    pub fn check_complexity(
        &self,
        constraint_id: &str,
        observed: u32,
    ) -> Result<ConstraintCheck, SweGateError> {
        let constraint = self
            .constraints
            .iter()
            .find(|constraint| constraint.id == constraint_id)
            .ok_or_else(|| SweGateError::UnknownConstraint(constraint_id.to_string()))?;
        let max = constraint.max_complexity.ok_or_else(|| {
            SweGateError::UnknownConstraint(format!("{constraint_id} has no complexity ceiling"))
        })?;
        let passed = observed <= max;
        let evidence = if passed {
            format!("complexity {observed} <= {max}")
        } else {
            format!("complexity {observed} > {max}")
        };
        ConstraintCheck::new(constraint_id, passed, evidence)
    }

    /// Combine functional evidence, constraint observations, and sandbox
    /// artifacts. A missing observation blocks the gate.
    pub fn evaluate(
        &self,
        functional: FunctionalResult,
        observations: &[ConstraintCheck],
        reward_hack: RewardHackSignals,
    ) -> GateReport {
        let mut reasons = Vec::new();
        let functional_passed = functional.clean();
        if !functional_passed {
            if functional.passed == 0 {
                reasons.push("no functional tests ran".to_string());
            }
            if functional.failed > 0 {
                reasons.push(format!("{} functional tests failed", functional.failed));
            }
        }

        let mut failed_constraints = Vec::new();
        for constraint in &self.constraints {
            let observation = observations
                .iter()
                .find(|observation| observation.constraint_id == constraint.id);
            match observation {
                Some(observation) if observation.passed => {}
                Some(observation) => {
                    failed_constraints.push(constraint.id.clone());
                    reasons.push(format!(
                        "constraint `{}` failed: {}",
                        constraint.id, observation.evidence
                    ));
                }
                None => {
                    failed_constraints.push(constraint.id.clone());
                    reasons.push(format!("constraint `{}` has no observation", constraint.id));
                }
            }
        }
        if !reward_hack.clean() {
            reasons.push("sandbox exposes a gold-patch or test-answer channel".to_string());
        }

        let constraints_passed = failed_constraints.is_empty();
        let resolved = functional_passed && constraints_passed && reward_hack.clean();
        GateReport {
            functional_passed,
            constraints_passed,
            resolved,
            failed_constraints,
            reward_hack,
            reasons,
        }
    }
}

/// The clean-room scrubber's report.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ScrubReport {
    /// All leak channels are removed.
    pub clean: bool,
    /// The leak channels that were present before scrubbing.
    pub removed: Vec<String>,
}

/// Deterministic pre-sandbox scrub policy for SWE-Bench Pro Verified.
#[derive(Debug, Clone, Copy, Default)]
pub struct RewardHackScrubber;

impl RewardHackScrubber {
    /// Report which channels the caller must remove before starting the
    /// sandbox. The scrubber never deletes files itself.
    pub fn scan(signals: RewardHackSignals) -> ScrubReport {
        let mut removed = Vec::new();
        if signals.git_logs_exposed {
            removed.push(".git/logs".to_string());
        }
        if signals.remote_tags_exposed {
            removed.push("remote-tags".to_string());
        }
        if signals.future_commits_exposed {
            removed.push("future-commits".to_string());
        }
        if signals.inline_test_answers {
            removed.push("inline-test-answers".to_string());
        }
        ScrubReport {
            clean: removed.is_empty(),
            removed,
        }
    }
}

fn require_non_empty(field: &'static str, value: &str) -> Result<(), SweGateError> {
    if value.trim().is_empty() {
        Err(SweGateError::EmptyField(field))
    } else {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn constraints() -> Vec<ReviewConstraint> {
        vec![
            ReviewConstraint::new(
                "layering",
                ConstraintKind::ArchitectureLayering,
                "api must not import storage",
            )
            .expect("constraint")
            .forbids("use crate::storage"),
            ReviewConstraint::new(
                "api-compat",
                ConstraintKind::ApiCompatibility,
                "keep the public signature",
            )
            .expect("constraint")
            .requires("pub fn fetch_user("),
            ReviewConstraint::new(
                "complexity",
                ConstraintKind::ComplexityBound,
                "keep complexity bounded",
            )
            .expect("constraint")
            .with_max_complexity(8),
        ]
    }

    fn passing_checks() -> Vec<ConstraintCheck> {
        vec![
            ConstraintCheck::new("layering", true, "no forbidden import").expect("check"),
            ConstraintCheck::new("api-compat", true, "signature retained").expect("check"),
            ConstraintCheck::new("complexity", true, "complexity 4 <= 8").expect("check"),
        ]
    }

    #[test]
    fn dual_gate_requires_both_functional_and_review_evidence() {
        let verifier = SweGateVerifier::new(constraints()).expect("verifier");
        let report = verifier.evaluate(
            FunctionalResult {
                passed: 3,
                failed: 0,
            },
            &passing_checks(),
            RewardHackSignals::default(),
        );
        assert!(report.resolved);
        assert!(report.dual_gate_passed());
        assert!(report.reasons.is_empty());
    }

    #[test]
    fn functional_success_alone_does_not_resolve_the_patch() {
        let verifier = SweGateVerifier::new(constraints()).expect("verifier");
        let report = verifier.evaluate(
            FunctionalResult {
                passed: 3,
                failed: 0,
            },
            &[],
            RewardHackSignals::default(),
        );
        assert!(!report.resolved);
        assert!(!report.constraints_passed);
        assert_eq!(report.failed_constraints.len(), 3);
    }

    #[test]
    fn failing_review_constraint_blocks_a_functionally_correct_patch() {
        let verifier = SweGateVerifier::new(constraints()).expect("verifier");
        let mut checks = passing_checks();
        checks[1] = ConstraintCheck::new("api-compat", false, "signature changed").expect("check");
        let report = verifier.evaluate(
            FunctionalResult {
                passed: 5,
                failed: 0,
            },
            &checks,
            RewardHackSignals::default(),
        );
        assert!(!report.resolved);
        assert_eq!(report.failed_constraints, vec!["api-compat"]);
        assert!(report
            .reasons
            .iter()
            .any(|reason| reason.contains("signature changed")));
    }

    #[test]
    fn missing_functional_evidence_blocks_resolution() {
        let verifier = SweGateVerifier::new(constraints()).expect("verifier");
        let report = verifier.evaluate(
            FunctionalResult {
                passed: 0,
                failed: 0,
            },
            &passing_checks(),
            RewardHackSignals::default(),
        );
        assert!(!report.resolved);
        assert!(report
            .reasons
            .iter()
            .any(|reason| reason.contains("no functional tests")));
    }

    #[test]
    fn failed_functional_test_blocks_resolution() {
        let verifier = SweGateVerifier::new(constraints()).expect("verifier");
        let report = verifier.evaluate(
            FunctionalResult {
                passed: 4,
                failed: 1,
            },
            &passing_checks(),
            RewardHackSignals::default(),
        );
        assert!(!report.resolved);
        assert!(report
            .reasons
            .iter()
            .any(|reason| reason.contains("1 functional")));
    }

    #[test]
    fn forbidden_pattern_and_required_pattern_are_checked() {
        let verifier = SweGateVerifier::new(constraints()).expect("verifier");
        let checks = verifier.check_patch("use crate::storage\nfn helper() {}\n");
        assert_eq!(checks.len(), 2);
        assert!(!checks[0].passed);
        assert!(!checks[1].passed);
        assert!(checks[0].evidence.contains("forbidden"));
        assert!(checks[1].evidence.contains("required"));
    }

    #[test]
    fn clean_patch_passes_the_automatic_predicates() {
        let verifier = SweGateVerifier::new(constraints()).expect("verifier");
        let checks = verifier.check_patch("pub fn fetch_user(id: u64) -> User { todo!() }");
        assert!(checks.iter().all(|check| check.passed));
    }

    #[test]
    fn complexity_is_checked_against_the_ceiling() {
        let verifier = SweGateVerifier::new(constraints()).expect("verifier");
        assert!(
            verifier
                .check_complexity("complexity", 8)
                .expect("check")
                .passed
        );
        assert!(
            !verifier
                .check_complexity("complexity", 9)
                .expect("check")
                .passed
        );
    }

    #[test]
    fn reward_hack_channel_blocks_even_a_clean_patch() {
        let verifier = SweGateVerifier::new(constraints()).expect("verifier");
        let report = verifier.evaluate(
            FunctionalResult {
                passed: 2,
                failed: 0,
            },
            &passing_checks(),
            RewardHackSignals {
                git_logs_exposed: true,
                ..RewardHackSignals::default()
            },
        );
        assert!(!report.resolved);
        assert!(report
            .reasons
            .iter()
            .any(|reason| reason.contains("gold-patch")));
    }

    #[test]
    fn scrubber_reports_every_exposed_channel() {
        let report = RewardHackScrubber::scan(RewardHackSignals {
            git_logs_exposed: true,
            remote_tags_exposed: true,
            future_commits_exposed: true,
            inline_test_answers: true,
        });
        assert!(!report.clean);
        assert_eq!(report.removed.len(), 4);
        assert!(RewardHackScrubber::scan(RewardHackSignals::default()).clean);
    }

    #[test]
    fn duplicate_and_unknown_constraints_are_rejected() {
        let constraint =
            ReviewConstraint::new("a", ConstraintKind::ApiCompatibility, "a").expect("constraint");
        let mut verifier = SweGateVerifier::new(vec![constraint.clone()]).expect("verifier");
        assert_eq!(
            verifier.register(constraint),
            Err(SweGateError::DuplicateConstraint("a".to_string()))
        );
        assert_eq!(
            verifier.check_complexity("missing", 1),
            Err(SweGateError::UnknownConstraint("missing".to_string()))
        );
    }

    #[test]
    fn empty_fields_and_capacity_are_rejected() {
        assert_eq!(
            ReviewConstraint::new("", ConstraintKind::ApiCompatibility, "x"),
            Err(SweGateError::EmptyField("id"))
        );
        let mut verifier = SweGateVerifier::with_capacity(Vec::new(), 0).expect("verifier");
        let constraint =
            ReviewConstraint::new("a", ConstraintKind::ApiCompatibility, "a").expect("constraint");
        assert_eq!(
            verifier.register(constraint),
            Err(SweGateError::CapacityReached)
        );
    }
}
