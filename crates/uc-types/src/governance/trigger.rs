//! Triggers and subscriptions — how external events reach a `WorkCase`.
//!
//! The inbox is the **only** entry point for external events. Each event is
//! reduced to a deterministic dedup key before it touches anything durable, so
//! GitHub's at-least-once webhook delivery (and a polling reconciler racing a
//! webhook) collapse into one record. Matching is by *thread*: a comment on PR
//! `#123` updates the case that already tracks `#123` instead of opening a new
//! one.
//!
//! Triggers carry **no authority**. An event can ask for work; it cannot name a
//! mandate scope, raise a budget, or pick a tool. That separation is what keeps
//! `不越权` true even when the webhook payload is attacker-controlled.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

use crate::merge::sha256_hex;

/// Record-store namespace for deduplicated trigger events.
pub const TRIGGER_NAMESPACE: &str = "trigger_event";
/// Record-store namespace for subscriptions.
pub const SUBSCRIPTION_NAMESPACE: &str = "subscription";

/// Where an event came from.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SourceKind {
    GitHub,
    Ci,
    Lark,
    Dashboard,
    Api,
    Cron,
    Manual,
}

impl SourceKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            SourceKind::GitHub => "github",
            SourceKind::Ci => "ci",
            SourceKind::Lark => "lark",
            SourceKind::Dashboard => "dashboard",
            SourceKind::Api => "api",
            SourceKind::Cron => "cron",
            SourceKind::Manual => "manual",
        }
    }

    pub fn from_str_lossy(value: &str) -> Option<Self> {
        Some(match value.trim().to_ascii_lowercase().as_str() {
            "github" => SourceKind::GitHub,
            "ci" => SourceKind::Ci,
            "lark" | "feishu" => SourceKind::Lark,
            "dashboard" => SourceKind::Dashboard,
            "api" => SourceKind::Api,
            "cron" => SourceKind::Cron,
            "manual" => SourceKind::Manual,
            _ => return None,
        })
    }
}

/// What happened.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TriggerKind {
    IssueOpened,
    IssueComment,
    PullRequestOpened,
    PullRequestSynchronize,
    PullRequestReview,
    PullRequestComment,
    Push,
    CheckRun,
    WorkflowRun,
    Message,
    CronTick,
    ApiCall,
    Manual,
}

impl TriggerKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            TriggerKind::IssueOpened => "issue_opened",
            TriggerKind::IssueComment => "issue_comment",
            TriggerKind::PullRequestOpened => "pull_request_opened",
            TriggerKind::PullRequestSynchronize => "pull_request_synchronize",
            TriggerKind::PullRequestReview => "pull_request_review",
            TriggerKind::PullRequestComment => "pull_request_comment",
            TriggerKind::Push => "push",
            TriggerKind::CheckRun => "check_run",
            TriggerKind::WorkflowRun => "workflow_run",
            TriggerKind::Message => "message",
            TriggerKind::CronTick => "cron_tick",
            TriggerKind::ApiCall => "api_call",
            TriggerKind::Manual => "manual",
        }
    }

    pub fn from_str_lossy(value: &str) -> Option<Self> {
        Some(match value.trim().to_ascii_lowercase().as_str() {
            "issue_opened" => TriggerKind::IssueOpened,
            "issue_comment" => TriggerKind::IssueComment,
            "pull_request_opened" => TriggerKind::PullRequestOpened,
            "pull_request_synchronize" => TriggerKind::PullRequestSynchronize,
            "pull_request_review" => TriggerKind::PullRequestReview,
            "pull_request_comment" => TriggerKind::PullRequestComment,
            "push" => TriggerKind::Push,
            "check_run" => TriggerKind::CheckRun,
            "workflow_run" => TriggerKind::WorkflowRun,
            "message" => TriggerKind::Message,
            "cron_tick" => TriggerKind::CronTick,
            "api_call" => TriggerKind::ApiCall,
            "manual" => TriggerKind::Manual,
            _ => return None,
        })
    }

    /// Kinds that describe progress *on an existing thread* rather than new
    /// work. These attach to a case's subscription instead of opening one.
    pub fn is_thread_update(&self) -> bool {
        matches!(
            self,
            TriggerKind::IssueComment
                | TriggerKind::PullRequestSynchronize
                | TriggerKind::PullRequestReview
                | TriggerKind::PullRequestComment
                | TriggerKind::CheckRun
                | TriggerKind::WorkflowRun
                | TriggerKind::Push
        )
    }
}

/// Identifier of a trigger event record.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct TriggerId(pub String);

impl TriggerId {
    pub fn new() -> Self {
        Self(uuid::Uuid::new_v4().to_string())
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl Default for TriggerId {
    fn default() -> Self {
        Self::new()
    }
}

/// An inbound event, already authenticated by the API layer.
///
/// `payload` is stored verbatim for audit and for the Python evaluator; nothing
/// in it is trusted for authorization decisions.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TriggerEvent {
    pub id: TriggerId,
    pub source: SourceKind,
    pub kind: TriggerKind,
    /// Source-native event identity (delivery id, check-run id, comment id).
    pub external_id: String,
    /// Identity of the object the event is about (`owner/repo#123`,
    /// `owner/repo@sha`). Used for thread matching.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub thread_key: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub repo: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub actor: Option<String>,
    /// Digest of the payload used in the dedup key when no delivery id exists.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub payload_digest: Option<String>,
    #[serde(default)]
    pub payload: serde_json::Value,
    pub received_at: DateTime<Utc>,
}

impl TriggerEvent {
    pub fn new(
        source: SourceKind,
        kind: TriggerKind,
        external_id: impl Into<String>,
        payload: serde_json::Value,
        now: DateTime<Utc>,
    ) -> Self {
        let digest = sha256_hex(payload.to_string().as_bytes());
        Self {
            id: TriggerId::new(),
            source,
            kind,
            external_id: external_id.into(),
            thread_key: None,
            repo: None,
            actor: None,
            payload_digest: Some(digest),
            payload,
            received_at: now,
        }
    }

    pub fn with_thread(mut self, thread_key: impl Into<String>) -> Self {
        self.thread_key = Some(thread_key.into());
        self
    }

    pub fn with_repo(mut self, repo: impl Into<String>) -> Self {
        self.repo = Some(repo.into());
        self
    }

    pub fn with_actor(mut self, actor: impl Into<String>) -> Self {
        self.actor = Some(actor.into());
        self
    }

    /// Canonical thread key for a GitHub-like object: `owner/repo#number`.
    pub fn thread_key_for(repo: &str, number: u64) -> String {
        format!("{repo}#{number}")
    }

    /// Dedup key.
    ///
    /// Webhook re-delivery uses the same `external_id`, so this collapses it.
    /// When the source cannot supply a stable id, the payload digest is mixed
    /// in — two genuinely identical payloads are then indistinguishable, which
    /// is the correct (conservative) reading for an event that carries no id.
    ///
    /// A `check_run`/`workflow_run` completion and its re-poll must converge
    /// too, so the key excludes the receive time entirely.
    pub fn dedup_key(&self) -> String {
        let material = format!(
            "{}|{}|{}|{}",
            self.source.as_str(),
            self.kind.as_str(),
            self.external_id,
            self.payload_digest.as_deref().unwrap_or("")
        );
        sha256_hex(material.as_bytes())
    }
}

/// Filter describing which events a subscription accepts.
#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]
pub struct SubscriptionFilter {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub repo: Option<String>,
    /// Empty = every kind from the source.
    #[serde(default)]
    pub kinds: Vec<TriggerKind>,
    /// Match only this thread (`owner/repo#123`). Set for thread subscriptions.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub thread_key: Option<String>,
    /// Optional label gate (issue/PR labels).
    #[serde(default)]
    pub labels: Vec<String>,
}

impl SubscriptionFilter {
    pub fn thread(thread_key: impl Into<String>) -> Self {
        Self {
            thread_key: Some(thread_key.into()),
            ..Default::default()
        }
    }

    pub fn repo(repo: impl Into<String>) -> Self {
        Self {
            repo: Some(repo.into()),
            ..Default::default()
        }
    }

    pub fn with_kinds(mut self, kinds: Vec<TriggerKind>) -> Self {
        self.kinds = kinds;
        self
    }

    pub fn matches(&self, event: &TriggerEvent) -> bool {
        if let Some(repo) = &self.repo {
            if event.repo.as_deref() != Some(repo.as_str()) {
                return false;
            }
        }
        if !self.kinds.is_empty() && !self.kinds.contains(&event.kind) {
            return false;
        }
        if let Some(thread) = &self.thread_key {
            if event.thread_key.as_deref() != Some(thread.as_str()) {
                return false;
            }
        }
        if !self.labels.is_empty() {
            let labels = event
                .payload
                .get("labels")
                .and_then(|value| value.as_array())
                .map(|values| {
                    values
                        .iter()
                        .filter_map(|value| value.as_str())
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            if !self
                .labels
                .iter()
                .any(|wanted| labels.contains(&wanted.as_str()))
            {
                return false;
            }
        }
        true
    }
}

/// Identifier of a subscription.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct SubscriptionId(pub String);

impl SubscriptionId {
    pub fn new() -> Self {
        Self(uuid::Uuid::new_v4().to_string())
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl Default for SubscriptionId {
    fn default() -> Self {
        Self::new()
    }
}

/// A case's standing interest in a stream of events.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Subscription {
    pub id: SubscriptionId,
    pub case_id: String,
    pub source: SourceKind,
    pub filter: SubscriptionFilter,
    pub created_at: DateTime<Utc>,
    #[serde(default)]
    pub active: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_event_at: Option<DateTime<Utc>>,
    /// Number of events delivered to the case; lets a requester tell "no events
    /// yet" from "subscription broken".
    #[serde(default)]
    pub event_count: u64,
    /// Opt-in: when a matching event arrives, start a new governed run for the
    /// case (the `case_wake` path wakes it, the resume path submits work).
    ///
    /// Default **false**: a subscription that only observes must never be able to
    /// start work. Turning it on is a per-case decision made by an operator, and
    /// the resume path still requires a live mandate — the subscription is the
    /// "do I want work to start", the mandate is the "am I allowed".
    #[serde(default)]
    pub auto_resume: bool,
}

impl Subscription {
    pub fn new(
        case_id: impl Into<String>,
        source: SourceKind,
        filter: SubscriptionFilter,
        now: DateTime<Utc>,
    ) -> Self {
        Self {
            id: SubscriptionId::new(),
            case_id: case_id.into(),
            source,
            filter,
            created_at: now,
            active: true,
            last_event_at: None,
            event_count: 0,
            auto_resume: false,
        }
    }

    /// Opt this subscription into starting new runs when its events arrive.
    ///
    /// Kept as an explicit builder rather than a constructor flag so enabling it
    /// is always visible at the call site: it is the one subscription property
    /// that makes the system start work by itself.
    pub fn with_auto_resume(mut self) -> Self {
        self.auto_resume = true;
        self
    }

    pub fn record_event(&mut self, now: DateTime<Utc>) {
        self.event_count = self.event_count.saturating_add(1);
        self.last_event_at = Some(now);
    }
}

/// Result of ingesting a trigger event.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct IngestOutcome {
    pub event_id: String,
    pub dedup_key: String,
    /// `false` when the event was already recorded — a duplicate delivery.
    pub accepted: bool,
    pub duplicate: bool,
    /// Cases whose subscription matched, in subscription creation order.
    #[serde(default)]
    pub matched_cases: Vec<String>,
    /// Why the event matched nothing (``"no_subscription"`` or
    /// ``"thread_has_no_case"``), for the dashboard's trigger inspector.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unmatched_reason: Option<String>,
    /// Matched cases whose subscription opted into `auto_resume`. The resume plan
    /// is still gated by the case's mandate and run state; this is only "the
    /// subscription asked for work to start".
    #[serde(default)]
    pub auto_resume_cases: Vec<String>,
}

impl IngestOutcome {
    pub fn duplicate(event_id: impl Into<String>, dedup_key: impl Into<String>) -> Self {
        Self {
            event_id: event_id.into(),
            dedup_key: dedup_key.into(),
            accepted: false,
            duplicate: true,
            matched_cases: Vec::new(),
            unmatched_reason: None,
            auto_resume_cases: Vec::new(),
        }
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
    fn redelivery_of_the_same_webhook_dedupes() {
        let payload = serde_json::json!({"action": "created", "number": 7});
        let a = TriggerEvent::new(
            SourceKind::GitHub,
            TriggerKind::PullRequestComment,
            "delivery-1",
            payload.clone(),
            now(),
        );
        let b = TriggerEvent::new(
            SourceKind::GitHub,
            TriggerKind::PullRequestComment,
            "delivery-1",
            payload,
            now() + chrono::Duration::seconds(30),
        );
        assert_eq!(a.dedup_key(), b.dedup_key());
    }

    #[test]
    fn different_events_on_the_same_thread_have_different_keys() {
        let a = TriggerEvent::new(
            SourceKind::GitHub,
            TriggerKind::PullRequestComment,
            "delivery-1",
            serde_json::json!({"n": 1}),
            now(),
        );
        let b = TriggerEvent::new(
            SourceKind::GitHub,
            TriggerKind::CheckRun,
            "delivery-2",
            serde_json::json!({"n": 2}),
            now(),
        );
        // Same thread, different events — both must be processed.
        assert_ne!(a.dedup_key(), b.dedup_key());
    }

    #[test]
    fn thread_subscription_matches_only_its_thread() {
        let filter = SubscriptionFilter::thread("acme/widgets#12");
        let on_thread = TriggerEvent::new(
            SourceKind::GitHub,
            TriggerKind::PullRequestComment,
            "d1",
            serde_json::json!({}),
            now(),
        )
        .with_thread("acme/widgets#12");
        let other_thread = TriggerEvent::new(
            SourceKind::GitHub,
            TriggerKind::PullRequestComment,
            "d2",
            serde_json::json!({}),
            now(),
        )
        .with_thread("acme/widgets#13");
        assert!(filter.matches(&on_thread));
        assert!(!filter.matches(&other_thread));
    }

    #[test]
    fn kind_and_repo_filters_compose() {
        let filter = SubscriptionFilter::repo("acme/widgets")
            .with_kinds(vec![TriggerKind::PullRequestSynchronize]);
        let matching = TriggerEvent::new(
            SourceKind::GitHub,
            TriggerKind::PullRequestSynchronize,
            "d1",
            serde_json::json!({}),
            now(),
        )
        .with_repo("acme/widgets");
        let wrong_kind = TriggerEvent::new(
            SourceKind::GitHub,
            TriggerKind::IssueComment,
            "d2",
            serde_json::json!({}),
            now(),
        )
        .with_repo("acme/widgets");
        let wrong_repo = TriggerEvent::new(
            SourceKind::GitHub,
            TriggerKind::PullRequestSynchronize,
            "d3",
            serde_json::json!({}),
            now(),
        )
        .with_repo("acme/other");
        assert!(filter.matches(&matching));
        assert!(!filter.matches(&wrong_kind));
        assert!(!filter.matches(&wrong_repo));
    }

    #[test]
    fn label_filter_reads_payload_labels() {
        let filter = SubscriptionFilter {
            labels: vec!["uc".to_string()],
            ..Default::default()
        };
        let labelled = TriggerEvent::new(
            SourceKind::GitHub,
            TriggerKind::IssueOpened,
            "d1",
            serde_json::json!({"labels": ["uc", "bug"]}),
            now(),
        );
        let unlabelled = TriggerEvent::new(
            SourceKind::GitHub,
            TriggerKind::IssueOpened,
            "d2",
            serde_json::json!({"labels": ["docs"]}),
            now(),
        );
        assert!(filter.matches(&labelled));
        assert!(!filter.matches(&unlabelled));
    }

    #[test]
    fn thread_key_format_is_stable() {
        assert_eq!(
            TriggerEvent::thread_key_for("acme/widgets", 7),
            "acme/widgets#7"
        );
    }

    #[test]
    fn thread_updates_are_distinguished_from_new_work() {
        assert!(TriggerKind::CheckRun.is_thread_update());
        assert!(TriggerKind::PullRequestReview.is_thread_update());
        assert!(!TriggerKind::IssueOpened.is_thread_update());
        assert!(!TriggerKind::Manual.is_thread_update());
    }

    #[test]
    fn auto_resume_is_opt_in_and_off_by_default() {
        let observe = Subscription::new(
            "case-1",
            SourceKind::GitHub,
            SubscriptionFilter::thread("acme/widgets#1"),
            now(),
        );
        assert!(
            !observe.auto_resume,
            "a subscription that only observes must never start work"
        );
        let resume = observe.with_auto_resume();
        assert!(resume.auto_resume);
        // Serialized form keeps the default explicit for older readers.
        let json = serde_json::to_value(&resume).expect("serializes");
        assert_eq!(json["auto_resume"], serde_json::json!(true));
        let observe_json = serde_json::to_value(Subscription::new(
            "case-2",
            SourceKind::GitHub,
            SubscriptionFilter::thread("acme/widgets#2"),
            now(),
        ))
        .expect("serializes");
        assert_eq!(observe_json["auto_resume"], serde_json::json!(false));
    }

    #[test]
    fn subscription_counts_events() {
        let mut subscription = Subscription::new(
            "case-1",
            SourceKind::GitHub,
            SubscriptionFilter::thread("acme/widgets#1"),
            now(),
        );
        assert_eq!(subscription.event_count, 0);
        subscription.record_event(now());
        assert_eq!(subscription.event_count, 1);
        assert_eq!(subscription.last_event_at, Some(now()));
    }
}
