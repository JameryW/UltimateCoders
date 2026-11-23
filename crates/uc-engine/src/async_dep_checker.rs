//! AsynCodeBench asynchronous dependency consistency (#37).
//!
//! The checker is the pure bookkeeping half of the AsynCodeBench integration
//! guard: callers register the directed dependency edges of a multi-agent
//! graph, report executable dependency probes at integration checkpoints, and
//! read back the average dependency pass rate (ADPR) and normalised dependency
//! resolution steps (DRS-tilde).
//!
//! It also models the Hopping Window barrier: when an upstream node that owns
//! a core interface commits, every downstream edge is invalidated so active
//! attempts have to rebase and re-probe before the next checkpoint.
//!
//! This module never executes a probe, talks to NATS, or reads a workspace.
//! Probe results arrive already observed from the sandbox.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Default bound on registered dependency edges.
pub const DEFAULT_MAX_EDGES: usize = 4096;

/// What an edge contracts between two graph nodes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DependencyKind {
    /// A public symbol must exist with the expected name.
    Symbol,
    /// A public symbol must keep the expected signature.
    Signature,
    /// A configuration key or environment contract must be present.
    Configuration,
    /// A test contract (fixture or command) must remain executable.
    TestContract,
}

/// Current resolution state of one edge.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DependencyState {
    Pending,
    Resolved,
    Broken,
}

/// One directed dependency edge in the asynchronous execution graph.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DependencyEdge {
    pub id: String,
    pub upstream: String,
    pub downstream: String,
    pub symbol: String,
    pub expected_signature: String,
    pub kind: DependencyKind,
    /// Committing this edge's upstream invalidates downstream attempts.
    pub core_interface: bool,
}

impl DependencyEdge {
    /// Create a symbol edge with the default non-core interface marker.
    pub fn new(
        id: impl Into<String>,
        upstream: impl Into<String>,
        downstream: impl Into<String>,
        symbol: impl Into<String>,
        expected_signature: impl Into<String>,
    ) -> Self {
        Self {
            id: id.into(),
            upstream: upstream.into(),
            downstream: downstream.into(),
            symbol: symbol.into(),
            expected_signature: expected_signature.into(),
            kind: DependencyKind::Symbol,
            core_interface: false,
        }
    }

    /// Mark this edge as a core-interface edge, enabling the Hopping Window.
    #[must_use]
    pub fn core_interface(mut self) -> Self {
        self.core_interface = true;
        self
    }

    /// Replace the edge kind.
    #[must_use]
    pub fn with_kind(mut self, kind: DependencyKind) -> Self {
        self.kind = kind;
        self
    }
}

/// An already-observed dependency probe for one edge.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DependencyProbe {
    pub edge_id: String,
    pub observed_symbol: String,
    pub observed_signature: String,
    pub step: u32,
    pub attempt: String,
}

impl DependencyProbe {
    /// Create a probe result; the checker validates the referenced edge.
    pub fn new(
        edge_id: impl Into<String>,
        observed_symbol: impl Into<String>,
        observed_signature: impl Into<String>,
        step: u32,
        attempt: impl Into<String>,
    ) -> Self {
        Self {
            edge_id: edge_id.into(),
            observed_symbol: observed_symbol.into(),
            observed_signature: observed_signature.into(),
            step,
            attempt: attempt.into(),
        }
    }
}

/// Checkpoint report used by the integration gate.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct IntegrationReport {
    pub checkpoint: u32,
    /// Resolved edges divided by all registered edges.
    pub adpr: f64,
    /// Sum of resolution steps divided by `last_step + 1`.
    pub drs_tilde: f64,
    pub resolved: usize,
    pub broken: usize,
    pub pending: usize,
    pub test_pass_rate: Option<f64>,
    /// A test pass rate above ADPR is the ledger's false-positive warning.
    pub tests_above_adpr: bool,
    /// True only when every edge is resolved and none is broken.
    pub clean: bool,
}

/// Result of a Hopping Window trigger.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HoppingWindow {
    pub upstream: String,
    pub at_step: u32,
    pub edges: Vec<String>,
    pub downstream: Vec<String>,
    pub rebase_required: bool,
}

/// Errors from the pure dependency checker.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum AsyncDepError {
    #[error("dependency graph field `{field}` must not be empty")]
    EmptyField { field: &'static str },
    #[error("duplicate dependency edge `{0}`")]
    DuplicateEdge(String),
    #[error("unknown dependency edge `{0}`")]
    UnknownEdge(String),
    #[error("dependency edge capacity reached")]
    CapacityReached,
}

#[derive(Debug, Clone)]
struct EdgeRuntime {
    state: DependencyState,
    resolution_step: Option<u32>,
}

impl Default for EdgeRuntime {
    fn default() -> Self {
        Self {
            state: DependencyState::Pending,
            resolution_step: None,
        }
    }
}

fn require_non_empty(field: &'static str, value: &str) -> Result<(), AsyncDepError> {
    if value.trim().is_empty() {
        Err(AsyncDepError::EmptyField { field })
    } else {
        Ok(())
    }
}

/// Normalise a signature for comparison: whitespace is not semantic here.
fn normalize_signature(signature: &str) -> String {
    signature.chars().filter(|ch| !ch.is_whitespace()).collect()
}

/// Pure, bounded asynchronous dependency checker.
#[derive(Debug, Clone)]
pub struct AsyncDependencyChecker {
    edges: BTreeMap<String, DependencyEdge>,
    runtime: BTreeMap<String, EdgeRuntime>,
    max_edges: usize,
    last_step: u32,
    checkpoint_seq: u32,
}

impl AsyncDependencyChecker {
    /// Build a checker with the default edge bound.
    pub fn new(edges: impl IntoIterator<Item = DependencyEdge>) -> Result<Self, AsyncDepError> {
        Self::with_capacity(edges, DEFAULT_MAX_EDGES)
    }

    /// Build a checker with an explicit edge bound.
    pub fn with_capacity(
        edges: impl IntoIterator<Item = DependencyEdge>,
        max_edges: usize,
    ) -> Result<Self, AsyncDepError> {
        if max_edges == 0 {
            return Err(AsyncDepError::CapacityReached);
        }
        let mut checker = Self {
            edges: BTreeMap::new(),
            runtime: BTreeMap::new(),
            max_edges,
            last_step: 0,
            checkpoint_seq: 0,
        };
        for edge in edges {
            checker.register(edge)?;
        }
        Ok(checker)
    }

    /// Register one edge after validating its identity and references.
    pub fn register(&mut self, edge: DependencyEdge) -> Result<(), AsyncDepError> {
        require_non_empty("id", &edge.id)?;
        require_non_empty("upstream", &edge.upstream)?;
        require_non_empty("downstream", &edge.downstream)?;
        require_non_empty("symbol", &edge.symbol)?;
        require_non_empty("expected_signature", &edge.expected_signature)?;
        if self.edges.len() >= self.max_edges {
            return Err(AsyncDepError::CapacityReached);
        }
        if self.edges.contains_key(&edge.id) {
            return Err(AsyncDepError::DuplicateEdge(edge.id));
        }
        self.runtime.insert(edge.id.clone(), EdgeRuntime::default());
        self.edges.insert(edge.id.clone(), edge);
        Ok(())
    }

    /// Number of registered edges.
    pub fn edge_count(&self) -> usize {
        self.edges.len()
    }

    /// Current state of one edge.
    pub fn state_of(&self, edge_id: &str) -> Option<DependencyState> {
        self.runtime.get(edge_id).map(|runtime| runtime.state)
    }

    /// Record an observed probe and return the new edge state.
    pub fn observe(&mut self, probe: DependencyProbe) -> Result<DependencyState, AsyncDepError> {
        require_non_empty("edge_id", &probe.edge_id)?;
        require_non_empty("observed_symbol", &probe.observed_symbol)?;
        let edge = self
            .edges
            .get(&probe.edge_id)
            .ok_or_else(|| AsyncDepError::UnknownEdge(probe.edge_id.clone()))?;
        let matches = probe.observed_symbol == edge.symbol
            && normalize_signature(&probe.observed_signature)
                == normalize_signature(&edge.expected_signature);
        let state = if matches {
            DependencyState::Resolved
        } else {
            DependencyState::Broken
        };
        let resolution_step = matches.then_some(probe.step);
        self.runtime.insert(
            probe.edge_id,
            EdgeRuntime {
                state,
                resolution_step,
            },
        );
        self.last_step = self.last_step.max(probe.step);
        Ok(state)
    }

    /// Reset every edge owned by a downstream node, forcing a re-probe.
    pub fn rebase(&mut self, downstream: &str) -> usize {
        let ids: Vec<String> = self
            .edges
            .values()
            .filter(|edge| edge.downstream == downstream)
            .map(|edge| edge.id.clone())
            .collect();
        for id in &ids {
            self.runtime.insert(id.clone(), EdgeRuntime::default());
        }
        ids.len()
    }

    /// Trigger the Hopping Window for an upstream core-interface commit.
    pub fn hopping_window(&mut self, upstream: &str, at_step: u32) -> HoppingWindow {
        let mut edge_ids = Vec::new();
        let mut downstream = BTreeSet::new();
        for edge in self.edges.values() {
            if edge.upstream == upstream && edge.core_interface {
                edge_ids.push(edge.id.clone());
                downstream.insert(edge.downstream.clone());
            }
        }
        for id in &edge_ids {
            self.runtime.insert(id.clone(), EdgeRuntime::default());
        }
        self.last_step = self.last_step.max(at_step);
        HoppingWindow {
            upstream: upstream.to_string(),
            at_step,
            rebase_required: !edge_ids.is_empty(),
            edges: edge_ids,
            downstream: downstream.into_iter().collect(),
        }
    }

    /// Current ADPR without advancing the checkpoint.
    pub fn adpr(&self) -> f64 {
        if self.edges.is_empty() {
            return 0.0;
        }
        let resolved = self
            .runtime
            .values()
            .filter(|runtime| runtime.state == DependencyState::Resolved)
            .count();
        resolved as f64 / self.edges.len() as f64
    }

    /// Current DRS-tilde without advancing the checkpoint.
    pub fn drs_tilde(&self) -> f64 {
        let total: u32 = self
            .runtime
            .values()
            .filter(|runtime| runtime.state == DependencyState::Resolved)
            .filter_map(|runtime| runtime.resolution_step)
            .sum();
        total as f64 / (self.last_step as f64 + 1.0)
    }

    /// Advance the integration checkpoint and produce its report.
    pub fn integration_checkpoint(&mut self, test_pass_rate: Option<f64>) -> IntegrationReport {
        self.checkpoint_seq = self.checkpoint_seq.saturating_add(1);
        let resolved = self
            .runtime
            .values()
            .filter(|runtime| runtime.state == DependencyState::Resolved)
            .count();
        let broken = self
            .runtime
            .values()
            .filter(|runtime| runtime.state == DependencyState::Broken)
            .count();
        let pending = self.edges.len().saturating_sub(resolved + broken);
        let adpr = self.adpr();
        let pass_rate = test_pass_rate.map(|rate| rate.clamp(0.0, 1.0));
        IntegrationReport {
            checkpoint: self.checkpoint_seq,
            adpr,
            drs_tilde: self.drs_tilde(),
            resolved,
            broken,
            pending,
            tests_above_adpr: pass_rate.is_some_and(|rate| rate > adpr),
            clean: resolved == self.edges.len() && broken == 0,
            test_pass_rate: pass_rate,
        }
    }

    /// Bounded per-edge snapshot, useful for dashboards and logs.
    pub fn edge_states(&self) -> Vec<(String, DependencyState)> {
        self.runtime
            .iter()
            .map(|(id, runtime)| (id.clone(), runtime.state))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn edge(
        id: &str,
        upstream: &str,
        downstream: &str,
        symbol: &str,
        signature: &str,
    ) -> DependencyEdge {
        DependencyEdge::new(id, upstream, downstream, symbol, signature)
    }

    fn checker() -> AsyncDependencyChecker {
        AsyncDependencyChecker::new([
            edge(
                "a",
                "api",
                "worker",
                "build_index",
                "fn build_index(i64) -> usize",
            ),
            edge("b", "api", "worker", "flush", "fn flush()"),
        ])
        .expect("checker")
    }

    #[test]
    fn rejects_empty_and_duplicate_edges() {
        let empty =
            AsyncDependencyChecker::new([edge("", "api", "worker", "symbol", "sig")]).unwrap_err();
        assert!(matches!(empty, AsyncDepError::EmptyField { .. }));

        let duplicate = AsyncDependencyChecker::new([
            edge("dup", "api", "worker", "a", "sig"),
            edge("dup", "api", "worker", "b", "sig"),
        ])
        .unwrap_err();
        assert_eq!(duplicate, AsyncDepError::DuplicateEdge("dup".into()));
    }

    #[test]
    fn capacity_is_enforced() {
        let err =
            AsyncDependencyChecker::with_capacity([edge("a", "api", "worker", "a", "sig")], 0)
                .unwrap_err();
        assert_eq!(err, AsyncDepError::CapacityReached);

        let mut bounded = AsyncDependencyChecker::with_capacity([], 1).expect("bounded checker");
        bounded
            .register(edge("first", "api", "worker", "a", "sig"))
            .expect("first edge");
        let err = bounded
            .register(edge("second", "api", "worker", "b", "sig"))
            .unwrap_err();
        assert_eq!(err, AsyncDepError::CapacityReached);
    }

    #[test]
    fn matching_probe_resolves_and_normalises_whitespace() {
        let mut checker = checker();
        let state = checker
            .observe(DependencyProbe::new(
                "a",
                "build_index",
                "fn build_index(i64) -> usize",
                4,
                "attempt-1",
            ))
            .expect("probe");
        assert_eq!(state, DependencyState::Resolved);
        assert_eq!(checker.state_of("a"), Some(DependencyState::Resolved));

        let state = checker
            .observe(DependencyProbe::new(
                "b",
                "flush",
                "fn  flush( )",
                5,
                "attempt-1",
            ))
            .expect("probe");
        assert_eq!(state, DependencyState::Resolved);
    }

    #[test]
    fn mismatch_marks_the_edge_broken() {
        let mut checker = checker();
        let state = checker
            .observe(DependencyProbe::new(
                "a",
                "build_index",
                "fn build_index(i64) -> u64",
                3,
                "attempt-1",
            ))
            .expect("probe");
        assert_eq!(state, DependencyState::Broken);
        assert_eq!(checker.state_of("a"), Some(DependencyState::Broken));
    }

    #[test]
    fn unknown_edge_is_rejected() {
        let mut checker = checker();
        let err = checker
            .observe(DependencyProbe::new("missing", "a", "b", 1, "attempt"))
            .unwrap_err();
        assert_eq!(err, AsyncDepError::UnknownEdge("missing".into()));
    }

    #[test]
    fn reports_adpr_and_normalised_resolution_steps() {
        let mut checker = checker();
        checker
            .observe(DependencyProbe::new(
                "a",
                "build_index",
                "fn build_index(i64) -> usize",
                2,
                "attempt",
            ))
            .expect("probe");
        let report = checker.integration_checkpoint(Some(0.9));
        assert!((report.adpr - 0.5).abs() < 1e-9);
        assert!((report.drs_tilde - (2.0 / 3.0)).abs() < 1e-9);
        assert_eq!(report.resolved, 1);
        assert_eq!(report.pending, 1);
        assert_eq!(report.broken, 0);
        assert!(report.tests_above_adpr);
        assert!(!report.clean);
    }

    #[test]
    fn clean_checkpoint_has_full_adpr() {
        let mut checker = checker();
        for (id, symbol, signature, step) in [
            ("a", "build_index", "fn build_index(i64) -> usize", 3),
            ("b", "flush", "fn flush()", 4),
        ] {
            checker
                .observe(DependencyProbe::new(id, symbol, signature, step, "attempt"))
                .expect("probe");
        }
        let report = checker.integration_checkpoint(Some(0.95));
        assert!(report.clean);
        assert!((report.adpr - 1.0).abs() < 1e-9);
        assert!(!report.tests_above_adpr);
    }

    #[test]
    fn hopping_window_resets_only_core_interface_edges() {
        let mut checker = AsyncDependencyChecker::new([
            edge("a", "api", "worker", "build_index", "fn build_index()").core_interface(),
            edge("b", "api", "worker", "flush", "fn flush()"),
        ])
        .expect("checker");
        for (id, symbol, signature) in [
            ("a", "build_index", "fn build_index()"),
            ("b", "flush", "fn flush()"),
        ] {
            checker
                .observe(DependencyProbe::new(id, symbol, signature, 2, "attempt"))
                .expect("probe");
        }
        let window = checker.hopping_window("api", 5);
        assert!(window.rebase_required);
        assert_eq!(window.edges, vec!["a".to_string()]);
        assert_eq!(window.downstream, vec!["worker".to_string()]);
        assert_eq!(checker.state_of("a"), Some(DependencyState::Pending));
        assert_eq!(checker.state_of("b"), Some(DependencyState::Resolved));

        let quiet = checker.hopping_window("other", 6);
        assert!(!quiet.rebase_required);
    }

    #[test]
    fn rebase_resets_every_edge_of_a_downstream_node() {
        let mut checker = checker();
        for (id, symbol, signature) in [
            ("a", "build_index", "fn build_index(i64) -> usize"),
            ("b", "flush", "fn flush()"),
        ] {
            checker
                .observe(DependencyProbe::new(id, symbol, signature, 2, "attempt"))
                .expect("probe");
        }
        assert_eq!(checker.rebase("worker"), 2);
        assert_eq!(checker.adpr(), 0.0);
        assert_eq!(checker.rebase("missing"), 0);
    }
}
