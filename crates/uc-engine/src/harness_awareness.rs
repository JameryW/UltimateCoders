//! Harness awareness telemetry routing (ledger item #79).
//!
//! Turns verified tool health telemetry into a pre-dispatch routing decision
//! instead of blindly retrying the same endpoint. The module is deliberately
//! deterministic and in-memory: callers feed observations, the router scores
//! candidates by expected policy regret.

use serde::{Deserialize, Serialize};
use std::collections::{HashMap, VecDeque};
use thiserror::Error;

pub const DEFAULT_HEALTH_WINDOW: usize = 32;
pub const DEFAULT_FAULT_PENALTY_MS: f64 = 10_000.0;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ToolObservation {
    pub endpoint: String,
    pub success: bool,
    pub latency_ms: f64,
    pub fault_injected: bool,
    pub verified: bool,
}

impl ToolObservation {
    pub fn new(
        endpoint: impl Into<String>,
        success: bool,
        latency_ms: f64,
        verified: bool,
    ) -> Self {
        Self {
            endpoint: endpoint.into(),
            success,
            latency_ms,
            fault_injected: false,
            verified,
        }
    }

    pub fn with_fault(mut self, fault_injected: bool) -> Self {
        self.fault_injected = fault_injected;
        self
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ToolHealthSnapshot {
    pub endpoint: String,
    pub samples: usize,
    pub success_rate: f64,
    pub p95_latency_ms: f64,
    pub fault_injected: bool,
    pub healthy: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ToolCandidate {
    pub endpoint: String,
    pub base_latency_ms: f64,
}

impl ToolCandidate {
    pub fn new(endpoint: impl Into<String>, base_latency_ms: f64) -> Self {
        Self {
            endpoint: endpoint.into(),
            base_latency_ms,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RouteScore {
    pub endpoint: String,
    pub success_probability: f64,
    pub expected_latency_ms: f64,
    pub expected_regret_ms: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RouteDecision {
    pub selected: String,
    pub scores: Vec<RouteScore>,
    pub reason: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum HarnessAwarenessError {
    #[error("tool endpoint must not be empty")]
    EmptyEndpoint,
    #[error("latency must be finite and non-negative")]
    InvalidLatency,
    #[error("route requires at least one candidate")]
    EmptyCandidates,
}

#[derive(Debug, Clone)]
struct Window {
    observations: VecDeque<ToolObservation>,
}

impl Window {
    fn new() -> Self {
        Self {
            observations: VecDeque::new(),
        }
    }

    fn record(&mut self, observation: ToolObservation, capacity: usize) {
        if self.observations.len() == capacity {
            self.observations.pop_front();
        }
        self.observations.push_back(observation);
    }

    fn snapshot(&self, endpoint: &str) -> ToolHealthSnapshot {
        let samples = self.observations.len();
        let successes = self.observations.iter().filter(|item| item.success).count();
        let success_rate = if samples == 0 {
            0.5
        } else {
            successes as f64 / samples as f64
        };
        let p95_latency_ms = if samples == 0 {
            0.0
        } else {
            let mut latencies: Vec<f64> = self
                .observations
                .iter()
                .map(|item| item.latency_ms)
                .collect();
            latencies.sort_by(|a, b| a.total_cmp(b));
            let index = ((samples as f64) * 0.95).ceil() as usize - 1;
            latencies[index.min(samples - 1)]
        };
        let fault_injected = self.observations.iter().any(|item| item.fault_injected);
        ToolHealthSnapshot {
            endpoint: endpoint.to_string(),
            samples,
            success_rate,
            p95_latency_ms,
            fault_injected,
            healthy: !fault_injected && success_rate >= 0.5,
        }
    }
}

#[derive(Debug, Clone)]
pub struct HarnessAwarenessRouter {
    window_capacity: usize,
    fault_penalty_ms: f64,
    windows: HashMap<String, Window>,
}

impl Default for HarnessAwarenessRouter {
    fn default() -> Self {
        Self::new()
    }
}

impl HarnessAwarenessRouter {
    pub fn new() -> Self {
        Self::with_capacity(DEFAULT_HEALTH_WINDOW)
    }

    pub fn with_capacity(window_capacity: usize) -> Self {
        Self {
            window_capacity: window_capacity.max(1),
            fault_penalty_ms: DEFAULT_FAULT_PENALTY_MS,
            windows: HashMap::new(),
        }
    }

    pub fn record(&mut self, observation: ToolObservation) -> Result<(), HarnessAwarenessError> {
        if observation.endpoint.trim().is_empty() {
            return Err(HarnessAwarenessError::EmptyEndpoint);
        }
        if !observation.latency_ms.is_finite() || observation.latency_ms < 0.0 {
            return Err(HarnessAwarenessError::InvalidLatency);
        }
        if !observation.verified {
            return Ok(());
        }
        self.windows
            .entry(observation.endpoint.clone())
            .or_insert_with(Window::new)
            .record(observation, self.window_capacity);
        Ok(())
    }

    pub fn snapshot(&self, endpoint: &str) -> ToolHealthSnapshot {
        self.windows
            .get(endpoint)
            .map(|window| window.snapshot(endpoint))
            .unwrap_or(ToolHealthSnapshot {
                endpoint: endpoint.to_string(),
                samples: 0,
                success_rate: 0.5,
                p95_latency_ms: 0.0,
                fault_injected: false,
                healthy: true,
            })
    }

    pub fn route(
        &self,
        candidates: &[ToolCandidate],
    ) -> Result<RouteDecision, HarnessAwarenessError> {
        if candidates.is_empty() {
            return Err(HarnessAwarenessError::EmptyCandidates);
        }
        let mut scores = Vec::with_capacity(candidates.len());
        for candidate in candidates {
            if candidate.endpoint.trim().is_empty() {
                return Err(HarnessAwarenessError::EmptyEndpoint);
            }
            if !candidate.base_latency_ms.is_finite() || candidate.base_latency_ms < 0.0 {
                return Err(HarnessAwarenessError::InvalidLatency);
            }
            let health = self.snapshot(&candidate.endpoint);
            let expected_latency = candidate.base_latency_ms.max(health.p95_latency_ms);
            let fault_penalty = if health.fault_injected {
                self.fault_penalty_ms
            } else {
                0.0
            };
            let expected_regret = expected_latency * (1.0 - health.success_rate) + fault_penalty;
            scores.push(RouteScore {
                endpoint: candidate.endpoint.clone(),
                success_probability: health.success_rate,
                expected_latency_ms: expected_latency,
                expected_regret_ms: expected_regret,
            });
        }
        scores.sort_by(|a, b| {
            a.expected_regret_ms
                .total_cmp(&b.expected_regret_ms)
                .then_with(|| a.endpoint.cmp(&b.endpoint))
        });
        let selected = scores[0].endpoint.clone();
        Ok(RouteDecision {
            selected,
            scores,
            reason: "minimum expected policy regret over verified telemetry".to_string(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn obs(endpoint: &str, success: bool, latency: f64) -> ToolObservation {
        ToolObservation::new(endpoint, success, latency, true)
    }

    #[test]
    fn verified_observations_update_snapshot() {
        let mut router = HarnessAwarenessRouter::new();
        assert!(router.record(obs("a", true, 100.0)).is_ok());
        assert!(router.record(obs("a", false, 300.0)).is_ok());
        let snapshot = router.snapshot("a");
        assert_eq!(snapshot.samples, 2);
        assert!((snapshot.success_rate - 0.5).abs() < 1e-9);
        assert_eq!(snapshot.p95_latency_ms, 300.0);
    }

    #[test]
    fn unverified_observations_are_ignored() {
        let mut router = HarnessAwarenessRouter::new();
        router
            .record(ToolObservation::new("a", false, 1000.0, false))
            .unwrap();
        let snapshot = router.snapshot("a");
        assert_eq!(snapshot.samples, 0);
        assert!((snapshot.success_rate - 0.5).abs() < 1e-9);
    }

    #[test]
    fn faulty_endpoint_gets_large_penalty() {
        let mut router = HarnessAwarenessRouter::new();
        router
            .record(obs("bad", true, 100.0).with_fault(true))
            .unwrap();
        router.record(obs("good", true, 120.0)).unwrap();
        let decision = router
            .route(&[
                ToolCandidate::new("bad", 100.0),
                ToolCandidate::new("good", 120.0),
            ])
            .unwrap();
        assert_eq!(decision.selected, "good");
    }

    #[test]
    fn routing_prefers_higher_success_rate_at_equal_latency() {
        let mut router = HarnessAwarenessRouter::new();
        router.record(obs("a", false, 100.0)).unwrap();
        router.record(obs("b", true, 100.0)).unwrap();
        let decision = router
            .route(&[
                ToolCandidate::new("a", 100.0),
                ToolCandidate::new("b", 100.0),
            ])
            .unwrap();
        assert_eq!(decision.selected, "b");
    }

    #[test]
    fn equal_scores_tie_break_by_endpoint() {
        let router = HarnessAwarenessRouter::new();
        let decision = router
            .route(&[
                ToolCandidate::new("b", 100.0),
                ToolCandidate::new("a", 100.0),
            ])
            .unwrap();
        assert_eq!(decision.selected, "a");
    }

    #[test]
    fn empty_candidates_are_rejected() {
        let router = HarnessAwarenessRouter::new();
        assert!(matches!(
            router.route(&[]),
            Err(HarnessAwarenessError::EmptyCandidates)
        ));
    }

    #[test]
    fn empty_endpoint_is_rejected() {
        let mut router = HarnessAwarenessRouter::new();
        assert!(matches!(
            router.record(ToolObservation::new("", true, 1.0, true)),
            Err(HarnessAwarenessError::EmptyEndpoint)
        ));
    }

    #[test]
    fn invalid_latency_is_rejected() {
        let mut router = HarnessAwarenessRouter::new();
        assert!(matches!(
            router.record(ToolObservation::new("a", true, -1.0, true)),
            Err(HarnessAwarenessError::InvalidLatency)
        ));
    }

    #[test]
    fn window_is_bounded() {
        let mut router = HarnessAwarenessRouter::with_capacity(2);
        for index in 0..5 {
            router
                .record(obs("a", index % 2 == 0, index as f64))
                .unwrap();
        }
        assert_eq!(router.snapshot("a").samples, 2);
    }

    #[test]
    fn snapshot_for_unknown_endpoint_is_optimistic_prior() {
        let router = HarnessAwarenessRouter::new();
        let snapshot = router.snapshot("unknown");
        assert_eq!(snapshot.samples, 0);
        assert!(snapshot.healthy);
    }
}
