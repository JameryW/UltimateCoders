//! Dual hard/soft rule audit with a hash-linked trail (V28 item #130).
//!
//! Hard rules are deterministic and can reject a proposal on their own.  Soft
//! rules are a cheap semantic pre-screen that flags disguised violations the
//! hard prefix rules miss.  Every decision is appended to a tamper-evident
//! chain so a later audit can reconstruct why a mutation was allowed.
//!
//! The ledger names Ed25519 for per-hop capability propagation.  This module
//! implements the same wire shape with a deterministic keyed BLAKE3 attestation
//! so it needs no new cryptography dependency; the field names and propagation
//! semantics match the design.  It is bookkeeping only and never executes the
//! proposed action.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Default score at which a soft rule blocks a proposal.
pub const DEFAULT_SOFT_THRESHOLD: f64 = 0.5;
/// Default bound on the audit trail.
pub const DEFAULT_MAX_AUDIT_ENTRIES: usize = 4096;
/// Marker for the first hop in a propagation chain.
pub const ROOT_SIGNATURE: &str = "0";

/// One proposed state mutation.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Proposal {
    pub proposal_id: String,
    pub actor: String,
    pub action: String,
    pub target: String,
    pub justification: String,
}

impl Proposal {
    /// Build a validated proposal.
    pub fn new(
        proposal_id: impl Into<String>,
        actor: impl Into<String>,
        action: impl Into<String>,
        target: impl Into<String>,
        justification: impl Into<String>,
    ) -> Result<Self, DualRuleError> {
        let proposal = Self {
            proposal_id: proposal_id.into(),
            actor: actor.into(),
            action: action.into(),
            target: target.into(),
            justification: justification.into(),
        };
        if proposal.proposal_id.trim().is_empty()
            || proposal.actor.trim().is_empty()
            || proposal.action.trim().is_empty()
            || proposal.target.trim().is_empty()
        {
            return Err(DualRuleError::EmptyField);
        }
        Ok(proposal)
    }
}

/// A deterministic hard rule.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HardRule {
    pub id: String,
    pub forbidden_action_prefix: Option<String>,
    pub protected_target_prefix: Option<String>,
}

impl HardRule {
    /// Forbid any action beginning with `prefix`.
    pub fn forbid_action(
        id: impl Into<String>,
        prefix: impl Into<String>,
    ) -> Result<Self, DualRuleError> {
        let id = id.into();
        let prefix = prefix.into();
        if id.trim().is_empty() || prefix.trim().is_empty() {
            return Err(DualRuleError::EmptyField);
        }
        Ok(Self {
            id,
            forbidden_action_prefix: Some(prefix),
            protected_target_prefix: None,
        })
    }

    /// Protect any target beginning with `prefix`.
    pub fn protect_target(
        id: impl Into<String>,
        prefix: impl Into<String>,
    ) -> Result<Self, DualRuleError> {
        let id = id.into();
        let prefix = prefix.into();
        if id.trim().is_empty() || prefix.trim().is_empty() {
            return Err(DualRuleError::EmptyField);
        }
        Ok(Self {
            id,
            forbidden_action_prefix: None,
            protected_target_prefix: Some(prefix),
        })
    }
}

/// A lightweight semantic rule.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SoftRule {
    pub id: String,
    pub suspicious_terms: Vec<String>,
    pub weight: f64,
}

impl SoftRule {
    /// Build a soft rule with a positive weight.
    pub fn new(
        id: impl Into<String>,
        suspicious_terms: Vec<String>,
        weight: f64,
    ) -> Result<Self, DualRuleError> {
        let id = id.into();
        if id.trim().is_empty() || suspicious_terms.is_empty() {
            return Err(DualRuleError::EmptyField);
        }
        if !weight.is_finite() || weight <= 0.0 {
            return Err(DualRuleError::InvalidWeight);
        }
        let terms = suspicious_terms
            .into_iter()
            .map(|term| term.trim().to_lowercase())
            .filter(|term| !term.is_empty())
            .collect::<Vec<_>>();
        if terms.is_empty() {
            return Err(DualRuleError::EmptyField);
        }
        Ok(Self {
            id,
            suspicious_terms: terms,
            weight,
        })
    }

    fn score(&self, text: &str) -> f64 {
        if self.suspicious_terms.iter().any(|term| text.contains(term)) {
            self.weight
        } else {
            0.0
        }
    }
}

/// A signed hop in a capability-propagation chain.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HopAttestation {
    pub actor: String,
    pub action: String,
    pub target: String,
    pub previous_signature: String,
    pub signature: String,
}

/// A bounded propagation token carried across agents.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PropagationToken {
    pub actor: String,
    pub scope: String,
    pub budget: u32,
    pub hops: Vec<HopAttestation>,
}

impl PropagationToken {
    /// Create a token with a positive hop budget.
    pub fn new(
        actor: impl Into<String>,
        scope: impl Into<String>,
        budget: u32,
    ) -> Result<Self, DualRuleError> {
        let actor = actor.into();
        let scope = scope.into();
        if actor.trim().is_empty() || scope.trim().is_empty() {
            return Err(DualRuleError::EmptyField);
        }
        if budget == 0 {
            return Err(DualRuleError::EmptyBudget);
        }
        Ok(Self {
            actor,
            scope,
            budget,
            hops: Vec::new(),
        })
    }

    /// Sign and append one hop.  The caller's secret is never stored.
    pub fn sign_hop(
        &mut self,
        secret: &str,
        action: impl Into<String>,
        target: impl Into<String>,
    ) -> Result<(), DualRuleError> {
        if self.hops.len() >= self.budget as usize {
            return Err(DualRuleError::BudgetExhausted);
        }
        let action = action.into();
        let target = target.into();
        if secret.is_empty() || action.trim().is_empty() || target.trim().is_empty() {
            return Err(DualRuleError::EmptyField);
        }
        let previous = self
            .hops
            .last()
            .map(|hop| hop.signature.clone())
            .unwrap_or_else(|| ROOT_SIGNATURE.to_string());
        let signature = hop_signature(secret, &self.actor, &action, &target, &previous);
        self.hops.push(HopAttestation {
            actor: self.actor.clone(),
            action,
            target,
            previous_signature: previous,
            signature,
        });
        Ok(())
    }

    /// Verify the complete chain against an actor-to-secret map.
    pub fn verify(&self, keys: &BTreeMap<String, String>) -> bool {
        let mut previous = ROOT_SIGNATURE.to_string();
        if self.hops.len() > self.budget as usize {
            return false;
        }
        for hop in &self.hops {
            if hop.previous_signature != previous {
                return false;
            }
            let Some(secret) = keys.get(&hop.actor) else {
                return false;
            };
            let expected = hop_signature(secret, &hop.actor, &hop.action, &hop.target, &previous);
            if expected != hop.signature {
                return false;
            }
            previous = hop.signature.clone();
        }
        true
    }
}

/// One entry in the audit trail.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AuditEntry {
    pub seq: u64,
    pub proposal_id: String,
    pub actor: String,
    pub action: String,
    pub target: String,
    pub allowed: bool,
    pub reasons: Vec<String>,
    pub previous_hash: String,
    pub entry_hash: String,
}

/// The result returned to the caller.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AuditDecision {
    pub allowed: bool,
    pub reasons: Vec<String>,
    pub entry_seq: u64,
}

/// Rule or input error.
#[derive(Debug, Error, Clone, PartialEq)]
pub enum DualRuleError {
    #[error("field must not be empty")]
    EmptyField,
    #[error("duplicate rule id: {0}")]
    DuplicateRule(String),
    #[error("soft-rule weight must be finite and positive")]
    InvalidWeight,
    #[error("soft threshold must be in [0, 1]")]
    InvalidThreshold,
    #[error("propagation budget must be positive")]
    EmptyBudget,
    #[error("propagation budget exhausted")]
    BudgetExhausted,
    #[error("audit trail capacity reached")]
    AuditCapacity,
}

/// Hard plus soft screening with a hash-linked audit trail.
#[derive(Debug, Clone)]
pub struct DualRuleAuditor {
    hard_rules: Vec<HardRule>,
    soft_rules: Vec<SoftRule>,
    soft_threshold: f64,
    max_entries: usize,
    entries: Vec<AuditEntry>,
    rule_ids: BTreeSet<String>,
}

impl DualRuleAuditor {
    /// Build an auditor with a soft threshold in `[0, 1]`.
    pub fn new(soft_threshold: f64) -> Result<Self, DualRuleError> {
        if !soft_threshold.is_finite() || !(0.0..=1.0).contains(&soft_threshold) {
            return Err(DualRuleError::InvalidThreshold);
        }
        Ok(Self {
            hard_rules: Vec::new(),
            soft_rules: Vec::new(),
            soft_threshold,
            max_entries: DEFAULT_MAX_AUDIT_ENTRIES,
            entries: Vec::new(),
            rule_ids: BTreeSet::new(),
        })
    }

    /// Default auditor: threshold 0.5, no rules.
    pub fn default_auditor() -> Self {
        Self::new(DEFAULT_SOFT_THRESHOLD).expect("default threshold is valid")
    }

    /// Add a hard rule.
    pub fn add_hard_rule(&mut self, rule: HardRule) -> Result<(), DualRuleError> {
        self.insert_rule_id(&rule.id)?;
        self.hard_rules.push(rule);
        Ok(())
    }

    /// Add a soft rule.
    pub fn add_soft_rule(&mut self, rule: SoftRule) -> Result<(), DualRuleError> {
        self.insert_rule_id(&rule.id)?;
        self.soft_rules.push(rule);
        Ok(())
    }

    /// Current audit entries.
    pub fn entries(&self) -> &[AuditEntry] {
        &self.entries
    }

    /// Verify the hash chain.
    pub fn verify_chain(&self) -> bool {
        let mut previous = "0".to_string();
        for entry in &self.entries {
            if entry.previous_hash != previous {
                return false;
            }
            let mut hasher = blake3::Hasher::new();
            hasher.update(entry.seq.to_string().as_bytes());
            hasher.update(b"\x1f");
            hasher.update(entry.proposal_id.as_bytes());
            hasher.update(b"\x1f");
            hasher.update(entry.actor.as_bytes());
            hasher.update(b"\x1f");
            hasher.update(entry.action.as_bytes());
            hasher.update(b"\x1f");
            hasher.update(entry.target.as_bytes());
            hasher.update(b"\x1f");
            hasher.update(if entry.allowed { b"1" } else { b"0" });
            hasher.update(b"\x1f");
            hasher.update(entry.previous_hash.as_bytes());
            if hasher.finalize().to_hex().to_string() != entry.entry_hash {
                return false;
            }
            previous = entry.entry_hash.clone();
        }
        true
    }

    /// Screen a proposal, optionally verifying a propagation token.
    pub fn authorize(
        &mut self,
        proposal: &Proposal,
        token: Option<&PropagationToken>,
        keys: Option<&BTreeMap<String, String>>,
    ) -> Result<AuditDecision, DualRuleError> {
        let mut reasons = Vec::new();

        if let Some(token) = token {
            if token.actor != proposal.actor {
                return Err(DualRuleError::EmptyField);
            }
            let valid = match keys {
                Some(keys) => token.verify(keys),
                None => false,
            };
            if !valid {
                reasons.push("propagation_invalid".to_string());
            }
            if token.hops.len() >= token.budget as usize {
                reasons.push("propagation_budget_exhausted".to_string());
            }
        }

        for rule in &self.hard_rules {
            if rule
                .forbidden_action_prefix
                .as_deref()
                .is_some_and(|prefix| proposal.action.starts_with(prefix))
            {
                reasons.push(format!("hard_forbidden_action:{}", rule.id));
            }
            if rule
                .protected_target_prefix
                .as_deref()
                .is_some_and(|prefix| proposal.target.starts_with(prefix))
            {
                reasons.push(format!("hard_protected_target:{}", rule.id));
            }
        }

        let mut soft_score = 0.0;
        let mut soft_hits = Vec::new();
        let text = format!(
            "{} {} {}",
            proposal.action.to_lowercase(),
            proposal.target.to_lowercase(),
            proposal.justification.to_lowercase()
        );
        for rule in &self.soft_rules {
            let score = rule.score(&text);
            if score > 0.0 {
                soft_score += score;
                soft_hits.push(format!("soft_rule:{}", rule.id));
            }
        }
        if soft_score >= self.soft_threshold {
            reasons.extend(soft_hits);
            reasons.push("soft_threshold_exceeded".to_string());
        }

        let allowed = reasons.is_empty();
        let seq = self.entries.len() as u64;
        let previous_hash = self
            .entries
            .last()
            .map(|entry| entry.entry_hash.clone())
            .unwrap_or_else(|| "0".to_string());
        let entry_hash = entry_digest(
            seq,
            &proposal.proposal_id,
            &proposal.actor,
            &proposal.action,
            &proposal.target,
            allowed,
            &previous_hash,
        );
        if self.entries.len() >= self.max_entries {
            self.entries.remove(0);
        }
        self.entries.push(AuditEntry {
            seq,
            proposal_id: proposal.proposal_id.clone(),
            actor: proposal.actor.clone(),
            action: proposal.action.clone(),
            target: proposal.target.clone(),
            allowed,
            reasons: reasons.clone(),
            previous_hash,
            entry_hash,
        });

        Ok(AuditDecision {
            allowed,
            reasons,
            entry_seq: seq,
        })
    }

    fn insert_rule_id(&mut self, id: &str) -> Result<(), DualRuleError> {
        if !self.rule_ids.insert(id.to_string()) {
            return Err(DualRuleError::DuplicateRule(id.to_string()));
        }
        Ok(())
    }
}

fn hop_signature(secret: &str, actor: &str, action: &str, target: &str, previous: &str) -> String {
    let mut hasher = blake3::Hasher::new_derive_key("uc-engine/propagation-hop/v1");
    hasher.update(secret.as_bytes());
    hasher.update(b"\x1f");
    hasher.update(actor.as_bytes());
    hasher.update(b"\x1f");
    hasher.update(action.as_bytes());
    hasher.update(b"\x1f");
    hasher.update(target.as_bytes());
    hasher.update(b"\x1f");
    hasher.update(previous.as_bytes());
    hasher.finalize().to_hex().to_string()
}

fn entry_digest(
    seq: u64,
    proposal_id: &str,
    actor: &str,
    action: &str,
    target: &str,
    allowed: bool,
    previous: &str,
) -> String {
    let mut hasher = blake3::Hasher::new();
    hasher.update(seq.to_string().as_bytes());
    hasher.update(b"\x1f");
    hasher.update(proposal_id.as_bytes());
    hasher.update(b"\x1f");
    hasher.update(actor.as_bytes());
    hasher.update(b"\x1f");
    hasher.update(action.as_bytes());
    hasher.update(b"\x1f");
    hasher.update(target.as_bytes());
    hasher.update(b"\x1f");
    hasher.update(if allowed { b"1" } else { b"0" });
    hasher.update(b"\x1f");
    hasher.update(previous.as_bytes());
    hasher.finalize().to_hex().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn proposal(action: &str, target: &str) -> Proposal {
        Proposal::new("p1", "agent-a", action, target, "routine update").expect("proposal")
    }

    #[test]
    fn hard_rule_blocks_forbidden_action() {
        let mut auditor = DualRuleAuditor::default_auditor();
        auditor
            .add_hard_rule(HardRule::forbid_action("no-delete", "delete").expect("rule"))
            .expect("add");
        let decision = auditor
            .authorize(&proposal("delete_file", "src/a.rs"), None, None)
            .expect("audit");
        assert!(!decision.allowed);
        assert!(decision
            .reasons
            .iter()
            .any(|r| r == "hard_forbidden_action:no-delete"));
        assert!(auditor.verify_chain());
    }

    #[test]
    fn hard_rule_blocks_protected_target() {
        let mut auditor = DualRuleAuditor::default_auditor();
        auditor
            .add_hard_rule(HardRule::protect_target("protect-tests", "tests/").expect("rule"))
            .expect("add");
        let decision = auditor
            .authorize(&proposal("edit", "tests/test_a.py"), None, None)
            .expect("audit");
        assert!(!decision.allowed);
        assert!(decision
            .reasons
            .iter()
            .any(|r| r == "hard_protected_target:protect-tests"));
    }

    #[test]
    fn soft_screen_blocks_disguised_violation() {
        let mut auditor = DualRuleAuditor::new(0.5).expect("auditor");
        auditor
            .add_soft_rule(
                SoftRule::new("skip-check", vec!["skip tests".into()], 0.6).expect("rule"),
            )
            .expect("add");
        let mut p = proposal("edit", "src/a.rs");
        p.justification = "skip tests to save time".into();
        let decision = auditor.authorize(&p, None, None).expect("audit");
        assert!(!decision.allowed);
        assert!(decision.reasons.iter().any(|r| r == "soft_rule:skip-check"));
    }

    #[test]
    fn soft_screen_allows_low_score() {
        let mut auditor = DualRuleAuditor::new(0.5).expect("auditor");
        auditor
            .add_soft_rule(SoftRule::new("hint", vec!["maybe".into()], 0.2).expect("rule"))
            .expect("add");
        let mut p = proposal("edit", "src/a.rs");
        p.justification = "maybe adjust a comment".into();
        let decision = auditor.authorize(&p, None, None).expect("audit");
        assert!(decision.allowed);
    }

    #[test]
    fn clean_proposal_is_allowed_and_recorded() {
        let mut auditor = DualRuleAuditor::default_auditor();
        let decision = auditor
            .authorize(&proposal("edit", "src/a.rs"), None, None)
            .expect("audit");
        assert!(decision.allowed);
        assert_eq!(auditor.entries().len(), 1);
        assert_eq!(auditor.entries()[0].seq, 0);
        assert!(auditor.verify_chain());
    }

    #[test]
    fn tampered_chain_is_detected() {
        let mut auditor = DualRuleAuditor::default_auditor();
        auditor
            .authorize(&proposal("edit", "src/a.rs"), None, None)
            .expect("audit");
        auditor.entries[0].action = "delete".to_string();
        assert!(!auditor.verify_chain());
    }

    #[test]
    fn propagation_chain_verifies_and_rejects_tampering() {
        let mut token = PropagationToken::new("agent-a", "write", 2).expect("token");
        token.sign_hop("secret-a", "edit", "src/a.rs").expect("hop");
        let mut keys = BTreeMap::new();
        keys.insert("agent-a".to_string(), "secret-a".to_string());
        assert!(token.verify(&keys));
        token.hops[0].target = "src/b.rs".to_string();
        assert!(!token.verify(&keys));
    }

    #[test]
    fn token_actor_must_match_proposal_actor() {
        let mut auditor = DualRuleAuditor::default_auditor();
        let mut token = PropagationToken::new("agent-b", "write", 1).expect("token");
        token.sign_hop("secret-b", "edit", "src/a.rs").expect("hop");
        let mut keys = BTreeMap::new();
        keys.insert("agent-b".to_string(), "secret-b".to_string());
        let error = auditor
            .authorize(&proposal("edit", "src/a.rs"), Some(&token), Some(&keys))
            .expect_err("actor mismatch");
        assert!(matches!(error, DualRuleError::EmptyField));
    }

    #[test]
    fn invalid_propagation_is_recorded_as_denied() {
        let mut auditor = DualRuleAuditor::default_auditor();
        let mut token = PropagationToken::new("agent-a", "write", 1).expect("token");
        token.sign_hop("secret-a", "edit", "src/a.rs").expect("hop");
        let decision = auditor
            .authorize(&proposal("edit", "src/a.rs"), Some(&token), None)
            .expect("audit");
        assert!(!decision.allowed);
        assert!(decision.reasons.iter().any(|r| r == "propagation_invalid"));
    }

    #[test]
    fn budget_and_duplicate_rules_are_validated() {
        let mut token = PropagationToken::new("agent-a", "write", 1).expect("token");
        token.sign_hop("secret-a", "edit", "src/a.rs").expect("hop");
        assert!(matches!(
            token.sign_hop("secret-a", "edit", "src/b.rs"),
            Err(DualRuleError::BudgetExhausted)
        ));
        let mut auditor = DualRuleAuditor::default_auditor();
        auditor
            .add_hard_rule(HardRule::forbid_action("dup", "delete").expect("rule"))
            .expect("add");
        assert!(matches!(
            auditor.add_soft_rule(SoftRule::new("dup", vec!["x".into()], 0.5).expect("rule")),
            Err(DualRuleError::DuplicateRule(_))
        ));
    }
}
