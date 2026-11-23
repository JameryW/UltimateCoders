//! Argus runtime state and Physical Mapping Guard (#77).
//!
//! Argus keeps model weights frozen and evolves a persistent, verification-gated
//! strategy state across waves. `PmgGuard` removes self-verification authority:
//! only an allowed external verifier may approve a strategy, and a deterministic
//! semantic-to-physical (S2P) mapping must observe every declared physical
//! constraint before execution is accepted.
//!
//! The module is pure bookkeeping. It does not run a model or a sandbox.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Maximum number of strategies retained by the runtime.
pub const DEFAULT_MAX_STRATEGIES: usize = 64;

/// Number of waves before the runtime is considered mature.
pub const MATURE_WAVE_THRESHOLD: u32 = 3;

/// One strategy that can be selected by the frozen-model runtime.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArgusStrategy {
    /// Stable strategy id.
    pub id: String,
    /// Human-readable description.
    pub description: String,
    /// Whether the strategy needs an external verifier before execution.
    pub external_verification_required: bool,
}

impl ArgusStrategy {
    /// Build a strategy. External verification is required by default.
    pub fn new(id: impl Into<String>, description: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            description: description.into(),
            external_verification_required: true,
        }
    }

    /// Allow a strategy that is already verified by construction.
    pub fn without_external_verification(mut self) -> Self {
        self.external_verification_required = false;
        self
    }
}

/// One execution wave outcome.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WaveOutcome {
    /// Wave number (1-based).
    pub wave: u32,
    /// Strategy used for this wave.
    pub strategy_id: String,
    /// External verifier result.
    pub verified: bool,
    /// Whether the S2P mapping observed every required physical constraint.
    pub physical_ok: bool,
    /// Tokens spent in the wave.
    pub token_cost: u64,
    /// Elapsed milliseconds.
    pub latency_ms: u64,
}

impl WaveOutcome {
    /// Whether the wave is a complete success.
    pub fn succeeded(&self) -> bool {
        self.verified && self.physical_ok
    }
}

/// Maturity phase of the runtime.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ArgusPhase {
    /// No waves recorded.
    Cold,
    /// One or two waves recorded.
    Warm,
    /// At least `MATURE_WAVE_THRESHOLD` waves recorded.
    Mature,
}

/// Aggregated runtime report.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ArgusReport {
    /// Number of waves recorded.
    pub waves: u32,
    /// Number of successful waves.
    pub successes: u32,
    /// Number of failed waves.
    pub failures: u32,
    /// Current maturity phase.
    pub phase: ArgusPhase,
    /// Token savings ratio of the latest wave versus the first wave, if both exist.
    pub token_savings_ratio: f64,
    /// Latency savings ratio of the latest wave versus the first wave, if both exist.
    pub latency_savings_ratio: f64,
}

/// Errors raised by Argus runtime bookkeeping.
#[derive(Debug, Error, PartialEq, Eq)]
pub enum ArgusError {
    /// A required string was empty.
    #[error("field {0} must not be empty")]
    EmptyField(&'static str),
    /// Strategy id already registered.
    #[error("duplicate strategy: {0}")]
    DuplicateStrategy(String),
    /// Strategy capacity reached.
    #[error("strategy capacity reached")]
    CapacityReached,
    /// Unknown strategy id.
    #[error("unknown strategy: {0}")]
    UnknownStrategy(String),
}

/// Persistent fixed-weight runtime state.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ArgusRuntime {
    strategies: HashMap<String, ArgusStrategy>,
    outcomes: Vec<WaveOutcome>,
    max_strategies: usize,
    next_wave: u32,
}

impl Default for ArgusRuntime {
    fn default() -> Self {
        Self::new()
    }
}

impl ArgusRuntime {
    /// Create a runtime with the default strategy capacity.
    pub fn new() -> Self {
        Self::with_capacity(DEFAULT_MAX_STRATEGIES)
    }

    /// Create a runtime with an explicit strategy capacity.
    pub fn with_capacity(max_strategies: usize) -> Self {
        Self {
            strategies: HashMap::new(),
            outcomes: Vec::new(),
            max_strategies,
            next_wave: 1,
        }
    }

    /// Register a strategy.
    pub fn register(&mut self, strategy: ArgusStrategy) -> Result<(), ArgusError> {
        if strategy.id.trim().is_empty() {
            return Err(ArgusError::EmptyField("strategy id"));
        }
        if strategy.description.trim().is_empty() {
            return Err(ArgusError::EmptyField("description"));
        }
        if self.strategies.contains_key(&strategy.id) {
            return Err(ArgusError::DuplicateStrategy(strategy.id));
        }
        if self.strategies.len() >= self.max_strategies {
            return Err(ArgusError::CapacityReached);
        }
        self.strategies.insert(strategy.id.clone(), strategy);
        Ok(())
    }

    /// Record a wave outcome for a registered strategy.
    pub fn record(
        &mut self,
        strategy_id: impl Into<String>,
        verified: bool,
        physical_ok: bool,
        token_cost: u64,
        latency_ms: u64,
    ) -> Result<WaveOutcome, ArgusError> {
        let strategy_id = strategy_id.into();
        if !self.strategies.contains_key(&strategy_id) {
            return Err(ArgusError::UnknownStrategy(strategy_id));
        }
        let outcome = WaveOutcome {
            wave: self.next_wave,
            strategy_id,
            verified,
            physical_ok,
            token_cost,
            latency_ms,
        };
        self.next_wave += 1;
        self.outcomes.push(outcome.clone());
        Ok(outcome)
    }

    /// Number of registered strategies.
    pub fn strategy_count(&self) -> usize {
        self.strategies.len()
    }

    /// Recorded waves in order.
    pub fn outcomes(&self) -> &[WaveOutcome] {
        &self.outcomes
    }

    /// Current maturity phase.
    pub fn phase(&self) -> ArgusPhase {
        match self.outcomes.len() as u32 {
            0 => ArgusPhase::Cold,
            waves if waves < MATURE_WAVE_THRESHOLD => ArgusPhase::Warm,
            _ => ArgusPhase::Mature,
        }
    }

    /// Aggregate report.
    pub fn report(&self) -> ArgusReport {
        let successes = self
            .outcomes
            .iter()
            .filter(|outcome| outcome.succeeded())
            .count() as u32;
        let failures = self.outcomes.len() as u32 - successes;
        let (token_savings_ratio, latency_savings_ratio) =
            match (self.outcomes.first(), self.outcomes.last()) {
                (Some(first), Some(last)) if self.outcomes.len() > 1 => (
                    savings(first.token_cost as f64, last.token_cost as f64),
                    savings(first.latency_ms as f64, last.latency_ms as f64),
                ),
                _ => (0.0, 0.0),
            };
        ArgusReport {
            waves: self.outcomes.len() as u32,
            successes,
            failures,
            phase: self.phase(),
            token_savings_ratio,
            latency_savings_ratio,
        }
    }
}

fn savings(first: f64, last: f64) -> f64 {
    if first <= 0.0 {
        0.0
    } else {
        ((first - last) / first).clamp(-1.0, 1.0)
    }
}

/// One physical constraint declared by a strategy.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PhysicalConstraint {
    /// Constraint id.
    pub id: String,
    /// Human-readable requirement.
    pub description: String,
}

impl PhysicalConstraint {
    /// Build a constraint.
    pub fn new(id: impl Into<String>, description: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            description: description.into(),
        }
    }
}

/// Deterministic semantic-to-physical mapping.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct S2PMapping {
    constraints: Vec<PhysicalConstraint>,
}

impl S2PMapping {
    /// Build a mapping from constraints.
    pub fn new(constraints: Vec<PhysicalConstraint>) -> Self {
        Self { constraints }
    }

    /// Number of declared constraints.
    pub fn len(&self) -> usize {
        self.constraints.len()
    }

    /// Whether no constraints are declared.
    pub fn is_empty(&self) -> bool {
        self.constraints.is_empty()
    }

    /// Check that every constraint id was observed.
    pub fn validate(&self, observed: &[String]) -> bool {
        let observed: std::collections::HashSet<&str> =
            observed.iter().map(String::as_str).collect();
        self.constraints
            .iter()
            .all(|constraint| observed.contains(constraint.id.as_str()))
    }
}

/// The result of a PMG authorization check.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum PmgDecision {
    /// External verification and the physical mapping both passed.
    Allowed,
    /// The agent attempted to verify itself.
    SelfVerificationDenied,
    /// The verifier is not in the trusted set.
    UntrustedVerifier,
    /// A physical constraint was missing.
    PhysicalConstraintMissing,
}

/// Errors raised by PMG configuration.
#[derive(Debug, Error, PartialEq, Eq)]
pub enum PmgError {
    /// An allowed verifier id was empty.
    #[error("verifier id must not be empty")]
    EmptyVerifier,
    /// An agent id was empty.
    #[error("agent id must not be empty")]
    EmptyAgent,
}

/// Physical Mapping Guard: removes self-verification authority.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PmgGuard {
    allowed_verifiers: Vec<String>,
}

impl PmgGuard {
    /// Build a guard from trusted external verifier ids.
    pub fn new(
        allowed_verifiers: impl IntoIterator<Item = impl Into<String>>,
    ) -> Result<Self, PmgError> {
        let mut verifiers = Vec::new();
        for verifier in allowed_verifiers {
            let verifier = verifier.into();
            if verifier.trim().is_empty() {
                return Err(PmgError::EmptyVerifier);
            }
            if !verifiers.contains(&verifier) {
                verifiers.push(verifier);
            }
        }
        Ok(Self {
            allowed_verifiers: verifiers,
        })
    }

    /// Verify a strategy: the verifier must be external and trusted, and every
    /// physical constraint must be observed.
    pub fn authorize(
        &self,
        agent_id: &str,
        verifier_id: &str,
        mapping: &S2PMapping,
        observed_constraints: &[String],
    ) -> PmgDecision {
        if agent_id.trim().is_empty() || verifier_id.trim().is_empty() {
            return PmgDecision::UntrustedVerifier;
        }
        if agent_id == verifier_id {
            return PmgDecision::SelfVerificationDenied;
        }
        if !self
            .allowed_verifiers
            .iter()
            .any(|item| item == verifier_id)
        {
            return PmgDecision::UntrustedVerifier;
        }
        if !mapping.validate(observed_constraints) {
            return PmgDecision::PhysicalConstraintMissing;
        }
        PmgDecision::Allowed
    }

    /// Trusted verifier ids.
    pub fn allowed_verifiers(&self) -> &[String] {
        &self.allowed_verifiers
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn strategy(id: &str) -> ArgusStrategy {
        ArgusStrategy::new(id, format!("strategy {id}"))
    }

    #[test]
    fn strategies_are_registered_and_bounded() {
        let mut runtime = ArgusRuntime::with_capacity(2);
        runtime.register(strategy("a")).unwrap();
        runtime.register(strategy("b")).unwrap();
        assert_eq!(runtime.strategy_count(), 2);
        assert_eq!(
            runtime.register(strategy("c")),
            Err(ArgusError::CapacityReached)
        );
        assert_eq!(
            runtime.register(strategy("a")),
            Err(ArgusError::DuplicateStrategy("a".into()))
        );
    }

    #[test]
    fn empty_strategy_fields_are_rejected() {
        let mut runtime = ArgusRuntime::new();
        assert_eq!(
            runtime.register(ArgusStrategy::new("", "x")),
            Err(ArgusError::EmptyField("strategy id"))
        );
        assert_eq!(
            runtime.register(ArgusStrategy::new("a", " ")),
            Err(ArgusError::EmptyField("description"))
        );
    }

    #[test]
    fn unknown_strategy_cannot_record() {
        let mut runtime = ArgusRuntime::new();
        assert_eq!(
            runtime.record("missing", true, true, 10, 5),
            Err(ArgusError::UnknownStrategy("missing".into()))
        );
    }

    #[test]
    fn success_requires_verification_and_physical_mapping() {
        let mut runtime = ArgusRuntime::new();
        runtime.register(strategy("a")).unwrap();
        runtime.record("a", true, false, 100, 10).unwrap();
        runtime.record("a", false, true, 90, 9).unwrap();
        let report = runtime.report();
        assert_eq!(report.successes, 0);
        assert_eq!(report.failures, 2);
    }

    #[test]
    fn phase_transitions_through_cold_warm_and_mature() {
        let mut runtime = ArgusRuntime::new();
        runtime.register(strategy("a")).unwrap();
        assert_eq!(runtime.phase(), ArgusPhase::Cold);
        runtime.record("a", true, true, 100, 10).unwrap();
        assert_eq!(runtime.phase(), ArgusPhase::Warm);
        runtime.record("a", true, true, 90, 9).unwrap();
        assert_eq!(runtime.phase(), ArgusPhase::Warm);
        runtime.record("a", true, true, 80, 8).unwrap();
        assert_eq!(runtime.phase(), ArgusPhase::Mature);
    }

    #[test]
    fn report_computes_savings_from_first_to_last_wave() {
        let mut runtime = ArgusRuntime::new();
        runtime.register(strategy("a")).unwrap();
        runtime.record("a", true, true, 100, 20).unwrap();
        runtime.record("a", true, true, 50, 10).unwrap();
        let report = runtime.report();
        assert_eq!(report.waves, 2);
        assert!((report.token_savings_ratio - 0.5).abs() < 1e-9);
        assert!((report.latency_savings_ratio - 0.5).abs() < 1e-9);
    }

    #[test]
    fn pmg_rejects_self_verification_and_untrusted_verifiers() {
        let guard = PmgGuard::new(["reviewer"]).unwrap();
        let mapping = S2PMapping::new(vec![PhysicalConstraint::new("c1", "no overflow")]);
        assert_eq!(
            guard.authorize("agent", "agent", &mapping, &["c1".into()]),
            PmgDecision::SelfVerificationDenied
        );
        assert_eq!(
            guard.authorize("agent", "other", &mapping, &["c1".into()]),
            PmgDecision::UntrustedVerifier
        );
    }

    #[test]
    fn pmg_requires_every_physical_constraint() {
        let guard = PmgGuard::new(["reviewer"]).unwrap();
        let mapping = S2PMapping::new(vec![
            PhysicalConstraint::new("c1", "no overflow"),
            PhysicalConstraint::new("c2", "bounded memory"),
        ]);
        assert_eq!(
            guard.authorize("agent", "reviewer", &mapping, &["c1".into()]),
            PmgDecision::PhysicalConstraintMissing
        );
        assert_eq!(
            guard.authorize("agent", "reviewer", &mapping, &["c1".into(), "c2".into()]),
            PmgDecision::Allowed
        );
    }

    #[test]
    fn pmg_configuration_rejects_empty_verifier() {
        assert_eq!(PmgGuard::new([" "]), Err(PmgError::EmptyVerifier));
        let guard = PmgGuard::new(["reviewer", "reviewer"]).unwrap();
        assert_eq!(guard.allowed_verifiers(), &["reviewer".to_string()]);
    }
}
