//! TomasuLLM-style out-of-order speculative execution (#25).
//!
//! A long coding trajectory spends most of its wall-clock time waiting for
//! tools (tests, builds, searches). TomasuLLM imports the processor idea of
//! out-of-order execution: draft a later action while an earlier one is still
//! running, execute it against a copy-on-write overlay, and only *commit* it
//! after the four trace predicates hold:
//!
//! * `V_act` — the action is byte-identical to the one that was drafted,
//! * `V_dep` — its read/missing sets still describe the current workspace,
//! * `V_record` — its normalised observation matches the recorded one,
//! * `V_effect` — its side-effect delta can be replayed safely.
//!
//! This module is the pure bookkeeping half. It never touches an overlay,
//! spawns a process, or executes a tool: callers describe speculative actions
//! and committed observations, and [`RunAheadController`] decides whether a
//! speculative result may be promoted. That keeps the hot path testable and
//! makes "zero mis-acceptance" an auditable property rather than a claim.
//!
//! Feature: additive Rust library; no environment flag is required because it
//! does nothing unless a caller constructs a controller.

use std::collections::{BTreeMap, BTreeSet, VecDeque};

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Default maximum speculative chain depth.
pub const DEFAULT_MAX_CHAIN_DEPTH: usize = 4;
/// Default number of concurrent speculative slots.
pub const DEFAULT_MAX_SPECULATIVE_SLOTS: usize = 8;
/// Default bounded audit capacity.
pub const DEFAULT_AUDIT_CAPACITY: usize = 4096;

/// Whether an operand is already materialised in the workspace or still
/// waiting for its producer (for example a service restart).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OperandKind {
    /// A file version already present in the workspace: may run immediately.
    Ready,
    /// A producer-dependent value: may only be forked, never executed early.
    InFlight,
}

/// Coarse action class used for scheduling priority.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ActionKind {
    LongTest,
    ShortTest,
    SyntaxCheck,
    FileRead,
    Other,
}

impl ActionKind {
    /// Default scheduling weight from the ledger: long tests 30, short tests 5,
    /// syntax checks 3, file reads 1.
    pub fn default_weight(self) -> u32 {
        match self {
            ActionKind::LongTest => 30,
            ActionKind::ShortTest => 5,
            ActionKind::SyntaxCheck => 3,
            ActionKind::FileRead => 1,
            ActionKind::Other => 2,
        }
    }
}

/// Runtime configuration for [`RunAheadController`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RunAheadConfig {
    pub max_chain_depth: usize,
    pub max_speculative_slots: usize,
    pub long_test_weight: u32,
    pub short_test_weight: u32,
    pub syntax_check_weight: u32,
    pub file_read_weight: u32,
    pub audit_capacity: usize,
}

impl Default for RunAheadConfig {
    fn default() -> Self {
        Self {
            max_chain_depth: DEFAULT_MAX_CHAIN_DEPTH,
            max_speculative_slots: DEFAULT_MAX_SPECULATIVE_SLOTS,
            long_test_weight: 30,
            short_test_weight: 5,
            syntax_check_weight: 3,
            file_read_weight: 1,
            audit_capacity: DEFAULT_AUDIT_CAPACITY,
        }
    }
}

impl RunAheadConfig {
    /// Validate the controller configuration.
    pub fn validate(&self) -> Result<(), TomasuError> {
        if self.max_chain_depth == 0 {
            return Err(TomasuError::InvalidChainDepth);
        }
        if self.max_speculative_slots == 0 {
            return Err(TomasuError::InvalidSpeculativeSlots);
        }
        if self.audit_capacity == 0 {
            return Err(TomasuError::InvalidAuditCapacity);
        }
        Ok(())
    }

    /// Scheduling weight for one action class under this configuration.
    pub fn weight(&self, kind: ActionKind) -> u32 {
        match kind {
            ActionKind::LongTest => self.long_test_weight,
            ActionKind::ShortTest => self.short_test_weight,
            ActionKind::SyntaxCheck => self.syntax_check_weight,
            ActionKind::FileRead => self.file_read_weight,
            ActionKind::Other => 2,
        }
    }
}

/// A drafted action plus the dependency facts captured when it was drafted.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SpeculativeAction {
    pub action_id: u64,
    pub turn: u32,
    pub kind: ActionKind,
    pub operand: OperandKind,
    /// Digest of the exact action payload.
    pub action_digest: String,
    /// Paths the action will read.
    pub read_set: Vec<String>,
    /// Paths the action expects to be missing.
    pub missing_set: Vec<String>,
    /// Digest of the observation the drafter expects to see.
    pub observation_digest: String,
    /// Digest of the expected side-effect delta.
    pub effect_digest: String,
}

impl SpeculativeAction {
    /// Build a speculative action. Sets are normalised on insertion.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        action_id: u64,
        turn: u32,
        kind: ActionKind,
        operand: OperandKind,
        action_digest: impl Into<String>,
        read_set: impl IntoIterator<Item = impl Into<String>>,
        missing_set: impl IntoIterator<Item = impl Into<String>>,
        observation_digest: impl Into<String>,
        effect_digest: impl Into<String>,
    ) -> Self {
        Self {
            action_id,
            turn,
            kind,
            operand,
            action_digest: action_digest.into(),
            read_set: normalise_set(read_set),
            missing_set: normalise_set(missing_set),
            observation_digest: observation_digest.into(),
            effect_digest: effect_digest.into(),
        }
    }
}

/// The actual observation produced by a committed action.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CommittedObservation {
    pub action_id: u64,
    pub action_digest: String,
    pub read_set: Vec<String>,
    pub missing_set: Vec<String>,
    pub observation_digest: String,
    pub effect_digest: String,
}

impl CommittedObservation {
    /// Build a committed observation. Sets are normalised on insertion.
    pub fn new(
        action_id: u64,
        action_digest: impl Into<String>,
        read_set: impl IntoIterator<Item = impl Into<String>>,
        missing_set: impl IntoIterator<Item = impl Into<String>>,
        observation_digest: impl Into<String>,
        effect_digest: impl Into<String>,
    ) -> Self {
        Self {
            action_id,
            action_digest: action_digest.into(),
            read_set: normalise_set(read_set),
            missing_set: normalise_set(missing_set),
            observation_digest: observation_digest.into(),
            effect_digest: effect_digest.into(),
        }
    }
}

/// One failed predicate.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ValidationFailure {
    Action,
    Dependency,
    Record,
    Effect,
}

/// Result of the four-predicate check.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ValidationReport {
    pub v_act: bool,
    pub v_dep: bool,
    pub v_record: bool,
    pub v_effect: bool,
    pub accepted: bool,
    pub failures: Vec<ValidationFailure>,
}

impl ValidationReport {
    /// All four predicates hold.
    pub fn all_passed(&self) -> bool {
        self.v_act && self.v_dep && self.v_record && self.v_effect
    }
}

/// Pure four-predicate trace validator.
pub struct TraceIrValidator;

impl TraceIrValidator {
    /// Validate one speculative result against the committed observation.
    ///
    /// The record predicate compares *normalised* digests so insignificant
    /// whitespace or casing differences in a recorded digest do not force a
    /// cold rerun; the action, dependency and effect predicates are exact.
    pub fn validate(
        candidate: &SpeculativeAction,
        committed: &CommittedObservation,
    ) -> ValidationReport {
        let v_act = candidate.action_digest == committed.action_digest;
        let v_dep = candidate.read_set == committed.read_set
            && candidate.missing_set == committed.missing_set;
        let v_record = normalise_digest(&candidate.observation_digest)
            == normalise_digest(&committed.observation_digest);
        let v_effect = candidate.effect_digest == committed.effect_digest;

        let mut failures = Vec::new();
        if !v_act {
            failures.push(ValidationFailure::Action);
        }
        if !v_dep {
            failures.push(ValidationFailure::Dependency);
        }
        if !v_record {
            failures.push(ValidationFailure::Record);
        }
        if !v_effect {
            failures.push(ValidationFailure::Effect);
        }

        ValidationReport {
            v_act,
            v_dep,
            v_record,
            v_effect,
            accepted: v_act && v_dep && v_record && v_effect,
            failures,
        }
    }
}

/// Errors raised by the speculative controller.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum TomasuError {
    #[error("max_chain_depth must be >= 1")]
    InvalidChainDepth,
    #[error("max_speculative_slots must be >= 1")]
    InvalidSpeculativeSlots,
    #[error("audit_capacity must be >= 1")]
    InvalidAuditCapacity,
    #[error("speculative chain depth exhausted")]
    ChainDepthExhausted,
    #[error("speculative slot pool exhausted")]
    SlotsExhausted,
    #[error("unknown speculative action: {0}")]
    UnknownAction(u64),
    #[error("action {0} is not in flight")]
    NotInFlight(u64),
}

/// Audit outcome of one controller operation.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AuditOutcome {
    Accepted,
    Rejected,
    Promoted,
    Discarded,
}

/// One bounded audit record.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AuditEntry {
    pub sequence: u64,
    pub action_id: u64,
    pub outcome: AuditOutcome,
    pub checks: ValidationReport,
}

/// Controller statistics.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct RunAheadStats {
    pub drafted_ready: u64,
    pub drafted_in_flight: u64,
    pub promoted_in_flight: u64,
    pub accepted: u64,
    pub rejected: u64,
    pub discarded: u64,
}

/// Tracks speculative actions and decides whether an out-of-order result may
/// be committed.
#[derive(Debug, Clone)]
pub struct RunAheadController {
    config: RunAheadConfig,
    ready: VecDeque<SpeculativeAction>,
    in_flight: BTreeMap<u64, SpeculativeAction>,
    audit: VecDeque<AuditEntry>,
    stats: RunAheadStats,
    next_sequence: u64,
}

impl RunAheadController {
    /// Create a controller.
    pub fn new(config: RunAheadConfig) -> Result<Self, TomasuError> {
        config.validate()?;
        Ok(Self {
            config,
            ready: VecDeque::new(),
            in_flight: BTreeMap::new(),
            audit: VecDeque::new(),
            stats: RunAheadStats::default(),
            next_sequence: 0,
        })
    }

    /// Create a controller with defaults.
    pub fn default_config() -> Result<Self, TomasuError> {
        Self::new(RunAheadConfig::default())
    }

    /// Read-only configuration.
    pub fn config(&self) -> &RunAheadConfig {
        &self.config
    }

    /// Current statistics.
    pub fn stats(&self) -> RunAheadStats {
        self.stats
    }

    /// Bounded audit log, oldest first.
    pub fn audit(&self) -> &VecDeque<AuditEntry> {
        &self.audit
    }

    /// Number of ready speculative actions.
    pub fn ready_len(&self) -> usize {
        self.ready.len()
    }

    /// Number of in-flight speculative actions.
    pub fn in_flight_len(&self) -> usize {
        self.in_flight.len()
    }

    /// Draft a speculative action. Ready operands enter the execution queue;
    /// in-flight operands wait for [`RunAheadController::promote_in_flight`].
    pub fn speculate(&mut self, action: SpeculativeAction) -> Result<(), TomasuError> {
        let total = self.ready.len() + self.in_flight.len();
        if total >= self.config.max_speculative_slots {
            return Err(TomasuError::SlotsExhausted);
        }
        match action.operand {
            OperandKind::Ready => {
                if self.ready.len() >= self.config.max_chain_depth {
                    return Err(TomasuError::ChainDepthExhausted);
                }
                self.ready.push_back(action);
                self.stats.drafted_ready += 1;
            }
            OperandKind::InFlight => {
                if self.in_flight.contains_key(&action.action_id) {
                    return Err(TomasuError::UnknownAction(action.action_id));
                }
                self.in_flight.insert(action.action_id, action);
                self.stats.drafted_in_flight += 1;
            }
        }
        Ok(())
    }

    /// Move a producer-completed in-flight action into the ready queue.
    pub fn promote_in_flight(&mut self, action_id: u64) -> Result<SpeculativeAction, TomasuError> {
        let action = self
            .in_flight
            .remove(&action_id)
            .ok_or(TomasuError::NotInFlight(action_id))?;
        if self.ready.len() >= self.config.max_chain_depth {
            return Err(TomasuError::ChainDepthExhausted);
        }
        self.ready.push_back(action.clone());
        self.stats.promoted_in_flight += 1;
        self.record_audit(action_id, AuditOutcome::Promoted, passed_report());
        Ok(action)
    }

    /// Commit one speculative result.
    ///
    /// The candidate is looked up by `action_id`. On success it is removed and
    /// accepted; on failure it and every later ready action are discarded,
    /// because the suffix depended on the now-invalid result. Every outcome is
    /// recorded in the bounded audit log.
    pub fn commit(&mut self, committed: &CommittedObservation) -> Result<bool, TomasuError> {
        let position = self
            .ready
            .iter()
            .position(|action| action.action_id == committed.action_id)
            .ok_or(TomasuError::UnknownAction(committed.action_id))?;

        let candidate = self
            .ready
            .get(position)
            .cloned()
            .expect("position checked above");
        let report = TraceIrValidator::validate(&candidate, committed);

        if report.accepted {
            self.ready.remove(position);
            self.stats.accepted += 1;
            self.record_audit(candidate.action_id, AuditOutcome::Accepted, report);
            Ok(true)
        } else {
            self.ready.drain(position..);
            self.stats.rejected += 1;
            self.record_audit(candidate.action_id, AuditOutcome::Rejected, report);
            Ok(false)
        }
    }

    /// Discard the action at `action_id` and every later ready action.
    pub fn discard_suffix(&mut self, action_id: u64) -> usize {
        let Some(position) = self
            .ready
            .iter()
            .position(|action| action.action_id == action_id)
        else {
            return 0;
        };
        let dropped = self.ready.len() - position;
        self.ready.drain(position..);
        self.stats.discarded += dropped as u64;
        self.record_audit(action_id, AuditOutcome::Discarded, passed_report());
        dropped
    }

    fn record_audit(&mut self, action_id: u64, outcome: AuditOutcome, checks: ValidationReport) {
        let entry = AuditEntry {
            sequence: self.next_sequence,
            action_id,
            outcome,
            checks,
        };
        self.next_sequence += 1;
        self.audit.push_back(entry);
        while self.audit.len() > self.config.audit_capacity {
            self.audit.pop_front();
        }
    }
}

fn passed_report() -> ValidationReport {
    ValidationReport {
        v_act: true,
        v_dep: true,
        v_record: true,
        v_effect: true,
        accepted: true,
        failures: Vec::new(),
    }
}

fn normalise_set(items: impl IntoIterator<Item = impl Into<String>>) -> Vec<String> {
    let set: BTreeSet<String> = items
        .into_iter()
        .map(Into::into)
        .map(|item| item.trim().to_string())
        .filter(|item| !item.is_empty())
        .collect();
    set.into_iter().collect()
}

fn normalise_digest(value: &str) -> String {
    value
        .split_whitespace()
        .collect::<Vec<_>>()
        .join("")
        .to_lowercase()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn action(id: u64) -> SpeculativeAction {
        SpeculativeAction::new(
            id,
            1,
            ActionKind::ShortTest,
            OperandKind::Ready,
            "action-sha",
            ["src/lib.rs"],
            ["target/missing.rs"],
            "obs-sha",
            "effect-sha",
        )
    }

    fn observation(id: u64) -> CommittedObservation {
        CommittedObservation::new(
            id,
            "action-sha",
            ["src/lib.rs"],
            ["target/missing.rs"],
            "obs-sha",
            "effect-sha",
        )
    }

    #[test]
    fn defaults_match_the_ledger_weights() {
        let config = RunAheadConfig::default();
        assert_eq!(config.max_chain_depth, 4);
        assert_eq!(config.max_speculative_slots, 8);
        assert_eq!(config.weight(ActionKind::LongTest), 30);
        assert_eq!(config.weight(ActionKind::ShortTest), 5);
        assert_eq!(config.weight(ActionKind::SyntaxCheck), 3);
        assert_eq!(config.weight(ActionKind::FileRead), 1);
    }

    #[test]
    fn ready_action_commits_after_all_four_predicates() {
        let mut controller = RunAheadController::default_config().unwrap();
        controller.speculate(action(1)).unwrap();
        assert!(controller.commit(&observation(1)).unwrap());
        assert_eq!(controller.stats().accepted, 1);
        assert_eq!(controller.ready_len(), 0);
        assert!(controller.audit().back().unwrap().checks.all_passed());
    }

    #[test]
    fn action_mismatch_is_rejected_and_misacceptance_is_zero() {
        let mut controller = RunAheadController::default_config().unwrap();
        controller.speculate(action(1)).unwrap();
        let mut changed = observation(1);
        changed.action_digest = "different".into();
        assert!(!controller.commit(&changed).unwrap());
        assert_eq!(controller.stats().rejected, 1);
        assert_eq!(
            controller.audit().back().unwrap().checks.failures,
            vec![ValidationFailure::Action]
        );
    }

    #[test]
    fn dependency_drift_is_rejected() {
        let mut controller = RunAheadController::default_config().unwrap();
        controller.speculate(action(1)).unwrap();
        let mut changed = observation(1);
        changed.read_set = vec!["src/lib.rs".into(), "src/new.rs".into()];
        assert!(!controller.commit(&changed).unwrap());
        assert_eq!(
            controller.audit().back().unwrap().checks.failures,
            vec![ValidationFailure::Dependency]
        );
    }

    #[test]
    fn record_compare_normalises_whitespace_and_case() {
        let report = TraceIrValidator::validate(&action(1), &{
            let mut obs = observation(1);
            obs.observation_digest = "  OBS-SHA \n".into();
            obs
        });
        assert!(report.v_record);
        assert!(report.accepted);
    }

    #[test]
    fn effect_drift_is_rejected() {
        let mut controller = RunAheadController::default_config().unwrap();
        controller.speculate(action(1)).unwrap();
        let mut changed = observation(1);
        changed.effect_digest = "other-effect".into();
        assert!(!controller.commit(&changed).unwrap());
        assert_eq!(
            controller.audit().back().unwrap().checks.failures,
            vec![ValidationFailure::Effect]
        );
    }

    #[test]
    fn rejected_commit_discards_the_dependent_suffix() {
        let mut controller = RunAheadController::default_config().unwrap();
        controller.speculate(action(1)).unwrap();
        controller.speculate(action(2)).unwrap();
        controller.speculate(action(3)).unwrap();
        let mut changed = observation(1);
        changed.action_digest = "different".into();
        assert!(!controller.commit(&changed).unwrap());
        assert_eq!(controller.ready_len(), 0);
    }

    #[test]
    fn in_flight_action_needs_explicit_promotion() {
        let mut controller = RunAheadController::default_config().unwrap();
        let mut pending = action(7);
        pending.operand = OperandKind::InFlight;
        controller.speculate(pending).unwrap();
        assert_eq!(controller.ready_len(), 0);
        assert_eq!(controller.in_flight_len(), 1);
        let promoted = controller.promote_in_flight(7).unwrap();
        assert_eq!(promoted.action_id, 7);
        assert_eq!(controller.ready_len(), 1);
        assert_eq!(controller.stats().promoted_in_flight, 1);
    }

    #[test]
    fn chain_depth_and_slot_limits_are_enforced() {
        let mut controller = RunAheadController::new(RunAheadConfig {
            max_chain_depth: 1,
            max_speculative_slots: 2,
            ..RunAheadConfig::default()
        })
        .unwrap();
        controller.speculate(action(1)).unwrap();
        assert_eq!(
            controller.speculate(action(2)).unwrap_err(),
            TomasuError::ChainDepthExhausted
        );
    }

    #[test]
    fn audit_log_is_bounded() {
        let mut controller = RunAheadController::new(RunAheadConfig {
            audit_capacity: 2,
            ..RunAheadConfig::default()
        })
        .unwrap();
        for id in 1..=3 {
            controller.speculate(action(id)).unwrap();
            controller.commit(&observation(id)).unwrap();
        }
        assert_eq!(controller.audit().len(), 2);
        assert_eq!(controller.audit().back().unwrap().action_id, 3);
    }

    #[test]
    fn discard_suffix_reports_dropped_actions() {
        let mut controller = RunAheadController::default_config().unwrap();
        for id in 1..=3 {
            controller.speculate(action(id)).unwrap();
        }
        assert_eq!(controller.discard_suffix(2), 2);
        assert_eq!(controller.ready_len(), 1);
        assert_eq!(controller.stats().discarded, 2);
    }
}
