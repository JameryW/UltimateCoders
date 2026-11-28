//! Pooled formal-verification service and curiosity-driven investigation
//! (ledger item #120).
//!
//! Isabelle Pool Server (arXiv:2611.24788) keeps a warm pool of Isabelle /
//! Lean 4 / Miri workers so multi-agent proof and RL rollouts do not pay the
//! repeated prover cold-start cost. Sampling Is Not Curiosity
//! (arXiv:2611.10501, NeurIPS 2026) replaces blind Best-of-N sampling with
//! an online environment-prediction model that selects investigations by
//! expected information gain per unit cost.
//!
//! This module is a deterministic bookkeeping layer: it hands out warm
//! prover slots and ranks investigation candidates. It does not run a
//! prover, spawn a process, or call a model.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Default maximum number of warm prover slots.
pub const DEFAULT_MAX_PROVER_SLOTS: usize = 32;

/// Default maximum number of candidates considered by one curiosity query.
pub const DEFAULT_MAX_CURIOSITY_CANDIDATES: usize = 256;

/// Supported formal-verification backends.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub enum ProverKind {
    /// Isabelle/HOL.
    Isabelle,
    /// Lean 4.
    Lean4,
    /// Miri undefined-behaviour checker.
    Miri,
}

/// A pooled prover slot.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProverSlot {
    /// Stable slot id.
    pub id: String,
    /// Backend served by this slot.
    pub kind: ProverKind,
    /// Whether the slot is currently leased.
    pub busy: bool,
    /// Number of successful verifications completed by this slot.
    pub completed: u64,
}

/// Pool statistics.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct ProverPoolStats {
    /// Total slots.
    pub slots: usize,
    /// Warm idle slots.
    pub idle: usize,
    /// Leased slots.
    pub busy: usize,
    /// Completed verifications across all slots.
    pub completed: u64,
}

/// Errors produced by the pool.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum ProverError {
    /// A required field was empty.
    #[error("empty field: {0}")]
    EmptyField(&'static str),
    /// Slot capacity reached.
    #[error("prover slot capacity reached")]
    CapacityReached,
    /// Duplicate slot id.
    #[error("duplicate prover slot: {0}")]
    DuplicateSlot(String),
    /// Unknown slot id.
    #[error("unknown prover slot: {0}")]
    UnknownSlot(String),
    /// No warm idle slot of the requested kind.
    #[error("no warm {0:?} prover available")]
    NoWarmProver(ProverKind),
    /// Slot was released but was not busy.
    #[error("prover slot is not leased: {0}")]
    SlotNotLeased(String),
    /// A curiosity candidate has invalid values.
    #[error("invalid curiosity candidate: {0}")]
    InvalidCandidate(&'static str),
    /// No candidate has positive expected information gain.
    #[error("no candidate has positive information gain")]
    NoPositiveGain,
    /// Candidate capacity reached.
    #[error("curiosity candidate capacity reached")]
    CandidateCapacityReached,
    /// Duplicate candidate action.
    #[error("duplicate curiosity candidate: {0}")]
    DuplicateCandidate(String),
}

/// A prewarmed proof worker.
#[derive(Debug, Clone)]
pub struct ProverPool {
    max_slots: usize,
    slots: BTreeMap<String, ProverSlot>,
}

impl Default for ProverPool {
    fn default() -> Self {
        Self::new()
    }
}

impl ProverPool {
    /// Creates an empty pool with the default bound.
    pub fn new() -> Self {
        Self::with_capacity(DEFAULT_MAX_PROVER_SLOTS)
    }

    /// Creates an empty pool with an explicit bound.
    pub fn with_capacity(max_slots: usize) -> Self {
        Self {
            max_slots: max_slots.max(1),
            slots: BTreeMap::new(),
        }
    }

    /// Prewarms `count` slots for `kind`.
    ///
    /// Slot ids are deterministic: `{kind:?}-{index}` with the smallest
    /// unused index starting at zero.
    pub fn prewarm(&mut self, kind: ProverKind, count: usize) -> Result<Vec<String>, ProverError> {
        let mut created = Vec::new();
        for _ in 0..count {
            if self.slots.len() >= self.max_slots {
                return Err(ProverError::CapacityReached);
            }
            let mut index = 0usize;
            let id = loop {
                let candidate = format!("{kind:?}-{index}");
                if !self.slots.contains_key(&candidate) {
                    break candidate;
                }
                index += 1;
            };
            self.slots.insert(
                id.clone(),
                ProverSlot {
                    id: id.clone(),
                    kind,
                    busy: false,
                    completed: 0,
                },
            );
            created.push(id);
        }
        Ok(created)
    }

    /// Leases the first warm idle slot of `kind` in id order.
    pub fn acquire(&mut self, kind: ProverKind) -> Result<String, ProverError> {
        let candidate = self
            .slots
            .values_mut()
            .find(|slot| slot.kind == kind && !slot.busy);
        match candidate {
            Some(slot) => {
                slot.busy = true;
                Ok(slot.id.clone())
            }
            None => Err(ProverError::NoWarmProver(kind)),
        }
    }

    /// Releases a leased slot and records whether verification succeeded.
    pub fn release(&mut self, slot_id: &str, verified: bool) -> Result<(), ProverError> {
        let slot = self
            .slots
            .get_mut(slot_id)
            .ok_or_else(|| ProverError::UnknownSlot(slot_id.to_owned()))?;
        if !slot.busy {
            return Err(ProverError::SlotNotLeased(slot_id.to_owned()));
        }
        slot.busy = false;
        if verified {
            slot.completed = slot.completed.saturating_add(1);
        }
        Ok(())
    }

    /// Returns the slot if present.
    pub fn slot(&self, slot_id: &str) -> Option<&ProverSlot> {
        self.slots.get(slot_id)
    }

    /// All slots in id order.
    pub fn slots(&self) -> Vec<&ProverSlot> {
        self.slots.values().collect()
    }

    /// Pool statistics.
    pub fn stats(&self) -> ProverPoolStats {
        let busy = self.slots.values().filter(|slot| slot.busy).count();
        ProverPoolStats {
            slots: self.slots.len(),
            idle: self.slots.len().saturating_sub(busy),
            busy,
            completed: self.slots.values().map(|slot| slot.completed).sum(),
        }
    }
}

/// A candidate investigation evaluated by the curiosity model.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CuriosityCandidate {
    /// Candidate action or investigation id.
    pub action: String,
    /// Current uncertainty, in `[0, 1]`.
    pub prior_uncertainty: f64,
    /// Predicted uncertainty after observing the result, in `[0, 1]`.
    pub expected_uncertainty: f64,
    /// Relative execution cost, greater than zero.
    pub cost: f64,
}

impl CuriosityCandidate {
    /// Creates a candidate and validates its numeric fields.
    pub fn new(
        action: impl Into<String>,
        prior_uncertainty: f64,
        expected_uncertainty: f64,
        cost: f64,
    ) -> Result<Self, ProverError> {
        let action = action.into();
        if action.trim().is_empty() {
            return Err(ProverError::EmptyField("candidate action"));
        }
        if !(0.0..=1.0).contains(&prior_uncertainty) {
            return Err(ProverError::InvalidCandidate("prior uncertainty"));
        }
        if !(0.0..=1.0).contains(&expected_uncertainty) {
            return Err(ProverError::InvalidCandidate("expected uncertainty"));
        }
        if !cost.is_finite() || cost <= 0.0 {
            return Err(ProverError::InvalidCandidate("cost"));
        }
        Ok(Self {
            action,
            prior_uncertainty,
            expected_uncertainty,
            cost,
        })
    }

    /// Expected information gain.
    pub fn gain(&self) -> f64 {
        (self.prior_uncertainty - self.expected_uncertainty).max(0.0)
    }

    /// Expected gain per unit cost.
    pub fn gain_per_cost(&self) -> f64 {
        self.gain() / self.cost
    }
}

/// A curiosity selection.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CuriosityChoice {
    /// Selected action.
    pub action: String,
    /// Expected gain.
    pub gain: f64,
    /// Expected gain per unit cost.
    pub gain_per_cost: f64,
    /// Candidate cost.
    pub cost: f64,
}

/// Deterministic curiosity ranker.
#[derive(Debug, Clone)]
pub struct CuriosityEngine {
    max_candidates: usize,
}

impl Default for CuriosityEngine {
    fn default() -> Self {
        Self::new()
    }
}

impl CuriosityEngine {
    /// Creates a ranker with the default bound.
    pub fn new() -> Self {
        Self::with_capacity(DEFAULT_MAX_CURIOSITY_CANDIDATES)
    }

    /// Creates a ranker with an explicit bound.
    pub fn with_capacity(max_candidates: usize) -> Self {
        Self {
            max_candidates: max_candidates.max(1),
        }
    }

    /// Selects the candidate with the highest expected gain per unit cost.
    ///
    /// Candidates with zero gain are ignored; ties are broken by lower cost
    /// and then lexicographically by action. Duplicate actions are rejected.
    pub fn select(
        &self,
        candidates: impl IntoIterator<Item = CuriosityCandidate>,
    ) -> Result<CuriosityChoice, ProverError> {
        let mut seen: Vec<String> = Vec::new();
        let mut best: Option<&CuriosityCandidate> = None;
        let mut owned = Vec::new();
        for candidate in candidates {
            if seen.len() >= self.max_candidates {
                return Err(ProverError::CandidateCapacityReached);
            }
            if seen.iter().any(|action| action == &candidate.action) {
                return Err(ProverError::DuplicateCandidate(candidate.action));
            }
            seen.push(candidate.action.clone());
            owned.push(candidate);
        }
        for candidate in &owned {
            if candidate.gain() <= 0.0 {
                continue;
            }
            let better = match best {
                None => true,
                Some(current) => {
                    candidate.gain_per_cost() > current.gain_per_cost()
                        || ((candidate.gain_per_cost() - current.gain_per_cost()).abs() < 1e-12
                            && (candidate.cost < current.cost
                                || (candidate.cost == current.cost
                                    && candidate.action < current.action)))
                }
            };
            if better {
                best = Some(candidate);
            }
        }
        let candidate = best.ok_or(ProverError::NoPositiveGain)?;
        Ok(CuriosityChoice {
            action: candidate.action.clone(),
            gain: candidate.gain(),
            gain_per_cost: candidate.gain_per_cost(),
            cost: candidate.cost,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prewarm_creates_deterministic_ids() {
        let mut pool = ProverPool::new();
        assert_eq!(
            pool.prewarm(ProverKind::Lean4, 2).unwrap(),
            vec!["Lean4-0", "Lean4-1"]
        );
        assert_eq!(pool.stats().slots, 2);
        assert_eq!(pool.stats().idle, 2);
    }

    #[test]
    fn acquire_leases_first_idle_slot() {
        let mut pool = ProverPool::new();
        pool.prewarm(ProverKind::Isabelle, 2).unwrap();
        assert_eq!(pool.acquire(ProverKind::Isabelle).unwrap(), "Isabelle-0");
        assert_eq!(pool.acquire(ProverKind::Isabelle).unwrap(), "Isabelle-1");
        assert_eq!(
            pool.acquire(ProverKind::Isabelle),
            Err(ProverError::NoWarmProver(ProverKind::Isabelle))
        );
    }

    #[test]
    fn kinds_do_not_cross_lease() {
        let mut pool = ProverPool::new();
        pool.prewarm(ProverKind::Miri, 1).unwrap();
        assert_eq!(
            pool.acquire(ProverKind::Lean4),
            Err(ProverError::NoWarmProver(ProverKind::Lean4))
        );
    }

    #[test]
    fn release_marks_idle_and_counts_success() {
        let mut pool = ProverPool::new();
        pool.prewarm(ProverKind::Lean4, 1).unwrap();
        let slot = pool.acquire(ProverKind::Lean4).unwrap();
        pool.release(&slot, true).unwrap();
        assert_eq!(pool.slot(&slot).unwrap().completed, 1);
        assert!(!pool.slot(&slot).unwrap().busy);
        assert_eq!(pool.stats().completed, 1);
    }

    #[test]
    fn release_rejects_unleased_and_unknown_slots() {
        let mut pool = ProverPool::new();
        pool.prewarm(ProverKind::Lean4, 1).unwrap();
        assert_eq!(
            pool.release("Lean4-0", true),
            Err(ProverError::SlotNotLeased("Lean4-0".into()))
        );
        assert_eq!(
            pool.release("missing", true),
            Err(ProverError::UnknownSlot("missing".into()))
        );
    }

    #[test]
    fn capacity_is_bounded() {
        let mut pool = ProverPool::with_capacity(1);
        pool.prewarm(ProverKind::Miri, 1).unwrap();
        assert_eq!(
            pool.prewarm(ProverKind::Miri, 1),
            Err(ProverError::CapacityReached)
        );
    }

    #[test]
    fn prover_stats_track_busy_and_idle() {
        let mut pool = ProverPool::new();
        pool.prewarm(ProverKind::Isabelle, 1).unwrap();
        pool.prewarm(ProverKind::Miri, 1).unwrap();
        pool.acquire(ProverKind::Miri).unwrap();
        let stats = pool.stats();
        assert_eq!(stats.slots, 2);
        assert_eq!(stats.idle, 1);
        assert_eq!(stats.busy, 1);
    }

    #[test]
    fn curiosity_prefers_gain_per_cost() {
        let engine = CuriosityEngine::new();
        let candidates = vec![
            CuriosityCandidate::new("a", 0.9, 0.4, 2.0).unwrap(),
            CuriosityCandidate::new("b", 0.8, 0.2, 1.0).unwrap(),
        ];
        let choice = engine.select(candidates).unwrap();
        assert_eq!(choice.action, "b");
        assert!((choice.gain - 0.6).abs() < 1e-12);
        assert!((choice.gain_per_cost - 0.6).abs() < 1e-12);
    }

    #[test]
    fn curiosity_rejects_zero_gain() {
        let engine = CuriosityEngine::new();
        let candidates = vec![CuriosityCandidate::new("a", 0.5, 0.5, 1.0).unwrap()];
        assert_eq!(engine.select(candidates), Err(ProverError::NoPositiveGain));
    }

    #[test]
    fn curiosity_ties_break_by_cost_then_action() {
        let engine = CuriosityEngine::new();
        let candidates = vec![
            CuriosityCandidate::new("b", 0.7, 0.2, 1.0).unwrap(),
            CuriosityCandidate::new("a", 0.7, 0.2, 1.0).unwrap(),
        ];
        assert_eq!(engine.select(candidates).unwrap().action, "a");
    }

    #[test]
    fn invalid_candidates_are_rejected() {
        assert!(CuriosityCandidate::new("", 0.5, 0.2, 1.0).is_err());
        assert!(CuriosityCandidate::new("a", 1.1, 0.2, 1.0).is_err());
        assert!(CuriosityCandidate::new("a", 0.5, -0.1, 1.0).is_err());
        assert!(CuriosityCandidate::new("a", 0.5, 0.2, 0.0).is_err());
    }

    #[test]
    fn duplicate_actions_are_rejected() {
        let engine = CuriosityEngine::new();
        let candidates = vec![
            CuriosityCandidate::new("a", 0.8, 0.2, 1.0).unwrap(),
            CuriosityCandidate::new("a", 0.9, 0.2, 1.0).unwrap(),
        ];
        assert_eq!(
            engine.select(candidates),
            Err(ProverError::DuplicateCandidate("a".into()))
        );
    }
}
