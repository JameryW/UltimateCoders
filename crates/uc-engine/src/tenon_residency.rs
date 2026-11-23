//! Sandbox residency virtualisation with a two-timescale admission gate (ledger item #97).
//!
//! TenonAgent separates a logical agent session from its physical sandbox. Idle
//! sessions are de-materialised (checkpointed and evicted), while a request at
//! an action boundary predicts and warms the sandbox it will need. This module
//! models the deterministic admission and eviction policy; it does not control
//! a real container runtime.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use thiserror::Error;

pub const DEFAULT_MEMORY_BUDGET_MB: u64 = 8_192;
pub const DEFAULT_MAX_OVERSUBSCRIPTION: f64 = 8.6;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum SessionState {
    Active,
    Idle,
    ToolCall,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ResidencyAction {
    Keep,
    Materialize,
    Dematerialize,
    Reject,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SessionDemand {
    pub session_id: String,
    pub state: SessionState,
    pub memory_mb: u64,
    pub last_active_step: u64,
}

impl SessionDemand {
    pub fn new(
        session_id: impl Into<String>,
        state: SessionState,
        memory_mb: u64,
        last_active_step: u64,
    ) -> Result<Self, TenonError> {
        let session_id = session_id.into();
        if session_id.trim().is_empty() {
            return Err(TenonError::EmptySessionId);
        }
        if memory_mb == 0 {
            return Err(TenonError::ZeroMemory);
        }
        Ok(Self {
            session_id,
            state,
            memory_mb,
            last_active_step,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ResidencyPlan {
    pub actions: Vec<(String, ResidencyAction)>,
    pub admitted: Vec<String>,
    pub rejected: Vec<String>,
    pub materialized_mb: u64,
    pub oversubscription: f64,
}

impl ResidencyPlan {
    pub fn action_for(&self, session_id: &str) -> Option<ResidencyAction> {
        self.actions
            .iter()
            .find(|(id, _)| id == session_id)
            .map(|(_, action)| *action)
    }
}

#[derive(Debug, Clone, Error, PartialEq, Eq)]
pub enum TenonError {
    #[error("session id must not be empty")]
    EmptySessionId,
    #[error("session memory must be positive")]
    ZeroMemory,
    #[error("duplicate session: {0}")]
    DuplicateSession(String),
    #[error("memory budget must be positive")]
    InvalidBudget,
    #[error("max oversubscription must be positive and finite")]
    InvalidOversubscription,
}

#[derive(Debug, Clone)]
pub struct TenonResidency {
    budget_mb: u64,
    max_oversubscription: f64,
}

impl Default for TenonResidency {
    fn default() -> Self {
        Self::new(DEFAULT_MEMORY_BUDGET_MB, DEFAULT_MAX_OVERSUBSCRIPTION)
    }
}

impl TenonResidency {
    pub fn new(budget_mb: u64, max_oversubscription: f64) -> Self {
        Self {
            budget_mb: budget_mb.max(1),
            max_oversubscription: if max_oversubscription.is_finite() && max_oversubscription > 0.0
            {
                max_oversubscription
            } else {
                DEFAULT_MAX_OVERSUBSCRIPTION
            },
        }
    }

    pub fn budget_mb(&self) -> u64 {
        self.budget_mb
    }

    pub fn max_oversubscription(&self) -> f64 {
        self.max_oversubscription
    }

    pub fn plan(&mut self, demands: &[SessionDemand]) -> Result<ResidencyPlan, TenonError> {
        let mut by_id: BTreeMap<String, SessionDemand> = BTreeMap::new();
        for demand in demands {
            if by_id
                .insert(demand.session_id.clone(), demand.clone())
                .is_some()
            {
                return Err(TenonError::DuplicateSession(demand.session_id.clone()));
            }
        }

        let mut keep: Vec<&SessionDemand> = Vec::new();
        let mut idle: Vec<&SessionDemand> = Vec::new();
        for demand in by_id.values() {
            match demand.state {
                SessionState::Idle => idle.push(demand),
                SessionState::Active | SessionState::ToolCall => keep.push(demand),
            }
        }
        // Keep the most recently active work; reject the newest overflow first.
        keep.sort_by_key(|demand| (demand.last_active_step, demand.session_id.as_str()));

        let limit = (self.budget_mb as f64 * self.max_oversubscription).floor() as u64;
        let mut materialized_mb: u64 = 0;
        let mut actions: Vec<(String, ResidencyAction)> = Vec::new();
        let mut rejected: Vec<String> = Vec::new();

        for demand in &idle {
            actions.push((demand.session_id.clone(), ResidencyAction::Dematerialize));
        }

        for demand in keep {
            if materialized_mb.saturating_add(demand.memory_mb) <= limit {
                materialized_mb = materialized_mb.saturating_add(demand.memory_mb);
                actions.push((demand.session_id.clone(), ResidencyAction::Materialize));
            } else {
                actions.push((demand.session_id.clone(), ResidencyAction::Reject));
                rejected.push(demand.session_id.clone());
            }
        }

        let admitted = by_id
            .keys()
            .filter(|id| !rejected.contains(id))
            .cloned()
            .collect::<Vec<_>>();
        let oversubscription = if self.budget_mb == 0 {
            0.0
        } else {
            materialized_mb as f64 / self.budget_mb as f64
        };
        actions.sort_by(|a, b| a.0.cmp(&b.0));
        Ok(ResidencyPlan {
            actions,
            admitted,
            rejected,
            materialized_mb,
            oversubscription,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn idle_sessions_are_dematerialised() {
        let mut residency = TenonResidency::default();
        let plan = residency
            .plan(&[SessionDemand::new("s1", SessionState::Idle, 100, 1).unwrap()])
            .unwrap();
        assert_eq!(plan.action_for("s1"), Some(ResidencyAction::Dematerialize));
        assert_eq!(plan.materialized_mb, 0);
    }

    #[test]
    fn active_sessions_are_materialised() {
        let mut residency = TenonResidency::default();
        let plan = residency
            .plan(&[SessionDemand::new("s1", SessionState::Active, 100, 1).unwrap()])
            .unwrap();
        assert_eq!(plan.action_for("s1"), Some(ResidencyAction::Materialize));
    }

    #[test]
    fn idle_sessions_are_evicted_lru_first_when_over_limit() {
        let mut residency = TenonResidency::new(100, 1.0);
        let plan = residency
            .plan(&[
                SessionDemand::new("new", SessionState::Active, 80, 3).unwrap(),
                SessionDemand::new("old", SessionState::Idle, 80, 1).unwrap(),
            ])
            .unwrap();
        assert_eq!(plan.action_for("old"), Some(ResidencyAction::Dematerialize));
        assert_eq!(plan.action_for("new"), Some(ResidencyAction::Materialize));
        assert!(plan.rejected.is_empty());
    }

    #[test]
    fn an_unavoidable_overcommit_rejects_the_new_session() {
        let mut residency = TenonResidency::new(100, 1.0);
        let plan = residency
            .plan(&[
                SessionDemand::new("a", SessionState::Active, 90, 1).unwrap(),
                SessionDemand::new("b", SessionState::Active, 90, 2).unwrap(),
            ])
            .unwrap();
        assert_eq!(plan.rejected, vec!["b".to_string()]);
        assert_eq!(plan.action_for("b"), Some(ResidencyAction::Reject));
    }

    #[test]
    fn oversubscription_allows_more_active_memory() {
        let mut residency = TenonResidency::new(100, 2.0);
        let plan = residency
            .plan(&[
                SessionDemand::new("a", SessionState::Active, 150, 1).unwrap(),
                SessionDemand::new("b", SessionState::Active, 40, 2).unwrap(),
            ])
            .unwrap();
        assert!(plan.admitted.contains(&"a".to_string()));
        assert!(plan.admitted.contains(&"b".to_string()));
    }

    #[test]
    fn duplicate_session_is_rejected() {
        let mut residency = TenonResidency::default();
        let demand = SessionDemand::new("a", SessionState::Active, 1, 1).unwrap();
        assert_eq!(
            residency.plan(&[demand.clone(), demand]),
            Err(TenonError::DuplicateSession("a".to_string()))
        );
    }

    #[test]
    fn empty_id_is_rejected() {
        assert_eq!(
            SessionDemand::new(" ", SessionState::Active, 1, 1),
            Err(TenonError::EmptySessionId)
        );
    }

    #[test]
    fn zero_memory_is_rejected() {
        assert_eq!(
            SessionDemand::new("a", SessionState::Active, 0, 1),
            Err(TenonError::ZeroMemory)
        );
    }

    #[test]
    fn invalid_oversubscription_falls_back_to_default() {
        let residency = TenonResidency::new(10, f64::NAN);
        assert_eq!(
            residency.max_oversubscription(),
            DEFAULT_MAX_OVERSUBSCRIPTION
        );
    }
}
