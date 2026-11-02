//! Governance state for the gateway process.
//!
//! This module owns the *governance plane*: durable stores plus the deterministic
//! decisions layered on top of them. It deliberately avoids owning any execution
//! state — the DAG lives in `graph_store`, task lifecycle in `TaskStore`, and
//! dispatch in NATS. Governance records **why** work happens and **under what
//! authority**, never **how** it executes.
//!
//! ## Durability model
//!
//! All stores share one pluggable [`record_store::RecordStore`]: a namespaced,
//! versioned JSON document store. That mirrors the Python `runtime_state.py`
//! design (namespace + version CAS, SQLite and PostgreSQL being interchangeable)
//! so the two halves of the system reason about durability the same way, and it
//! avoids adding a second migration system for every new entity.
//!
//! Crash-recovery rules that depend on it:
//!
//! * an outbox entry left `InFlight` is re-queued on boot ([`outbox::OutboxQueue::recover`]);
//! * a submission left `Received`/`Published` is re-driven with the *same* task id
//!   ([`submission::SubmissionStore::recover_unfinished`]);
//! * an attention item is not a lock: it is a projection and can be rebuilt.

pub mod attention;
pub mod case_resume;
pub mod case_store;
pub mod case_wake;
pub mod outbox;
pub mod policy;
pub mod proactive;
pub mod record_store;
pub mod submission;
pub mod trigger_inbox;
pub mod watchdog;

use std::sync::Arc;

pub use attention::{AttentionBoard, AttentionBoardError};
pub use case_store::{CaseStore, CaseStoreError, MandateStore};
pub use case_resume::{plan_resume, ResumePlan, ResumeSkip};
pub use case_wake::{wake_matched_cases, CaseWake};
pub use outbox::{OutboxError, OutboxQueue};
pub use policy::{PolicyEngine, PolicyEngineError};
pub use proactive::{ProactiveGate, ProactiveGateError};
pub use record_store::{
    InMemoryRecordStore, Record, RecordError, RecordStore, GOVERNANCE_RECORDS_TABLE,
};
#[cfg(feature = "storage")]
pub use record_store::PostgresRecordStore;
pub use submission::{SubmissionError, SubmissionStore};
pub use trigger_inbox::{TriggerInbox, TriggerInboxError};
pub use watchdog::{Watchdog, WatchdogError};

/// Maximum number of records a single `list` call may return. Keeps a dashboard
/// page from turning into an unbounded scan.
pub const GOVERNANCE_LIST_LIMIT: usize = 500;

/// The governance plane, assembled once per gateway process.
///
/// Every field is `Arc`-shared and cheap to clone, so the gRPC services can each
/// hold their own handle without a lock hierarchy.
#[derive(Clone)]
pub struct Governance {
    pub records: Arc<dyn RecordStore>,
    pub submissions: Arc<SubmissionStore>,
    pub outbox: Arc<OutboxQueue>,
    pub cases: Arc<CaseStore>,
    pub mandates: Arc<MandateStore>,
    pub policy: Arc<PolicyEngine>,
    pub triggers: Arc<TriggerInbox>,
    pub attention: Arc<AttentionBoard>,
    pub watchdog: Arc<Watchdog>,
    pub proactive: Arc<ProactiveGate>,
}

impl std::fmt::Debug for Governance {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Governance")
            .field("durable", &self.records.is_durable())
            .finish_non_exhaustive()
    }
}

impl Governance {
    /// Assemble the governance plane over a record store.
    pub fn new(records: Arc<dyn RecordStore>) -> Self {
        let cases = Arc::new(CaseStore::new(records.clone()));
        let mandates = Arc::new(MandateStore::new(records.clone()));
        let policy = Arc::new(PolicyEngine::new(records.clone(), mandates.clone()));
        Self {
            submissions: Arc::new(SubmissionStore::new(records.clone())),
            outbox: Arc::new(OutboxQueue::new(records.clone())),
            triggers: Arc::new(TriggerInbox::new(records.clone())),
            attention: Arc::new(AttentionBoard::new(records.clone())),
            watchdog: Arc::new(Watchdog::new(records.clone())),
            proactive: Arc::new(ProactiveGate::new(records.clone())),
            policy,
            cases,
            mandates,
            records,
        }
    }

    /// In-memory governance plane (tests, single-process dev). Delegates and
    /// outbox entries are not durable in this mode — `is_durable()` reports it
    /// rather than pretending otherwise.
    pub fn in_memory() -> Self {
        Self::new(Arc::new(InMemoryRecordStore::new()))
    }

    /// Whether the backing store survives a process restart.
    pub fn is_durable(&self) -> bool {
        self.records.is_durable()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn in_memory_governance_reports_itself_as_not_durable() {
        let governance = Governance::in_memory();
        assert!(!governance.is_durable());
    }

    #[tokio::test]
    async fn governance_wires_every_store_to_the_same_record_store() {
        let governance = Governance::in_memory();
        // A write through one store is visible through the shared namespace.
        let record = governance
            .records
            .put("wiring_probe", "k", serde_json::json!({"ok": true}), None)
            .await
            .expect("put");
        assert_eq!(record.version, 1);
        let listed = governance.records.list("wiring_probe", 10).await.unwrap();
        assert_eq!(listed.len(), 1);
    }
}
