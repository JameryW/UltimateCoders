//! Trigger inbox and subscription matching.
//!
//! Ingest is **idempotent by dedup key**: the event document is written with
//! create-only CAS, so the second copy of a GitHub delivery (or the polling
//! reconciler racing the webhook) is reported as `duplicate` and matches nothing.
//! Only the first copy is fanned out to subscriptions.
//!
//! Subscription matching lives here rather than in the service layer so that the
//! gRPC service, the NATS bridge and the in-process API all apply exactly the same
//! rule.

use std::sync::Arc;

use chrono::{DateTime, Utc};

use uc_types::governance::{
    IngestOutcome, SourceKind, Subscription, TriggerEvent, SUBSCRIPTION_NAMESPACE,
    TRIGGER_NAMESPACE,
};

use super::record_store::{NamespacedStore, RecordError, RecordStore};

/// Failure modes of the trigger inbox.
#[derive(Debug, Clone, PartialEq)]
pub enum TriggerInboxError {
    Store(RecordError),
    NotFound { subscription_id: String },
    Contended,
}

impl From<RecordError> for TriggerInboxError {
    fn from(error: RecordError) -> Self {
        TriggerInboxError::Store(error)
    }
}

impl std::fmt::Display for TriggerInboxError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            TriggerInboxError::Store(error) => write!(f, "trigger inbox: {error}"),
            TriggerInboxError::NotFound { subscription_id } => {
                write!(f, "subscription {subscription_id} not found")
            }
            TriggerInboxError::Contended => {
                f.write_str("trigger inbox contention; retry the operation")
            }
        }
    }
}

impl std::error::Error for TriggerInboxError {}

/// Deduplicated trigger events plus their subscriptions.
#[derive(Clone, Debug)]
pub struct TriggerInbox {
    events: NamespacedStore,
    subscriptions: NamespacedStore,
}

impl TriggerInbox {
    pub fn new(records: Arc<dyn RecordStore>) -> Self {
        Self {
            events: NamespacedStore::new(records.clone(), TRIGGER_NAMESPACE),
            subscriptions: NamespacedStore::new(records, SUBSCRIPTION_NAMESPACE),
        }
    }

    pub fn is_durable(&self) -> bool {
        self.events.is_durable()
    }

    /// Record an event and fan it out to matching subscriptions.
    ///
    /// "*Open a case when nothing matched*" is deliberately **not** done here: it
    /// is a policy decision (does an unlabelled comment deserve a whole case?), so
    /// the caller decides based on `unmatched_reason`.
    pub async fn ingest(
        &self,
        event: &TriggerEvent,
        now: DateTime<Utc>,
    ) -> Result<IngestOutcome, TriggerInboxError> {
        let dedup_key = event.dedup_key();
        match self
            .events
            .put(&dedup_key, event, Some(0))
            .await
        {
            Ok(_) => {}
            Err(RecordError::Conflict { .. }) => {
                return Ok(IngestOutcome::duplicate(
                    event.id.as_str().to_string(),
                    dedup_key,
                ));
            }
            Err(other) => return Err(TriggerInboxError::Store(other)),
        }

        let subscriptions = self.subscriptions.list::<Subscription>(1000).await?;
        let mut matched_cases: Vec<String> = Vec::new();
        let mut unmatched_reason = None;
        for (mut subscription, version) in subscriptions {
            if !subscription.active || subscription.source != event.source {
                continue;
            }
            if !subscription.filter.matches(event) {
                continue;
            }
            if !matched_cases.contains(&subscription.case_id) {
                matched_cases.push(subscription.case_id.clone());
            }
            subscription.record_event(now);
            match self
                .subscriptions
                .put(subscription.id.as_str(), &subscription, Some(version))
                .await
            {
                Ok(_) => {}
                Err(RecordError::Conflict { .. }) => {
                    // Losing the counter race is harmless; the delivery already
                    // counted for routing.
                }
                Err(other) => return Err(TriggerInboxError::Store(other)),
            }
        }
        if matched_cases.is_empty() {
            unmatched_reason = Some(if event.thread_key.is_some() {
                "thread_has_no_subscription".to_string()
            } else {
                "no_subscription".to_string()
            });
        }

        Ok(IngestOutcome {
            event_id: event.id.as_str().to_string(),
            dedup_key,
            accepted: true,
            duplicate: false,
            matched_cases,
            unmatched_reason,
        })
    }

    pub async fn upsert_subscription(
        &self,
        mut subscription: Subscription,
    ) -> Result<Subscription, TriggerInboxError> {
        for _ in 0..8 {
            match self
                .subscriptions
                .get::<Subscription>(subscription.id.as_str())
                .await?
            {
                None => {
                    return match self
                        .subscriptions
                        .put(subscription.id.as_str(), &subscription, Some(0))
                        .await
                    {
                        Ok(_) => Ok(subscription),
                        Err(RecordError::Conflict { .. }) => continue,
                        Err(other) => Err(TriggerInboxError::Store(other)),
                    };
                }
                Some((existing, version)) => {
                    // Preserve the delivery statistics across updates.
                    subscription.event_count = existing.event_count;
                    subscription.last_event_at = existing.last_event_at;
                    subscription.created_at = existing.created_at;
                    match self
                        .subscriptions
                        .put(subscription.id.as_str(), &subscription, Some(version))
                        .await
                    {
                        Ok(_) => return Ok(subscription),
                        Err(RecordError::Conflict { .. }) => continue,
                        Err(other) => return Err(TriggerInboxError::Store(other)),
                    }
                }
            }
        }
        Err(TriggerInboxError::Contended)
    }

    pub async fn delete_subscription(
        &self,
        subscription_id: &str,
    ) -> Result<bool, TriggerInboxError> {
        Ok(self.subscriptions.delete(subscription_id).await?)
    }

    pub async fn list_subscriptions(
        &self,
        case_id: Option<&str>,
        source: Option<SourceKind>,
        limit: usize,
    ) -> Result<Vec<Subscription>, TriggerInboxError> {
        let subscriptions = self.subscriptions.list::<Subscription>(limit).await?;
        Ok(subscriptions
            .into_iter()
            .map(|(subscription, _version)| subscription)
            .filter(|subscription| {
                case_id
                    .map(|case_id| subscription.case_id == case_id)
                    .unwrap_or(true)
                    && source
                        .map(|source| subscription.source == source)
                        .unwrap_or(true)
            })
            .collect())
    }

    pub async fn get_event(
        &self,
        dedup_key: &str,
    ) -> Result<Option<TriggerEvent>, TriggerInboxError> {
        Ok(self
            .events
            .get::<TriggerEvent>(dedup_key)
            .await?
            .map(|(event, _version)| event))
    }

    pub async fn list_events(&self, limit: usize) -> Result<Vec<TriggerEvent>, TriggerInboxError> {
        Ok(self
            .events
            .list::<TriggerEvent>(limit)
            .await?
            .into_iter()
            .map(|(event, _version)| event)
            .collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::governance::record_store::InMemoryRecordStore;
    use uc_types::governance::{
        SubscriptionFilter, TriggerId, TriggerKind,
    };

    fn now() -> DateTime<Utc> {
        DateTime::parse_from_rfc3339("2026-01-01T00:00:00Z")
            .unwrap()
            .with_timezone(&Utc)
    }

    fn inbox() -> TriggerInbox {
        let records: Arc<dyn RecordStore> = Arc::new(InMemoryRecordStore::new());
        TriggerInbox::new(records)
    }

    fn comment(delivery: &str, thread: &str) -> TriggerEvent {
        TriggerEvent::new(
            SourceKind::GitHub,
            TriggerKind::PullRequestComment,
            delivery,
            serde_json::json!({"body": "please fix"}),
            now(),
        )
        .with_thread(thread)
        .with_repo("acme/widgets")
    }

    #[tokio::test]
    async fn redelivery_is_reported_as_duplicate_and_matches_nothing() {
        let inbox = inbox();
        inbox
            .upsert_subscription(Subscription::new(
                "case-1",
                SourceKind::GitHub,
                SubscriptionFilter::thread("acme/widgets#1"),
                now(),
            ))
            .await
            .unwrap();
        let first = inbox.ingest(&comment("d1", "acme/widgets#1"), now()).await.unwrap();
        assert!(first.accepted);
        assert!(!first.duplicate);
        assert_eq!(first.matched_cases, vec!["case-1".to_string()]);

        let second = inbox.ingest(&comment("d1", "acme/widgets#1"), now()).await.unwrap();
        assert!(!second.accepted);
        assert!(second.duplicate);
        assert!(second.matched_cases.is_empty());
    }

    #[tokio::test]
    async fn three_events_on_the_same_thread_all_reach_the_same_case() {
        let inbox = inbox();
        inbox
            .upsert_subscription(Subscription::new(
                "case-1",
                SourceKind::GitHub,
                SubscriptionFilter::thread("acme/widgets#1"),
                now(),
            ))
            .await
            .unwrap();
        let events = [
            comment("d1", "acme/widgets#1"),
            TriggerEvent::new(
                SourceKind::GitHub,
                TriggerKind::CheckRun,
                "d2",
                serde_json::json!({"conclusion": "failure"}),
                now(),
            )
            .with_thread("acme/widgets#1")
            .with_repo("acme/widgets"),
            TriggerEvent::new(
                SourceKind::GitHub,
                TriggerKind::PullRequestSynchronize,
                "d3",
                serde_json::json!({"sha": "abc"}),
                now(),
            )
            .with_thread("acme/widgets#1")
            .with_repo("acme/widgets"),
        ];
        for event in &events {
            let outcome = inbox.ingest(event, now()).await.unwrap();
            assert_eq!(outcome.matched_cases, vec!["case-1".to_string()]);
        }
        let subscriptions = inbox.list_subscriptions(Some("case-1"), None, 10).await.unwrap();
        assert_eq!(subscriptions[0].event_count, 3);
    }

    #[tokio::test]
    async fn unmatched_event_reports_a_reason() {
        let inbox = inbox();
        let outcome = inbox.ingest(&comment("d1", "acme/widgets#9"), now()).await.unwrap();
        assert!(outcome.accepted);
        assert_eq!(
            outcome.unmatched_reason.as_deref(),
            Some("thread_has_no_subscription")
        );
    }

    #[tokio::test]
    async fn inactive_subscriptions_do_not_match() {
        let inbox = inbox();
        let mut subscription = Subscription::new(
            "case-1",
            SourceKind::GitHub,
            SubscriptionFilter::thread("acme/widgets#1"),
            now(),
        );
        subscription.active = false;
        inbox.upsert_subscription(subscription).await.unwrap();
        let outcome = inbox.ingest(&comment("d1", "acme/widgets#1"), now()).await.unwrap();
        assert!(outcome.matched_cases.is_empty());
    }

    #[tokio::test]
    async fn subscription_update_preserves_delivery_statistics() {
        let inbox = inbox();
        let subscription = Subscription::new(
            "case-1",
            SourceKind::GitHub,
            SubscriptionFilter::thread("acme/widgets#1"),
            now(),
        );
        let stored = inbox.upsert_subscription(subscription.clone()).await.unwrap();
        inbox.ingest(&comment("d1", "acme/widgets#1"), now()).await.unwrap();
        let mut update = stored.clone();
        update.filter.labels = vec!["uc".to_string()];
        let updated = inbox.upsert_subscription(update).await.unwrap();
        assert_eq!(updated.event_count, 1);
        assert_eq!(updated.filter.labels, vec!["uc".to_string()]);
    }

    #[tokio::test]
    async fn events_are_retained_for_audit() {
        let inbox = inbox();
        let event = comment("d1", "acme/widgets#1");
        inbox.ingest(&event, now()).await.unwrap();
        let key = event.dedup_key();
        let stored = inbox.get_event(&key).await.unwrap().expect("event retained");
        assert_eq!(stored.id, TriggerId(event.id.0.clone()));
        assert_eq!(inbox.list_events(10).await.unwrap().len(), 1);
    }
}
