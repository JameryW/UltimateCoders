//! Waking a case when the event it was waiting for arrives.
//!
//! A case parked in `WaitingEvent` holds no worker slot and interrupts nobody —
//! that is the whole point of the state. It only stays honest if *something* ends
//! the wait. Without this module the wait would never end: the attention item
//! would stay open forever (nobody can resolve an event by hand), `waiting_event`
//! would accumulate in the capacity projection, and a case could sit quiescent
//! long after its CI run finished.
//!
//! ## What wakes a case
//!
//! A trigger event that **matched one of the case's subscriptions**. Matching is
//! the authorization here: the subscription's filter already says which kinds of
//! event this case cares about, so an event that passes it is by definition the
//! awaited signal. Nothing about the event's payload is consulted — a webhook
//! body cannot name what it unblocks.
//!
//! ## What is deliberately *not* woken
//!
//! * `NeedsHuman` — a human decision is still outstanding; an incoming comment
//!   must not silently answer it.
//! * `Blocked` — blocked is not "waiting for an event"; unblocking has a different
//!   cause (dependency, budget, mandate) and a different actor.
//! * `Verifying` — the run finished; verification is in flight, and an event must
//!   not restart the case into `Active` behind the verifier's back.
//! * terminal cases — nothing to wake.
//!
//! Duplicate deliveries never reach here: the inbox reports them as `duplicate`
//! with an empty match list, so the wake pass sees nothing to do.

use chrono::{DateTime, Utc};

use uc_types::governance::{AttentionKind, AttentionResolution, CaseStatus};

use super::attention::{AttentionBoard, AttentionBoardError};
use super::case_store::CaseStore;
use super::Governance;

/// One case that a trigger event moved out of `WaitingEvent`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CaseWake {
    pub case_id: String,
    /// The attention item that was resolved, when one was open.
    pub resolved_attention: Option<String>,
}

/// Wake every matched case that is waiting for an event.
///
/// Idempotent and fail-soft: a case that cannot be woken (store error, illegal
/// transition because something else moved it first) is logged and skipped rather
/// than failing the ingestion — the event itself is already durably recorded, and
/// a failed wake must not turn a successfully ingested event into an error the
/// caller retries (the retry would be a duplicate and be dropped, losing the wake).
pub async fn wake_matched_cases(
    governance: &Governance,
    matched_cases: &[String],
    trigger_id: &str,
    now: DateTime<Utc>,
) -> Vec<CaseWake> {
    let mut woken = Vec::new();
    for case_id in matched_cases {
        match wake_case(
            governance.cases.as_ref(),
            governance.attention.as_ref(),
            case_id,
            trigger_id,
            now,
        )
        .await
        {
            Ok(Some(wake)) => woken.push(wake),
            Ok(None) => {}
            Err(detail) => {
                tracing::warn!(
                    case_id = %case_id,
                    trigger_id = %trigger_id,
                    detail = %detail,
                    "trigger event could not wake its case; the event itself is recorded"
                );
            }
        }
    }
    woken
}

/// Wake one case. `Ok(None)` means "nothing to do, and that is correct".
async fn wake_case(
    cases: &CaseStore,
    attention: &AttentionBoard,
    case_id: &str,
    trigger_id: &str,
    now: DateTime<Utc>,
) -> Result<Option<CaseWake>, String> {
    let Some((case, _version)) = cases
        .get(case_id)
        .await
        .map_err(|error| format!("case read failed: {error}"))?
    else {
        return Ok(None);
    };
    if case.status.is_terminal() || case.status != CaseStatus::WaitingEvent {
        return Ok(None);
    }

    // Resolve the attention item first: if the transition below fails, an open
    // `waiting_event` item for an active case would be a lie the worklist keeps
    // showing. Resolving first fails the other way — an event is reported as
    // received while the case has not moved — which the next ingest repairs.
    let resolved_attention = match open_waiting_item(attention, case_id).await {
        Ok(Some(item)) => {
            match attention
                .resolve(
                    &item.id,
                    AttentionResolution::EventReceived {
                        trigger_id: trigger_id.to_string(),
                    },
                    now,
                )
                .await
            {
                Ok(_) => Some(item.id),
                Err(error) => {
                    return Err(format!("attention resolve failed: {error}"));
                }
            }
        }
        Ok(None) => None,
        Err(error) => return Err(format!("attention read failed: {error}")),
    };

    cases
        .transition(case_id, CaseStatus::Active, now)
        .await
        .map_err(|error| format!("case transition failed: {error}"))?;

    tracing::info!(
        case_id = %case_id,
        trigger_id = %trigger_id,
        resolved_attention = resolved_attention.as_deref().unwrap_or(""),
        "waiting case woken by its trigger event"
    );
    Ok(Some(CaseWake {
        case_id: case_id.to_string(),
        resolved_attention,
    }))
}

async fn open_waiting_item(
    attention: &AttentionBoard,
    case_id: &str,
) -> Result<Option<uc_types::governance::AttentionItem>, AttentionBoardError> {
    let Some(item) = attention.open_for_case(case_id).await? else {
        return Ok(None);
    };
    if item.kind == AttentionKind::WaitingEvent {
        Ok(Some(item))
    } else {
        Ok(None)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::governance::record_store::InMemoryRecordStore;
    use std::sync::Arc;
    use uc_types::governance::{
        AttentionItem, CaseOrigin, Mandate, MandateOrigin, MandateScope, PolicyVersion, Principal,
        SourceKind, Subscription, SubscriptionFilter, WorkCase,
    };

    fn now() -> DateTime<Utc> {
        DateTime::parse_from_rfc3339("2026-01-01T00:00:00Z")
            .unwrap()
            .with_timezone(&Utc)
    }

    async fn governance_with_case(status: CaseStatus) -> (Governance, String) {
        let records: Arc<dyn crate::governance::RecordStore> = Arc::new(InMemoryRecordStore::new());
        let governance = Governance::new(records);
        let mut case = WorkCase::new(
            "fix the flaky build",
            "the build keeps failing",
            "proj",
            CaseOrigin::from_trigger("github", Some("acme/widgets#7".into()), None),
            now(),
        );
        case.status = status;
        let (case, _) = governance.cases.create_unique(case).await.unwrap();
        // A live mandate so the case *could* execute once woken.
        let mandate = Mandate::new(
            case.id.as_str(),
            Principal::user("alice"),
            MandateOrigin::HumanIssued,
            vec![MandateScope::Action {
                class: uc_types::governance::ActionClass::Write,
            }],
            PolicyVersion(1),
            now(),
        );
        governance.mandates.put_new(&mandate).await.unwrap();
        governance
            .cases
            .update(case.id.as_str(), |stored| {
                stored.mandate_id = Some(mandate.id.0.clone());
                Ok(())
            })
            .await
            .unwrap();
        (governance, case.id.0)
    }

    async fn subscribe(governance: &Governance, case_id: &str) {
        governance
            .triggers
            .upsert_subscription(Subscription::new(
                case_id,
                SourceKind::GitHub,
                SubscriptionFilter::thread("acme/widgets#7"),
                now(),
            ))
            .await
            .unwrap();
    }

    async fn raise_waiting(governance: &Governance, case_id: &str) {
        governance
            .attention
            .raise(
                AttentionItem::new(
                    "att-waiting",
                    case_id,
                    AttentionKind::WaitingEvent,
                    "waiting on the CI run",
                    now(),
                ),
                now(),
            )
            .await
            .unwrap();
    }

    fn event(delivery: &str) -> uc_types::governance::TriggerEvent {
        uc_types::governance::TriggerEvent::new(
            SourceKind::GitHub,
            uc_types::governance::TriggerKind::CheckRun,
            delivery,
            serde_json::json!({"conclusion": "success"}),
            now(),
        )
        .with_thread("acme/widgets#7")
        .with_repo("acme/widgets")
    }

    #[tokio::test]
    async fn a_matched_event_wakes_the_case_and_resolves_its_attention() {
        let (governance, case_id) = governance_with_case(CaseStatus::WaitingEvent).await;
        subscribe(&governance, &case_id).await;
        raise_waiting(&governance, &case_id).await;

        let outcome = governance
            .triggers
            .ingest(&event("check-1"), now())
            .await
            .unwrap();
        assert_eq!(outcome.matched_cases, vec![case_id.clone()]);

        let woken = wake_matched_cases(&governance, &outcome.matched_cases, "evt-1", now()).await;
        assert_eq!(woken.len(), 1);
        assert_eq!(woken[0].case_id, case_id);
        assert_eq!(woken[0].resolved_attention.as_deref(), Some("att-waiting"));

        let (case, _) = governance.cases.get(&case_id).await.unwrap().unwrap();
        assert_eq!(case.status, CaseStatus::Active);
        let item = governance
            .attention
            .get("att-waiting")
            .await
            .unwrap()
            .unwrap();
        assert!(!item.is_open());
        assert!(matches!(
            item.resolution,
            Some(AttentionResolution::EventReceived { .. })
        ));
    }

    #[tokio::test]
    async fn a_needs_human_case_is_not_woken_by_an_incoming_event() {
        let (governance, case_id) = governance_with_case(CaseStatus::NeedsHuman).await;
        subscribe(&governance, &case_id).await;
        governance
            .attention
            .raise(
                AttentionItem::new(
                    "att-human",
                    &case_id,
                    AttentionKind::NeedsHuman,
                    "which branch?",
                    now(),
                ),
                now(),
            )
            .await
            .unwrap();

        let outcome = governance
            .triggers
            .ingest(&event("check-2"), now())
            .await
            .unwrap();
        let woken = wake_matched_cases(&governance, &outcome.matched_cases, "evt-2", now()).await;
        assert!(
            woken.is_empty(),
            "a pending human decision must not be answered by a webhook"
        );

        let (case, _) = governance.cases.get(&case_id).await.unwrap().unwrap();
        assert_eq!(case.status, CaseStatus::NeedsHuman);
        let item = governance
            .attention
            .get("att-human")
            .await
            .unwrap()
            .unwrap();
        assert!(item.is_open());
    }

    #[tokio::test]
    async fn terminal_cases_are_left_alone() {
        let (governance, case_id) = governance_with_case(CaseStatus::Completed).await;
        subscribe(&governance, &case_id).await;
        let outcome = governance
            .triggers
            .ingest(&event("check-3"), now())
            .await
            .unwrap();
        let woken = wake_matched_cases(&governance, &outcome.matched_cases, "evt-3", now()).await;
        assert!(woken.is_empty());
        let (case, _) = governance.cases.get(&case_id).await.unwrap().unwrap();
        assert_eq!(case.status, CaseStatus::Completed);
    }

    #[tokio::test]
    async fn a_duplicate_delivery_wakes_nothing() {
        let (governance, case_id) = governance_with_case(CaseStatus::WaitingEvent).await;
        subscribe(&governance, &case_id).await;
        raise_waiting(&governance, &case_id).await;

        let first = governance
            .triggers
            .ingest(&event("check-4"), now())
            .await
            .unwrap();
        assert!(!first.duplicate);
        let woken = wake_matched_cases(&governance, &first.matched_cases, "evt-4a", now()).await;
        assert_eq!(woken.len(), 1);

        // Put the case back to waiting (the run reported it is waiting again) and
        // re-deliver the SAME event: the inbox drops it, so nothing wakes.
        governance
            .cases
            .transition(&case_id, CaseStatus::WaitingEvent, now())
            .await
            .unwrap();
        raise_waiting(&governance, &case_id).await;
        let second = governance
            .triggers
            .ingest(&event("check-4"), now())
            .await
            .unwrap();
        assert!(second.duplicate);
        let woken = wake_matched_cases(&governance, &second.matched_cases, "evt-4b", now()).await;
        assert!(
            woken.is_empty(),
            "a redelivery must not wake the case twice"
        );
        let (case, _) = governance.cases.get(&case_id).await.unwrap().unwrap();
        assert_eq!(case.status, CaseStatus::WaitingEvent);
    }

    #[tokio::test]
    async fn a_waiting_case_without_an_attention_item_still_wakes() {
        // The attention board is a projection: losing its item (restart, manual
        // clear) must not strand the case in `WaitingEvent`.
        let (governance, case_id) = governance_with_case(CaseStatus::WaitingEvent).await;
        subscribe(&governance, &case_id).await;
        let outcome = governance
            .triggers
            .ingest(&event("check-5"), now())
            .await
            .unwrap();
        let woken = wake_matched_cases(&governance, &outcome.matched_cases, "evt-5", now()).await;
        assert_eq!(woken.len(), 1);
        assert!(woken[0].resolved_attention.is_none());
        let (case, _) = governance.cases.get(&case_id).await.unwrap().unwrap();
        assert_eq!(case.status, CaseStatus::Active);
    }

    #[tokio::test]
    async fn an_unknown_case_id_is_skipped_not_an_error() {
        let (governance, _case_id) = governance_with_case(CaseStatus::WaitingEvent).await;
        let woken = wake_matched_cases(
            &governance,
            &["case-does-not-exist".to_string()],
            "evt-6",
            now(),
        )
        .await;
        assert!(woken.is_empty());
    }
}
