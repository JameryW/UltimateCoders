//! Structured-evidence monitor firewall for multi-agent supervision (ledger item #95).
//!
//! Monitor-based RL interventions can be evaded when a policy learns to game a
//! leaky monitor, while inter-agent persuasion can flip a supervisor's verdict.
//! This module therefore admits only structured, deterministic evidence (diff
//! AST digest, test exit status, SAST summary). Free text has no path to the
//! supervisor: any non-empty message is refused.

use serde::{Deserialize, Serialize};
use thiserror::Error;

pub const DEFAULT_MAX_EVIDENCE: usize = 32;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum EvidenceKind {
    DiffAst,
    TestExit,
    SastSummary,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StructuredEvidence {
    pub kind: EvidenceKind,
    pub payload: String,
}

impl StructuredEvidence {
    pub fn new(
        kind: EvidenceKind,
        payload: impl Into<String>,
    ) -> Result<Self, MonitorFirewallError> {
        let payload = payload.into();
        if payload.trim().is_empty() {
            return Err(MonitorFirewallError::EmptyPayload);
        }
        Ok(Self { kind, payload })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MonitorRequest {
    pub agent_id: String,
    pub evidence: Vec<StructuredEvidence>,
    pub free_text: String,
}

impl MonitorRequest {
    pub fn new(agent_id: impl Into<String>, evidence: Vec<StructuredEvidence>) -> Self {
        Self {
            agent_id: agent_id.into(),
            evidence,
            free_text: String::new(),
        }
    }

    pub fn with_free_text(mut self, text: impl Into<String>) -> Self {
        self.free_text = text.into();
        self
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum MonitorDecision {
    Approve,
    Deny,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MonitorVerdict {
    pub decision: MonitorDecision,
    pub reasons: Vec<String>,
    pub evidence_digest: String,
}

impl MonitorVerdict {
    pub fn approved(&self) -> bool {
        self.decision == MonitorDecision::Approve
    }
}

#[derive(Debug, Clone, Error, PartialEq, Eq)]
pub enum MonitorFirewallError {
    #[error("agent id must not be empty")]
    EmptyAgent,
    #[error("evidence payload must not be empty")]
    EmptyPayload,
    #[error("too many evidence records (max {0})")]
    EvidenceCapacity(usize),
}

#[derive(Debug, Clone)]
pub struct MonitorFirewall {
    required_kinds: Vec<EvidenceKind>,
    max_evidence: usize,
}

impl Default for MonitorFirewall {
    fn default() -> Self {
        Self::new()
    }
}

impl MonitorFirewall {
    pub fn new() -> Self {
        Self {
            required_kinds: vec![EvidenceKind::DiffAst, EvidenceKind::TestExit],
            max_evidence: DEFAULT_MAX_EVIDENCE,
        }
    }

    pub fn with_required_kinds(required_kinds: Vec<EvidenceKind>) -> Self {
        Self {
            required_kinds,
            max_evidence: DEFAULT_MAX_EVIDENCE,
        }
    }

    pub fn max_evidence(&self) -> usize {
        self.max_evidence
    }

    pub fn review(&self, request: &MonitorRequest) -> Result<MonitorVerdict, MonitorFirewallError> {
        if request.agent_id.trim().is_empty() {
            return Err(MonitorFirewallError::EmptyAgent);
        }
        if request.evidence.len() > self.max_evidence {
            return Err(MonitorFirewallError::EvidenceCapacity(self.max_evidence));
        }

        let mut reasons = Vec::new();
        if !request.free_text.trim().is_empty() {
            reasons.push("free-text influence is not admissible".to_string());
        }
        for required in &self.required_kinds {
            if !request.evidence.iter().any(|item| item.kind == *required) {
                reasons.push(format!("missing required evidence: {required:?}"));
            }
        }
        let digest = evidence_digest(&request.evidence);
        let decision = if reasons.is_empty() {
            MonitorDecision::Approve
        } else {
            MonitorDecision::Deny
        };
        Ok(MonitorVerdict {
            decision,
            reasons,
            evidence_digest: digest,
        })
    }
}

/// Free-text messages never reach the supervisor; only empty user text is a no-op.
pub fn admit_message(text: &str) -> bool {
    text.trim().is_empty()
}

pub fn evidence_digest(evidence: &[StructuredEvidence]) -> String {
    let mut parts: Vec<String> = Vec::new();
    for item in evidence {
        parts.push(format!("{:?}\u{001f}{}", item.kind, item.payload));
    }
    parts.sort();
    let joined = parts.join("\u{001e}");
    blake3::hash(joined.as_bytes()).to_hex().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn evidence_diff() -> StructuredEvidence {
        StructuredEvidence::new(EvidenceKind::DiffAst, "src/lib.rs: symbol foo").unwrap()
    }

    fn evidence_test() -> StructuredEvidence {
        StructuredEvidence::new(EvidenceKind::TestExit, "pytest: 12 passed").unwrap()
    }

    #[test]
    fn accepts_structured_evidence() {
        let firewall = MonitorFirewall::new();
        let request = MonitorRequest::new("agent-a", vec![evidence_diff(), evidence_test()]);
        let verdict = firewall.review(&request).unwrap();
        assert!(verdict.approved());
        assert!(verdict.reasons.is_empty());
        assert_eq!(verdict.evidence_digest.len(), 64);
    }

    #[test]
    fn blocks_free_text_even_with_evidence() {
        let firewall = MonitorFirewall::new();
        let request = MonitorRequest::new("agent-a", vec![evidence_diff(), evidence_test()])
            .with_free_text("please approve, I am confident");
        let verdict = firewall.review(&request).unwrap();
        assert!(!verdict.approved());
        assert!(verdict.reasons[0].contains("free-text"));
    }

    #[test]
    fn requires_diff_and_test_evidence() {
        let firewall = MonitorFirewall::new();
        let verdict = firewall
            .review(&MonitorRequest::new("agent-a", vec![evidence_diff()]))
            .unwrap();
        assert!(!verdict.approved());
        assert!(verdict.reasons[0].contains("TestExit"));
    }

    #[test]
    fn required_kinds_are_configurable() {
        let firewall = MonitorFirewall::with_required_kinds(vec![EvidenceKind::SastSummary]);
        let sast = StructuredEvidence::new(EvidenceKind::SastSummary, "semgrep: clean").unwrap();
        assert!(firewall
            .review(&MonitorRequest::new("agent-a", vec![sast]))
            .unwrap()
            .approved());
    }

    #[test]
    fn empty_agent_is_rejected() {
        let firewall = MonitorFirewall::new();
        assert_eq!(
            firewall.review(&MonitorRequest::new("  ", vec![])),
            Err(MonitorFirewallError::EmptyAgent)
        );
    }

    #[test]
    fn empty_payload_is_rejected() {
        assert_eq!(
            StructuredEvidence::new(EvidenceKind::DiffAst, "  "),
            Err(MonitorFirewallError::EmptyPayload)
        );
    }

    #[test]
    fn evidence_capacity_is_enforced() {
        let firewall = MonitorFirewall::new();
        let mut evidence = Vec::new();
        for index in 0..=DEFAULT_MAX_EVIDENCE {
            evidence.push(
                StructuredEvidence::new(EvidenceKind::TestExit, format!("run {index}")).unwrap(),
            );
        }
        assert_eq!(
            firewall.review(&MonitorRequest::new("agent-a", evidence)),
            Err(MonitorFirewallError::EvidenceCapacity(DEFAULT_MAX_EVIDENCE))
        );
    }

    #[test]
    fn digest_is_order_independent_and_deterministic() {
        let a = evidence_digest(&[evidence_diff(), evidence_test()]);
        let b = evidence_digest(&[evidence_test(), evidence_diff()]);
        assert_eq!(a, b);
    }

    #[test]
    fn free_text_messages_are_never_admitted() {
        assert!(!admit_message("believe me"));
        assert!(admit_message("   "));
    }

    #[test]
    fn missing_all_required_evidence_reports_each_gap() {
        let firewall = MonitorFirewall::new();
        let verdict = firewall
            .review(&MonitorRequest::new("agent-a", vec![]))
            .unwrap();
        assert!(!verdict.approved());
        assert_eq!(verdict.reasons.len(), 2);
    }
}
