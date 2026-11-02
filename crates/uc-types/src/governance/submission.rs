//! Submit-once: stable submission identity, durable submission records, and the
//! transactional outbox that makes publication crash-safe.
//!
//! ## The problem being solved
//!
//! The current submit path creates a task in the store, publishes to NATS, and
//! deletes the placeholder if the publish fails. A crash or a client retry
//! between those steps either loses the submission or creates a second task.
//!
//! ## The protocol
//!
//! ```text
//! client ──(submission_id)──► SubmissionStore.submit_once
//!                                │  durable: submission record + outbox entry
//!                                │  (one CAS write; in Postgres one transaction)
//!                                ▼
//!                           OutboxDispatcher ──► NATS uc.task.submit
//!                                │                 (at-least-once)
//!                                ▼
//!                           mark_published / mark_applied
//!                                ▲
//!   consumer dedupes on (submission_id, task_id) ──┘
//! ```
//!
//! Three properties follow, and they are the acceptance criteria of PR01:
//!
//! 1. **At most one logical task per submission.** The task id is *derived* from
//!    the submission id ([`derive_task_id`]), so it is stable across retries,
//!    reconnects and restart recovery even if the record write is lost after the
//!    publish.
//! 2. **No submission loss.** The outbox entry is written before the publish is
//!    attempted; a crash leaves a `Pending` entry that the dispatcher re-drives.
//! 3. **No silent divergence.** Reusing a `submission_id` with a different
//!    request body is a [`SubmitOnceOutcome::Conflict`], not a silent replay of
//!    the earlier work.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

use super::policy::Principal;
use crate::agent::TaskId;
use crate::merge::sha256_hex;

/// Record-store namespace for submission records.
pub const SUBMISSION_NAMESPACE: &str = "submission";

/// Record-store namespace for outbox entries.
pub const OUTBOX_NAMESPACE: &str = "outbox";

/// Upper bound of publish attempts before an outbox entry is parked for an
/// operator. Bounded so a poison payload cannot spin forever.
pub const OUTBOX_MAX_ATTEMPTS: u32 = 12;

/// Submission identifier.
///
/// Either client-supplied (an idempotency key, ideally a UUID) or derived from
/// the request so a client that cannot generate one still gets replay safety.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct SubmissionId(pub String);

impl SubmissionId {
    /// Use the caller's idempotency key verbatim (trimmed). Empty is rejected by
    /// the caller — a blank key would collapse unrelated submissions.
    pub fn caller_supplied(key: impl AsRef<str>) -> Self {
        Self(key.as_ref().trim().to_string())
    }

    /// Deterministic fallback when the client does not send a key: the same
    /// principal + project + request text always maps to the same submission.
    pub fn derive(principal: &Principal, project_id: &str, description: &str) -> Self {
        let digest = request_digest(project_id, description);
        Self(format!(
            "auto-{}",
            &sha256_hex(format!("{principal}|{project_id}|{digest}").as_bytes())[..32]
        ))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }

    pub fn is_empty(&self) -> bool {
        self.0.trim().is_empty()
    }
}

impl std::fmt::Display for SubmissionId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// Normalize a request before digesting it.
///
/// Mirrors the contract normalization the worker side already uses
/// (`contract.py`): line endings collapse to `\n`, trailing whitespace per line
/// is stripped, and leading/trailing blank lines are dropped. Wording changes —
/// including a single negation — must change the digest, so nothing else is
/// normalized.
pub fn normalize_request(description: &str) -> String {
    let unified = description.replace("\r\n", "\n").replace('\r', "\n");
    let trimmed_lines: Vec<&str> = unified.split('\n').map(|line| line.trim_end()).collect();
    trimmed_lines.join("\n").trim_matches('\n').to_string()
}

/// Digest identifying the *content* of a submission.
pub fn request_digest(project_id: &str, description: &str) -> String {
    sha256_hex(format!("{project_id}\u{0}{}", normalize_request(description)).as_bytes())
}

/// A task id derived from a submission id.
///
/// Stability is the whole point: the same submission always names the same
/// logical task, so replay after a crash cannot create a second one.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DerivedTaskId(pub TaskId);

impl DerivedTaskId {
    pub fn as_task_id(&self) -> &TaskId {
        &self.0
    }
}

/// Derive the stable logical task id for a submission.
pub fn derive_task_id(submission_id: &SubmissionId) -> TaskId {
    let digest = sha256_hex(format!("task:{}", submission_id.as_str()).as_bytes());
    TaskId(format!("sub-{}", &digest[..32]))
}

/// Lifecycle of a submission record.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SubmissionState {
    /// Durable record written; outbox entry pending. No transport attempt yet.
    Received,
    /// Handed to the transport at least once. Delivery is still unconfirmed.
    Published,
    /// The consumer confirmed it created the orchestration for `task_id`.
    Applied,
    /// Terminal failure (validation, policy denial, or exhausted publish
    /// attempts). Never silently retried.
    Failed,
    /// Superseded by an operator decision (e.g. cancelled before dispatch).
    Abandoned,
}

impl SubmissionState {
    pub fn as_str(&self) -> &'static str {
        match self {
            SubmissionState::Received => "received",
            SubmissionState::Published => "published",
            SubmissionState::Applied => "applied",
            SubmissionState::Failed => "failed",
            SubmissionState::Abandoned => "abandoned",
        }
    }

    pub fn from_str_lossy(value: &str) -> Option<Self> {
        Some(match value.trim().to_ascii_lowercase().as_str() {
            "received" => SubmissionState::Received,
            "published" => SubmissionState::Published,
            "applied" => SubmissionState::Applied,
            "failed" => SubmissionState::Failed,
            "abandoned" => SubmissionState::Abandoned,
            _ => return None,
        })
    }

    pub fn is_terminal(&self) -> bool {
        matches!(
            self,
            SubmissionState::Applied | SubmissionState::Failed | SubmissionState::Abandoned
        )
    }
}

/// A request to create exactly one logical task.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SubmissionRequest {
    pub submission_id: SubmissionId,
    pub principal: Principal,
    pub project_id: String,
    pub description: String,
    /// Optional case this submission belongs to (PR02 link).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub case_id: Option<String>,
}

impl SubmissionRequest {
    pub fn new(
        principal: Principal,
        project_id: impl Into<String>,
        description: impl Into<String>,
        submission_id: Option<SubmissionId>,
    ) -> Self {
        let project_id = project_id.into();
        let description = description.into();
        let submission_id = submission_id
            .unwrap_or_else(|| SubmissionId::derive(&principal, &project_id, &description));
        Self {
            submission_id,
            principal,
            project_id,
            description,
            case_id: None,
        }
    }

    pub fn with_case(mut self, case_id: impl Into<String>) -> Self {
        self.case_id = Some(case_id.into());
        self
    }

    /// Content digest used to detect "same key, different request".
    pub fn digest(&self) -> String {
        let mut material = format!(
            "{}\u{0}{}",
            self.project_id,
            normalize_request(&self.description)
        );
        if let Some(case_id) = &self.case_id {
            material.push('\u{0}');
            material.push_str(case_id);
        }
        sha256_hex(material.as_bytes())
    }
}

/// Durable record of one submission.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SubmissionRecord {
    pub submission_id: SubmissionId,
    /// The single logical task this submission created (derived, therefore
    /// stable across retries).
    pub task_id: String,
    pub state: SubmissionState,
    /// Who submitted it.
    pub principal: Principal,
    pub project_id: String,
    /// Content digest; a different digest under the same id is a conflict.
    pub request_digest: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub case_id: Option<String>,
    /// Outbox entry key carrying the publication of this submission.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub outbox_key: Option<String>,
    /// How many times the transport was handed this submission.
    #[serde(default)]
    pub publish_attempts: u32,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub published_at: Option<DateTime<Utc>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub applied_at: Option<DateTime<Utc>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_error: Option<String>,
    /// CAS token owned by the store.
    #[serde(default)]
    pub version: u64,
}

impl SubmissionRecord {
    pub fn new(request: &SubmissionRequest, now: DateTime<Utc>) -> Self {
        Self {
            submission_id: request.submission_id.clone(),
            task_id: derive_task_id(&request.submission_id).0,
            state: SubmissionState::Received,
            principal: request.principal.clone(),
            project_id: request.project_id.clone(),
            request_digest: request.digest(),
            case_id: request.case_id.clone(),
            outbox_key: None,
            publish_attempts: 0,
            created_at: now,
            updated_at: now,
            published_at: None,
            applied_at: None,
            last_error: None,
            version: 0,
        }
    }

    pub fn task_id_value(&self) -> TaskId {
        TaskId(self.task_id.clone())
    }

    pub fn task_id_matches_derivation(&self) -> bool {
        derive_task_id(&self.submission_id).0 == self.task_id
    }
}

/// Result of [`super::super::governance`]-level submit-once handling.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", tag = "outcome")]
pub enum SubmitOnceOutcome {
    /// A new submission record (and outbox entry) was created.
    Created { record: SubmissionRecord },
    /// The submission id was already recorded with identical content; the
    /// recorded task id is returned and nothing new is created.
    Replayed { record: SubmissionRecord },
    /// The submission id was reused with different content. Nothing is created.
    Conflict {
        submission_id: String,
        existing_digest: String,
        requested_digest: String,
    },
}

impl SubmitOnceOutcome {
    pub fn record(&self) -> Option<&SubmissionRecord> {
        match self {
            SubmitOnceOutcome::Created { record } | SubmitOnceOutcome::Replayed { record } => {
                Some(record)
            }
            SubmitOnceOutcome::Conflict { .. } => None,
        }
    }

    pub fn is_replayed(&self) -> bool {
        matches!(self, SubmitOnceOutcome::Replayed { .. })
    }

    pub fn is_conflict(&self) -> bool {
        matches!(self, SubmitOnceOutcome::Conflict { .. })
    }
}

/// Publication state of an outbox entry.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OutboxState {
    /// Queued and due (or scheduled for a later retry).
    Pending,
    /// Claimed by a dispatcher; a crash in this state is recovered by resetting
    /// to `Pending` on boot (at-least-once, never at-most-once).
    InFlight,
    /// Acknowledged by the transport.
    Delivered,
    /// Exhausted retries; parked for an operator.
    Dead,
}

impl OutboxState {
    pub fn as_str(&self) -> &'static str {
        match self {
            OutboxState::Pending => "pending",
            OutboxState::InFlight => "in_flight",
            OutboxState::Delivered => "delivered",
            OutboxState::Dead => "dead",
        }
    }
}

/// One pending publication: the transport half of the transactional outbox.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OutboxEntry {
    /// Stable key. Re-enqueueing the same key is a no-op, which is what makes
    /// the outbox participate in submit-once rather than double-publishing.
    pub key: String,
    /// Transport subject (`uc.task.submit`).
    pub subject: String,
    /// UTF-8 JSON payload, re-serialized verbatim at publish time.
    pub payload: String,
    pub state: OutboxState,
    #[serde(default)]
    pub attempts: u32,
    /// Earliest wall-clock time (ms) the entry may be claimed.
    #[serde(default)]
    pub next_attempt_at_ms: i64,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub delivered_at: Option<DateTime<Utc>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_error: Option<String>,
}

impl OutboxEntry {
    pub fn new(
        key: impl Into<String>,
        subject: impl Into<String>,
        payload: impl Into<String>,
        now: DateTime<Utc>,
    ) -> Self {
        let created_at_ms = now.timestamp_millis();
        Self {
            key: key.into(),
            subject: subject.into(),
            payload: payload.into(),
            state: OutboxState::Pending,
            attempts: 0,
            next_attempt_at_ms: created_at_ms,
            created_at: now,
            updated_at: now,
            delivered_at: None,
            last_error: None,
        }
    }

    pub fn is_due(&self, now_ms: i64) -> bool {
        matches!(self.state, OutboxState::Pending) && self.next_attempt_at_ms <= now_ms
    }

    /// Exponential backoff on publish failure: 250ms, 500ms, 1s, … capped at
    /// 30s. Backoff is additive to any broker delay, so it never shortens the
    /// window in which a duplicate could still be delivered.
    pub fn schedule_retry(&mut self, error: impl Into<String>, now: DateTime<Utc>) {
        self.attempts = self.attempts.saturating_add(1);
        self.state = OutboxState::Pending;
        self.last_error = Some(error.into());
        self.updated_at = now;
        if self.attempts >= OUTBOX_MAX_ATTEMPTS {
            self.state = OutboxState::Dead;
            return;
        }
        let backoff_ms = 250i64
            .saturating_mul(1i64 << self.attempts.min(7))
            .min(30_000);
        self.next_attempt_at_ms = now.timestamp_millis().saturating_add(backoff_ms);
    }

    pub fn mark_delivered(&mut self, now: DateTime<Utc>) {
        self.state = OutboxState::Delivered;
        self.delivered_at = Some(now);
        self.updated_at = now;
        self.last_error = None;
    }

    /// Recovery: an entry left `InFlight` by a crashed dispatcher is re-queued.
    /// This can produce a duplicate delivery, which is why the consumer dedupes.
    pub fn recover_in_flight(&mut self, now: DateTime<Utc>) -> bool {
        if !matches!(self.state, OutboxState::InFlight) {
            return false;
        }
        self.state = OutboxState::Pending;
        self.updated_at = now;
        self.next_attempt_at_ms = now.timestamp_millis();
        true
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
    fn derived_task_id_is_stable_across_retries() {
        let id = SubmissionId::caller_supplied("fixed-key");
        assert_eq!(derive_task_id(&id), derive_task_id(&id));
        assert!(derive_task_id(&id).0.starts_with("sub-"));
        let other = SubmissionId::caller_supplied("other-key");
        assert_ne!(derive_task_id(&id), derive_task_id(&other));
    }

    #[test]
    fn auto_submission_id_is_deterministic_for_same_request() {
        let principal = Principal::user("alice");
        let a = SubmissionId::derive(&principal, "proj", "fix the build");
        let b = SubmissionId::derive(&principal, "proj", "fix the build");
        let c = SubmissionId::derive(&principal, "proj", "fix the build for real");
        assert_eq!(a, b);
        assert_ne!(a, c);
    }

    #[test]
    fn request_digest_ignores_whitespace_noise_but_not_wording() {
        let a = request_digest("proj", "do the thing\n");
        let b = request_digest("proj", "do the thing\r\n");
        let c = request_digest("proj", "do the thing   ");
        let d = request_digest("proj", "do not the thing");
        assert_eq!(a, b);
        assert_eq!(a, c);
        assert_ne!(a, d);
    }

    #[test]
    fn same_id_different_content_is_a_conflict_shape() {
        let record = SubmissionRecord::new(
            &SubmissionRequest::new(
                Principal::user("alice"),
                "proj",
                "first request",
                Some(SubmissionId::caller_supplied("k1")),
            ),
            now(),
        );
        let conflicting = SubmissionRequest::new(
            Principal::user("alice"),
            "proj",
            "second request",
            Some(SubmissionId::caller_supplied("k1")),
        );
        assert_ne!(record.request_digest, conflicting.digest());
        assert!(record.task_id_matches_derivation());
    }

    #[test]
    fn outbox_retry_backs_off_and_dies_after_the_cap() {
        let mut entry = OutboxEntry::new("k", "uc.task.submit", "{}", now());
        assert!(entry.is_due(now().timestamp_millis()));
        for _ in 0..OUTBOX_MAX_ATTEMPTS {
            entry.schedule_retry("nats unavailable", now());
        }
        assert_eq!(entry.state, OutboxState::Dead);
        assert!(!entry.is_due(now().timestamp_millis() + 120_000));
    }

    #[test]
    fn in_flight_entry_is_recovered_after_crash() {
        let mut entry = OutboxEntry::new("k", "uc.task.submit", "{}", now());
        entry.state = OutboxState::InFlight;
        let later = now() + chrono::Duration::seconds(5);
        assert!(entry.recover_in_flight(later));
        assert_eq!(entry.state, OutboxState::Pending);
        assert!(entry.is_due(later.timestamp_millis()));
        // Idempotent: a second recovery is a no-op.
        assert!(!entry.recover_in_flight(later));
    }
}
