//! SetupX LIFO experience replay (#69): speculative repository setup against a
//! stack of copy-on-write snapshots with separated prosecutor/judge evidence.
//!
//! The module is pure bookkeeping. Callers register experience units (XPUs),
//! ask for a speculation, then submit already-collected dynamic-link, port and
//! smoke-test evidence. Rejected speculations pop the snapshot in O(1) and
//! update the XPU posterior; accepted speculations keep the checkpoint for a
//! later replay.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Default bound on registered experience units.
pub const DEFAULT_MAX_XPUS: usize = 64;
/// Default bound on retained snapshots.
pub const DEFAULT_MAX_SNAPSHOTS: usize = 64;

/// One setup experience unit: signature, textual guidance and executable steps.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Xpu {
    /// Stable identifier.
    pub id: String,
    /// Failure/dependency fingerprint this unit matches.
    pub signature: String,
    /// Human-readable diagnosis and repair strategy.
    pub guidance: String,
    /// Parameterised repair commands.
    pub actions: Vec<String>,
}

impl Xpu {
    /// Build an experience unit.
    #[must_use]
    pub fn new(
        id: impl Into<String>,
        signature: impl Into<String>,
        guidance: impl Into<String>,
        actions: Vec<String>,
    ) -> Self {
        Self {
            id: id.into(),
            signature: signature.into(),
            guidance: guidance.into(),
            actions,
        }
    }
}

/// Prosecutor evidence collected before the judge decides.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct SetupEvidence {
    /// Dynamic-link probe passed.
    pub dynamic_link_ok: bool,
    /// Required ports are reachable.
    pub ports_ok: bool,
    /// Smoke test passed.
    pub smoke_ok: bool,
}

impl SetupEvidence {
    /// Build an evidence bundle.
    #[must_use]
    pub fn new(dynamic_link_ok: bool, ports_ok: bool, smoke_ok: bool) -> Self {
        Self {
            dynamic_link_ok,
            ports_ok,
            smoke_ok,
        }
    }

    /// All three probes passed.
    #[must_use]
    pub fn all_ok(self) -> bool {
        self.dynamic_link_ok && self.ports_ok && self.smoke_ok
    }
}

/// Judge verdict.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Verdict {
    /// Environment is usable; keep the checkpoint.
    Accepted,
    /// Environment is not usable; roll the snapshot back.
    Rejected,
}

/// A copy-on-write snapshot on the LIFO stack.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Snapshot {
    /// Snapshot identifier.
    pub id: String,
    /// XPU that produced it.
    pub xpu_id: String,
    /// Content digest.
    pub digest: String,
    /// Stack depth after the push.
    pub depth: usize,
}

/// Active speculation.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Speculation {
    /// XPU under test.
    pub xpu_id: String,
    /// Snapshot pushed for the test.
    pub snapshot_id: String,
}

/// Result of judging one speculation.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SpeculationOutcome {
    /// XPU under test.
    pub xpu_id: String,
    /// Snapshot pushed for the test.
    pub snapshot_id: String,
    /// Judge verdict.
    pub verdict: Verdict,
    /// Posterior success probability after the observation.
    pub posterior: f64,
}

/// Errors returned by the SetupX runner.
#[derive(Debug, Error, PartialEq)]
pub enum SetupXError {
    /// A required field was empty.
    #[error("field `{0}` must not be empty")]
    EmptyField(&'static str),
    /// XPU identifier already registered.
    #[error("xpU `{0}` is already registered")]
    DuplicateXpu(String),
    /// XPU capacity reached.
    #[error("xpU capacity of {0} reached")]
    CapacityReached(usize),
    /// No XPU matched the signature.
    #[error("no xpU matches signature `{0}`")]
    UnknownXpu(String),
    /// A speculation is already in flight.
    #[error("a speculation is already pending")]
    PendingSpeculation,
    /// No speculation is in flight.
    #[error("no speculation is pending")]
    NoPendingSpeculation,
    /// Snapshot capacity reached.
    #[error("snapshot capacity of {0} reached")]
    SnapshotCapacityReached(usize),
    /// The snapshot stack is empty.
    #[error("snapshot stack is empty")]
    EmptySnapshotStack,
}

/// LIFO setup runner with posteriors.
#[derive(Debug, Clone, PartialEq)]
pub struct SetupXLifo {
    max_xpus: usize,
    max_snapshots: usize,
    xpus: BTreeMap<String, Xpu>,
    snapshots: Vec<Snapshot>,
    outcomes: BTreeMap<String, (u32, u32)>,
    pending: Option<Speculation>,
}

impl Default for SetupXLifo {
    fn default() -> Self {
        Self::new()
    }
}

impl SetupXLifo {
    /// Create a runner with default bounds.
    #[must_use]
    pub fn new() -> Self {
        Self::with_capacity(DEFAULT_MAX_XPUS, DEFAULT_MAX_SNAPSHOTS)
    }

    /// Create a runner with explicit bounds.
    #[must_use]
    pub fn with_capacity(max_xpus: usize, max_snapshots: usize) -> Self {
        Self {
            max_xpus,
            max_snapshots,
            xpus: BTreeMap::new(),
            snapshots: Vec::new(),
            outcomes: BTreeMap::new(),
            pending: None,
        }
    }

    /// Register an experience unit.
    pub fn register(&mut self, xpu: Xpu) -> Result<(), SetupXError> {
        if xpu.id.trim().is_empty() {
            return Err(SetupXError::EmptyField("id"));
        }
        if xpu.signature.trim().is_empty() {
            return Err(SetupXError::EmptyField("signature"));
        }
        if self.xpus.contains_key(&xpu.id) {
            return Err(SetupXError::DuplicateXpu(xpu.id));
        }
        if self.xpus.len() >= self.max_xpus {
            return Err(SetupXError::CapacityReached(self.max_xpus));
        }
        self.xpus.insert(xpu.id.clone(), xpu);
        Ok(())
    }

    /// Registered experience units in id order.
    #[must_use]
    pub fn xpus(&self) -> Vec<Xpu> {
        self.xpus.values().cloned().collect()
    }

    /// Snapshot stack from oldest to newest.
    #[must_use]
    pub fn snapshots(&self) -> &[Snapshot] {
        &self.snapshots
    }

    /// Current speculation, if any.
    #[must_use]
    pub fn pending(&self) -> Option<&Speculation> {
        self.pending.as_ref()
    }

    /// Number of registered XPUs.
    #[must_use]
    pub fn xpu_count(&self) -> usize {
        self.xpus.len()
    }

    /// Beta-smoothed posterior `(successes + 1) / (attempts + 2)`.
    #[must_use]
    pub fn posterior(&self, xpu_id: &str) -> Option<f64> {
        if !self.xpus.contains_key(xpu_id) {
            return None;
        }
        let (successes, attempts) = self.outcomes.get(xpu_id).copied().unwrap_or((0, 0));
        Some((f64::from(successes) + 1.0) / (f64::from(attempts) + 2.0))
    }

    /// Push a snapshot and select the best matching XPU.
    ///
    /// Matching is exact on the signature; ties are broken by posterior and
    /// then by XPU id so the result is deterministic.
    pub fn speculate(&mut self, signature: &str) -> Result<Speculation, SetupXError> {
        if signature.trim().is_empty() {
            return Err(SetupXError::EmptyField("signature"));
        }
        if self.pending.is_some() {
            return Err(SetupXError::PendingSpeculation);
        }
        if self.snapshots.len() >= self.max_snapshots {
            return Err(SetupXError::SnapshotCapacityReached(self.max_snapshots));
        }

        let chosen = self
            .xpus
            .values()
            .filter(|xpu| xpu.signature == signature)
            .max_by(|left, right| {
                let left_score = self.posterior(&left.id).unwrap_or(0.5);
                let right_score = self.posterior(&right.id).unwrap_or(0.5);
                left_score
                    .partial_cmp(&right_score)
                    .unwrap_or(std::cmp::Ordering::Equal)
                    .then_with(|| right.id.cmp(&left.id))
            })
            .cloned()
            .ok_or_else(|| SetupXError::UnknownXpu(signature.to_string()))?;

        let depth = self.snapshots.len() + 1;
        let digest =
            blake3::hash(format!("{}:{}:{}", chosen.id, chosen.signature, depth).as_bytes())
                .to_hex()
                .to_string();
        let snapshot = Snapshot {
            id: format!("snap-{depth}-{}", chosen.id),
            xpu_id: chosen.id.clone(),
            digest,
            depth,
        };
        self.snapshots.push(snapshot.clone());
        let speculation = Speculation {
            xpu_id: chosen.id,
            snapshot_id: snapshot.id,
        };
        self.pending = Some(speculation.clone());
        Ok(speculation)
    }

    /// Judge the pending speculation. A rejection pops its snapshot; an
    /// acceptance keeps it as a replay checkpoint.
    pub fn judge(&mut self, evidence: SetupEvidence) -> Result<SpeculationOutcome, SetupXError> {
        let pending = self
            .pending
            .take()
            .ok_or(SetupXError::NoPendingSpeculation)?;
        let verdict = if evidence.all_ok() {
            Verdict::Accepted
        } else {
            Verdict::Rejected
        };

        if verdict == Verdict::Rejected {
            let popped = self.snapshots.pop();
            if popped.as_ref().map(|snapshot| snapshot.id.as_str())
                != Some(pending.snapshot_id.as_str())
            {
                return Err(SetupXError::EmptySnapshotStack);
            }
        }

        let entry = self
            .outcomes
            .entry(pending.xpu_id.clone())
            .or_insert((0, 0));
        entry.1 = entry.1.saturating_add(1);
        if verdict == Verdict::Accepted {
            entry.0 = entry.0.saturating_add(1);
        }
        let posterior = self.posterior(&pending.xpu_id).unwrap_or(0.5);
        Ok(SpeculationOutcome {
            xpu_id: pending.xpu_id,
            snapshot_id: pending.snapshot_id,
            verdict,
            posterior,
        })
    }

    /// Pop the newest snapshot without judging (operator rollback).
    pub fn rollback(&mut self) -> Result<Snapshot, SetupXError> {
        if self.pending.is_some() {
            return Err(SetupXError::PendingSpeculation);
        }
        self.snapshots.pop().ok_or(SetupXError::EmptySnapshotStack)
    }

    /// Stack depth.
    #[must_use]
    pub fn snapshot_depth(&self) -> usize {
        self.snapshots.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn runner() -> SetupXLifo {
        let mut runner = SetupXLifo::new();
        runner
            .register(Xpu::new(
                "xpu-a",
                "missing-libfoo",
                "install libfoo and refresh the loader cache",
                vec!["apt-get install -y libfoo".into(), "ldconfig".into()],
            ))
            .expect("register");
        runner
            .register(Xpu::new(
                "xpu-b",
                "missing-libfoo",
                "build libfoo from source",
                vec!["make -C vendor/libfoo".into()],
            ))
            .expect("register");
        runner
    }

    #[test]
    fn registers_and_orders_xpus() {
        let runner = runner();
        assert_eq!(runner.xpu_count(), 2);
        assert_eq!(runner.xpus()[0].id, "xpu-a");
        assert_eq!(runner.posterior("xpu-a"), Some(0.5));
        assert_eq!(runner.posterior("missing"), None);
    }

    #[test]
    fn duplicate_and_empty_xpus_are_rejected() {
        let mut runner = runner();
        assert!(matches!(
            runner
                .register(Xpu::new("xpu-a", "s", "g", vec![]))
                .unwrap_err(),
            SetupXError::DuplicateXpu(id) if id == "xpu-a"
        ));
        assert!(matches!(
            runner.register(Xpu::new("", "s", "g", vec![])).unwrap_err(),
            SetupXError::EmptyField("id")
        ));
        assert!(matches!(
            runner
                .register(Xpu::new("xpu-c", " ", "g", vec![]))
                .unwrap_err(),
            SetupXError::EmptyField("signature")
        ));
    }

    #[test]
    fn capacity_is_enforced() {
        let mut runner = SetupXLifo::with_capacity(1, 8);
        runner
            .register(Xpu::new("xpu-a", "s", "g", vec![]))
            .expect("register");
        assert_eq!(
            runner
                .register(Xpu::new("xpu-b", "s", "g", vec![]))
                .unwrap_err(),
            SetupXError::CapacityReached(1)
        );
    }

    #[test]
    fn speculation_pushes_a_snapshot() {
        let mut runner = runner();
        let speculation = runner.speculate("missing-libfoo").expect("speculate");
        assert_eq!(runner.snapshot_depth(), 1);
        assert_eq!(runner.snapshots()[0].depth, 1);
        assert_eq!(runner.pending(), Some(&speculation));
    }

    #[test]
    fn accepted_speculation_keeps_checkpoint() {
        let mut runner = runner();
        runner.speculate("missing-libfoo").expect("speculate");
        let outcome = runner
            .judge(SetupEvidence::new(true, true, true))
            .expect("judge");
        assert_eq!(outcome.verdict, Verdict::Accepted);
        assert_eq!(runner.snapshot_depth(), 1);
        assert!(runner.pending().is_none());
        assert!(outcome.posterior > 0.5);
    }

    #[test]
    fn rejected_speculation_pops_snapshot() {
        let mut runner = runner();
        runner.speculate("missing-libfoo").expect("speculate");
        let outcome = runner
            .judge(SetupEvidence::new(true, false, true))
            .expect("judge");
        assert_eq!(outcome.verdict, Verdict::Rejected);
        assert_eq!(runner.snapshot_depth(), 0);
        assert!(outcome.posterior < 0.5);
    }

    #[test]
    fn unknown_signature_and_pending_are_rejected() {
        let mut runner = runner();
        assert!(matches!(
            runner.speculate("missing-other").unwrap_err(),
            SetupXError::UnknownXpu(_)
        ));
        assert_eq!(
            runner
                .judge(SetupEvidence::new(true, true, true))
                .unwrap_err(),
            SetupXError::NoPendingSpeculation
        );
        runner.speculate("missing-libfoo").expect("speculate");
        assert_eq!(
            runner.speculate("missing-libfoo").unwrap_err(),
            SetupXError::PendingSpeculation
        );
    }

    #[test]
    fn snapshot_capacity_is_enforced() {
        let mut runner = SetupXLifo::with_capacity(2, 1);
        runner
            .register(Xpu::new("xpu-a", "s", "g", vec![]))
            .expect("register");
        runner.speculate("s").expect("speculate");
        runner
            .judge(SetupEvidence::new(true, true, true))
            .expect("judge");
        assert_eq!(
            runner.speculate("s").unwrap_err(),
            SetupXError::SnapshotCapacityReached(1)
        );
    }

    #[test]
    fn rollback_requires_idle_and_non_empty() {
        let mut runner = runner();
        assert_eq!(
            runner.rollback().unwrap_err(),
            SetupXError::EmptySnapshotStack
        );
        runner.speculate("missing-libfoo").expect("speculate");
        assert_eq!(
            runner.rollback().unwrap_err(),
            SetupXError::PendingSpeculation
        );
        runner
            .judge(SetupEvidence::new(true, true, true))
            .expect("judge");
        assert!(runner.rollback().is_ok());
        assert_eq!(runner.snapshot_depth(), 0);
    }

    #[test]
    fn posterior_prefers_successful_xpu_and_ties_break_by_id() {
        let mut runner = runner();
        let first = runner.speculate("missing-libfoo").expect("speculate");
        assert_eq!(first.xpu_id, "xpu-a");
        runner
            .judge(SetupEvidence::new(true, true, true))
            .expect("judge");
        assert!(runner.posterior("xpu-a").unwrap() > runner.posterior("xpu-b").unwrap());
        let second = runner.speculate("missing-libfoo").expect("speculate");
        assert_eq!(second.xpu_id, "xpu-a");
        runner
            .judge(SetupEvidence::new(false, true, true))
            .expect("judge");
        assert_eq!(
            runner.posterior("xpu-a").unwrap(),
            runner.posterior("xpu-b").unwrap()
        );
    }
}
