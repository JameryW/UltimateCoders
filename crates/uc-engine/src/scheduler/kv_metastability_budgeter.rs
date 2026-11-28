//! Prefix-cache metastability budgeter (V28 item #128).
//!
//! A burst can leave an inference service in a metastable state where the
//! prefix-cache hit rate collapses and the queue keeps growing for tens of
//! minutes.  Rate limiting only new requests does not drain that state; the
//! budgeter instead tracks the resident KV demand of already-admitted sessions
//! and suspends low-value sessions *before* the cache crosses the safety
//! ceiling, preserving sessions with long shared prefixes.
//!
//! This module is deterministic bookkeeping.  It does not evict a real cache
//! or suspend a process; a scheduler consumes the returned plan.

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Default fraction of capacity that may be committed without suspending.
pub const DEFAULT_SAFETY_RATIO: f64 = 0.8;

/// One admitted session's resident KV demand.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SessionDemand {
    pub session_id: String,
    pub resident_kv_mb: f64,
    pub shared_prefix_tokens: u64,
    pub priority: u8,
}

impl SessionDemand {
    /// Build a validated demand.
    pub fn new(
        session_id: impl Into<String>,
        resident_kv_mb: f64,
        shared_prefix_tokens: u64,
        priority: u8,
    ) -> Result<Self, KvBudgetError> {
        let session_id = session_id.into();
        if session_id.trim().is_empty() {
            return Err(KvBudgetError::EmptySession);
        }
        if !resident_kv_mb.is_finite() || resident_kv_mb <= 0.0 {
            return Err(KvBudgetError::InvalidDemand);
        }
        Ok(Self {
            session_id,
            resident_kv_mb,
            shared_prefix_tokens,
            priority,
        })
    }
}

/// The scheduler's decision for one planning call.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BudgetPlan {
    pub admitted: Vec<String>,
    pub suspended: Vec<String>,
    pub rejected: Option<String>,
    pub resident_mb: f64,
    pub capacity_mb: f64,
    pub limit_mb: f64,
    pub saturated: bool,
}

impl BudgetPlan {
    /// True when no session had to be suspended or rejected.
    pub fn clean(&self) -> bool {
        self.suspended.is_empty() && self.rejected.is_none()
    }
}

/// Configuration or input error.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum KvBudgetError {
    #[error("session id must not be empty")]
    EmptySession,
    #[error("capacity must be finite and positive")]
    InvalidCapacity,
    #[error("safety ratio must be in (0, 1]")]
    InvalidRatio,
    #[error("resident KV demand must be finite and positive")]
    InvalidDemand,
    #[error("duplicate session: {0}")]
    DuplicateSession(String),
}

/// Plans cache-preserving suspensions for a set of active sessions.
#[derive(Debug, Clone)]
pub struct KvMetastabilityBudgeter {
    capacity_mb: f64,
    safety_ratio: f64,
}

impl KvMetastabilityBudgeter {
    /// Create a budgeter with a positive capacity and a ratio in `(0, 1]`.
    pub fn new(capacity_mb: f64, safety_ratio: f64) -> Result<Self, KvBudgetError> {
        if !capacity_mb.is_finite() || capacity_mb <= 0.0 {
            return Err(KvBudgetError::InvalidCapacity);
        }
        if !safety_ratio.is_finite() || safety_ratio <= 0.0 || safety_ratio > 1.0 {
            return Err(KvBudgetError::InvalidRatio);
        }
        Ok(Self {
            capacity_mb,
            safety_ratio,
        })
    }

    /// Capacity in megabytes.
    pub fn capacity_mb(&self) -> f64 {
        self.capacity_mb
    }

    /// Safety ratio.
    pub fn safety_ratio(&self) -> f64 {
        self.safety_ratio
    }

    /// The committed-KV ceiling (`capacity_mb * safety_ratio`).
    pub fn limit_mb(&self) -> f64 {
        self.capacity_mb * self.safety_ratio
    }

    /// Plan admissions for existing sessions and an optional incoming session.
    ///
    /// Existing sessions are suspended in increasing priority, then increasing
    /// shared-prefix size, then decreasing resident demand.  This drains the
    /// cheapest, least cache-sharing sessions first.  The incoming session is
    /// never suspended; if it cannot fit after all existing sessions are
    /// suspended, it is rejected.
    pub fn plan(
        &self,
        sessions: &[SessionDemand],
        incoming: Option<&SessionDemand>,
    ) -> Result<BudgetPlan, KvBudgetError> {
        let limit = self.limit_mb();
        let mut seen = std::collections::BTreeSet::new();
        for session in sessions {
            if !seen.insert(session.session_id.clone()) {
                return Err(KvBudgetError::DuplicateSession(session.session_id.clone()));
            }
            if session.session_id.trim().is_empty()
                || !session.resident_kv_mb.is_finite()
                || session.resident_kv_mb <= 0.0
            {
                return Err(KvBudgetError::InvalidDemand);
            }
        }
        if let Some(session) = incoming {
            if !seen.insert(session.session_id.clone()) {
                return Err(KvBudgetError::DuplicateSession(session.session_id.clone()));
            }
            if session.session_id.trim().is_empty()
                || !session.resident_kv_mb.is_finite()
                || session.resident_kv_mb <= 0.0
            {
                return Err(KvBudgetError::InvalidDemand);
            }
        }

        let mut active: Vec<SessionDemand> = sessions.to_vec();
        let incoming_mb = incoming.map_or(0.0, |session| session.resident_kv_mb);
        let mut total: f64 = active.iter().map(|s| s.resident_kv_mb).sum::<f64>() + incoming_mb;
        let mut suspended: Vec<String> = Vec::new();

        if total > limit {
            let mut candidates = active.clone();
            candidates.sort_by(|a, b| {
                a.priority
                    .cmp(&b.priority)
                    .then(a.shared_prefix_tokens.cmp(&b.shared_prefix_tokens))
                    .then_with(|| {
                        b.resident_kv_mb
                            .partial_cmp(&a.resident_kv_mb)
                            .unwrap_or(std::cmp::Ordering::Equal)
                    })
                    .then_with(|| a.session_id.cmp(&b.session_id))
            });
            for candidate in candidates {
                if total <= limit {
                    break;
                }
                if let Some(position) = active
                    .iter()
                    .position(|session| session.session_id == candidate.session_id)
                {
                    active.remove(position);
                    total -= candidate.resident_kv_mb;
                    suspended.push(candidate.session_id);
                }
            }
        }

        let mut rejected = None;
        if total > limit {
            if let Some(session) = incoming {
                rejected = Some(session.session_id.clone());
            }
        }
        if rejected.is_none() {
            if let Some(session) = incoming {
                active.push(session.clone());
            }
        }

        let resident_mb: f64 = active.iter().map(|s| s.resident_kv_mb).sum();
        let mut admitted: Vec<String> = active.iter().map(|s| s.session_id.clone()).collect();
        admitted.sort();
        suspended.sort();
        let saturated = resident_mb > limit || rejected.is_some();
        Ok(BudgetPlan {
            admitted,
            suspended,
            rejected,
            resident_mb,
            capacity_mb: self.capacity_mb,
            limit_mb: limit,
            saturated,
        })
    }
}

impl Default for KvMetastabilityBudgeter {
    fn default() -> Self {
        Self::new(1024.0, DEFAULT_SAFETY_RATIO).expect("default budgeter is valid")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn demand(id: &str, mb: f64, prefix: u64, priority: u8) -> SessionDemand {
        SessionDemand::new(id, mb, prefix, priority).expect("valid demand")
    }

    #[test]
    fn default_budgeter_has_expected_limit() {
        let budgeter = KvMetastabilityBudgeter::default();
        assert_eq!(budgeter.capacity_mb(), 1024.0);
        assert_eq!(budgeter.safety_ratio(), DEFAULT_SAFETY_RATIO);
        assert!((budgeter.limit_mb() - 819.2).abs() < 1e-9);
    }

    #[test]
    fn admits_everything_under_the_limit() {
        let budgeter = KvMetastabilityBudgeter::new(1000.0, 0.8).expect("budgeter");
        let plan = budgeter
            .plan(&[demand("a", 100.0, 10, 1), demand("b", 200.0, 5, 2)], None)
            .expect("plan");
        assert_eq!(plan.admitted, vec!["a", "b"]);
        assert!(plan.suspended.is_empty());
        assert!(plan.clean());
        assert!(!plan.saturated);
    }

    #[test]
    fn suspends_low_priority_sessions_first() {
        let budgeter = KvMetastabilityBudgeter::new(100.0, 0.8).expect("budgeter");
        let plan = budgeter
            .plan(
                &[
                    demand("critical", 30.0, 5, 9),
                    demand("low", 40.0, 5, 1),
                    demand("medium", 20.0, 5, 5),
                ],
                None,
            )
            .expect("plan");
        assert_eq!(plan.suspended, vec!["low"]);
        assert_eq!(plan.admitted, vec!["critical", "medium"]);
        assert!((plan.resident_mb - 50.0).abs() < 1e-9);
    }

    #[test]
    fn preserves_high_shared_prefix_before_low_prefix() {
        let budgeter = KvMetastabilityBudgeter::new(90.0, 0.8).expect("budgeter");
        let plan = budgeter
            .plan(
                &[
                    demand("shared", 40.0, 10_000, 5),
                    demand("cold", 40.0, 10, 5),
                ],
                None,
            )
            .expect("plan");
        assert_eq!(plan.suspended, vec!["cold"]);
        assert_eq!(plan.admitted, vec!["shared"]);
    }

    #[test]
    fn rejects_incoming_when_even_suspending_everything_is_not_enough() {
        let budgeter = KvMetastabilityBudgeter::new(100.0, 0.8).expect("budgeter");
        let incoming = demand("new", 90.0, 1, 1);
        let plan = budgeter
            .plan(&[demand("existing", 10.0, 1, 9)], Some(&incoming))
            .expect("plan");
        assert_eq!(plan.rejected.as_deref(), Some("new"));
        assert_eq!(plan.admitted, Vec::<String>::new());
        assert_eq!(plan.suspended, vec!["existing"]);
        assert!(plan.saturated);
    }

    #[test]
    fn admits_incoming_after_suspending_a_low_value_session() {
        let budgeter = KvMetastabilityBudgeter::new(100.0, 0.8).expect("budgeter");
        let incoming = demand("new", 30.0, 1000, 9);
        let plan = budgeter
            .plan(&[demand("existing", 60.0, 1, 1)], Some(&incoming))
            .expect("plan");
        assert_eq!(plan.admitted, vec!["new"]);
        assert_eq!(plan.suspended, vec!["existing"]);
        assert!(plan.rejected.is_none());
    }

    #[test]
    fn rejects_duplicate_sessions() {
        let budgeter = KvMetastabilityBudgeter::new(100.0, 0.8).expect("budgeter");
        let error = budgeter
            .plan(&[demand("a", 1.0, 1, 1), demand("a", 2.0, 1, 1)], None)
            .expect_err("duplicate must fail");
        assert!(matches!(error, KvBudgetError::DuplicateSession(session) if session == "a"));
    }

    #[test]
    fn rejects_invalid_configuration_and_demand() {
        assert!(matches!(
            KvMetastabilityBudgeter::new(0.0, 0.8),
            Err(KvBudgetError::InvalidCapacity)
        ));
        assert!(matches!(
            KvMetastabilityBudgeter::new(10.0, 0.0),
            Err(KvBudgetError::InvalidRatio)
        ));
        assert!(matches!(
            KvMetastabilityBudgeter::new(10.0, 1.5),
            Err(KvBudgetError::InvalidRatio)
        ));
        assert!(matches!(
            SessionDemand::new("", 1.0, 1, 1),
            Err(KvBudgetError::EmptySession)
        ));
        assert!(matches!(
            SessionDemand::new("a", 0.0, 1, 1),
            Err(KvBudgetError::InvalidDemand)
        ));
    }

    #[test]
    fn empty_session_set_is_an_empty_plan() {
        let budgeter = KvMetastabilityBudgeter::default();
        let plan = budgeter.plan(&[], None).expect("plan");
        assert!(plan.admitted.is_empty());
        assert!(plan.suspended.is_empty());
        assert_eq!(plan.resident_mb, 0.0);
        assert!(!plan.saturated);
    }

    #[test]
    fn incoming_larger_than_the_whole_capacity_is_rejected() {
        let budgeter = KvMetastabilityBudgeter::new(100.0, 1.0).expect("budgeter");
        let incoming = demand("huge", 101.0, 1, 1);
        let plan = budgeter.plan(&[], Some(&incoming)).expect("plan");
        assert_eq!(plan.rejected.as_deref(), Some("huge"));
        assert_eq!(plan.resident_mb, 0.0);
        assert!(plan.saturated);
    }
}
