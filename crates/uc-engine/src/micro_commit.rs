//! AgentStepper shadow micro-commits (#71): a deterministic event/breakpoint
//! state machine with a shadow commit after every tool call.
//!
//! The real repository is never touched. Callers feed already-observed agent
//! events and receive immutable micro-commit records that a debugger can replay
//! or diff. Prompt/tool hot edits are bounded in-memory overrides; applying
//! them to a live model or tool is the caller's responsibility.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Default bound on retained events.
pub const DEFAULT_MAX_EVENTS: usize = 1024;
/// Default bound on retained shadow commits.
pub const DEFAULT_MAX_COMMITS: usize = 1024;
/// Default bound on hot-edit overrides.
pub const DEFAULT_MAX_OVERRIDES: usize = 64;

/// High-level agent event classes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AgentEventKind {
    /// Before the LLM is queried.
    BeforeLlmQuery,
    /// After the LLM response arrives.
    AfterLlmQuery,
    /// Before a tool is executed.
    BeforeToolCall,
    /// After a tool result arrives.
    AfterToolCall,
}

impl AgentEventKind {
    /// Stable wire name.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::BeforeLlmQuery => "before_llm_query",
            Self::AfterLlmQuery => "after_llm_query",
            Self::BeforeToolCall => "before_tool_call",
            Self::AfterToolCall => "after_tool_call",
        }
    }
}

/// One observed agent event.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AgentEvent {
    /// Monotonic sequence number supplied by the caller.
    pub seq: u64,
    /// Event class.
    pub kind: AgentEventKind,
    /// Short human-readable summary.
    pub summary: String,
    /// Unified diff or tool payload digest.
    pub diff: String,
}

impl AgentEvent {
    /// Build an event.
    #[must_use]
    pub fn new(
        seq: u64,
        kind: AgentEventKind,
        summary: impl Into<String>,
        diff: impl Into<String>,
    ) -> Self {
        Self {
            seq,
            kind,
            summary: summary.into(),
            diff: diff.into(),
        }
    }
}

/// An immutable shadow-branch micro-commit.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MicroCommit {
    /// Commit identifier, unique inside the tracker.
    pub id: String,
    /// Previous shadow commit, if any.
    pub parent: Option<String>,
    /// Event that produced the commit.
    pub event_seq: u64,
    /// Event summary.
    pub summary: String,
    /// Captured diff.
    pub diff: String,
}

/// Stepper state.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StepperState {
    /// Execution is halted at a breakpoint or before the first event.
    Paused,
    /// A single event will execute, then the machine pauses again.
    Stepping,
    /// Events execute until a breakpoint is hit.
    Running,
}

/// Result of ingesting one event.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct IngestOutcome {
    /// State after the event.
    pub state: StepperState,
    /// Shadow commit created by an `AfterToolCall` event.
    pub commit: Option<MicroCommit>,
}

/// A bounded hot-edit override.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HotEdit {
    /// Field name, e.g. `prompt` or `tool.args`.
    pub key: String,
    /// Replacement value.
    pub value: String,
}

/// Errors returned by the stepper.
#[derive(Debug, Error, PartialEq, Eq)]
pub enum MicroCommitError {
    /// A required field was empty.
    #[error("field `{0}` must not be empty")]
    EmptyField(&'static str),
    /// Event capacity reached.
    #[error("event capacity of {0} reached")]
    EventCapacityReached(usize),
    /// Commit capacity reached.
    #[error("commit capacity of {0} reached")]
    CommitCapacityReached(usize),
    /// Override capacity reached.
    #[error("override capacity of {0} reached")]
    OverrideCapacityReached(usize),
}

/// Event/breakpoint state machine with shadow commits.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AgentStepper {
    max_events: usize,
    max_commits: usize,
    max_overrides: usize,
    state: StepperState,
    events: Vec<AgentEvent>,
    commits: Vec<MicroCommit>,
    breakpoints: BTreeSet<AgentEventKind>,
    overrides: BTreeMap<String, String>,
}

impl Default for AgentStepper {
    fn default() -> Self {
        Self::new()
    }
}

impl AgentStepper {
    /// Create a stepper with default bounds, paused at the start.
    #[must_use]
    pub fn new() -> Self {
        Self::with_capacity(
            DEFAULT_MAX_EVENTS,
            DEFAULT_MAX_COMMITS,
            DEFAULT_MAX_OVERRIDES,
        )
    }

    /// Create a stepper with explicit bounds.
    #[must_use]
    pub fn with_capacity(max_events: usize, max_commits: usize, max_overrides: usize) -> Self {
        Self {
            max_events,
            max_commits,
            max_overrides,
            state: StepperState::Paused,
            events: Vec::new(),
            commits: Vec::new(),
            breakpoints: BTreeSet::new(),
            overrides: BTreeMap::new(),
        }
    }

    /// Current state.
    #[must_use]
    pub fn state(&self) -> StepperState {
        self.state
    }

    /// Observed events.
    #[must_use]
    pub fn events(&self) -> &[AgentEvent] {
        &self.events
    }

    /// Shadow commits.
    #[must_use]
    pub fn commits(&self) -> &[MicroCommit] {
        &self.commits
    }

    /// Active breakpoints in deterministic order.
    #[must_use]
    pub fn breakpoints(&self) -> Vec<AgentEventKind> {
        self.breakpoints.iter().copied().collect()
    }

    /// Current hot-edit overrides.
    #[must_use]
    pub fn overrides(&self) -> Vec<HotEdit> {
        self.overrides
            .iter()
            .map(|(key, value)| HotEdit {
                key: key.clone(),
                value: value.clone(),
            })
            .collect()
    }

    /// Add a breakpoint. Returns `false` when it was already present.
    pub fn add_breakpoint(&mut self, kind: AgentEventKind) -> bool {
        self.breakpoints.insert(kind)
    }

    /// Remove a breakpoint. Returns `false` when it was not present.
    pub fn remove_breakpoint(&mut self, kind: AgentEventKind) -> bool {
        self.breakpoints.remove(&kind)
    }

    /// Continue until the next breakpoint.
    pub fn resume(&mut self) {
        self.state = StepperState::Running;
    }

    /// Halt execution.
    pub fn pause(&mut self) {
        self.state = StepperState::Paused;
    }

    /// Execute exactly one queued event, then pause again.
    pub fn step(&mut self) -> bool {
        if self.state == StepperState::Paused {
            self.state = StepperState::Stepping;
            true
        } else {
            false
        }
    }

    /// Record a bounded hot edit.
    pub fn hot_edit(
        &mut self,
        key: impl Into<String>,
        value: impl Into<String>,
    ) -> Result<(), MicroCommitError> {
        let key = key.into();
        if key.trim().is_empty() {
            return Err(MicroCommitError::EmptyField("key"));
        }
        if !self.overrides.contains_key(&key) && self.overrides.len() >= self.max_overrides {
            return Err(MicroCommitError::OverrideCapacityReached(
                self.max_overrides,
            ));
        }
        self.overrides.insert(key, value.into());
        Ok(())
    }

    /// Ingest one event. An `AfterToolCall` event produces a shadow commit.
    ///
    /// When the stepper is paused the event is still recorded (the caller
    /// already observed it), but a breakpoint match keeps it paused. A
    /// `Stepping` machine pauses after this event.
    pub fn ingest(&mut self, event: AgentEvent) -> Result<IngestOutcome, MicroCommitError> {
        if self.events.len() >= self.max_events {
            return Err(MicroCommitError::EventCapacityReached(self.max_events));
        }
        let hit_breakpoint = self.breakpoints.contains(&event.kind);
        let mut commit = None;
        if event.kind == AgentEventKind::AfterToolCall {
            if self.commits.len() >= self.max_commits {
                return Err(MicroCommitError::CommitCapacityReached(self.max_commits));
            }
            let parent = self.commits.last().map(|previous| previous.id.clone());
            commit = Some(MicroCommit {
                id: format!("shadow-{}-{}", self.commits.len() + 1, event.seq),
                parent,
                event_seq: event.seq,
                summary: event.summary.clone(),
                diff: event.diff.clone(),
            });
        }
        self.events.push(event);
        if let Some(commit) = &commit {
            self.commits.push(commit.clone());
        }
        if hit_breakpoint {
            self.state = StepperState::Paused;
        } else if self.state == StepperState::Stepping {
            self.state = StepperState::Paused;
        }
        Ok(IngestOutcome {
            state: self.state,
            commit,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn event(seq: u64, kind: AgentEventKind) -> AgentEvent {
        AgentEvent::new(seq, kind, format!("e{seq}"), format!("diff-{seq}"))
    }

    #[test]
    fn starts_paused() {
        assert_eq!(AgentStepper::new().state(), StepperState::Paused);
    }

    #[test]
    fn resume_and_pause_transition() {
        let mut stepper = AgentStepper::new();
        stepper.resume();
        assert_eq!(stepper.state(), StepperState::Running);
        stepper.pause();
        assert_eq!(stepper.state(), StepperState::Paused);
    }

    #[test]
    fn breakpoint_pauses_running_machine() {
        let mut stepper = AgentStepper::new();
        assert!(stepper.add_breakpoint(AgentEventKind::BeforeToolCall));
        assert!(!stepper.add_breakpoint(AgentEventKind::BeforeToolCall));
        stepper.resume();
        let outcome = stepper
            .ingest(event(1, AgentEventKind::BeforeToolCall))
            .expect("ingest");
        assert!(outcome.commit.is_none());
        assert_eq!(outcome.state, StepperState::Paused);
    }

    #[test]
    fn after_tool_call_creates_parented_commit() {
        let mut stepper = AgentStepper::new();
        let first = stepper
            .ingest(event(1, AgentEventKind::AfterToolCall))
            .expect("ingest")
            .commit
            .expect("commit");
        let second = stepper
            .ingest(event(2, AgentEventKind::AfterToolCall))
            .expect("ingest")
            .commit
            .expect("commit");
        assert_eq!(first.parent, None);
        assert_eq!(second.parent, Some(first.id));
        assert_eq!(stepper.commits().len(), 2);
    }

    #[test]
    fn step_runs_one_event_then_pauses() {
        let mut stepper = AgentStepper::new();
        assert!(stepper.step());
        assert_eq!(stepper.state(), StepperState::Stepping);
        let outcome = stepper
            .ingest(event(1, AgentEventKind::BeforeLlmQuery))
            .expect("ingest");
        assert_eq!(outcome.state, StepperState::Paused);
        assert!(!stepper.step() || stepper.state() == StepperState::Stepping);
    }

    #[test]
    fn breakpoints_are_removed() {
        let mut stepper = AgentStepper::new();
        stepper.add_breakpoint(AgentEventKind::AfterLlmQuery);
        assert_eq!(stepper.breakpoints(), vec![AgentEventKind::AfterLlmQuery]);
        assert!(stepper.remove_breakpoint(AgentEventKind::AfterLlmQuery));
        assert!(stepper.breakpoints().is_empty());
    }

    #[test]
    fn hot_edits_are_bounded_and_replaceable() {
        let mut stepper = AgentStepper::with_capacity(8, 8, 1);
        stepper.hot_edit("prompt", "new").expect("edit");
        stepper.hot_edit("prompt", "newer").expect("replace");
        assert_eq!(stepper.overrides().len(), 1);
        assert_eq!(stepper.overrides()[0].value, "newer");
        assert_eq!(
            stepper.hot_edit("tool.args", "x").unwrap_err(),
            MicroCommitError::OverrideCapacityReached(1)
        );
        assert_eq!(
            stepper.hot_edit(" ", "x").unwrap_err(),
            MicroCommitError::EmptyField("key")
        );
    }

    #[test]
    fn event_capacity_is_enforced() {
        let mut stepper = AgentStepper::with_capacity(1, 8, 8);
        stepper
            .ingest(event(1, AgentEventKind::BeforeLlmQuery))
            .expect("ingest");
        assert_eq!(
            stepper
                .ingest(event(2, AgentEventKind::BeforeLlmQuery))
                .unwrap_err(),
            MicroCommitError::EventCapacityReached(1)
        );
    }

    #[test]
    fn commit_capacity_is_enforced() {
        let mut stepper = AgentStepper::with_capacity(8, 1, 8);
        stepper
            .ingest(event(1, AgentEventKind::AfterToolCall))
            .expect("ingest");
        assert_eq!(
            stepper
                .ingest(event(2, AgentEventKind::AfterToolCall))
                .unwrap_err(),
            MicroCommitError::CommitCapacityReached(1)
        );
    }

    #[test]
    fn all_event_kinds_have_wire_names() {
        assert_eq!(AgentEventKind::BeforeLlmQuery.as_str(), "before_llm_query");
        assert_eq!(AgentEventKind::AfterLlmQuery.as_str(), "after_llm_query");
        assert_eq!(AgentEventKind::BeforeToolCall.as_str(), "before_tool_call");
        assert_eq!(AgentEventKind::AfterToolCall.as_str(), "after_tool_call");
    }
}
