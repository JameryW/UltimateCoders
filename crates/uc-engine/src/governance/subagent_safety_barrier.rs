//! Weakest-subagent safety barrier and coordination-tax gate (ledger item #136).
//!
//! A strong orchestrator that delegates evidence collection to a weaker child
//! can still be poisoned: the child summarizes untrusted files and the parent
//! repeats the summary.  The barrier enforces two hard rules:
//!
//! 1. A sensitive task that reads untrusted files may not be delegated to a
//!    weak subagent unless the raw evidence is returned to the orchestrator.
//! 2. A fan-out is allowed only when at least one compensating condition is
//!    present: partitioned evidence, external verification, or heterogeneous
//!    model-family errors.  Otherwise it folds to one deep agent.
//!
//! Deterministic policy only; it does not call a model or dispatch work.

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Capability tier of the worker that would receive the delegation.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum SubagentTier {
    /// The strongest available model family.
    Strong,
    /// A mid-tier model.
    Medium,
    /// A small or otherwise weak model.
    Weak,
}

/// How evidence crosses the delegation boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum EvidenceMode {
    /// The child returns raw, signed evidence.
    Raw,
    /// The child returns a partition-scoped summary.
    Partitioned,
    /// The child's work is checked by an independent verifier.
    ExternalVerified,
}

/// A proposed delegation.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DelegationRequest {
    /// Stable task id (non-empty).
    pub task_id: String,
    /// The task touches sensitive code, secrets, or security boundaries.
    pub sensitive: bool,
    /// The task must read untrusted external files.
    pub reads_untrusted: bool,
    /// Capability tier of the proposed child.
    pub child_tier: SubagentTier,
    /// How the child returns evidence.
    pub evidence_mode: EvidenceMode,
    /// Whether the parent intends to fan out to more than one child.
    pub fan_out: bool,
    /// Evidence can be split by independent scope.
    pub evidence_partitioned: bool,
    /// An independent verifier will check the result.
    pub external_verified: bool,
    /// Errors are expected to be diverse across model families.
    pub heterogeneous_error_diversity: bool,
}

impl DelegationRequest {
    /// Build a request with the most conservative defaults.
    pub fn new(task_id: impl Into<String>, child_tier: SubagentTier) -> Self {
        Self {
            task_id: task_id.into(),
            sensitive: false,
            reads_untrusted: false,
            child_tier,
            evidence_mode: EvidenceMode::Raw,
            fan_out: false,
            evidence_partitioned: false,
            external_verified: false,
            heterogeneous_error_diversity: false,
        }
    }

    /// Mark the task as sensitive and reading untrusted content.
    pub fn sensitive_untrusted(mut self) -> Self {
        self.sensitive = true;
        self.reads_untrusted = true;
        self
    }

    /// Set the evidence mode.
    pub fn with_evidence(mut self, mode: EvidenceMode) -> Self {
        self.evidence_mode = mode;
        self
    }

    /// Enable fan-out and declare one compensating condition.
    pub fn fan_out_with(mut self, partitioned: bool, verified: bool, diverse: bool) -> Self {
        self.fan_out = true;
        self.evidence_partitioned = partitioned;
        self.external_verified = verified;
        self.heterogeneous_error_diversity = diverse;
        self
    }

    /// Whether any compensating condition for fan-out is present.
    pub fn coordination_benefit(&self) -> bool {
        self.evidence_partitioned || self.external_verified || self.heterogeneous_error_diversity
    }
}

/// The barrier's decision.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DelegationDecision {
    /// Delegation is allowed as proposed.
    pub allowed: bool,
    /// The proposal was folded into a single deep agent.
    pub folded_to_single: bool,
    /// The raw evidence must be returned to the orchestrator.
    pub raw_evidence_required: bool,
    /// Stable reason codes, in deterministic order.
    pub reasons: Vec<String>,
}

impl DelegationDecision {
    /// The proposal is allowed without folding.
    pub fn straight_through(&self) -> bool {
        self.allowed && !self.folded_to_single
    }
}

/// The barrier.
#[derive(Debug, Clone)]
pub struct SubagentSafetyBarrier {
    max_fanout: usize,
}

impl Default for SubagentSafetyBarrier {
    fn default() -> Self {
        Self { max_fanout: 8 }
    }
}

impl SubagentSafetyBarrier {
    /// Build a barrier with a positive fan-out ceiling.
    pub fn new(max_fanout: usize) -> Result<Self, SubagentSafetyError> {
        if max_fanout == 0 {
            return Err(SubagentSafetyError::InvalidFanout);
        }
        Ok(Self { max_fanout })
    }

    /// Maximum allowed fan-out.
    pub fn max_fanout(&self) -> usize {
        self.max_fanout
    }

    /// Evaluate a delegation request.
    pub fn evaluate(
        &self,
        request: &DelegationRequest,
    ) -> Result<DelegationDecision, SubagentSafetyError> {
        if request.task_id.trim().is_empty() {
            return Err(SubagentSafetyError::EmptyTask);
        }
        if self.max_fanout == 0 {
            return Err(SubagentSafetyError::InvalidFanout);
        }

        let mut reasons = Vec::new();
        let weak = matches!(request.child_tier, SubagentTier::Weak);
        let untrusted = request.sensitive && request.reads_untrusted;

        if untrusted && weak && request.evidence_mode != EvidenceMode::Raw {
            reasons.push("weak_child_untrusted_evidence".to_string());
            return Ok(DelegationDecision {
                allowed: false,
                folded_to_single: true,
                raw_evidence_required: true,
                reasons,
            });
        }

        if request.fan_out && !request.coordination_benefit() {
            reasons.push("coordination_tax_without_benefit".to_string());
            return Ok(DelegationDecision {
                allowed: true,
                folded_to_single: true,
                raw_evidence_required: untrusted,
                reasons,
            });
        }

        if request.fan_out && self.max_fanout < 2 {
            reasons.push("fanout_ceiling".to_string());
            return Ok(DelegationDecision {
                allowed: false,
                folded_to_single: true,
                raw_evidence_required: untrusted,
                reasons,
            });
        }

        if untrusted && weak {
            reasons.push("raw_evidence_required".to_string());
        } else if request.fan_out {
            reasons.push("coordination_benefit_present".to_string());
        }

        Ok(DelegationDecision {
            allowed: true,
            folded_to_single: false,
            raw_evidence_required: untrusted && weak,
            reasons,
        })
    }

    /// Collapse a request to one deep agent, retaining the raw-evidence rule.
    pub fn fold(&self, request: &DelegationRequest) -> DelegationDecision {
        let mut reasons = vec!["folded_to_single".to_string()];
        if request.sensitive && request.reads_untrusted {
            reasons.push("raw_evidence_required".to_string());
        }
        DelegationDecision {
            allowed: true,
            folded_to_single: true,
            raw_evidence_required: request.sensitive && request.reads_untrusted,
            reasons,
        }
    }
}

/// Errors returned by the barrier.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum SubagentSafetyError {
    /// A task id was blank.
    #[error("task id must not be empty")]
    EmptyTask,
    /// Fan-out ceiling was zero.
    #[error("fan-out ceiling must be positive")]
    InvalidFanout,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strong_child_passes_sensitive_raw_evidence() {
        let barrier = SubagentSafetyBarrier::default();
        let request = DelegationRequest::new("t1", SubagentTier::Strong).sensitive_untrusted();
        let decision = barrier.evaluate(&request).expect("decision");
        assert!(decision.allowed);
        assert!(!decision.folded_to_single);
        assert!(!decision.raw_evidence_required);
    }

    #[test]
    fn weak_child_on_untrusted_content_must_be_folded() {
        let barrier = SubagentSafetyBarrier::default();
        let request = DelegationRequest::new("t1", SubagentTier::Weak)
            .sensitive_untrusted()
            .with_evidence(EvidenceMode::Partitioned);
        let decision = barrier.evaluate(&request).expect("decision");
        assert!(!decision.allowed);
        assert!(decision.folded_to_single);
        assert!(decision.raw_evidence_required);
        assert!(decision
            .reasons
            .contains(&"weak_child_untrusted_evidence".to_string()));
    }

    #[test]
    fn weak_child_with_raw_evidence_is_allowed_but_marks_raw_requirement() {
        let barrier = SubagentSafetyBarrier::default();
        let request = DelegationRequest::new("t1", SubagentTier::Weak).sensitive_untrusted();
        let decision = barrier.evaluate(&request).expect("decision");
        assert!(decision.allowed);
        assert!(decision.raw_evidence_required);
    }

    #[test]
    fn fan_out_without_a_compensating_condition_is_folded() {
        let barrier = SubagentSafetyBarrier::default();
        let request =
            DelegationRequest::new("t1", SubagentTier::Strong).fan_out_with(false, false, false);
        let decision = barrier.evaluate(&request).expect("decision");
        assert!(decision.allowed);
        assert!(decision.folded_to_single);
        assert!(decision
            .reasons
            .contains(&"coordination_tax_without_benefit".to_string()));
    }

    #[test]
    fn fan_out_with_partitioned_evidence_is_allowed() {
        let barrier = SubagentSafetyBarrier::default();
        let request =
            DelegationRequest::new("t1", SubagentTier::Strong).fan_out_with(true, false, false);
        let decision = barrier.evaluate(&request).expect("decision");
        assert!(decision.straight_through());
        assert!(decision
            .reasons
            .contains(&"coordination_benefit_present".to_string()));
    }

    #[test]
    fn fan_out_with_external_verification_is_allowed() {
        let barrier = SubagentSafetyBarrier::default();
        let request =
            DelegationRequest::new("t1", SubagentTier::Medium).fan_out_with(false, true, false);
        assert!(barrier
            .evaluate(&request)
            .expect("decision")
            .straight_through());
    }

    #[test]
    fn fan_out_with_heterogeneous_errors_is_allowed() {
        let barrier = SubagentSafetyBarrier::default();
        let request =
            DelegationRequest::new("t1", SubagentTier::Medium).fan_out_with(false, false, true);
        assert!(barrier
            .evaluate(&request)
            .expect("decision")
            .straight_through());
    }

    #[test]
    fn fanout_ceiling_of_one_blocks_parallel_delegation() {
        let barrier = SubagentSafetyBarrier::new(1).expect("barrier");
        let request =
            DelegationRequest::new("t1", SubagentTier::Strong).fan_out_with(true, false, false);
        let decision = barrier.evaluate(&request).expect("decision");
        assert!(!decision.allowed);
        assert!(decision.reasons.contains(&"fanout_ceiling".to_string()));
    }

    #[test]
    fn empty_task_and_zero_ceiling_are_rejected() {
        let barrier = SubagentSafetyBarrier::default();
        let empty = DelegationRequest::new("", SubagentTier::Strong);
        assert_eq!(
            barrier.evaluate(&empty),
            Err(SubagentSafetyError::EmptyTask)
        );
        assert_eq!(
            SubagentSafetyBarrier::new(0).err(),
            Some(SubagentSafetyError::InvalidFanout)
        );
    }

    #[test]
    fn fold_is_always_allowed_and_preserves_raw_evidence() {
        let barrier = SubagentSafetyBarrier::default();
        let request = DelegationRequest::new("t1", SubagentTier::Weak)
            .sensitive_untrusted()
            .fan_out_with(false, false, false);
        let decision = barrier.fold(&request);
        assert!(decision.allowed);
        assert!(decision.folded_to_single);
        assert!(decision.raw_evidence_required);
        assert!(decision.reasons.contains(&"folded_to_single".to_string()));
    }

    #[test]
    fn decision_serialises_deterministically() {
        let barrier = SubagentSafetyBarrier::default();
        let request = DelegationRequest::new("t1", SubagentTier::Strong);
        let decision = barrier.evaluate(&request).expect("decision");
        let first = serde_json::to_string(&decision).expect("json");
        let second = serde_json::to_string(&decision).expect("json");
        assert_eq!(first, second);
        assert!(first.contains("\"allowed\":true"));
    }
}
