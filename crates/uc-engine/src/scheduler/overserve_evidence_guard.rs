//! OverServe protection and counter-evidence completion fuses (ledger #137).
//!
//! Long-horizon agents can burn a run polling a state tool that never changes,
//! and a completion claim can be fabricated after a provider content-filter
//! failure.  This module is the deterministic guard layer:
//!
//! * repeated same-state polls receive exponential backoff and a hard breaker;
//! * observing the completed state closes the polling path;
//! * side-channel counter-evidence marks a claim for rollback;
//! * a provider crash forbids synthesized completion answers.
//!
//! No model call, no clock, no randomness.

use std::collections::{HashMap, HashSet};

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Base delay for the first repeated poll, in milliseconds.
pub const DEFAULT_BASE_DELAY_MS: u64 = 500;
/// Maximum backoff delay, in milliseconds.
pub const DEFAULT_MAX_DELAY_MS: u64 = 60_000;

/// One polling observation.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PollObservation {
    /// Tool that performed the poll.
    pub tool: String,
    /// Semantic state key; identical keys mean the state did not advance.
    pub state_key: String,
    /// The observed state says the task is complete.
    pub completed: bool,
}

impl PollObservation {
    /// Build a non-completed observation.
    pub fn new(tool: impl Into<String>, state_key: impl Into<String>) -> Self {
        Self {
            tool: tool.into(),
            state_key: state_key.into(),
            completed: false,
        }
    }

    /// Mark this observation as completed.
    pub fn completed(mut self) -> Self {
        self.completed = true;
        self
    }
}

/// Decision for one poll.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PollVerdict {
    /// The call may proceed.
    pub allow: bool,
    /// Delay before proceeding; zero when allowed immediately.
    pub delay_ms: u64,
    /// The completion breaker rejected the call.
    pub breaker_open: bool,
    /// Stable reason code.
    pub reason: String,
}

/// Exponential-backoff polling guard.
#[derive(Debug, Clone)]
pub struct PollThrottle {
    base_delay_ms: u64,
    max_delay_ms: u64,
    counts: HashMap<String, u64>,
    completed: HashSet<String>,
}

impl Default for PollThrottle {
    fn default() -> Self {
        Self::new(DEFAULT_BASE_DELAY_MS, DEFAULT_MAX_DELAY_MS)
    }
}

impl PollThrottle {
    /// Build a throttle with a base and maximum delay.
    pub fn new(base_delay_ms: u64, max_delay_ms: u64) -> Self {
        Self {
            base_delay_ms: base_delay_ms.max(1),
            max_delay_ms: max_delay_ms.max(base_delay_ms.max(1)),
            counts: HashMap::new(),
            completed: HashSet::new(),
        }
    }

    /// Observe one poll and decide whether it may proceed.
    pub fn observe(
        &mut self,
        observation: &PollObservation,
    ) -> Result<PollVerdict, OverServeError> {
        if observation.tool.trim().is_empty() {
            return Err(OverServeError::EmptyTool);
        }
        if observation.state_key.trim().is_empty() {
            return Err(OverServeError::EmptyStateKey);
        }
        if self.completed.contains(&observation.state_key) {
            return Ok(PollVerdict {
                allow: false,
                delay_ms: 0,
                breaker_open: true,
                reason: "completed_state_breaker".to_string(),
            });
        }
        let count = self
            .counts
            .entry(observation.state_key.clone())
            .and_modify(|value| *value = value.saturating_add(1))
            .or_insert(1);
        if observation.completed {
            self.completed.insert(observation.state_key.clone());
            return Ok(PollVerdict {
                allow: true,
                delay_ms: 0,
                breaker_open: false,
                reason: "completed_state_observed".to_string(),
            });
        }
        let exponent = count.saturating_sub(1).min(62);
        let delay = self
            .base_delay_ms
            .saturating_mul(1u64 << exponent)
            .min(self.max_delay_ms);
        Ok(PollVerdict {
            allow: true,
            delay_ms: delay,
            breaker_open: false,
            reason: if *count == 1 {
                "first_observation".to_string()
            } else {
                "repeated_state_backoff".to_string()
            },
        })
    }

    /// Number of observations for a state key.
    pub fn count(&self, state_key: &str) -> u64 {
        self.counts.get(state_key).copied().unwrap_or(0)
    }

    /// Whether a state key has closed the breaker.
    pub fn is_completed(&self, state_key: &str) -> bool {
        self.completed.contains(state_key)
    }

    /// Clear all counters and breakers.
    pub fn reset(&mut self) {
        self.counts.clear();
        self.completed.clear();
    }
}

/// Side-channel evidence about a completion claim.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ClaimEvidence {
    /// Claim being audited.
    pub claim_id: String,
    /// Evidence supporting the claim.
    pub supporting: Vec<String>,
    /// Evidence contradicting the claim.
    pub contradicting: Vec<String>,
}

impl ClaimEvidence {
    /// Build a claim with explicit supporting and contradicting evidence.
    pub fn new(
        claim_id: impl Into<String>,
        supporting: Vec<String>,
        contradicting: Vec<String>,
    ) -> Self {
        Self {
            claim_id: claim_id.into(),
            supporting,
            contradicting,
        }
    }
}

/// Result of auditing a claim.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EvidenceVerdict {
    /// Claim id.
    pub claim_id: String,
    /// The completion claim stands.
    pub accepted: bool,
    /// The prior patch should be rolled back.
    pub rollback: bool,
    /// Stable reason code.
    pub reason: String,
}

/// Counter-evidence registry.
#[derive(Debug, Clone, Default)]
pub struct CounterEvidenceGuard {
    claims: HashMap<String, ClaimEvidence>,
}

impl CounterEvidenceGuard {
    /// Record evidence for a claim.
    pub fn observe(&mut self, evidence: ClaimEvidence) -> Result<(), OverServeError> {
        if evidence.claim_id.trim().is_empty() {
            return Err(OverServeError::EmptyClaim);
        }
        if evidence.supporting.is_empty() && evidence.contradicting.is_empty() {
            return Err(OverServeError::EmptyEvidence);
        }
        self.claims.insert(evidence.claim_id.clone(), evidence);
        Ok(())
    }

    /// Audit a claim.  Any unresolved contradiction rolls the patch back.
    pub fn evaluate(&self, claim_id: &str) -> Result<EvidenceVerdict, OverServeError> {
        let evidence = self
            .claims
            .get(claim_id)
            .ok_or_else(|| OverServeError::UnknownClaim(claim_id.to_string()))?;
        if !evidence.contradicting.is_empty() {
            return Ok(EvidenceVerdict {
                claim_id: claim_id.to_string(),
                accepted: false,
                rollback: true,
                reason: "counter_evidence_rollback".to_string(),
            });
        }
        if evidence.supporting.is_empty() {
            return Ok(EvidenceVerdict {
                claim_id: claim_id.to_string(),
                accepted: false,
                rollback: false,
                reason: "no_supporting_evidence".to_string(),
            });
        }
        Ok(EvidenceVerdict {
            claim_id: claim_id.to_string(),
            accepted: true,
            rollback: false,
            reason: "evidence_accepted".to_string(),
        })
    }
}

/// Where a final answer came from.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum CompletionSource {
    /// The model produced a normal final answer.
    Model,
    /// The framework synthesized an answer after a provider failure.
    FrameworkSynthesis,
}

/// One completion attempt.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompletionAttempt {
    /// Provider that would serve the completion.
    pub provider: String,
    /// Source of the completion.
    pub source: CompletionSource,
}

/// Result of the crash fuse.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompletionVerdict {
    /// The completion may be emitted.
    pub allowed: bool,
    /// Stable reason code.
    pub reason: String,
}

/// Provider crash fuse.
#[derive(Debug, Clone, Default)]
pub struct ProviderCrashFuse {
    crashed: HashSet<String>,
}

impl ProviderCrashFuse {
    /// Record that a provider failed its content filter or crashed.
    pub fn record_crash(&mut self, provider: impl Into<String>) -> Result<(), OverServeError> {
        let provider = provider.into();
        if provider.trim().is_empty() {
            return Err(OverServeError::EmptyProvider);
        }
        self.crashed.insert(provider);
        Ok(())
    }

    /// Decide whether a completion may be emitted.
    pub fn completion_allowed(
        &self,
        attempt: &CompletionAttempt,
    ) -> Result<CompletionVerdict, OverServeError> {
        if attempt.provider.trim().is_empty() {
            return Err(OverServeError::EmptyProvider);
        }
        if self.crashed.contains(&attempt.provider)
            && attempt.source == CompletionSource::FrameworkSynthesis
        {
            return Ok(CompletionVerdict {
                allowed: false,
                reason: "no_synthesized_completion_after_crash".to_string(),
            });
        }
        Ok(CompletionVerdict {
            allowed: true,
            reason: "completion_allowed".to_string(),
        })
    }

    /// Whether a provider has crashed.
    pub fn crashed(&self, provider: &str) -> bool {
        self.crashed.contains(provider)
    }
}

/// Errors returned by the guard.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum OverServeError {
    /// Empty tool name.
    #[error("tool must not be empty")]
    EmptyTool,
    /// Empty state key.
    #[error("state key must not be empty")]
    EmptyStateKey,
    /// Empty claim id.
    #[error("claim id must not be empty")]
    EmptyClaim,
    /// Both evidence lists are empty.
    #[error("evidence must not be empty")]
    EmptyEvidence,
    /// Provider name is empty.
    #[error("provider must not be empty")]
    EmptyProvider,
    /// Claim was not observed.
    #[error("unknown claim: {0}")]
    UnknownClaim(String),
}

/// Convenience facade combining the three guards.
#[derive(Debug, Clone, Default)]
pub struct OverServeGuard {
    /// Polling throttle.
    pub throttle: PollThrottle,
    /// Counter-evidence registry.
    pub evidence: CounterEvidenceGuard,
    /// Provider crash fuse.
    pub crash: ProviderCrashFuse,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn observation(key: &str) -> PollObservation {
        PollObservation::new("status", key)
    }

    #[test]
    fn first_poll_is_immediate_and_repeats_back_off() {
        let mut throttle = PollThrottle::new(100, 1_000);
        assert_eq!(throttle.observe(&observation("s1")).unwrap().delay_ms, 100);
        assert_eq!(throttle.observe(&observation("s1")).unwrap().delay_ms, 200);
        assert_eq!(throttle.observe(&observation("s1")).unwrap().delay_ms, 400);
        assert_eq!(throttle.observe(&observation("s1")).unwrap().delay_ms, 800);
        assert_eq!(
            throttle.observe(&observation("s1")).unwrap().delay_ms,
            1_000
        );
        assert_eq!(throttle.count("s1"), 5);
    }

    #[test]
    fn different_states_have_independent_backoff() {
        let mut throttle = PollThrottle::new(100, 1_000);
        throttle.observe(&observation("a")).unwrap();
        throttle.observe(&observation("a")).unwrap();
        assert_eq!(throttle.observe(&observation("b")).unwrap().delay_ms, 100);
        assert_eq!(throttle.observe(&observation("a")).unwrap().delay_ms, 400);
    }

    #[test]
    fn completed_observation_closes_the_breaker() {
        let mut throttle = PollThrottle::default();
        let verdict = throttle.observe(&observation("done").completed()).unwrap();
        assert!(verdict.allow);
        assert_eq!(verdict.reason, "completed_state_observed");
        assert!(throttle.is_completed("done"));
        let rejected = throttle.observe(&observation("done")).unwrap();
        assert!(!rejected.allow);
        assert!(rejected.breaker_open);
        assert_eq!(rejected.reason, "completed_state_breaker");
    }

    #[test]
    fn reset_clears_state() {
        let mut throttle = PollThrottle::default();
        throttle.observe(&observation("x").completed()).unwrap();
        throttle.reset();
        assert!(!throttle.is_completed("x"));
        assert_eq!(throttle.count("x"), 0);
    }

    #[test]
    fn empty_tool_or_state_is_rejected() {
        let mut throttle = PollThrottle::default();
        assert_eq!(
            throttle.observe(&PollObservation::new("", "s")),
            Err(OverServeError::EmptyTool)
        );
        assert_eq!(
            throttle.observe(&PollObservation::new("t", " ")),
            Err(OverServeError::EmptyStateKey)
        );
    }

    #[test]
    fn counter_evidence_triggers_rollback() {
        let mut guard = CounterEvidenceGuard::default();
        guard
            .observe(ClaimEvidence::new(
                "c1",
                vec!["tests passed".to_string()],
                vec!["target test still red".to_string()],
            ))
            .unwrap();
        let verdict = guard.evaluate("c1").unwrap();
        assert!(!verdict.accepted);
        assert!(verdict.rollback);
        assert_eq!(verdict.reason, "counter_evidence_rollback");
    }

    #[test]
    fn supporting_evidence_accepts_when_clean() {
        let mut guard = CounterEvidenceGuard::default();
        guard
            .observe(ClaimEvidence::new(
                "c1",
                vec!["tests passed".to_string()],
                vec![],
            ))
            .unwrap();
        assert!(guard.evaluate("c1").unwrap().accepted);
    }

    #[test]
    fn empty_evidence_and_unknown_claim_are_rejected() {
        let mut guard = CounterEvidenceGuard::default();
        assert_eq!(
            guard.observe(ClaimEvidence::new("c1", vec![], vec![])),
            Err(OverServeError::EmptyEvidence)
        );
        assert_eq!(
            guard.evaluate("missing"),
            Err(OverServeError::UnknownClaim("missing".to_string()))
        );
    }

    #[test]
    fn provider_crash_blocks_framework_synthesis_only() {
        let mut fuse = ProviderCrashFuse::default();
        fuse.record_crash("provider-a").unwrap();
        assert!(fuse.crashed("provider-a"));
        let synthesized = CompletionAttempt {
            provider: "provider-a".to_string(),
            source: CompletionSource::FrameworkSynthesis,
        };
        assert!(!fuse.completion_allowed(&synthesized).unwrap().allowed);
        let model = CompletionAttempt {
            provider: "provider-a".to_string(),
            source: CompletionSource::Model,
        };
        assert!(fuse.completion_allowed(&model).unwrap().allowed);
    }

    #[test]
    fn facade_composes_the_three_guards() {
        let mut guard = OverServeGuard::default();
        guard.throttle.observe(&observation("s")).unwrap();
        guard
            .evidence
            .observe(ClaimEvidence::new("c", vec!["ok".to_string()], vec![]))
            .unwrap();
        guard.crash.record_crash("p").unwrap();
        assert!(guard.evidence.evaluate("c").unwrap().accepted);
        assert!(guard.crash.crashed("p"));
    }
}
