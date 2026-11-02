//! Transactional outbox: at-least-once publication with crash recovery.
//!
//! ## The contract
//!
//! * An entry is written **before** the transport is touched. A crash therefore
//!   leaves evidence, never a silent drop.
//! * Claiming sets `InFlight`; a crash in `InFlight` is recovered on boot by
//!   [`OutboxQueue::recover`], which re-queues the entry. That can duplicate a
//!   delivery, which is why every consumer dedupes on the same stable identity.
//! * Enqueue is idempotent by key. Re-enqueueing a delivered entry is a no-op, so
//!   the recovery sweep cannot resurrect work that already landed.
//!
//! The queue never interprets payloads; it moves opaque JSON to a subject.

use std::sync::Arc;

use chrono::{DateTime, Utc};

use uc_types::governance::{OutboxEntry, OutboxState, OUTBOX_MAX_ATTEMPTS, OUTBOX_NAMESPACE};

use super::record_store::{NamespacedStore, RecordError, RecordStore};

/// Failure modes of the outbox.
#[derive(Debug, Clone, PartialEq)]
pub enum OutboxError {
    Store(RecordError),
}

impl From<RecordError> for OutboxError {
    fn from(error: RecordError) -> Self {
        OutboxError::Store(error)
    }
}

impl std::fmt::Display for OutboxError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            OutboxError::Store(error) => write!(f, "outbox: {error}"),
        }
    }
}

impl std::error::Error for OutboxError {}

/// Durable publication queue.
#[derive(Clone, Debug)]
pub struct OutboxQueue {
    view: NamespacedStore,
}

impl OutboxQueue {
    pub fn new(records: Arc<dyn RecordStore>) -> Self {
        Self {
            view: NamespacedStore::new(records, OUTBOX_NAMESPACE),
        }
    }

    pub fn is_durable(&self) -> bool {
        self.view.is_durable()
    }

    /// Add an entry. Idempotent: an existing key (in any state) is returned
    /// unchanged, so a retried submission cannot publish twice through the queue.
    pub async fn enqueue(&self, entry: &OutboxEntry) -> Result<OutboxEntry, OutboxError> {
        match self.view.put(&entry.key, entry, Some(0)).await {
            Ok(_) => Ok(entry.clone()),
            Err(RecordError::Conflict { .. }) => match self.get(&entry.key).await? {
                Some(existing) => Ok(existing),
                None => Err(OutboxError::Store(RecordError::Conflict {
                    expected: 0,
                    actual: None,
                })),
            },
            Err(other) => Err(OutboxError::Store(other)),
        }
    }

    pub async fn get(&self, key: &str) -> Result<Option<OutboxEntry>, OutboxError> {
        Ok(self
            .view
            .get::<OutboxEntry>(key)
            .await?
            .map(|(entry, _version)| entry))
    }

    pub async fn list(&self, limit: usize) -> Result<Vec<(OutboxEntry, u64)>, OutboxError> {
        Ok(self.view.list(limit).await?)
    }

    /// Claim up to `limit` due entries, marking them `InFlight`.
    ///
    /// A claim is a CAS write, so two dispatchers cannot both claim the same entry
    /// (the loser simply skips it and picks it up on the next tick).
    pub async fn claim_due(
        &self,
        now: DateTime<Utc>,
        limit: usize,
    ) -> Result<Vec<OutboxEntry>, OutboxError> {
        let now_ms = now.timestamp_millis();
        let candidates = self.view.list::<OutboxEntry>(limit.saturating_mul(4).max(limit)).await?;
        let mut claimed = Vec::new();
        for (mut entry, version) in candidates {
            if claimed.len() >= limit {
                break;
            }
            if !entry.is_due(now_ms) || !matches!(entry.state, OutboxState::Pending) {
                continue;
            }
            entry.state = OutboxState::InFlight;
            entry.updated_at = now;
            match self.view.put(&entry.key, &entry, Some(version)).await {
                Ok(_) => claimed.push(entry),
                Err(RecordError::Conflict { .. }) => continue,
                Err(other) => return Err(OutboxError::Store(other)),
            }
        }
        // Claim in queue order so the oldest publication goes first.
        claimed.sort_by_key(|entry| entry.created_at);
        Ok(claimed)
    }

    pub async fn mark_delivered(
        &self,
        key: &str,
        now: DateTime<Utc>,
    ) -> Result<Option<OutboxEntry>, OutboxError> {
        self.mutate(key, |entry| entry.mark_delivered(now)).await
    }

    pub async fn schedule_retry(
        &self,
        key: &str,
        error: impl Into<String>,
        now: DateTime<Utc>,
    ) -> Result<Option<OutboxEntry>, OutboxError> {
        let error = error.into();
        self.mutate(key, |entry| entry.schedule_retry(error.clone(), now))
            .await
    }

    /// Boot-time recovery: every `InFlight` entry (a dispatcher died holding it)
    /// goes back to `Pending` and is immediately due.
    pub async fn recover(&self, now: DateTime<Utc>) -> Result<usize, OutboxError> {
        let entries = self.view.list::<OutboxEntry>(1000).await?;
        let mut recovered = 0usize;
        for (mut entry, version) in entries {
            if !matches!(entry.state, OutboxState::InFlight) {
                continue;
            }
            if !entry.recover_in_flight(now) {
                continue;
            }
            match self.view.put(&entry.key, &entry, Some(version)).await {
                Ok(_) => recovered += 1,
                Err(RecordError::Conflict { .. }) => continue,
                Err(other) => return Err(OutboxError::Store(other)),
            }
        }
        Ok(recovered)
    }

    /// Entries that exhausted their publish attempts and need an operator.
    pub async fn dead_letters(&self, limit: usize) -> Result<Vec<OutboxEntry>, OutboxError> {
        let entries = self.view.list::<OutboxEntry>(limit).await?;
        Ok(entries
            .into_iter()
            .map(|(entry, _version)| entry)
            .filter(|entry| matches!(entry.state, OutboxState::Dead))
            .collect())
    }

    /// Count of entries still awaiting delivery (inspected by the dashboard's
    /// liveness panel and by tests).
    pub async fn pending_count(&self) -> Result<usize, OutboxError> {
        let entries = self.view.list::<OutboxEntry>(1000).await?;
        Ok(entries
            .into_iter()
            .filter(|(entry, _)| {
                matches!(entry.state, OutboxState::Pending | OutboxState::InFlight)
            })
            .count())
    }

    /// Number of publish attempts allowed before an entry is parked.
    pub fn max_attempts() -> u32 {
        OUTBOX_MAX_ATTEMPTS
    }

    async fn mutate<F>(
        &self,
        key: &str,
        mutate: F,
    ) -> Result<Option<OutboxEntry>, OutboxError>
    where
        F: Fn(&mut OutboxEntry),
    {
        for _ in 0..8 {
            let Some((mut entry, version)) = self.view.get::<OutboxEntry>(key).await? else {
                return Ok(None);
            };
            mutate(&mut entry);
            match self.view.put(key, &entry, Some(version)).await {
                Ok(_) => return Ok(Some(entry)),
                Err(RecordError::Conflict { .. }) => continue,
                Err(other) => return Err(OutboxError::Store(other)),
            }
        }
        Err(OutboxError::Store(RecordError::Conflict {
            expected: 0,
            actual: None,
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::governance::record_store::InMemoryRecordStore;

    fn now() -> DateTime<Utc> {
        DateTime::parse_from_rfc3339("2026-01-01T00:00:00Z")
            .unwrap()
            .with_timezone(&Utc)
    }

    fn queue() -> OutboxQueue {
        let store: Arc<dyn RecordStore> = Arc::new(InMemoryRecordStore::new());
        OutboxQueue::new(store)
    }

    fn entry(key: &str) -> OutboxEntry {
        OutboxEntry::new(key, "uc.task.submit", "{}", now())
    }

    #[tokio::test]
    async fn enqueue_is_idempotent_by_key() {
        let queue = queue();
        let first = queue.enqueue(&entry("k1")).await.unwrap();
        let second = queue.enqueue(&entry("k1")).await.unwrap();
        assert_eq!(first.key, second.key);
        assert_eq!(queue.list(10).await.unwrap().len(), 1);
    }

    #[tokio::test]
    async fn claim_moves_pending_to_in_flight_and_is_exclusive() {
        let queue = queue();
        queue.enqueue(&entry("k1")).await.unwrap();
        queue.enqueue(&entry("k2")).await.unwrap();
        let claimed = queue.claim_due(now(), 10).await.unwrap();
        assert_eq!(claimed.len(), 2);
        for entry in &claimed {
            assert_eq!(entry.state, OutboxState::InFlight);
        }
        // Nothing left to claim.
        assert!(queue.claim_due(now(), 10).await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn recover_requeues_entries_orphaned_in_flight() {
        let queue = queue();
        queue.enqueue(&entry("k1")).await.unwrap();
        queue.claim_due(now(), 10).await.unwrap();
        assert_eq!(queue.pending_count().await.unwrap(), 1);

        let recovered = queue.recover(now()).await.unwrap();
        assert_eq!(recovered, 1);
        let claimed_again = queue.claim_due(now(), 10).await.unwrap();
        assert_eq!(claimed_again.len(), 1, "recovered entry must be re-deliverable");
    }

    #[tokio::test]
    async fn delivered_entries_are_never_reclaimed() {
        let queue = queue();
        queue.enqueue(&entry("k1")).await.unwrap();
        queue.claim_due(now(), 10).await.unwrap();
        queue.mark_delivered("k1", now()).await.unwrap();
        assert!(queue.claim_due(now(), 10).await.unwrap().is_empty());
        // Recovery must not resurrect a delivered entry.
        assert_eq!(queue.recover(now()).await.unwrap(), 0);
        assert_eq!(queue.pending_count().await.unwrap(), 0);
    }

    #[tokio::test]
    async fn failing_publishes_back_off_then_dead_letter() {
        let queue = queue();
        queue.enqueue(&entry("k1")).await.unwrap();
        for _ in 0..OUTBOX_MAX_ATTEMPTS {
            let claimed = queue.claim_due(now(), 1).await.unwrap();
            if claimed.is_empty() {
                // Backoff pushed the entry into the future; force it due again by
                // claiming at a later clock.
                let later = now() + chrono::Duration::seconds(60);
                let claimed = queue.claim_due(later, 1).await.unwrap();
                assert!(!claimed.is_empty() || queue.dead_letters(10).await.unwrap().len() == 1);
                if claimed.is_empty() {
                    break;
                }
            }
            queue
                .schedule_retry("k1", "nats unavailable", now())
                .await
                .unwrap();
        }
        let dead = queue.dead_letters(10).await.unwrap();
        assert_eq!(dead.len(), 1);
        assert_eq!(dead[0].state, OutboxState::Dead);
    }

    #[tokio::test]
    async fn claim_respects_the_limit() {
        let queue = queue();
        for index in 0..5 {
            queue.enqueue(&entry(&format!("k{index}"))).await.unwrap();
        }
        let claimed = queue.claim_due(now(), 2).await.unwrap();
        assert_eq!(claimed.len(), 2);
    }

    #[tokio::test]
    async fn missing_key_mutations_are_no_ops() {
        let queue = queue();
        assert!(queue.mark_delivered("nope", now()).await.unwrap().is_none());
        assert!(queue
            .schedule_retry("nope", "err", now())
            .await
            .unwrap()
            .is_none());
    }
}
