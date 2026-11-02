//! Attention — the typed "why is this case not progressing" surface.
//!
//! The plan's `少打扰` criterion has two halves, and both live here:
//!
//! * **A waiting case holds no execution resource.** [`AttentionKind`] is the
//!   single place that decides whether a case consumes a worker slot
//!   ([`AttentionKind::holds_worker_slot`]). `WaitingEvent` and `Verifying` are
//!   explicitly slot-free.
//! * **Only human decisions interrupt a human.** `NeedsHuman` is a distinct
//!   state with reserved capacity, so an operator sees a small, ordered
//!   worklist instead of a stream of progress events.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

/// Record-store namespace for attention items.
pub const ATTENTION_NAMESPACE: &str = "attention";

/// Why a case cannot move forward without something external happening.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AttentionKind {
    /// Waiting on an external event (CI run, PR review, webhook). No worker
    /// slot, no human interruption.
    WaitingEvent,
    /// A human decision is required (ambiguous requirement, approval, merge
    /// conflict arbitration). Reserves human-priority capacity.
    NeedsHuman,
    /// Blocked on a prerequisite that is not an event and not a human decision
    /// (dependency failure, exhausted budget, revoked mandate).
    Blocked,
    /// Execution finished; independent verification is running. Slot-free.
    Verifying,
}

impl AttentionKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            AttentionKind::WaitingEvent => "waiting_event",
            AttentionKind::NeedsHuman => "needs_human",
            AttentionKind::Blocked => "blocked",
            AttentionKind::Verifying => "verifying",
        }
    }

    pub fn from_str_lossy(value: &str) -> Option<Self> {
        Some(match value.trim().to_ascii_lowercase().as_str() {
            "waiting_event" => AttentionKind::WaitingEvent,
            "needs_human" => AttentionKind::NeedsHuman,
            "blocked" => AttentionKind::Blocked,
            "verifying" => AttentionKind::Verifying,
            _ => return None,
        })
    }

    /// Does a case in this attention state occupy a worker execution slot?
    ///
    /// This is the acceptance criterion "等待 CI 不消耗 Worker 槽位" expressed as
    /// a total function: there is no default, every kind is classified, and the
    /// answer is `false` for all of them. An attention state means the case is
    /// *not executing* — if it were executing it would be `Active`, which is not
    /// an attention state at all.
    pub fn holds_worker_slot(&self) -> bool {
        match self {
            AttentionKind::WaitingEvent | AttentionKind::Verifying => false,
            AttentionKind::NeedsHuman | AttentionKind::Blocked => false,
        }
    }

    /// Does this state reserve a *human-priority* slot so that a decision, once
    /// made, can start executing immediately instead of queueing behind long
    /// coding runs?
    ///
    /// Only `NeedsHuman` does. `WaitingEvent` explicitly does not: reserving
    /// capacity for a case that may be parked for hours is exactly the waste the
    /// plan forbids.
    pub fn reserves_human_slot(&self) -> bool {
        matches!(self, AttentionKind::NeedsHuman)
    }

    /// Does a case in this state interrupt a human?
    ///
    /// Only `NeedsHuman`. A `Blocked` case is reported in the dashboard but does
    /// not page anyone: by definition the operator cannot unblock it by deciding
    /// something right now.
    pub fn requires_human_now(&self) -> bool {
        matches!(self, AttentionKind::NeedsHuman)
    }

    /// Should this state be surfaced in the operator worklist?
    pub fn is_visible_in_worklist(&self) -> bool {
        !matches!(self, AttentionKind::Verifying)
    }
}

impl std::fmt::Display for AttentionKind {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// How an attention item was resolved.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", tag = "kind")]
pub enum AttentionResolution {
    /// A human answered / decided.
    HumanDecision { by: String, answer: String },
    /// The awaited event arrived.
    EventReceived { trigger_id: String },
    /// The case moved on by itself (retry, recovery, or cancellation).
    Automatic { detail: String },
    /// The case was cancelled / failed while waiting.
    CaseClosed { status: String },
}

impl AttentionResolution {
    pub fn summary(&self) -> String {
        match self {
            AttentionResolution::HumanDecision { by, answer } => {
                format!("human:{by}:{answer}")
            }
            AttentionResolution::EventReceived { trigger_id } => {
                format!("event:{trigger_id}")
            }
            AttentionResolution::Automatic { detail } => format!("auto:{detail}"),
            AttentionResolution::CaseClosed { status } => format!("closed:{status}"),
        }
    }
}

/// One thing a case is waiting on.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AttentionItem {
    pub id: String,
    pub case_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub task_id: Option<String>,
    pub kind: AttentionKind,
    /// Short one-line summary for the worklist.
    pub summary: String,
    /// Longer detail / the exact question a human must answer.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
    /// When this attention state began (used for ageing / ordering).
    pub since: DateTime<Utc>,
    /// Human priority inherited from the case, used to order the worklist.
    #[serde(default)]
    pub priority: u8,
    /// What the human is expected to do, when `kind == NeedsHuman`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub human_action: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub resolved_at: Option<DateTime<Utc>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub resolution: Option<AttentionResolution>,
    #[serde(default)]
    pub version: u64,
}

impl AttentionItem {
    pub fn new(
        id: impl Into<String>,
        case_id: impl Into<String>,
        kind: AttentionKind,
        summary: impl Into<String>,
        since: DateTime<Utc>,
    ) -> Self {
        Self {
            id: id.into(),
            case_id: case_id.into(),
            task_id: None,
            kind,
            summary: summary.into(),
            detail: None,
            since,
            priority: 0,
            human_action: None,
            resolved_at: None,
            resolution: None,
            version: 0,
        }
    }

    pub fn with_detail(mut self, detail: impl Into<String>) -> Self {
        self.detail = Some(detail.into());
        self
    }

    pub fn with_human_action(mut self, action: impl Into<String>) -> Self {
        self.human_action = Some(action.into());
        self
    }

    pub fn with_task(mut self, task_id: impl Into<String>) -> Self {
        self.task_id = Some(task_id.into());
        self
    }

    pub fn with_priority(mut self, priority: u8) -> Self {
        self.priority = priority;
        self
    }

    pub fn is_open(&self) -> bool {
        self.resolved_at.is_none()
    }

    pub fn resolve(&mut self, resolution: AttentionResolution, now: DateTime<Utc>) {
        if self.resolved_at.is_some() {
            return;
        }
        self.resolved_at = Some(now);
        self.resolution = Some(resolution);
    }

    /// Ordering for the operator worklist: human decisions first, then priority
    /// (descending), then age (oldest first). A deterministic total order keeps
    /// the dashboard stable between refreshes.
    pub fn worklist_order_key(&self) -> (u8, std::cmp::Reverse<u8>, i64, String) {
        let human_first = if self.kind.requires_human_now() { 0 } else { 1 };
        (
            human_first,
            std::cmp::Reverse(self.priority),
            self.since.timestamp_millis(),
            self.case_id.clone(),
        )
    }
}

/// Capacity accounting for the scheduler: what is reserved for humans and what
/// waiting cases would have cost if they held slots.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct CapacitySnapshot {
    /// Worker slots configured.
    pub worker_slots_total: u32,
    /// Slots currently executing subtasks.
    pub worker_slots_busy: u32,
    /// Slots reserved so a human decision can be acted on immediately when it
    /// arrives, instead of queueing behind long coding runs.
    pub worker_slots_reserved_human: u32,
    /// Cases parked in `WaitingEvent` — these occupy no slot.
    pub waiting_event_cases: u32,
    /// Cases parked in `Verifying` — also slot-free.
    pub verifying_cases: u32,
    /// Open `NeedsHuman` items (the human worklist depth).
    pub needs_human_items: u32,
    #[serde(default)]
    pub blocked_cases: u32,
}

impl CapacitySnapshot {
    /// Slots that may accept ordinary (non-human) work.
    pub fn worker_slots_available(&self) -> u32 {
        self.worker_slots_total
            .saturating_sub(self.worker_slots_busy)
            .saturating_sub(self.worker_slots_reserved_human)
    }

    /// Would admitting one more execution exceed reserved capacity?
    pub fn can_admit_worker(&self) -> bool {
        self.worker_slots_available() > 0
    }

    /// How many worker slots would be wasted if waiting cases held slots. This
    /// is the `少打扰` metric made observable: it should equal
    /// `waiting_event_cases + verifying_cases`.
    pub fn slots_saved_by_quiet_waiting(&self) -> u32 {
        self.waiting_event_cases.saturating_add(self.verifying_cases)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn now() -> DateTime<Utc> {
        DateTime::parse_from_rfc3339("2026-01-01T00:00:00Z")
            .unwrap()
            .with_timezone(&Utc)
    }

    #[test]
    fn waiting_for_ci_never_holds_a_worker_slot() {
        assert!(!AttentionKind::WaitingEvent.holds_worker_slot());
        assert!(!AttentionKind::Verifying.holds_worker_slot());
        assert!(!AttentionKind::NeedsHuman.holds_worker_slot());
        assert!(!AttentionKind::Blocked.holds_worker_slot());
        // Only a pending human decision reserves priority capacity.
        assert!(AttentionKind::NeedsHuman.reserves_human_slot());
        assert!(!AttentionKind::WaitingEvent.reserves_human_slot());
    }

    #[test]
    fn only_needs_human_interrupts_a_human() {
        assert!(AttentionKind::NeedsHuman.requires_human_now());
        assert!(!AttentionKind::WaitingEvent.requires_human_now());
        assert!(!AttentionKind::Blocked.requires_human_now());
        assert!(!AttentionKind::Verifying.requires_human_now());
    }

    #[test]
    fn resolution_is_idempotent() {
        let mut item = AttentionItem::new("a", "case-1", AttentionKind::NeedsHuman, "pick a branch", now());
        item.resolve(
            AttentionResolution::HumanDecision {
                by: "alice".to_string(),
                answer: "release/2.0".to_string(),
            },
            now(),
        );
        assert!(!item.is_open());
        let first = item.resolution.clone();
        item.resolve(
            AttentionResolution::Automatic {
                detail: "late".to_string(),
            },
            now(),
        );
        assert_eq!(item.resolution, first);
    }

    #[test]
    fn worklist_puts_human_decisions_first_then_priority() {
        let human = AttentionItem::new("a", "case-a", AttentionKind::NeedsHuman, "x", now());
        let blocked_high =
            AttentionItem::new("b", "case-b", AttentionKind::Blocked, "y", now()).with_priority(9);
        let blocked_low =
            AttentionItem::new("c", "case-c", AttentionKind::Blocked, "z", now()).with_priority(1);
        let mut items = vec![blocked_low.clone(), human.clone(), blocked_high.clone()];
        items.sort_by_key(|item| item.worklist_order_key());
        assert_eq!(items[0].id, human.id);
        assert_eq!(items[1].id, blocked_high.id);
        assert_eq!(items[2].id, blocked_low.id);
    }

    #[test]
    fn capacity_reserves_human_headroom_and_reports_savings() {
        let snapshot = CapacitySnapshot {
            worker_slots_total: 8,
            worker_slots_busy: 5,
            worker_slots_reserved_human: 2,
            waiting_event_cases: 4,
            verifying_cases: 1,
            needs_human_items: 3,
            blocked_cases: 1,
        };
        assert_eq!(snapshot.worker_slots_available(), 1);
        assert!(snapshot.can_admit_worker());
        assert_eq!(snapshot.slots_saved_by_quiet_waiting(), 5);
        let exhausted = CapacitySnapshot {
            worker_slots_busy: 6,
            ..snapshot
        };
        assert_eq!(exhausted.worker_slots_available(), 0);
        assert!(!exhausted.can_admit_worker());
    }

    #[test]
    fn attention_kind_tokens_round_trip() {
        for kind in [
            AttentionKind::WaitingEvent,
            AttentionKind::NeedsHuman,
            AttentionKind::Blocked,
            AttentionKind::Verifying,
        ] {
            assert_eq!(AttentionKind::from_str_lossy(kind.as_str()), Some(kind));
        }
        assert!(!AttentionKind::Verifying.is_visible_in_worklist());
        assert!(AttentionKind::NeedsHuman.is_visible_in_worklist());
    }
}
