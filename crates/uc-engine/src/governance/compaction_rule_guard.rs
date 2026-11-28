//! Compaction-immune mandate rules for the governance plane.
//!
//! Ledger item #122 (ARCBench, arXiv:2611.03223). Context compaction can
//! preserve the text of a rule while still increasing violations, so hard
//! mandates are stored outside the compacted summary and re-checked by the
//! tool boundary. This module is deterministic: it never calls a model and
//! never executes a tool.

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Default bound on the registered mandate rules.
pub const DEFAULT_MAX_RULES: usize = 128;

/// One non-negotiable governance rule.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MandateRule {
    pub id: String,
    pub statement: String,
    pub forbidden_actions: Vec<String>,
    pub protected_paths: Vec<String>,
}

impl MandateRule {
    pub fn new(id: impl Into<String>, statement: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            statement: statement.into(),
            forbidden_actions: Vec::new(),
            protected_paths: Vec::new(),
        }
    }

    pub fn forbidding(mut self, action: impl Into<String>) -> Self {
        self.forbidden_actions.push(action.into());
        self
    }

    pub fn protecting(mut self, path: impl Into<String>) -> Self {
        self.protected_paths.push(path.into());
        self
    }

    fn validate(&self) -> Result<(), CompactionRuleError> {
        if self.id.trim().is_empty() {
            return Err(CompactionRuleError::EmptyField("id"));
        }
        if self.statement.trim().is_empty() {
            return Err(CompactionRuleError::EmptyField("statement"));
        }
        if self.forbidden_actions.iter().any(|a| a.trim().is_empty()) {
            return Err(CompactionRuleError::EmptyField("forbidden_action"));
        }
        if self.protected_paths.iter().any(|p| p.trim().is_empty()) {
            return Err(CompactionRuleError::EmptyField("protected_path"));
        }
        Ok(())
    }
}

/// Why a proposed tool action was rejected.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum RuleViolationKind {
    ForbiddenAction { action: String },
    ProtectedPath { path: String },
}

/// A single boundary rejection.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RuleViolation {
    pub rule_id: String,
    pub kind: RuleViolationKind,
    pub statement: String,
}

/// Result of re-anchoring all hard rules after a compaction.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompactionAnchor {
    pub rules: usize,
    pub missing: Vec<String>,
    pub anchored: bool,
    pub rendered: String,
}

/// Errors raised by the mandate registry.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum CompactionRuleError {
    #[error("empty {0}")]
    EmptyField(&'static str),
    #[error("duplicate mandate rule {0}")]
    DuplicateRule(String),
    #[error("mandate rule capacity reached")]
    CapacityReached,
    #[error("tool action blocked by mandate {rule_id}")]
    Blocked { rule_id: String },
}

/// Registry of hard rules kept outside compacted context.
#[derive(Debug, Clone)]
pub struct CompactionRuleGuard {
    rules: Vec<MandateRule>,
    max_rules: usize,
}

impl Default for CompactionRuleGuard {
    fn default() -> Self {
        Self::new(DEFAULT_MAX_RULES)
    }
}

impl CompactionRuleGuard {
    pub fn new(max_rules: usize) -> Self {
        Self {
            rules: Vec::new(),
            max_rules,
        }
    }

    pub fn register(&mut self, rule: MandateRule) -> Result<(), CompactionRuleError> {
        rule.validate()?;
        if self.rules.iter().any(|existing| existing.id == rule.id) {
            return Err(CompactionRuleError::DuplicateRule(rule.id));
        }
        if self.rules.len() >= self.max_rules {
            return Err(CompactionRuleError::CapacityReached);
        }
        self.rules.push(rule);
        Ok(())
    }

    pub fn rules(&self) -> &[MandateRule] {
        &self.rules
    }

    /// Re-attach every hard rule after a compaction summary was built.
    pub fn anchor_after_compaction(&self, summary: &str) -> CompactionAnchor {
        let lowered = summary.to_lowercase();
        let missing = self
            .rules
            .iter()
            .filter(|rule| !lowered.contains(&rule.statement.to_lowercase()))
            .map(|rule| rule.id.clone())
            .collect::<Vec<_>>();
        let rendered = self
            .rules
            .iter()
            .map(|rule| format!("- [{}] {}", rule.id, rule.statement))
            .collect::<Vec<_>>()
            .join("\n");
        CompactionAnchor {
            rules: self.rules.len(),
            missing: missing.clone(),
            anchored: missing.is_empty(),
            rendered,
        }
    }

    /// Check a proposed action against every rule without executing it.
    pub fn violations(&self, action: &str, target: &str) -> Vec<RuleViolation> {
        let action = action.trim();
        let target = target.trim();
        let mut violations = Vec::new();
        for rule in &self.rules {
            for forbidden in &rule.forbidden_actions {
                if forbidden.eq_ignore_ascii_case(action) {
                    violations.push(RuleViolation {
                        rule_id: rule.id.clone(),
                        kind: RuleViolationKind::ForbiddenAction {
                            action: action.to_string(),
                        },
                        statement: rule.statement.clone(),
                    });
                }
            }
            for protected in &rule.protected_paths {
                let protected = protected.trim_end_matches('/');
                if !protected.is_empty()
                    && (target == protected
                        || target.starts_with(&format!("{protected}/"))
                        || target.ends_with(&format!("/{protected}")))
                {
                    violations.push(RuleViolation {
                        rule_id: rule.id.clone(),
                        kind: RuleViolationKind::ProtectedPath {
                            path: target.to_string(),
                        },
                        statement: rule.statement.clone(),
                    });
                }
            }
        }
        violations
    }

    /// Boundary gate: reject the action if any rule matches.
    pub fn authorize(&self, action: &str, target: &str) -> Result<(), CompactionRuleError> {
        if let Some(violation) = self.violations(action, target).into_iter().next() {
            return Err(CompactionRuleError::Blocked {
                rule_id: violation.rule_id,
            });
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn guard() -> CompactionRuleGuard {
        let mut guard = CompactionRuleGuard::default();
        guard
            .register(
                MandateRule::new("tests-protected", "Never modify evaluator tests")
                    .protecting("tests/evaluator")
                    .forbidding("delete_tests"),
            )
            .unwrap();
        guard
    }

    #[test]
    fn valid_action_is_allowed() {
        assert!(guard().authorize("edit_file", "src/lib.rs").is_ok());
    }

    #[test]
    fn forbidden_action_is_blocked() {
        assert!(matches!(
            guard().authorize("delete_tests", "src/lib.rs"),
            Err(CompactionRuleError::Blocked { .. })
        ));
    }

    #[test]
    fn protected_path_prefix_is_blocked() {
        let violations = guard().violations("edit_file", "tests/evaluator/test_scoring.py");
        assert_eq!(violations.len(), 1);
        assert!(matches!(
            violations[0].kind,
            RuleViolationKind::ProtectedPath { .. }
        ));
    }

    #[test]
    fn similarly_named_path_is_not_blocked() {
        assert!(guard()
            .violations("edit_file", "tests/evaluator_notes.md")
            .is_empty());
    }

    #[test]
    fn anchor_reports_missing_rule_text() {
        let anchor = guard().anchor_after_compaction("The task is complete.");
        assert!(!anchor.anchored);
        assert_eq!(anchor.missing, vec!["tests-protected"]);
        assert!(anchor.rendered.contains("Never modify evaluator tests"));
    }

    #[test]
    fn anchor_accepts_summary_that_retains_rule() {
        let anchor =
            guard().anchor_after_compaction("Never modify evaluator tests; continue the patch.");
        assert!(anchor.anchored);
        assert!(anchor.missing.is_empty());
    }

    #[test]
    fn duplicate_rule_is_rejected() {
        let mut guard = guard();
        assert!(matches!(
            guard.register(MandateRule::new("tests-protected", "another")),
            Err(CompactionRuleError::DuplicateRule(_))
        ));
    }

    #[test]
    fn invalid_rule_and_capacity_are_rejected() {
        assert!(matches!(
            CompactionRuleGuard::default().register(MandateRule::new("", "x")),
            Err(CompactionRuleError::EmptyField("id"))
        ));
        let mut tiny = CompactionRuleGuard::new(1);
        tiny.register(MandateRule::new("a", "one")).unwrap();
        assert_eq!(
            tiny.register(MandateRule::new("b", "two")),
            Err(CompactionRuleError::CapacityReached)
        );
    }

    #[test]
    fn empty_forbidden_action_is_rejected() {
        let rule = MandateRule::new("r", "statement").forbidding("");
        assert_eq!(
            CompactionRuleGuard::default().register(rule),
            Err(CompactionRuleError::EmptyField("forbidden_action"))
        );
    }
}
