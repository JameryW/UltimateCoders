//! SWE-Shift mid-execution reconciler (#26).
//!
//! A coding agent can start a task, then have the world move underneath it:
//! a collaborator pushes a commit, a public interface changes, the tool session
//! is rebound, the requirement is revised, or evidence the agent is holding
//! becomes stale. SWE-Shift reports that even the strongest systems collapse
//! from ~67% to ~25% success in these conditions.
//!
//! The recovery protocol has three explicit steps:
//!
//! 1. **Change notification** — freeze writes and emit a structured event.
//! 2. **Read-only refresh** — re-read the affected state against the new epoch.
//! 3. **History reset** — fold the pre-change reasoning out of the active tail
//!    and require at least one *fresh* observation before the first edit.
//!
//! This module is the pure state machine for that protocol. It never reads a
//! file or talks to NATS; callers supply epoch snapshots and observations.

use std::collections::{BTreeSet, VecDeque};

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Default bounded event-log capacity.
pub const DEFAULT_EVENT_CAPACITY: usize = 512;

/// Why the workspace moved.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ShiftKind {
    /// A collaborator merged or pushed a commit.
    CollaboratorCommit,
    /// Public AST symbols changed (added or removed).
    InterfaceChange,
    /// The tool session was rebound to a different schema/epoch.
    ToolSessionRebind,
    /// The requirement or task statement was revised.
    RequirementRevision,
    /// Evidence held by the agent no longer describes the workspace.
    StaleEvidence,
}

impl ShiftKind {
    /// Human-readable label used in recovery plans.
    pub fn label(self) -> &'static str {
        match self {
            ShiftKind::CollaboratorCommit => "collaborator commit",
            ShiftKind::InterfaceChange => "interface change",
            ShiftKind::ToolSessionRebind => "tool session rebind",
            ShiftKind::RequirementRevision => "requirement revision",
            ShiftKind::StaleEvidence => "stale evidence",
        }
    }

    /// Every shift requires the history-reset step in the three-step protocol.
    pub fn requires_history_reset(self) -> bool {
        true
    }
}

/// The externally observable identity of the workspace.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct WorkspaceEpoch {
    pub git_head: String,
    pub tool_schema_epoch: u64,
    pub requirement_revision: u64,
}

impl WorkspaceEpoch {
    /// Construct an epoch snapshot.
    pub fn new(
        git_head: impl Into<String>,
        tool_schema_epoch: u64,
        requirement_revision: u64,
    ) -> Self {
        Self {
            git_head: git_head.into(),
            tool_schema_epoch,
            requirement_revision,
        }
    }
}

/// One detected workspace shift.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ShiftEvent {
    pub kind: ShiftKind,
    pub previous: WorkspaceEpoch,
    pub current: WorkspaceEpoch,
    pub changed_paths: Vec<String>,
    pub added_symbols: Vec<String>,
    pub removed_symbols: Vec<String>,
    pub detail: String,
}

/// Current position in the three-step recovery protocol.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ShiftPhase {
    Stable,
    Frozen,
    RefreshPending,
    HistoryResetRequired,
    AwaitingFreshObservation,
    Ready,
}

/// Plan emitted for one shift event.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RecoveryPlan {
    pub event: ShiftEvent,
    pub read_only_refresh: bool,
    pub history_reset: bool,
    pub must_observe_paths: Vec<String>,
    pub preserved_symbols: Vec<String>,
    pub steps: Vec<String>,
}

impl RecoveryPlan {
    /// Whether every path required by the plan has been freshly observed.
    pub fn observations_satisfied(&self, observed: &BTreeSet<String>) -> bool {
        if self.must_observe_paths.is_empty() {
            return observed.contains("*");
        }
        observed.contains("*")
            || self
                .must_observe_paths
                .iter()
                .all(|path| observed.contains(path))
    }
}

/// Errors raised by the reconciler.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum ShiftError {
    #[error("event capacity must be >= 1")]
    InvalidEventCapacity,
    #[error("preserved collaborator symbol was removed: {0}")]
    PreservedSymbolRemoved(String),
    #[error("no shift recovery is pending")]
    NoPendingShift,
    #[error("recovery step is out of order")]
    InvalidTransition,
}

/// Pure three-step shift reconciler.
#[derive(Debug, Clone)]
pub struct ShiftReconciler {
    current: WorkspaceEpoch,
    phase: ShiftPhase,
    pending: Option<RecoveryPlan>,
    observed: BTreeSet<String>,
    preserved_symbols: BTreeSet<String>,
    events: VecDeque<ShiftEvent>,
    max_events: usize,
}

impl ShiftReconciler {
    /// Create a reconciler anchored at `initial_epoch`.
    pub fn new(initial_epoch: WorkspaceEpoch, max_events: usize) -> Result<Self, ShiftError> {
        if max_events == 0 {
            return Err(ShiftError::InvalidEventCapacity);
        }
        Ok(Self {
            current: initial_epoch,
            phase: ShiftPhase::Stable,
            pending: None,
            observed: BTreeSet::new(),
            preserved_symbols: BTreeSet::new(),
            events: VecDeque::new(),
            max_events,
        })
    }

    /// Current epoch.
    pub fn current_epoch(&self) -> &WorkspaceEpoch {
        &self.current
    }

    /// Current phase.
    pub fn phase(&self) -> ShiftPhase {
        self.phase
    }

    /// Pending recovery plan, if any.
    pub fn plan(&self) -> Option<&RecoveryPlan> {
        self.pending.as_ref()
    }

    /// Bounded event log, oldest first.
    pub fn events(&self) -> &VecDeque<ShiftEvent> {
        &self.events
    }

    /// Paths that still need a fresh observation.
    pub fn remaining_observations(&self) -> Vec<String> {
        let Some(plan) = &self.pending else {
            return Vec::new();
        };
        if plan.observations_satisfied(&self.observed) {
            return Vec::new();
        }
        if plan.must_observe_paths.is_empty() {
            return vec!["*".to_string()];
        }
        plan.must_observe_paths
            .iter()
            .filter(|path| !self.observed.contains(*path))
            .cloned()
            .collect()
    }

    /// Observe a new epoch snapshot. Returns a [`ShiftEvent`] exactly when the
    /// epoch changed; an unchanged snapshot is a no-op.
    pub fn observe_epoch(
        &mut self,
        epoch: WorkspaceEpoch,
        changed_paths: impl IntoIterator<Item = impl Into<String>>,
        added_symbols: impl IntoIterator<Item = impl Into<String>>,
        removed_symbols: impl IntoIterator<Item = impl Into<String>>,
    ) -> Option<ShiftEvent> {
        if epoch == self.current {
            return None;
        }

        let previous = self.current.clone();
        let changed = normalise_paths(changed_paths);
        let added = normalise_paths(added_symbols);
        let removed = normalise_paths(removed_symbols);

        let kind = if epoch.requirement_revision != previous.requirement_revision {
            ShiftKind::RequirementRevision
        } else if epoch.tool_schema_epoch != previous.tool_schema_epoch {
            ShiftKind::ToolSessionRebind
        } else if !added.is_empty() || !removed.is_empty() {
            ShiftKind::InterfaceChange
        } else {
            ShiftKind::CollaboratorCommit
        };

        // New collaborator symbols become preserved requirements: a worker may
        // not delete them just to make an old test pass.
        for symbol in &added {
            self.preserved_symbols.insert(symbol.clone());
        }

        self.current = epoch;
        let event = ShiftEvent {
            kind,
            previous,
            current: self.current.clone(),
            changed_paths: changed.clone(),
            added_symbols: added,
            removed_symbols: removed,
            detail: kind.label().to_string(),
        };
        self.begin_recovery(event.clone());
        Some(event)
    }

    /// Record stale evidence explicitly (for example a cached file the agent
    /// read before the shift).
    pub fn observe_stale_evidence(
        &mut self,
        detail: impl Into<String>,
        affected_paths: impl IntoIterator<Item = impl Into<String>>,
    ) -> ShiftEvent {
        let event = ShiftEvent {
            kind: ShiftKind::StaleEvidence,
            previous: self.current.clone(),
            current: self.current.clone(),
            changed_paths: normalise_paths(affected_paths),
            added_symbols: Vec::new(),
            removed_symbols: Vec::new(),
            detail: detail.into(),
        };
        self.begin_recovery(event.clone());
        event
    }

    /// Step 1 acknowledgement: the caller has received the notification and
    /// performed the read-only refresh.
    pub fn acknowledge_read_only_refresh(&mut self) -> Result<(), ShiftError> {
        if self.phase != ShiftPhase::Frozen {
            return Err(ShiftError::InvalidTransition);
        }
        self.phase = ShiftPhase::RefreshPending;
        Ok(())
    }

    /// Step 2: fold the pre-change reasoning history out of the active tail.
    pub fn apply_history_reset(&mut self) -> Result<(), ShiftError> {
        if self.phase != ShiftPhase::RefreshPending {
            return Err(ShiftError::InvalidTransition);
        }
        if self.pending.is_none() {
            return Err(ShiftError::NoPendingShift);
        }
        self.phase = ShiftPhase::HistoryResetRequired;
        Ok(())
    }

    /// Step 3: accept one fresh observation performed in the current epoch.
    /// Returns `true` once the plan's observation requirement is satisfied.
    pub fn record_fresh_observation(&mut self, path: impl Into<String>) -> bool {
        if self.phase != ShiftPhase::HistoryResetRequired
            && self.phase != ShiftPhase::AwaitingFreshObservation
        {
            return false;
        }
        let path = path.into().trim().to_string();
        if path.is_empty() {
            return false;
        }
        self.observed.insert(path);
        let satisfied = self
            .pending
            .as_ref()
            .map(|plan| plan.observations_satisfied(&self.observed))
            .unwrap_or(true);
        if satisfied {
            self.phase = ShiftPhase::Ready;
        } else {
            self.phase = ShiftPhase::AwaitingFreshObservation;
        }
        satisfied
    }

    /// Writes are permitted only while stable or after a completed recovery.
    pub fn can_write(&self) -> bool {
        matches!(self.phase, ShiftPhase::Stable | ShiftPhase::Ready)
    }

    /// Verify that no removed symbol is one of the collaborator symbols this
    /// attempt promised to preserve.
    pub fn verify_symbols_preserved(
        &self,
        removed_symbols: impl IntoIterator<Item = impl Into<String>>,
    ) -> Result<(), ShiftError> {
        for symbol in removed_symbols {
            let symbol = symbol.into();
            if self.preserved_symbols.contains(&symbol) {
                return Err(ShiftError::PreservedSymbolRemoved(symbol));
            }
        }
        Ok(())
    }

    fn begin_recovery(&mut self, event: ShiftEvent) {
        self.phase = ShiftPhase::Frozen;
        self.observed.clear();
        let preserved = self.preserved_symbols.iter().cloned().collect::<Vec<_>>();
        self.pending = Some(RecoveryPlan {
            read_only_refresh: true,
            history_reset: event.kind.requires_history_reset(),
            must_observe_paths: event.changed_paths.clone(),
            preserved_symbols: preserved,
            steps: vec![
                format!("freeze writes after {}", event.kind.label()),
                "read-only refresh of affected state".to_string(),
                "reset pre-change reasoning history".to_string(),
                "require one fresh observation in the current epoch".to_string(),
            ],
            event: event.clone(),
        });
        self.events.push_back(event);
        while self.events.len() > self.max_events {
            self.events.pop_front();
        }
    }
}

fn normalise_paths(items: impl IntoIterator<Item = impl Into<String>>) -> Vec<String> {
    let set: BTreeSet<String> = items
        .into_iter()
        .map(Into::into)
        .map(|item| item.trim().to_string())
        .filter(|item| !item.is_empty())
        .collect();
    set.into_iter().collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn epoch(head: &str, schema: u64, requirement: u64) -> WorkspaceEpoch {
        WorkspaceEpoch::new(head, schema, requirement)
    }

    #[test]
    fn unchanged_epoch_is_a_noop() {
        let mut reconciler =
            ShiftReconciler::new(epoch("abc", 1, 1), DEFAULT_EVENT_CAPACITY).unwrap();
        assert!(reconciler
            .observe_epoch(
                epoch("abc", 1, 1),
                ["src/lib.rs"],
                [] as [&str; 0],
                [] as [&str; 0],
            )
            .is_none());
        assert_eq!(reconciler.phase(), ShiftPhase::Stable);
        assert!(reconciler.can_write());
    }

    #[test]
    fn collaborator_commit_runs_the_three_step_protocol() {
        let mut reconciler =
            ShiftReconciler::new(epoch("abc", 1, 1), DEFAULT_EVENT_CAPACITY).unwrap();
        let event = reconciler
            .observe_epoch(
                epoch("def", 1, 1),
                ["src/lib.rs"],
                [] as [&str; 0],
                [] as [&str; 0],
            )
            .unwrap();
        assert_eq!(event.kind, ShiftKind::CollaboratorCommit);
        assert_eq!(reconciler.phase(), ShiftPhase::Frozen);
        assert!(!reconciler.can_write());
        let plan = reconciler.plan().unwrap();
        assert!(plan.read_only_refresh);
        assert!(plan.history_reset);
        assert_eq!(plan.must_observe_paths, vec!["src/lib.rs"]);

        reconciler.acknowledge_read_only_refresh().unwrap();
        reconciler.apply_history_reset().unwrap();
        assert!(!reconciler.record_fresh_observation("src/other.rs"));
        assert_eq!(reconciler.remaining_observations(), vec!["src/lib.rs"]);
        assert!(reconciler.record_fresh_observation("src/lib.rs"));
        assert_eq!(reconciler.phase(), ShiftPhase::Ready);
        assert!(reconciler.can_write());
    }

    #[test]
    fn requirement_revision_takes_precedence() {
        let mut reconciler =
            ShiftReconciler::new(epoch("abc", 1, 1), DEFAULT_EVENT_CAPACITY).unwrap();
        let event = reconciler
            .observe_epoch(
                epoch("def", 2, 2),
                ["docs/spec.md"],
                ["NewHook"],
                [] as [&str; 0],
            )
            .unwrap();
        assert_eq!(event.kind, ShiftKind::RequirementRevision);
        assert!(reconciler
            .plan()
            .unwrap()
            .preserved_symbols
            .contains(&"NewHook".to_string()));
    }

    #[test]
    fn tool_session_rebind_is_detected() {
        let mut reconciler =
            ShiftReconciler::new(epoch("abc", 1, 1), DEFAULT_EVENT_CAPACITY).unwrap();
        let event = reconciler
            .observe_epoch(
                epoch("abc", 2, 1),
                [] as [&str; 0],
                [] as [&str; 0],
                [] as [&str; 0],
            )
            .unwrap();
        assert_eq!(event.kind, ShiftKind::ToolSessionRebind);
        assert_eq!(reconciler.remaining_observations(), vec!["*"]);
    }

    #[test]
    fn interface_change_preserves_added_symbols() {
        let mut reconciler =
            ShiftReconciler::new(epoch("abc", 1, 1), DEFAULT_EVENT_CAPACITY).unwrap();
        let event = reconciler
            .observe_epoch(
                epoch("def", 1, 1),
                ["src/cache.rs"],
                ["guard_lock"],
                [] as [&str; 0],
            )
            .unwrap();
        assert_eq!(event.kind, ShiftKind::InterfaceChange);
        assert_eq!(
            reconciler.verify_symbols_preserved(["guard_lock"]),
            Err(ShiftError::PreservedSymbolRemoved("guard_lock".into()))
        );
        assert!(reconciler.verify_symbols_preserved(["old_helper"]).is_ok());
    }

    #[test]
    fn stale_evidence_also_freezes_writes() {
        let mut reconciler =
            ShiftReconciler::new(epoch("abc", 1, 1), DEFAULT_EVENT_CAPACITY).unwrap();
        let event = reconciler.observe_stale_evidence("cached read is stale", ["src/lib.rs"]);
        assert_eq!(event.kind, ShiftKind::StaleEvidence);
        assert_eq!(reconciler.phase(), ShiftPhase::Frozen);
        assert!(!reconciler.can_write());
    }

    #[test]
    fn recovery_steps_are_ordered() {
        let mut reconciler =
            ShiftReconciler::new(epoch("abc", 1, 1), DEFAULT_EVENT_CAPACITY).unwrap();
        reconciler.observe_epoch(
            epoch("def", 1, 1),
            ["src/lib.rs"],
            [] as [&str; 0],
            [] as [&str; 0],
        );
        assert_eq!(
            reconciler.apply_history_reset(),
            Err(ShiftError::InvalidTransition)
        );
        assert!(!reconciler.record_fresh_observation("src/lib.rs"));
    }

    #[test]
    fn event_log_is_bounded() {
        let mut reconciler = ShiftReconciler::new(epoch("a", 0, 0), 2).unwrap();
        for (index, head) in ["b", "c", "d"].into_iter().enumerate() {
            reconciler.observe_epoch(
                epoch(head, 0, 0),
                [format!("src/{index}.rs")],
                [] as [&str; 0],
                [] as [&str; 0],
            );
        }
        assert_eq!(reconciler.events().len(), 2);
        assert_eq!(reconciler.events().back().unwrap().current.git_head, "d");
    }
}
