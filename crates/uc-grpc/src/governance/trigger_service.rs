//! `TriggerService` — inbound trigger ingestion and case subscriptions.
//!
//! Triggers carry **no authority**: an event can ask for work, it can never name
//! a mandate scope, raise a budget or pick a tool. That separation is what keeps
//! authority from being widened by an attacker-controlled webhook payload, and it
//! is enforced here by treating the payload as opaque data ([`IngestTriggerRequest::payload_json`]
//! is stored verbatim and never consulted for authorization).
//!
//! Ingestion is idempotent by dedup key, so GitHub's at-least-once delivery (and
//! a polling reconciler racing the webhook) collapse into one record. Case
//! identity for a thread is deterministic (`CaseId::from_parts`), so a re-delivered
//! event converges on the same case instead of opening a second one.

use std::sync::Arc;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use tonic::{Request, Response, Status};
use uc_engine::governance::{Governance, TriggerInboxError};
use uc_types::governance::{
    CaseId, CaseOrigin, Permission, SourceKind, Subscription, SubscriptionFilter, SubscriptionId,
    TriggerEvent, TriggerKind, WorkCase,
};

use super::{
    clamp_limit, encode_optional_timestamp, encode_timestamp, optional_text, required_text,
    OpError, OpResult,
};
use crate::governance::policy_interceptor::{require, resolve_declared_principal};
use crate::ultimate_coders::trigger_service_server::TriggerService;

/// Wire form of [`Subscription`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SubscriptionView {
    pub id: String,
    pub case_id: String,
    pub source: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub repo: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub thread_key: Option<String>,
    #[serde(default)]
    pub kinds: Vec<String>,
    #[serde(default)]
    pub labels: Vec<String>,
    pub active: bool,
    pub created_at: DateTime<Utc>,
    pub event_count: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_event_at: Option<DateTime<Utc>>,
}

impl SubscriptionView {
    fn from_subscription(subscription: &Subscription) -> Self {
        let mut kinds: Vec<String> = subscription
            .filter
            .kinds
            .iter()
            .map(|kind| kind.as_str().to_string())
            .collect();
        kinds.sort();
        let mut labels = subscription.filter.labels.clone();
        labels.sort();
        Self {
            id: subscription.id.0.clone(),
            case_id: subscription.case_id.clone(),
            source: subscription.source.as_str().to_string(),
            repo: subscription.filter.repo.clone(),
            thread_key: subscription.filter.thread_key.clone(),
            kinds,
            labels,
            active: subscription.active,
            created_at: subscription.created_at,
            event_count: subscription.event_count,
            last_event_at: subscription.last_event_at,
        }
    }

    fn to_proto(&self) -> crate::ultimate_coders::SubscriptionProto {
        crate::ultimate_coders::SubscriptionProto {
            id: self.id.clone(),
            case_id: self.case_id.clone(),
            source: self.source.clone(),
            repo: self.repo.clone(),
            thread_key: self.thread_key.clone(),
            kinds: self.kinds.clone(),
            labels: self.labels.clone(),
            active: self.active,
            created_at: encode_timestamp(self.created_at),
            event_count: self.event_count,
            last_event_at: encode_optional_timestamp(self.last_event_at),
        }
    }
}

/// Result of [`ingest`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct IngestView {
    pub accepted: bool,
    pub duplicate: bool,
    pub event_id: String,
    pub dedup_key: String,
    #[serde(default)]
    pub matched_cases: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unmatched_reason: Option<String>,
    /// Case opened because nothing matched and the caller asked for one.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub opened_case_id: Option<String>,
}

/// Ingest one external event and fan it out to matching subscriptions.
pub async fn ingest(
    governance: &Governance,
    request: crate::ultimate_coders::IngestTriggerRequest,
) -> OpResult<IngestView> {
    let now = super::now();
    let principal = resolve_declared_principal(
        optional_text(Some(request.principal_kind.as_str())).as_deref(),
        optional_text(Some(request.principal_id.as_str())).as_deref(),
    )?;
    // Ingesting an event *is* the authenticated service's act, not the event's.
    require(
        governance,
        &principal,
        Permission::TriggerIngest,
        None,
        now,
    )?;

    let source_token = required_text(&request.source, "source")?;
    let source = SourceKind::from_str_lossy(&source_token).ok_or_else(|| {
        OpError::invalid(
            "unknown_source",
            format!("unknown trigger source '{source_token}'"),
        )
    })?;
    let kind_token = required_text(&request.kind, "kind")?;
    let kind = TriggerKind::from_str_lossy(&kind_token).ok_or_else(|| {
        OpError::invalid(
            "unknown_kind",
            format!("unknown trigger kind '{kind_token}'"),
        )
    })?;

    // Identity is the *source-native* event identity, never GitHub's delivery id.
    //
    // This is deliberate and load-bearing: a webhook delivery and its polling
    // reconciler twin describe the same event but carry different delivery ids.
    // Keying on the delivery id would make them distinct events, so a PR's
    // comment/CI/status changes could each open a second case — exactly what
    // PR04 forbids. The delivery id is accepted for audit/logging only and is
    // never part of the dedup key (see the proto comment on `delivery_id` and
    // `uc_types::governance::trigger::TriggerEvent::dedup_key`).
    let delivery_id = optional_text(request.delivery_id.as_deref());
    let external_id = optional_text(Some(request.external_id.as_str())).ok_or_else(|| {
        OpError::invalid(
            "invalid_external_id",
            "external_id is required: an event without a source identity cannot be deduplicated",
        )
    })?;
    if let Some(delivery) = &delivery_id {
        tracing::debug!(
            delivery_id = %delivery,
            external_id = %external_id,
            "trigger ingest: delivery id recorded for audit (not part of dedup identity)"
        );
    }

    // The payload is stored verbatim and never inspected for authorization; it
    // only has to be valid JSON so the Python evaluator can read it.
    let payload: serde_json::Value = serde_json::from_str(&request.payload_json).map_err(|error| {
        OpError::invalid(
            "invalid_payload_json",
            format!("payload_json must be valid JSON: {error}"),
        )
    })?;

    let mut event = TriggerEvent::new(source, kind, external_id, payload, now);
    event.thread_key = optional_text(request.thread_key.as_deref());
    event.repo = optional_text(request.repo.as_deref());
    event.actor = optional_text(request.actor.as_deref());

    let outcome = governance
        .triggers
        .ingest(&event, now)
        .await
        .map_err(trigger_error)?;

    // Waking the cases this event belongs to: a case parked in `waiting_event`
    // holds no worker slot, so the only thing that keeps it honest is an arriving
    // event ending the wait. Matching is the authorization — the subscription's
    // filter already said which kinds this case cares about.
    //
    // Done before the duplicate/opened-case bookkeeping so a successful ingest
    // always reports the wake; duplicates carry an empty match list and wake
    // nothing. Failures here are logged, never returned: the event is already
    // durable, and turning a wake failure into an ingestion error would make the
    // caller retry a delivery the inbox would then drop as a duplicate.
    let woken_cases =
        uc_engine::governance::wake_matched_cases(governance, &outcome.matched_cases, &outcome.event_id, now)
            .await;

    let mut opened_case_id = None;
    if !outcome.duplicate
        && outcome.matched_cases.is_empty()
        && request.open_case_if_unmatched
    {
        opened_case_id = Some(
            open_case_for_event(
                governance,
                &principal,
                &event,
                request.open_case_project_id.as_deref(),
                now,
            )
            .await?,
        );
    }

    if outcome.duplicate {
        tracing::info!(
            source = source.as_str(),
            kind = event.kind.as_str(),
            dedup_key = %outcome.dedup_key,
            "duplicate trigger delivery ignored"
        );
    } else {
        tracing::info!(
            event_id = %outcome.event_id,
            source = source.as_str(),
            kind = event.kind.as_str(),
            matched = outcome.matched_cases.len(),
            woken = woken_cases.len(),
            opened_case = opened_case_id.as_deref().unwrap_or(""),
            "trigger ingested"
        );
    }

    Ok(IngestView {
        accepted: outcome.accepted,
        duplicate: outcome.duplicate,
        event_id: outcome.event_id,
        dedup_key: outcome.dedup_key,
        matched_cases: outcome.matched_cases,
        unmatched_reason: outcome.unmatched_reason,
        opened_case_id,
    })
}

/// Open a case for an event that matched nothing, and subscribe it to the thread.
///
/// This is the explicit "label a PR" entry point: an operator asks for the work
/// to start even though no standing subscription matched. It needs
/// `case_create`, because opening a case is a governance act — event ingestion
/// alone does not carry that authority.
async fn open_case_for_event(
    governance: &Governance,
    principal: &uc_types::governance::Principal,
    event: &TriggerEvent,
    requested_project: Option<&str>,
    now: DateTime<Utc>,
) -> OpResult<String> {
    let identity = event
        .thread_key
        .clone()
        .unwrap_or_else(|| event.external_id.clone());
    let project_id = optional_text(requested_project)
        .or_else(|| event.repo.clone())
        .ok_or_else(|| {
            OpError::invalid(
                "invalid_open_case_project_id",
                "open_case_project_id (or repo) is required to open a case for an unmatched event",
            )
        })?;
    require(
        governance,
        principal,
        Permission::CaseCreate,
        Some(project_id.as_str()),
        now,
    )?;

    // A human-readable provisional title: the trigger payload's title when it has
    // one, otherwise the event's own identity. The operator's later attention
    // item carries the real question, so this only has to be recognizable.
    let title = event
        .payload
        .get("title")
        .and_then(|value| value.as_str())
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
        .unwrap_or_else(|| {
            format!(
                "{} on {}",
                event.kind.as_str(),
                event
                    .thread_key
                    .as_deref()
                    .unwrap_or(event.external_id.as_str())
            )
        });
    let description = format!(
        "Opened from a {} {} event (external id {}).",
        event.source.as_str(),
        event.kind.as_str(),
        event.external_id
    );

    let case = WorkCase::new(
        title,
        description,
        project_id,
        CaseOrigin::from_trigger(
            event.source.as_str(),
            event.thread_key.clone(),
            Some(event.id.0.clone()),
        ),
        now,
    )
    .with_id(CaseId::from_parts(
        event.source.as_str(),
        identity.as_str(),
    ));
    let (case, replayed) = governance
        .cases
        .create_unique(case)
        .await
        .map_err(super::case_service::case_store_error)?;
    let case_id = case.id.0.clone();

    // Standing interest: subsequent events on the same thread (comments, CI
    // results) attach to this case instead of opening another one.
    if !replayed {
        if let Some(thread_key) = event.thread_key.clone() {
            let mut subscription = Subscription::new(
                case_id.clone(),
                event.source,
                SubscriptionFilter::thread(thread_key),
                now,
            );
            subscription.active = true;
            match governance.triggers.upsert_subscription(subscription).await {
                Ok(subscription) => {
                    let subscription_id = subscription.id.0.clone();
                    // The closure runs once per CAS attempt, so it must not consume
                    // the id: clone inside.
                    let updated = governance
                        .cases
                        .update(&case_id, |case| {
                            case.attach_subscription(subscription_id.clone());
                            Ok(())
                        })
                        .await;
                    if let Err(error) = updated {
                        tracing::warn!(
                            error = %error,
                            case_id = %case_id,
                            "opened case is not linked to its own thread subscription"
                        );
                    }
                }
                Err(error) => {
                    tracing::warn!(
                        error = %error,
                        case_id = %case_id,
                        "thread subscription for the opened case was not created"
                    );
                }
            }
        }
    }

    tracing::info!(
        case_id = %case_id,
        source = event.source.as_str(),
        kind = event.kind.as_str(),
        replayed,
        "case opened from an unmatched trigger"
    );
    Ok(case_id)
}

/// List subscriptions, optionally scoped to a case or a source.
///
/// Read-only projection: governance metadata, not authority (same rule as
/// `CaseService`'s reads).
pub async fn list_subscriptions(
    governance: &Governance,
    request: crate::ultimate_coders::ListSubscriptionsRequest,
) -> OpResult<Vec<SubscriptionView>> {
    let limit = clamp_limit(request.limit);
    let source = match optional_text(request.source.as_deref()) {
        Some(token) => Some(SourceKind::from_str_lossy(&token).ok_or_else(|| {
            OpError::invalid(
                "unknown_source",
                format!("unknown trigger source '{token}'"),
            )
        })?),
        None => None,
    };
    let case_id = optional_text(request.case_id.as_deref());
    let mut subscriptions = governance
        .triggers
        .list_subscriptions(case_id.as_deref(), source, limit)
        .await
        .map_err(trigger_error)?
        .iter()
        .map(SubscriptionView::from_subscription)
        .collect::<Vec<_>>();
    // Newest first: the subscription an operator just created is the one they are
    // looking for.
    subscriptions.sort_by_key(|subscription| std::cmp::Reverse(subscription.created_at));
    Ok(subscriptions)
}

/// Create or update a subscription.
///
/// Idempotent by `subscription_id`: a re-sent upsert updates the same document
/// (and preserves its delivery counters) instead of creating a second one.
///
/// The proto message carries no caller identity, so this is not permission-gated;
/// it is behind the transport auth interceptor. The `active` field follows the
/// proto default (`false`): a caller creating a subscription must set it.
pub async fn upsert_subscription(
    governance: &Governance,
    request: crate::ultimate_coders::UpsertSubscriptionRequest,
) -> OpResult<SubscriptionView> {
    let now = super::now();
    let case_id = required_text(&request.case_id, "case_id")?;
    let source_token = required_text(&request.source, "source")?;
    let source = SourceKind::from_str_lossy(&source_token).ok_or_else(|| {
        OpError::invalid(
            "unknown_source",
            format!("unknown trigger source '{source_token}'"),
        )
    })?;
    let kinds = request
        .kinds
        .iter()
        .map(|token| {
            TriggerKind::from_str_lossy(token).ok_or_else(|| {
                OpError::invalid("unknown_kind", format!("unknown trigger kind '{token}'"))
            })
        })
        .collect::<OpResult<Vec<_>>>()?;
    let filter = SubscriptionFilter {
        repo: optional_text(request.repo.as_deref()),
        kinds,
        thread_key: optional_text(request.thread_key.as_deref()),
        labels: request
            .labels
            .into_iter()
            .map(|label| label.trim().to_string())
            .filter(|label| !label.is_empty())
            .collect(),
    };
    let mut subscription = Subscription::new(case_id, source, filter, now);
    if let Some(id) = optional_text(request.subscription_id.as_deref()) {
        subscription.id = SubscriptionId(id);
    }
    subscription.active = request.active;

    let subscription = governance
        .triggers
        .upsert_subscription(subscription)
        .await
        .map_err(trigger_error)?;
    tracing::info!(
        subscription_id = %subscription.id.0,
        case_id = %subscription.case_id,
        source = subscription.source.as_str(),
        active = subscription.active,
        "subscription upserted"
    );
    Ok(SubscriptionView::from_subscription(&subscription))
}

/// Delete a subscription. `false` means it did not exist (already deleted).
pub async fn delete_subscription(
    governance: &Governance,
    request: crate::ultimate_coders::DeleteSubscriptionRequest,
) -> OpResult<bool> {
    let subscription_id = required_text(&request.subscription_id, "subscription_id")?;
    let deleted = governance
        .triggers
        .delete_subscription(&subscription_id)
        .await
        .map_err(trigger_error)?;
    tracing::info!(subscription_id, deleted, "subscription delete requested");
    Ok(deleted)
}

/// Map a trigger-inbox failure onto the operation vocabulary.
fn trigger_error(error: TriggerInboxError) -> OpError {
    match &error {
        TriggerInboxError::NotFound { subscription_id } => OpError::not_found(
            "subscription_not_found",
            format!("subscription '{subscription_id}' does not exist"),
        ),
        TriggerInboxError::Contended => OpError::conflict(
            "trigger_inbox_contended",
            "the trigger inbox is contended; retry the operation",
        ),
        TriggerInboxError::Store(_) => {
            OpError::internal("trigger_inbox_failed", error.to_string())
        }
    }
}

/// gRPC adapter over the operations above.
#[derive(Clone)]
pub struct TriggerServiceImpl {
    governance: Arc<Governance>,
}

impl TriggerServiceImpl {
    pub fn new(governance: Arc<Governance>) -> Self {
        Self { governance }
    }

    pub fn governance(&self) -> &Arc<Governance> {
        &self.governance
    }
}

#[tonic::async_trait]
impl TriggerService for TriggerServiceImpl {
    async fn ingest_trigger(
        &self,
        request: Request<crate::ultimate_coders::IngestTriggerRequest>,
    ) -> Result<Response<crate::ultimate_coders::IngestTriggerResponse>, Status> {
        let outcome = ingest(&self.governance, request.into_inner()).await?;
        Ok(Response::new(
            crate::ultimate_coders::IngestTriggerResponse {
                success: true,
                accepted: outcome.accepted,
                duplicate: outcome.duplicate,
                event_id: outcome.event_id,
                dedup_key: outcome.dedup_key,
                matched_cases: outcome.matched_cases,
                unmatched_reason: outcome.unmatched_reason,
                opened_case_id: outcome.opened_case_id,
                error: None,
            },
        ))
    }

    async fn list_subscriptions(
        &self,
        request: Request<crate::ultimate_coders::ListSubscriptionsRequest>,
    ) -> Result<Response<crate::ultimate_coders::ListSubscriptionsResponse>, Status> {
        let subscriptions = list_subscriptions(&self.governance, request.into_inner()).await?;
        Ok(Response::new(
            crate::ultimate_coders::ListSubscriptionsResponse {
                subscriptions: subscriptions
                    .iter()
                    .map(SubscriptionView::to_proto)
                    .collect(),
            },
        ))
    }

    async fn upsert_subscription(
        &self,
        request: Request<crate::ultimate_coders::UpsertSubscriptionRequest>,
    ) -> Result<Response<crate::ultimate_coders::UpsertSubscriptionResponse>, Status> {
        let subscription = upsert_subscription(&self.governance, request.into_inner()).await?;
        Ok(Response::new(
            crate::ultimate_coders::UpsertSubscriptionResponse {
                success: true,
                subscription: Some(subscription.to_proto()),
                error: None,
            },
        ))
    }

    async fn delete_subscription(
        &self,
        request: Request<crate::ultimate_coders::DeleteSubscriptionRequest>,
    ) -> Result<Response<crate::ultimate_coders::DeleteSubscriptionResponse>, Status> {
        let deleted = delete_subscription(&self.governance, request.into_inner()).await?;
        Ok(Response::new(
            crate::ultimate_coders::DeleteSubscriptionResponse {
                success: deleted,
                error: if deleted {
                    None
                } else {
                    Some("subscription did not exist".to_string())
                },
            },
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use uc_types::governance::{Grant, PolicySnapshot, PolicyVersion};

    async fn ingesting_plane() -> Governance {
        let governance = Governance::in_memory();
        let mut snapshot = PolicySnapshot::empty(PolicyVersion::initial());
        for permission in [Permission::TriggerIngest, Permission::CaseCreate] {
            snapshot = snapshot
                .with_grant(Grant::new(
                    uc_types::governance::Principal::service("dashboard"),
                    vec![permission],
                ))
                .expect("service grants are structurally valid");
        }
        governance.policy.install(snapshot).await.unwrap();
        governance
    }

    fn event(delivery: &str) -> crate::ultimate_coders::IngestTriggerRequest {
        crate::ultimate_coders::IngestTriggerRequest {
            source: "github".to_string(),
            kind: "pull_request_comment".to_string(),
            external_id: "comment-1".to_string(),
            thread_key: Some("acme/widgets#7".to_string()),
            repo: Some("acme/widgets".to_string()),
            actor: Some("alice".to_string()),
            payload_json: r#"{"comment":"please fix"}"#.to_string(),
            delivery_id: Some(delivery.to_string()),
            principal_kind: "service".to_string(),
            principal_id: "dashboard".to_string(),
            open_case_if_unmatched: false,
            open_case_project_id: None,
        }
    }

    #[tokio::test]
    async fn a_redelivered_webhook_is_a_duplicate() {
        let governance = ingesting_plane().await;
        let first = ingest(&governance, event("delivery-1")).await.unwrap();
        assert!(first.accepted);
        assert!(!first.duplicate);
        assert_eq!(first.unmatched_reason.as_deref(), Some("thread_has_no_subscription"));

        let second = ingest(&governance, event("delivery-1")).await.unwrap();
        assert!(second.duplicate);
        assert!(!second.accepted);
        assert!(second.matched_cases.is_empty());
    }

    #[tokio::test]
    async fn unmatched_event_opens_a_case_and_a_thread_subscription_when_asked() {
        let governance = ingesting_plane().await;
        let mut request = event("delivery-2");
        request.open_case_if_unmatched = true;
        request.open_case_project_id = Some("proj".to_string());
        let outcome = ingest(&governance, request).await.unwrap();
        let case_id = outcome.opened_case_id.expect("case opened");

        let subscriptions = list_subscriptions(
            &governance,
            crate::ultimate_coders::ListSubscriptionsRequest {
                case_id: Some(case_id.clone()),
                source: None,
                limit: 10,
            },
        )
        .await
        .unwrap();
        assert_eq!(subscriptions.len(), 1);
        assert_eq!(subscriptions[0].thread_key.as_deref(), Some("acme/widgets#7"));

        // A second event on the same thread now matches the subscription.
        let mut follow_up = event("delivery-3");
        follow_up.kind = "issue_comment".to_string();
        let matched = ingest(&governance, follow_up).await.unwrap();
        assert_eq!(matched.matched_cases, vec![case_id]);
    }

    #[tokio::test]
    async fn opening_a_case_needs_a_project_scope() {
        let governance = ingesting_plane().await;
        let mut request = event("delivery-4");
        request.open_case_if_unmatched = true;
        request.repo = None;
        let error = ingest(&governance, request).await.unwrap_err();
        assert_eq!(error.code, "invalid_open_case_project_id");
    }

    #[tokio::test]
    async fn ingestion_requires_the_trigger_ingest_permission() {
        let governance = ingesting_plane().await;
        let mut request = event("delivery-5");
        request.principal_id = "mallory".to_string();
        let error = ingest(&governance, request).await.unwrap_err();
        assert_eq!(error.code, "missing_permission");
    }

    #[tokio::test]
    async fn a_delivery_id_alone_is_not_a_source_identity() {
        // A webhook delivery id must NOT become the event identity: the polling
        // reconciler emits the same event with a different (or absent) delivery
        // id, and keying on it would open a second case for the same PR event.
        // Identity is `external_id`; without it the request is malformed.
        let governance = ingesting_plane().await;
        let mut request = event("delivery-6");
        request.external_id = String::new();
        let error = ingest(&governance, request).await.unwrap_err();
        assert_eq!(error.code, "invalid_external_id");
    }

    #[tokio::test]
    async fn delivery_id_does_not_change_the_dedup_identity() {
        // Same source event, different delivery ids (webhook vs polled twin):
        // the second ingest must collapse as a duplicate.
        let governance = ingesting_plane().await;
        let mut first = event("delivery-6");
        first.delivery_id = Some("delivery-aaa".to_string());
        let mut second = event("delivery-6");
        second.delivery_id = Some("delivery-bbb".to_string());
        let accepted = ingest(&governance, first).await.unwrap();
        assert!(accepted.accepted);
        let duplicate = ingest(&governance, second).await.unwrap();
        assert!(duplicate.duplicate);
        assert_eq!(accepted.dedup_key, duplicate.dedup_key);
    }

    #[tokio::test]
    async fn malformed_payload_json_is_an_invalid_argument() {
        let governance = ingesting_plane().await;
        let mut request = event("delivery-7");
        request.payload_json = "{".to_string();
        let error = ingest(&governance, request).await.unwrap_err();
        assert_eq!(error.code, "invalid_payload_json");
    }

    #[tokio::test]
    async fn upsert_is_idempotent_and_keeps_the_delivery_counters() {
        let governance = ingesting_plane().await;
        let request = crate::ultimate_coders::UpsertSubscriptionRequest {
            subscription_id: Some("sub-fixed".to_string()),
            case_id: "case-1".to_string(),
            source: "github".to_string(),
            repo: Some("acme/widgets".to_string()),
            thread_key: Some("acme/widgets#7".to_string()),
            kinds: vec!["pull_request_comment".to_string()],
            labels: vec!["uc".to_string(), "  ".to_string()],
            active: true,
        };
        let first = upsert_subscription(&governance, request.clone())
            .await
            .unwrap();
        assert_eq!(first.id, "sub-fixed");
        assert_eq!(first.labels, vec!["uc".to_string()]);
        let second = upsert_subscription(&governance, request).await.unwrap();
        assert_eq!(first.id, second.id);
        assert_eq!(
            list_subscriptions(
                &governance,
                crate::ultimate_coders::ListSubscriptionsRequest {
                    case_id: None,
                    source: None,
                    limit: 10,
                }
            )
            .await
            .unwrap()
            .len(),
            1
        );
    }

    #[tokio::test]
    async fn delete_reports_whether_the_subscription_existed() {
        let governance = ingesting_plane().await;
        let subscription = upsert_subscription(
            &governance,
            crate::ultimate_coders::UpsertSubscriptionRequest {
                subscription_id: None,
                case_id: "case-1".to_string(),
                source: "ci".to_string(),
                repo: None,
                thread_key: None,
                kinds: Vec::new(),
                labels: Vec::new(),
                active: true,
            },
        )
        .await
        .unwrap();
        assert!(delete_subscription(
            &governance,
            crate::ultimate_coders::DeleteSubscriptionRequest {
                subscription_id: subscription.id.clone()
            }
        )
        .await
        .unwrap());
        assert!(!delete_subscription(
            &governance,
            crate::ultimate_coders::DeleteSubscriptionRequest {
                subscription_id: subscription.id
            }
        )
        .await
        .unwrap());
    }
}
