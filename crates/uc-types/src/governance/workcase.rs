//! WorkCase — the long-running unit of governed work.
//!
//! A `WorkCase` is what survives between `TaskRun`s. The existing `Task` type
//! still models a single execution request; a case records **why** the work
//! exists (its origin trigger), **under what authority** (its mandate), and
//! **what has already been tried** (its run history). It deliberately does not
//! contain a DAG: the graph plane stays the single state authority for
//! execution planning.
//!
//! Invariants:
//!
//! * `runs` is append-only and ordered by `attempt`; a run is never removed.
//! * A case may have at most one **open** run at a time (`TaskRun` serialization
//!   at the governance layer — concurrent DAG execution is still the graph
//!   plane's call after a run is opened).
//! * `status` transitions are validated ([`CaseStatus::can_transition_to`]) so
//!   e.g. a `Completed` case cannot silently go back to `Active`, and
//!   `NeedsHuman` is only reachable when a human decision is genuinely required.
//! * `version` is a monotonically increasing CAS token owned by the store.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

/// Record-store namespace for work cases.
pub const CASE_NAMESPACE: &str = "work_case";

/// Upper bound on retained run links inside the case document. Older runs are
/// compacted into a count, never silently dropped from the *audit* (the run
/// records themselves live in their own namespace).
pub const MAX_RUN_HISTORY: usize = 64;

/// Stable identifier for a long-running governed work item.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct CaseId(pub String);

impl CaseId {
    pub fn new() -> Self {
        Self(uuid::Uuid::new_v4().to_string())
    }

    pub fn from_parts(source: &str, external_ref: &str) -> Self {
        // Deterministic case identity for a thread (repo#123): re-delivered or
        // polled events must converge on the same case instead of creating a
        // second one.
        let digest = crate::merge::sha256_hex(format!("case:{source}:{external_ref}").as_bytes());
        Self(format!("case-{}", &digest[..32]))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl Default for CaseId {
    fn default() -> Self {
        Self::new()
    }
}

impl std::fmt::Display for CaseId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// Identifier of one `TaskRun` (a single execution attempt of the case).
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct RunId(pub String);

impl RunId {
    pub fn new() -> Self {
        Self(uuid::Uuid::new_v4().to_string())
    }
}

impl Default for RunId {
    fn default() -> Self {
        Self::new()
    }
}

/// Lifecycle of a governed case.
///
/// `WaitingEvent`, `Verifying` and `NeedsHuman` are *non-executing* states: no
/// subtask may be dispatched for a case in them (see
/// [`crate::governance::attention::AttentionKind::holds_worker_slot`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CaseStatus {
    /// Created, not yet started. No run has been opened.
    Open,
    /// At least one run is executing, or work is scheduled to continue.
    Active,
    /// Blocked on an external event (CI result, PR review, webhook). Holds no
    /// worker slot.
    WaitingEvent,
    /// Blocked on a human decision. Priority capacity is reserved, but no
    /// worker executes until the decision lands.
    NeedsHuman,
    /// Blocked on a prerequisite that is neither an event nor a human action
    /// (e.g. failing dependency, exhausted budget).
    Blocked,
    /// Work finished; independent verification is running. Not yet successful.
    Verifying,
    /// Independently verified success. Terminal.
    Completed,
    /// Terminal failure (verification failed / gave up / cancelled by policy).
    Failed,
    /// Terminal, cancelled by an authorized principal.
    Cancelled,
}

impl CaseStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            CaseStatus::Open => "open",
            CaseStatus::Active => "active",
            CaseStatus::WaitingEvent => "waiting_event",
            CaseStatus::NeedsHuman => "needs_human",
            CaseStatus::Blocked => "blocked",
            CaseStatus::Verifying => "verifying",
            CaseStatus::Completed => "completed",
            CaseStatus::Failed => "failed",
            CaseStatus::Cancelled => "cancelled",
        }
    }

    pub fn from_str_lossy(value: &str) -> Option<Self> {
        Some(match value.trim().to_ascii_lowercase().as_str() {
            "open" => CaseStatus::Open,
            "active" => CaseStatus::Active,
            "waiting_event" => CaseStatus::WaitingEvent,
            "needs_human" => CaseStatus::NeedsHuman,
            "blocked" => CaseStatus::Blocked,
            "verifying" => CaseStatus::Verifying,
            "completed" => CaseStatus::Completed,
            "failed" => CaseStatus::Failed,
            "cancelled" | "canceled" => CaseStatus::Cancelled,
            _ => return None,
        })
    }

    /// Terminal states cannot transition anywhere.
    pub fn is_terminal(&self) -> bool {
        matches!(
            self,
            CaseStatus::Completed | CaseStatus::Failed | CaseStatus::Cancelled
        )
    }

    /// States in which no execution resource may be consumed.
    pub fn is_quiescent(&self) -> bool {
        matches!(
            self,
            CaseStatus::WaitingEvent
                | CaseStatus::NeedsHuman
                | CaseStatus::Blocked
                | CaseStatus::Verifying
        )
    }

    /// Only independently verified success reaches `Completed` — a run
    /// reporting success moves the case to `Verifying`, never straight to
    /// `Completed`.
    pub fn can_transition_to(&self, next: CaseStatus) -> bool {
        use CaseStatus::*;
        if self.is_terminal() {
            return false;
        }
        if *self == next {
            return true;
        }
        match (self, next) {
            (Open, Active) => true,
            (Open, Blocked) => true,
            (Open, NeedsHuman) => true,
            (Open, Cancelled) => true,
            (Open, Failed) => true,
            (Active, WaitingEvent) => true,
            (Active, NeedsHuman) => true,
            (Active, Blocked) => true,
            (Active, Verifying) => true,
            (Active, Completed) => false, // must verify first
            (Active, Failed) => true,
            (Active, Cancelled) => true,
            (WaitingEvent, Active) => true,
            (WaitingEvent, NeedsHuman) => true,
            (WaitingEvent, Blocked) => true,
            (WaitingEvent, Verifying) => true,
            (WaitingEvent, Cancelled) => true,
            (WaitingEvent, Failed) => true,
            (NeedsHuman, Active) => true,
            (NeedsHuman, Blocked) => true,
            (NeedsHuman, Cancelled) => true,
            (NeedsHuman, Failed) => true,
            (Blocked, Active) => true,
            (Blocked, NeedsHuman) => true,
            (Blocked, WaitingEvent) => true,
            (Blocked, Cancelled) => true,
            (Blocked, Failed) => true,
            (Verifying, Completed) => true,
            (Verifying, Failed) => true,
            (Verifying, Active) => true, // verification found more work
            (Verifying, NeedsHuman) => true,
            (Verifying, Cancelled) => true,
            _ => false,
        }
    }
}

impl std::fmt::Display for CaseStatus {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Where a case came from. Provenance is what lets a later trigger/comment be
/// attached to the *same* case instead of opening a new one.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct CaseOrigin {
    /// Trigger source kind (`github`, `ci`, `lark`, `dashboard`, `api`, `cron`).
    pub source: String,
    /// Source-native reference (`JameryW/UltimateCoders#123`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub external_ref: Option<String>,
    /// Normalized thread key used for subscription matching.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub thread_key: Option<String>,
    /// Trigger event that opened the case.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub trigger_id: Option<String>,
}

impl CaseOrigin {
    /// Origin for a case opened directly by an authenticated principal. The
    /// principal itself is recorded as the mandate grantor, not duplicated
    /// here — origin describes *the event*, not the authority.
    pub fn manual() -> Self {
        Self {
            source: "manual".to_string(),
            external_ref: None,
            thread_key: None,
            trigger_id: None,
        }
    }

    pub fn from_trigger(source: &str, thread_key: Option<String>, trigger_id: Option<String>) -> Self {
        Self {
            source: source.to_string(),
            external_ref: thread_key.clone(),
            thread_key,
            trigger_id,
        }
    }
}

/// One execution attempt of a case: the link from governance to the existing
/// execution plane. The DAG itself lives in the graph store, keyed by `task_id`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CaseRun {
    pub run_id: RunId,
    /// Logical task created for this run (`TaskId` in the execution plane).
    pub task_id: String,
    /// Monotonic attempt index within the case (1-based).
    pub attempt: u32,
    /// Stable submission id that created this run (idempotency handle).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub submission_id: Option<String>,
    pub started_at: DateTime<Utc>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ended_at: Option<DateTime<Utc>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub outcome: Option<CaseRunOutcome>,
    /// Why the run ended, when it did not simply succeed.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

impl CaseRun {
    pub fn open(task_id: impl Into<String>, attempt: u32, now: DateTime<Utc>) -> Self {
        Self {
            run_id: RunId::new(),
            task_id: task_id.into(),
            attempt,
            submission_id: None,
            started_at: now,
            ended_at: None,
            outcome: None,
            detail: None,
        }
    }

    pub fn is_open(&self) -> bool {
        self.outcome.is_none()
    }

    pub fn finish(&mut self, outcome: CaseRunOutcome, detail: Option<String>, now: DateTime<Utc>) {
        if self.outcome.is_some() {
            return;
        }
        self.outcome = Some(outcome);
        self.ended_at = Some(now);
        self.detail = detail;
    }
}

/// How a run ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CaseRunOutcome {
    /// The execution plane reported success; the case still has to verify.
    Succeeded,
    Failed,
    Cancelled,
    /// A newer run superseded this one (e.g. a follow-up commit arrived).
    Superseded,
}

/// A long-running governed unit of work.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WorkCase {
    pub id: CaseId,
    pub title: String,
    pub description: String,
    /// Execution scope; every run of the case is confined to it.
    pub project_id: String,
    pub status: CaseStatus,
    /// Provenance of the case (which trigger opened it, on which thread).
    pub origin: CaseOrigin,
    /// Normalized digest of the original human request — the contract identity
    /// shared with `contract.py` on the worker side.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub contract_digest: Option<String>,
    /// Deterministic completion conditions (kept opaque here; the execution
    /// plane owns evaluation).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub completion_conditions: Option<serde_json::Value>,
    /// Currently valid mandate. `None` means the case may not execute anything.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mandate_id: Option<String>,
    /// Active subscription ids feeding this case.
    #[serde(default)]
    pub subscriptions: Vec<String>,
    /// Append-only run links, bounded by [`MAX_RUN_HISTORY`].
    #[serde(default)]
    pub runs: Vec<CaseRun>,
    /// How many runs were compacted out of `runs` (audit trail preserved in the
    /// run namespace).
    #[serde(default)]
    pub compacted_runs: u32,
    /// Human priority for attention ordering (higher = more urgent). Capped so
    /// that a trigger cannot self-assign unbounded priority.
    #[serde(default)]
    pub priority: u8,
    #[serde(default)]
    pub labels: Vec<String>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    /// CAS token; the store rejects writes carrying a stale version.
    #[serde(default)]
    pub version: u64,
}

impl WorkCase {
    /// Create an open case. `priority` is clamped to [`WorkCase::MAX_PRIORITY`]:
    /// priority is an operator hint, and an inbound trigger must not be able to
    /// starve every other case by claiming an extreme value.
    pub fn new(
        title: impl Into<String>,
        description: impl Into<String>,
        project_id: impl Into<String>,
        origin: CaseOrigin,
        now: DateTime<Utc>,
    ) -> Self {
        Self {
            id: CaseId::new(),
            title: title.into(),
            description: description.into(),
            project_id: project_id.into(),
            status: CaseStatus::Open,
            origin,
            contract_digest: None,
            completion_conditions: None,
            mandate_id: None,
            subscriptions: Vec::new(),
            runs: Vec::new(),
            compacted_runs: 0,
            priority: 0,
            labels: Vec::new(),
            created_at: now,
            updated_at: now,
            version: 0,
        }
    }

    pub const MAX_PRIORITY: u8 = 9;

    pub fn with_id(mut self, id: CaseId) -> Self {
        self.id = id;
        self
    }

    pub fn with_contract_digest(mut self, digest: impl Into<String>) -> Self {
        self.contract_digest = Some(digest.into());
        self
    }

    pub fn set_priority(&mut self, priority: u8) {
        self.priority = priority.min(Self::MAX_PRIORITY);
    }

    /// The currently open run, if any. At most one exists.
    pub fn active_run(&self) -> Option<&CaseRun> {
        self.runs.iter().find(|r| r.is_open())
    }

    pub fn next_attempt(&self) -> u32 {
        self.compacted_runs + self.runs.len() as u32 + 1
    }

    /// Open a new run. Fails when the case is terminal, quiescent, or already
    /// has an open run — those are the conditions under which starting another
    /// DAG would duplicate work or violate governance state.
    pub fn open_run(
        &mut self,
        task_id: impl Into<String>,
        submission_id: Option<String>,
        now: DateTime<Utc>,
    ) -> Result<RunId, CaseTransitionError> {
        if self.status.is_terminal() {
            return Err(CaseTransitionError::Terminal(self.status));
        }
        if self.status.is_quiescent() {
            return Err(CaseTransitionError::Quiescent(self.status));
        }
        if self.active_run().is_some() {
            return Err(CaseTransitionError::RunAlreadyOpen);
        }
        if self.mandate_id.is_none() {
            return Err(CaseTransitionError::NoMandate);
        }
        let mut run = CaseRun::open(task_id, self.next_attempt(), now);
        run.submission_id = submission_id;
        let run_id = run.run_id.clone();
        self.runs.push(run);
        self.compact_runs();
        self.updated_at = now;
        Ok(run_id)
    }

    /// Finish the open run. `Succeeded` moves the case to `Verifying` — success
    /// is only declared after independent verification.
    pub fn finish_run(
        &mut self,
        run_id: &RunId,
        outcome: CaseRunOutcome,
        detail: Option<String>,
        now: DateTime<Utc>,
    ) -> Result<(), CaseTransitionError> {
        let run = self
            .runs
            .iter_mut()
            .find(|r| &r.run_id == run_id)
            .ok_or(CaseTransitionError::UnknownRun)?;
        if !run.is_open() {
            return Err(CaseTransitionError::RunAlreadyFinished);
        }
        run.finish(outcome, detail, now);
        let next = match outcome {
            CaseRunOutcome::Succeeded => CaseStatus::Verifying,
            CaseRunOutcome::Failed => CaseStatus::Active,
            CaseRunOutcome::Cancelled => CaseStatus::Cancelled,
            CaseRunOutcome::Superseded => CaseStatus::Active,
        };
        self.transition(next, now)?;
        self.updated_at = now;
        Ok(())
    }

    pub fn transition(
        &mut self,
        next: CaseStatus,
        now: DateTime<Utc>,
    ) -> Result<(), CaseTransitionError> {
        if !self.status.can_transition_to(next) {
            return Err(CaseTransitionError::Illegal {
                from: self.status,
                to: next,
            });
        }
        self.status = next;
        self.updated_at = now;
        Ok(())
    }

    pub fn attach_subscription(&mut self, subscription_id: impl Into<String>) {
        let id = subscription_id.into();
        if !self.subscriptions.contains(&id) {
            self.subscriptions.push(id);
        }
    }

    fn compact_runs(&mut self) {
        while self.runs.len() > MAX_RUN_HISTORY {
            let removed = self.runs.remove(0);
            // Only finished runs may be compacted out; an open run is never the
            // oldest by construction (it is pushed last).
            if removed.is_open() {
                self.runs.insert(0, removed);
                break;
            }
            self.compacted_runs += 1;
        }
    }
}

/// Rejected case state transition.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CaseTransitionError {
    Illegal { from: CaseStatus, to: CaseStatus },
    Terminal(CaseStatus),
    Quiescent(CaseStatus),
    RunAlreadyOpen,
    RunAlreadyFinished,
    UnknownRun,
    NoMandate,
}

impl std::fmt::Display for CaseTransitionError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CaseTransitionError::Illegal { from, to } => {
                write!(f, "illegal case transition {from} -> {to}")
            }
            CaseTransitionError::Terminal(status) => {
                write!(f, "case is terminal ({status})")
            }
            CaseTransitionError::Quiescent(status) => {
                write!(f, "case is quiescent ({status}) and may not start a run")
            }
            CaseTransitionError::RunAlreadyOpen => {
                f.write_str("case already has an open run")
            }
            CaseTransitionError::RunAlreadyFinished => {
                f.write_str("run is already finished")
            }
            CaseTransitionError::UnknownRun => f.write_str("unknown run id"),
            CaseTransitionError::NoMandate => {
                f.write_str("case has no mandate and may not execute")
            }
        }
    }
}

impl std::error::Error for CaseTransitionError {}

#[cfg(test)]
mod tests {
    use super::*;

    fn now() -> DateTime<Utc> {
        DateTime::parse_from_rfc3339("2026-01-01T00:00:00Z")
            .unwrap()
            .with_timezone(&Utc)
    }

    fn case_with_mandate() -> WorkCase {
        let mut case = WorkCase::new("t", "d", "proj", CaseOrigin::manual(), now());
        case.mandate_id = Some("m-1".to_string());
        case
    }

    #[test]
    fn success_goes_through_verifying_not_straight_to_completed() {
        let mut case = case_with_mandate();
        assert!(case.transition(CaseStatus::Active, now()).is_ok());
        let run = case
            .open_run("task-1", None, now())
            .expect("run opens on an active case");
        case.finish_run(&run, CaseRunOutcome::Succeeded, None, now())
            .expect("finish ok");
        assert_eq!(case.status, CaseStatus::Verifying);
        assert!(!CaseStatus::Active.can_transition_to(CaseStatus::Completed));
        case.transition(CaseStatus::Completed, now()).expect("verify completes");
        assert!(case.status.is_terminal());
        assert!(!case.status.can_transition_to(CaseStatus::Active));
    }

    #[test]
    fn active_case_cannot_jump_to_completed() {
        assert!(!CaseStatus::Active.can_transition_to(CaseStatus::Completed));
        assert!(CaseStatus::Verifying.can_transition_to(CaseStatus::Completed));
    }

    #[test]
    fn quiescent_case_cannot_open_a_run() {
        let mut case = case_with_mandate();
        case.status = CaseStatus::WaitingEvent;
        assert_eq!(
            case.open_run("t", None, now()),
            Err(CaseTransitionError::Quiescent(CaseStatus::WaitingEvent))
        );
        case.status = CaseStatus::NeedsHuman;
        assert!(case.open_run("t", None, now()).is_err());
    }

    #[test]
    fn case_without_mandate_cannot_execute() {
        let mut case = WorkCase::new("t", "d", "p", CaseOrigin::manual(), now());
        case.status = CaseStatus::Active;
        assert_eq!(
            case.open_run("t", None, now()),
            Err(CaseTransitionError::NoMandate)
        );
    }

    #[test]
    fn only_one_run_may_be_open() {
        let mut case = case_with_mandate();
        case.transition(CaseStatus::Active, now()).unwrap();
        let first = case.open_run("task-1", None, now()).unwrap();
        assert_eq!(
            case.open_run("task-2", None, now()),
            Err(CaseTransitionError::RunAlreadyOpen)
        );
        case.finish_run(&first, CaseRunOutcome::Failed, None, now())
            .unwrap();
        // Failed run returns the case to Active so a retry can open a run.
        assert_eq!(case.status, CaseStatus::Active);
        assert_eq!(case.next_attempt(), 2);
    }

    #[test]
    fn deterministic_case_identity_converges_for_same_thread() {
        let a = CaseId::from_parts("github", "JameryW/UltimateCoders#7");
        let b = CaseId::from_parts("github", "JameryW/UltimateCoders#7");
        let c = CaseId::from_parts("github", "JameryW/UltimateCoders#8");
        assert_eq!(a, b);
        assert_ne!(a, c);
        assert!(a.as_str().starts_with("case-"));
    }

    #[test]
    fn priority_is_capped() {
        let mut case = case_with_mandate();
        case.set_priority(250);
        assert_eq!(case.priority, WorkCase::MAX_PRIORITY);
    }

    #[test]
    fn run_history_is_bounded_but_counted() {
        let mut case = case_with_mandate();
        case.status = CaseStatus::Active;
        for i in 0..(MAX_RUN_HISTORY + 5) {
            let run = case.open_run(format!("task-{i}"), None, now()).unwrap();
            case.finish_run(&run, CaseRunOutcome::Failed, None, now())
                .unwrap();
        }
        assert_eq!(case.runs.len(), MAX_RUN_HISTORY);
        assert!(case.compacted_runs >= 5);
        assert_eq!(case.next_attempt(), MAX_RUN_HISTORY as u32 + case.compacted_runs + 1);
    }

    #[test]
    fn every_status_round_trips_through_its_wire_token() {
        for status in [
            CaseStatus::Open,
            CaseStatus::Active,
            CaseStatus::WaitingEvent,
            CaseStatus::NeedsHuman,
            CaseStatus::Blocked,
            CaseStatus::Verifying,
            CaseStatus::Completed,
            CaseStatus::Failed,
            CaseStatus::Cancelled,
        ] {
            assert_eq!(CaseStatus::from_str_lossy(status.as_str()), Some(status));
            let json = serde_json::to_string(&status).unwrap();
            let back: CaseStatus = serde_json::from_str(&json).unwrap();
            assert_eq!(back, status);
        }
    }
}

