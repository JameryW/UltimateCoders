//! # Governance — long-running task governance for the Engineering Agent OS
//!
//! This module is the **two-layer split** described by the UC upgrade plan:
//!
//! ```text
//! DeepSeek-borrowed layer : Source → Trigger → Policy → Mandate → WorkCase → Subscription → Attention
//! UC core execution layer : TaskRun → DAG → Node → Attempt → Worker → EffectLedger → Oracle
//! ```
//!
//! Everything here belongs to the first layer. It is deliberately **pure data +
//! deterministic evaluation**: no I/O, no clocks read from the environment, no
//! LLM calls. Where a decision depends on time, the caller passes `now` in so
//! the same inputs always produce the same output (and tests can pin it).
//!
//! The second layer is untouched: `Task`/`Subtask`/`SubtaskStatus` in
//! [`crate::agent`] remain the execution contract, the Rust `GraphStore` remains
//! the DAG authority, and NATS remains the dispatch transport. A [`WorkCase`]
//! is a *governance* entity that owns many `TaskRun`s — it never replaces the
//! existing scheduler.
//!
//! ## Non-negotiable invariants encoded here
//!
//! 1. **No duplicate work** ([`submission`]) — a stable `submission_id` maps to
//!    at most one logical task; replays return the recorded outcome.
//! 2. **No privilege escalation** ([`mandate`], [`policy`]) — a mandate can only
//!    ever be narrowed, never widened; external triggers cannot originate one;
//!    tool and sandbox authorization is fail-closed.
//! 3. **Durable cases** ([`workcase`]) — one case spans many runs, CI cycles and
//!    restarts; run history is append-only.
//! 4. **Quiet waiting** ([`attention`]) — a case waiting on an external event
//!    holds no worker slot; only human decisions get priority capacity.
//! 5. **Bounded autonomy** ([`proactive`]) — Off/Observe/Propose/Execute is an
//!    explicit mode, proposals are auditable, watchdog replans are capped.

pub mod approval;
pub mod attention;
pub mod mandate;
pub mod policy;
pub mod proactive;
pub mod submission;
pub mod trigger;
pub mod workcase;

pub use approval::{
    approval_id, ApprovalError, ApprovalRequest, ApprovalState, ToolApproval, APPROVAL_TTL_MINUTES,
    MAX_APPROVAL_TTL_MINUTES,
};
pub use attention::{
    AttentionItem, AttentionKind, AttentionResolution, CapacitySnapshot, ATTENTION_NAMESPACE,
};
pub use mandate::{
    pattern_covers, Mandate, MandateDecision, MandateDenial, MandateId, MandateOrigin,
    MandateRequest, MandateRevocation, MandateScope,
};
pub use policy::{
    ActionClass, DenyReason, Grant, GrantError, Permission, PolicyDecision, PolicySnapshot,
    PolicyVersion, Principal, PrincipalKind, SandboxSecurityProfile, SandboxViolation, ToolPolicy,
    ToolPolicyTable,
};
pub use proactive::{
    ProactiveAuditEntry, ProactiveGateState, ProactiveMode, ProactiveProposal, ProposalDecision,
    ProposalError, ProposalKind, ProposalStatus, WatchdogConfig, WatchdogDecision, WatchdogSignal,
    WatchdogVerdict, PROACTIVE_AUDIT_NAMESPACE, PROACTIVE_NAMESPACE,
};
pub use submission::{
    derive_task_id, request_digest, DerivedTaskId, OutboxEntry, OutboxState, SubmissionId,
    SubmissionRecord, SubmissionRequest, SubmissionState, SubmitOnceOutcome, OUTBOX_MAX_ATTEMPTS,
    OUTBOX_NAMESPACE, SUBMISSION_NAMESPACE,
};
pub use trigger::{
    IngestOutcome, SourceKind, Subscription, SubscriptionFilter, SubscriptionId, TriggerEvent,
    TriggerId, TriggerKind, SUBSCRIPTION_NAMESPACE, TRIGGER_NAMESPACE,
};
pub use workcase::{
    CaseId, CaseOrigin, CaseRun, CaseRunOutcome, CaseStatus, CaseTransitionError, RunId, WorkCase,
    CASE_NAMESPACE, MAX_RUN_HISTORY,
};
