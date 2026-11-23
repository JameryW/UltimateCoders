//! Causal counterfactual privilege minimisation and capability tokens
//! (ledger item #99, PRISM + AgentCap).

use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use thiserror::Error;

pub const DEFAULT_NECESSITY_THRESHOLD: f64 = 0.5;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ToolPrivilege {
    pub tool: String,
    pub required: bool,
    pub necessity: f64,
    pub blast_radius: f64,
}

impl ToolPrivilege {
    pub fn new(
        tool: impl Into<String>,
        required: bool,
        necessity: f64,
        blast_radius: f64,
    ) -> Result<Self, PrismError> {
        let tool = tool.into();
        if tool.trim().is_empty() {
            return Err(PrismError::EmptyTool);
        }
        if !(0.0..=1.0).contains(&necessity) || !(0.0..=1.0).contains(&blast_radius) {
            return Err(PrismError::InvalidScore);
        }
        Ok(Self {
            tool,
            required,
            necessity,
            blast_radius,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MinimizedRole {
    pub role: String,
    pub kept: Vec<String>,
    pub removed: Vec<String>,
    pub compression_ratio: f64,
    pub blast_radius: f64,
}

#[derive(Debug, Clone)]
pub struct PrismMinimizer {
    necessity_threshold: f64,
}

impl Default for PrismMinimizer {
    fn default() -> Self {
        Self::new(DEFAULT_NECESSITY_THRESHOLD)
    }
}

impl PrismMinimizer {
    pub fn new(necessity_threshold: f64) -> Self {
        Self {
            necessity_threshold: if (0.0..=1.0).contains(&necessity_threshold) {
                necessity_threshold
            } else {
                DEFAULT_NECESSITY_THRESHOLD
            },
        }
    }

    pub fn minimize(
        &self,
        role: impl Into<String>,
        privileges: &[ToolPrivilege],
    ) -> Result<MinimizedRole, PrismError> {
        let role = role.into();
        if role.trim().is_empty() {
            return Err(PrismError::EmptyRole);
        }
        if privileges.is_empty() {
            return Err(PrismError::NoPrivileges);
        }
        let mut kept = Vec::new();
        let mut removed = Vec::new();
        for privilege in privileges {
            if privilege.required || privilege.necessity >= self.necessity_threshold {
                kept.push(privilege.tool.clone());
            } else {
                removed.push(privilege.tool.clone());
            }
        }
        let total = privileges.len() as f64;
        let compression_ratio = if total == 0.0 {
            0.0
        } else {
            removed.len() as f64 / total
        };
        let blast_radius = privileges
            .iter()
            .filter(|privilege| kept.contains(&privilege.tool))
            .map(|privilege| privilege.blast_radius)
            .fold(0.0, f64::max);
        Ok(MinimizedRole {
            role,
            kept,
            removed,
            compression_ratio,
            blast_radius,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CapabilityToken {
    pub token_id: String,
    pub session_id: String,
    pub tool: String,
    pub budget: u32,
    pub ttl_seconds: u64,
    pub parent_budget: u32,
    pub issued_at: u64,
    pub expires_at: u64,
    pub revoked: bool,
}

#[derive(Debug, Clone, Error, PartialEq, Eq)]
pub enum PrismError {
    #[error("tool name must not be empty")]
    EmptyTool,
    #[error("role must not be empty")]
    EmptyRole,
    #[error("privileges must not be empty")]
    NoPrivileges,
    #[error("necessity and blast radius must be in [0, 1]")]
    InvalidScore,
    #[error("capability token id must not be empty")]
    EmptyToken,
    #[error("session id must not be empty")]
    EmptySession,
    #[error("child budget exceeds parent budget")]
    BudgetExceeded,
    #[error("child ttl exceeds parent ttl")]
    TtlExceeded,
    #[error("parent token is revoked")]
    Revoked,
    #[error("token is expired")]
    Expired,
    #[error("token session does not match")]
    SessionMismatch,
    #[error("token tool does not match")]
    ToolMismatch,
    #[error("requested budget exceeds token budget")]
    RequestExceeded,
}

#[derive(Debug, Clone)]
pub struct CapabilityIssuer;

impl CapabilityIssuer {
    pub fn issue(
        parent: &CapabilityToken,
        token_id: impl Into<String>,
        budget: u32,
        ttl_seconds: u64,
        now: u64,
    ) -> Result<CapabilityToken, PrismError> {
        if parent.revoked {
            return Err(PrismError::Revoked);
        }
        let token_id = token_id.into();
        if token_id.trim().is_empty() {
            return Err(PrismError::EmptyToken);
        }
        if budget > parent.budget {
            return Err(PrismError::BudgetExceeded);
        }
        if ttl_seconds > parent.ttl_seconds {
            return Err(PrismError::TtlExceeded);
        }
        Ok(CapabilityToken {
            token_id,
            session_id: parent.session_id.clone(),
            tool: parent.tool.clone(),
            budget,
            ttl_seconds,
            parent_budget: parent.budget,
            issued_at: now,
            expires_at: now.saturating_add(ttl_seconds),
            revoked: false,
        })
    }
}

#[derive(Debug, Clone)]
pub struct AgentCapVerifier;

impl AgentCapVerifier {
    pub fn verify(
        token: &CapabilityToken,
        session_id: &str,
        tool: &str,
        now: u64,
        requested_budget: u32,
    ) -> Result<(), PrismError> {
        if token.revoked {
            return Err(PrismError::Revoked);
        }
        if now >= token.expires_at {
            return Err(PrismError::Expired);
        }
        if token.session_id != session_id {
            return Err(PrismError::SessionMismatch);
        }
        if token.tool != tool {
            return Err(PrismError::ToolMismatch);
        }
        if requested_budget > token.budget {
            return Err(PrismError::RequestExceeded);
        }
        Ok(())
    }
}

pub fn distinct_tools(privileges: &[ToolPrivilege]) -> usize {
    privileges
        .iter()
        .map(|privilege| privilege.tool.as_str())
        .collect::<BTreeSet<_>>()
        .len()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn privilege(tool: &str, required: bool, necessity: f64, blast: f64) -> ToolPrivilege {
        ToolPrivilege::new(tool, required, necessity, blast).unwrap()
    }

    #[test]
    fn required_tools_are_kept_even_when_low_necessity() {
        let minimized = PrismMinimizer::default()
            .minimize("coder", &[privilege("read", true, 0.0, 0.1)])
            .unwrap();
        assert_eq!(minimized.kept, vec!["read"]);
        assert!(minimized.removed.is_empty());
    }

    #[test]
    fn low_necessity_tools_are_removed() {
        let minimized = PrismMinimizer::default()
            .minimize(
                "coder",
                &[
                    privilege("read", false, 0.9, 0.1),
                    privilege("deploy", false, 0.1, 0.9),
                ],
            )
            .unwrap();
        assert_eq!(minimized.kept, vec!["read"]);
        assert_eq!(minimized.removed, vec!["deploy"]);
        assert!((minimized.compression_ratio - 0.5).abs() < 1e-12);
    }

    #[test]
    fn blast_radius_is_maximum_of_kept() {
        let minimized = PrismMinimizer::default()
            .minimize(
                "coder",
                &[
                    privilege("a", true, 0.0, 0.2),
                    privilege("b", true, 0.0, 0.7),
                ],
            )
            .unwrap();
        assert!((minimized.blast_radius - 0.7).abs() < 1e-12);
    }

    #[test]
    fn invalid_scores_are_rejected() {
        assert_eq!(
            ToolPrivilege::new("a", false, 1.2, 0.1),
            Err(PrismError::InvalidScore)
        );
        assert_eq!(
            ToolPrivilege::new("a", false, 0.1, -0.1),
            Err(PrismError::InvalidScore)
        );
    }

    fn parent() -> CapabilityToken {
        CapabilityToken {
            token_id: "root".into(),
            session_id: "s1".into(),
            tool: "fs.read".into(),
            budget: 100,
            ttl_seconds: 60,
            parent_budget: 100,
            issued_at: 10,
            expires_at: 70,
            revoked: false,
        }
    }

    #[test]
    fn child_budget_can_only_decay() {
        let parent = parent();
        let child = CapabilityIssuer::issue(&parent, "child", 50, 30, 20).unwrap();
        assert_eq!(child.budget, 50);
        assert!(CapabilityIssuer::issue(&parent, "bad", 101, 30, 20).is_err());
    }

    #[test]
    fn child_ttl_can_only_decay() {
        let parent = parent();
        assert!(CapabilityIssuer::issue(&parent, "bad", 10, 61, 20).is_err());
        let child = CapabilityIssuer::issue(&parent, "child", 10, 60, 20).unwrap();
        assert_eq!(child.expires_at, 80);
    }

    #[test]
    fn verify_checks_all_bindings() {
        let token = CapabilityIssuer::issue(&parent(), "child", 10, 30, 20).unwrap();
        assert!(AgentCapVerifier::verify(&token, "s1", "fs.read", 25, 10).is_ok());
        assert_eq!(
            AgentCapVerifier::verify(&token, "s2", "fs.read", 25, 10),
            Err(PrismError::SessionMismatch)
        );
        assert_eq!(
            AgentCapVerifier::verify(&token, "s1", "fs.write", 25, 10),
            Err(PrismError::ToolMismatch)
        );
        assert_eq!(
            AgentCapVerifier::verify(&token, "s1", "fs.read", 25, 11),
            Err(PrismError::RequestExceeded)
        );
    }

    #[test]
    fn expired_token_is_rejected() {
        let token = CapabilityIssuer::issue(&parent(), "child", 10, 10, 20).unwrap();
        assert_eq!(
            AgentCapVerifier::verify(&token, "s1", "fs.read", 30, 10),
            Err(PrismError::Expired)
        );
    }

    #[test]
    fn revoked_token_is_rejected() {
        let mut token = CapabilityIssuer::issue(&parent(), "child", 10, 10, 20).unwrap();
        token.revoked = true;
        assert_eq!(
            AgentCapVerifier::verify(&token, "s1", "fs.read", 25, 10),
            Err(PrismError::Revoked)
        );
    }
}
