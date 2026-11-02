//! Durable submit-once: the store half of PR01.
//!
//! Responsibilities are deliberately split from publication:
//!
//! * [`SubmissionStore`] answers "has this submission already created a task, and
//!   which one?" It owns the CAS write that makes that answer atomic.
//! * [`super::outbox::OutboxQueue`] owns "has the transport been handed this
//!   submission yet?" — at-least-once, re-driven after a crash.
//!
//! The task id is *derived* from the submission id
//! ([`uc_types::governance::derive_task_id`]), so even the pathological case
//! "outbox published, process died before the record write" cannot produce two
//! logical tasks: the consumer keys its own dedup on the same identity.

use std::sync::Arc;

use chrono::{DateTime, Utc};

use uc_types::governance::{
    derive_task_id, OutboxEntry, SubmissionId, SubmissionRecord, SubmissionRequest,
    SubmissionState, SubmitOnceOutcome, SUBMISSION_NAMESPACE,
};

use super::outbox::OutboxQueue;
use super::record_store::{NamespacedStore, RecordError, RecordStore};

/// Failure modes of the submission store.
#[derive(Debug, Clone, PartialEq)]
pub enum SubmissionError {
    /// The record store failed (I/O, encoding, decode).
    Store(RecordError),
    /// The outbox entry could not be written. The submission record exists, so
    /// the caller must report "recorded but not queued" rather than success.
    Outbox(String),
}

impl From<RecordError> for SubmissionError {
    fn from(error: RecordError) -> Self {
        SubmissionError::Store(error)
    }
}

impl std::fmt::Display for SubmissionError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SubmissionError::Store(error) => write!(f, "submission store: {error}"),
            SubmissionError::Outbox(detail) => write!(f, "submission outbox: {detail}"),
        }
    }
}

impl std::error::Error for SubmissionError {}

/// Outcome of a recovery sweep.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SubmissionRecovery {
    /// Records that were re-queued in the outbox (transport never confirmed).
    pub requeued: usize,
    /// Records found already applied — nothing to do.
    pub already_applied: usize,
    /// Records parked as failed; recovery cannot help them.
    pub failed: usize,
    /// Task ids touched, for logging.
    pub task_ids: Vec<String>,
}

/// Durable submission records.
#[derive(Clone, Debug)]
pub struct SubmissionStore {
    view: NamespacedStore,
}

impl SubmissionStore {
    pub fn new(records: Arc<dyn RecordStore>) -> Self {
        Self {
            view: NamespacedStore::new(records, SUBMISSION_NAMESPACE),
        }
    }

    pub fn is_durable(&self) -> bool {
        self.view.is_durable()
    }

    /// Record a submission exactly once and queue its publication.
    ///
    /// Order matters for crash safety:
    ///
    /// 1. CAS-create the submission record (fails cleanly if it exists).
    /// 2. Enqueue the outbox entry (idempotent by key).
    ///
    /// A crash between the two leaves a `Received` record with no outbox entry;
    /// [`Self::recover_unfinished`] re-creates it. The reverse order would allow a
    /// publish with no record, which is why it is never done.
    pub async fn submit_once(
        &self,
        request: &SubmissionRequest,
        outbox: &OutboxQueue,
        now: DateTime<Utc>,
    ) -> Result<SubmitOnceOutcome, SubmissionError> {
        let key = request.submission_id.as_str();
        let record = SubmissionRecord::new(request, now);
        match self.view.put(key, &record, Some(0)).await {
            Ok(_) => {
                self.queue_publication(&record, request, outbox, now)
                    .await?;
                Ok(SubmitOnceOutcome::Created { record })
            }
            Err(RecordError::Conflict { .. }) => {
                let Some((existing, _version)) = self.view.get::<SubmissionRecord>(key).await?
                else {
                    // Lost a race against a concurrent delete; treat as a
                    // retryable conflict rather than pretending success.
                    return Err(SubmissionError::Store(RecordError::Conflict {
                        expected: 0,
                        actual: None,
                    }));
                };
                let requested_digest = request.digest();
                if existing.request_digest != requested_digest {
                    return Ok(SubmitOnceOutcome::Conflict {
                        submission_id: existing.submission_id.0.clone(),
                        existing_digest: existing.request_digest.clone(),
                        requested_digest,
                    });
                }
                // Same submission, same content: make sure the publication is
                // queued (covers the crash-after-record window) and report a
                // replay.
                self.queue_publication(&existing, request, outbox, now)
                    .await?;
                Ok(SubmitOnceOutcome::Replayed { record: existing })
            }
            Err(other) => Err(SubmissionError::Store(other)),
        }
    }

    async fn queue_publication(
        &self,
        record: &SubmissionRecord,
        request: &SubmissionRequest,
        outbox: &OutboxQueue,
        now: DateTime<Utc>,
    ) -> Result<(), SubmissionError> {
        let payload = serde_json::json!({
            "task_id": record.task_id,
            "description": request.description,
            "project_id": request.project_id,
            "submission_id": record.submission_id.as_str(),
            "case_id": record.case_id,
            // Real-time submission — the Python consumer treats absence of
            // `scheduled` as "not a scheduled job".
        });
        let entry = OutboxEntry::new(
            outbox_key(&record.submission_id),
            OUTBOX_SUBJECT_TASK_SUBMIT,
            payload.to_string(),
            now,
        );
        outbox
            .enqueue(&entry)
            .await
            .map_err(|error| SubmissionError::Outbox(error.to_string()))?;
        Ok(())
    }

    pub async fn get(
        &self,
        submission_id: &SubmissionId,
    ) -> Result<Option<(SubmissionRecord, u64)>, SubmissionError> {
        Ok(self.view.get(submission_id.as_str()).await?)
    }

    pub async fn list(
        &self,
        limit: usize,
    ) -> Result<Vec<(SubmissionRecord, u64)>, SubmissionError> {
        Ok(self.view.list(limit).await?)
    }

    /// Record that the transport accepted the publication.
    pub async fn mark_published(
        &self,
        submission_id: &SubmissionId,
        now: DateTime<Utc>,
    ) -> Result<Option<SubmissionRecord>, SubmissionError> {
        self.transition(submission_id, now, |record| {
            if record.state == SubmissionState::Received {
                record.state = SubmissionState::Published;
                record.published_at = Some(now);
            }
            record.publish_attempts = record.publish_attempts.saturating_add(1);
        })
        .await
    }

    /// Record that the consumer confirmed it created the orchestration.
    pub async fn mark_applied(
        &self,
        submission_id: &SubmissionId,
        now: DateTime<Utc>,
    ) -> Result<Option<SubmissionRecord>, SubmissionError> {
        self.transition(submission_id, now, |record| {
            record.state = SubmissionState::Applied;
            record.applied_at = Some(now);
        })
        .await
    }

    pub async fn mark_failed(
        &self,
        submission_id: &SubmissionId,
        error: impl Into<String>,
        now: DateTime<Utc>,
    ) -> Result<Option<SubmissionRecord>, SubmissionError> {
        let error = error.into();
        self.transition(submission_id, now, |record| {
            record.state = SubmissionState::Failed;
            record.last_error = Some(error.clone());
        })
        .await
    }

    async fn transition<F>(
        &self,
        submission_id: &SubmissionId,
        now: DateTime<Utc>,
        mut mutate: F,
    ) -> Result<Option<SubmissionRecord>, SubmissionError>
    where
        F: FnMut(&mut SubmissionRecord),
    {
        let key = submission_id.as_str();
        // Bounded CAS loop: each iteration either wins or learns a new version.
        for _ in 0..8 {
            let Some((mut record, version)) = self.view.get::<SubmissionRecord>(key).await? else {
                return Ok(None);
            };
            mutate(&mut record);
            record.updated_at = now;
            record.version = version + 1;
            match self.view.put(key, &record, Some(version)).await {
                Ok(_) => return Ok(Some(record)),
                Err(RecordError::Conflict { .. }) => continue,
                Err(other) => return Err(SubmissionError::Store(other)),
            }
        }
        Err(SubmissionError::Store(RecordError::Conflict {
            expected: 0,
            actual: None,
        }))
    }

    /// Startup sweep: re-queue every submission whose publication was never
    /// confirmed, using the *same* derived task id.
    ///
    /// This is what makes "crash recovery does not create a second task" true even
    /// when the crash happened between the record write and the outbox write.
    pub async fn recover_unfinished(
        &self,
        outbox: &OutboxQueue,
        now: DateTime<Utc>,
        limit: usize,
    ) -> Result<SubmissionRecovery, SubmissionError> {
        let mut summary = SubmissionRecovery::default();
        let records = self.view.list::<SubmissionRecord>(limit).await?;
        for (record, _version) in records {
            match record.state {
                SubmissionState::Applied | SubmissionState::Abandoned => {
                    summary.already_applied += 1;
                }
                SubmissionState::Failed => {
                    summary.failed += 1;
                }
                SubmissionState::Received | SubmissionState::Published => {
                    let payload = serde_json::json!({
                        "task_id": record.task_id,
                        "project_id": record.project_id,
                        "submission_id": record.submission_id.as_str(),
                        "case_id": record.case_id,
                        "recovered": true,
                    });
                    let entry = OutboxEntry::new(
                        outbox_key(&record.submission_id),
                        OUTBOX_SUBJECT_TASK_SUBMIT,
                        payload.to_string(),
                        now,
                    );
                    match outbox.enqueue(&entry).await {
                        Ok(_) => {
                            summary.requeued += 1;
                            summary.task_ids.push(record.task_id.clone());
                        }
                        Err(error) => {
                            tracing::warn!(
                                submission_id = %record.submission_id,
                                error = %error,
                                "failed to re-queue recovered submission"
                            );
                        }
                    }
                }
            }
        }
        Ok(summary)
    }

    /// The task id a submission maps to, without touching storage.
    pub fn task_id_for(submission_id: &SubmissionId) -> String {
        derive_task_id(submission_id).0
    }
}

/// Outbox key for a submission. Stable so re-queueing is a no-op.
pub fn outbox_key(submission_id: &SubmissionId) -> String {
    format!("task-submit:{}", submission_id.as_str())
}

/// Outbox entries for submissions live in the outbox namespace; this helper lets
/// recovery/observability code list just the submission publications.
pub fn submission_outbox_prefix() -> &'static str {
    "task-submit:"
}

/// Transport subject for submission publications. Kept here so the store, the
/// outbox dispatcher and the recovery sweep cannot disagree about the string.
pub const OUTBOX_SUBJECT_TASK_SUBMIT: &str = "uc.task.submit";

#[cfg(test)]
mod tests {
    use super::*;
    use crate::governance::record_store::InMemoryRecordStore;
    use uc_types::governance::policy::Principal;

    fn now() -> DateTime<Utc> {
        DateTime::parse_from_rfc3339("2026-01-01T00:00:00Z")
            .unwrap()
            .with_timezone(&Utc)
    }

    struct Fixture {
        submissions: SubmissionStore,
        outbox: OutboxQueue,
    }

    fn fixture() -> Fixture {
        let records: Arc<dyn RecordStore> = Arc::new(InMemoryRecordStore::new());
        Fixture {
            submissions: SubmissionStore::new(records.clone()),
            outbox: OutboxQueue::new(records),
        }
    }

    fn request(id: &str, description: &str) -> SubmissionRequest {
        SubmissionRequest::new(
            Principal::user("alice"),
            "proj",
            description,
            Some(SubmissionId::caller_supplied(id)),
        )
    }

    #[tokio::test]
    async fn first_submit_creates_exactly_one_task_and_one_outbox_entry() {
        let fixture = fixture();
        let outcome = fixture
            .submissions
            .submit_once(&request("key-1", "fix it"), &fixture.outbox, now())
            .await
            .expect("submit");
        let SubmitOnceOutcome::Created { record } = outcome else {
            panic!("expected Created");
        };
        assert_eq!(record.state, SubmissionState::Received);
        assert!(record.task_id.starts_with("sub-"));
        let entries = fixture.outbox.list(10).await.unwrap();
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].0.subject, OUTBOX_SUBJECT_TASK_SUBMIT);
    }

    #[tokio::test]
    async fn retry_returns_the_same_task_and_does_not_duplicate_the_outbox_entry() {
        let fixture = fixture();
        let first = fixture
            .submissions
            .submit_once(&request("key-1", "fix it"), &fixture.outbox, now())
            .await
            .unwrap();
        let second = fixture
            .submissions
            .submit_once(&request("key-1", "fix it"), &fixture.outbox, now())
            .await
            .unwrap();
        let (
            SubmitOnceOutcome::Created { record: first },
            SubmitOnceOutcome::Replayed { record: second },
        ) = (first, second)
        else {
            panic!("expected Created then Replayed");
        };
        assert_eq!(first.task_id, second.task_id);
        assert_eq!(fixture.outbox.list(10).await.unwrap().len(), 1);
    }

    #[tokio::test]
    async fn same_key_different_content_is_rejected_as_conflict() {
        let fixture = fixture();
        fixture
            .submissions
            .submit_once(&request("key-1", "fix it"), &fixture.outbox, now())
            .await
            .unwrap();
        let outcome = fixture
            .submissions
            .submit_once(
                &request("key-1", "do something else"),
                &fixture.outbox,
                now(),
            )
            .await
            .unwrap();
        assert!(outcome.is_conflict());
        // Nothing new was queued.
        assert_eq!(fixture.outbox.list(10).await.unwrap().len(), 1);
    }

    #[tokio::test]
    async fn crash_between_record_and_outbox_is_recovered_with_the_same_task_id() {
        let fixture = fixture();
        // Simulate: record written, outbox never written (crash in between).
        let request = request("key-1", "fix it");
        let record = SubmissionRecord::new(&request, now());
        fixture
            .submissions
            .view
            .put("key-1", &record, Some(0))
            .await
            .expect("record");
        assert!(fixture.outbox.list(10).await.unwrap().is_empty());

        let summary = fixture
            .submissions
            .recover_unfinished(&fixture.outbox, now(), 100)
            .await
            .expect("recover");
        assert_eq!(summary.requeued, 1);
        assert_eq!(summary.task_ids, vec![record.task_id.clone()]);

        // A client retry after recovery replays onto the same task.
        let replay = fixture
            .submissions
            .submit_once(&request, &fixture.outbox, now())
            .await
            .unwrap();
        assert!(replay.is_replayed());
        assert_eq!(
            replay.record().map(|record| record.task_id.clone()),
            Some(record.task_id)
        );
    }

    #[tokio::test]
    async fn recovery_skips_applied_and_failed_submissions() {
        let fixture = fixture();
        fixture
            .submissions
            .submit_once(&request("a", "one"), &fixture.outbox, now())
            .await
            .unwrap();
        fixture
            .submissions
            .mark_applied(&SubmissionId::caller_supplied("a"), now())
            .await
            .unwrap();
        fixture
            .submissions
            .submit_once(&request("b", "two"), &fixture.outbox, now())
            .await
            .unwrap();
        fixture
            .submissions
            .mark_failed(&SubmissionId::caller_supplied("b"), "policy denied", now())
            .await
            .unwrap();

        let summary = fixture
            .submissions
            .recover_unfinished(&fixture.outbox, now(), 100)
            .await
            .unwrap();
        assert_eq!(summary.requeued, 0);
        assert_eq!(summary.already_applied, 1);
        assert_eq!(summary.failed, 1);
    }

    #[tokio::test]
    async fn mark_published_and_applied_advance_the_state_machine() {
        let fixture = fixture();
        let id = SubmissionId::caller_supplied("key-1");
        fixture
            .submissions
            .submit_once(&request("key-1", "fix it"), &fixture.outbox, now())
            .await
            .unwrap();
        let published = fixture
            .submissions
            .mark_published(&id, now())
            .await
            .unwrap()
            .expect("record");
        assert_eq!(published.state, SubmissionState::Published);
        assert_eq!(published.publish_attempts, 1);
        let applied = fixture
            .submissions
            .mark_applied(&id, now())
            .await
            .unwrap()
            .expect("record");
        assert_eq!(applied.state, SubmissionState::Applied);
        assert!(applied.applied_at.is_some());
    }

    #[tokio::test]
    async fn derived_task_id_is_the_single_source_of_identity() {
        let id = SubmissionId::caller_supplied("key-1");
        assert_eq!(SubmissionStore::task_id_for(&id), derive_task_id(&id).0);
        assert_eq!(outbox_key(&id), "task-submit:key-1");
    }
}
