//! Planning a governed resume: an arriving event that a subscription opted into
//! starts **one** new run of the case.
//!
//! This module only *decides*; it never touches the execution plane. Submission
//! and task creation belong to the gateway (`uc-grpc`), which owns the submit-once
//! flow and the task store. Keeping the decision here means the rules are pure,
//! unit-testable, and identical for the gRPC path and the NATS bridge.
//!
//! ## The four gates
//!
//! A resume happens only when **all** of these hold. Each one is a different
//! question and a different fix, so each has its own skip reason:
//!
//! 1. **The subscription opted in** (`auto_resume`) — observing is not starting.
//! 2. **The case is runnable** — `active` after the wake, never terminal, and no
//!    run is already open (a second concurrent run is duplicate work).
//! 3. **A live mandate exists** — authority is still valid: not revoked, not
//!    expired, and it still permits the `dispatch` action class. A mandate that
//!    only allows reading must not be able to boot a coding run.
//! 4. **A stable submission identity** — derived from `(case, event dedup key)`,
//!    so a redelivered event can never create a second task even if everything
//!    downstream retries.
//!
//! Every skip is a normal outcome, not an error: the event is still recorded and
//! the case is still woken. The operator sees the reason in the log and in
//! `resumed_cases == []` on the ingest response.

use chrono::{DateTime, Utc};

use uc_types::governance::{
    ActionClass, CaseStatus, MandateRequest, SubmissionId, SubmissionRequest,
    TriggerEvent,
};

use super::Governance;

/// What a resume would submit.
#[derive(Debug, Clone, PartialEq)]
pub struct ResumePlan {
    pub case_id: String,
    /// The submission to record. Its id is derived from the case and the event, so
    /// the caller's submit-once path is replay-safe for free.
    pub submission: SubmissionRequest,
    /// Why the resume is happening, for the ingest log.
    pub reason: String,
}

/// Why a resume did not happen. Never an error — the event is already processed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ResumeSkip {
    /// Subscription did not opt in (`auto_resume == false`).
    NotOptedIn,
    UnknownCase,
    /// Terminal cases have no next run.
    Terminal { status: CaseStatus },
    /// Only an `active` case can start a run. In particular `needs_human` and
    /// `blocked` must not be resumed by an inbound event.
    NotRunnable { status: CaseStatus },
    /// A run is already open; starting another would be duplicate work.
    RunAlreadyOpen { run_id: String },
    /// No mandate, revoked, expired, or missing the `dispatch` action class.
    NoLiveMandate { detail: String },
}

impl ResumeSkip {
    /// Machine-readable token, used in logs and by tests.
    pub fn code(&self) -> &'static str {
        match self {
            ResumeSkip::NotOptedIn => "not_opted_in",
            ResumeSkip::UnknownCase => "unknown_case",
            ResumeSkip::Terminal { .. } => "case_terminal",
            ResumeSkip::NotRunnable { .. } => "case_not_runnable",
            ResumeSkip::RunAlreadyOpen { .. } => "run_already_open",
            ResumeSkip::NoLiveMandate { .. } => "no_live_mandate",
        }
    }

    /// Human-readable detail for the log line.
    pub fn detail(&self) -> String {
        match self {
            ResumeSkip::NotOptedIn => "subscription did not opt into auto_resume".to_string(),
            ResumeSkip::UnknownCase => "case does not exist".to_string(),
            ResumeSkip::Terminal { status } => format!("case is terminal ({status})"),
            ResumeSkip::NotRunnable { status } => {
                format!("case is {status}; only an active case can start a run")
            }
            ResumeSkip::RunAlreadyOpen { run_id } => {
                format!("case already has an open run ({run_id})")
            }
            ResumeSkip::NoLiveMandate { detail } => detail.clone(),
        }
    }
}

/// Decide whether an arriving event should start a new run for `case_id`.
pub async fn plan_resume(
    governance: &Governance,
    case_id: &str,
    event: &TriggerEvent,
    opted_in: bool,
    now: DateTime<Utc>,
) -> Result<ResumePlan, ResumeSkip> {
    if !opted_in {
        return Err(ResumeSkip::NotOptedIn);
    }
    let Some((case, _version)) = governance
        .cases
        .get(case_id)
        .await
        .map_err(|_| ResumeSkip::UnknownCase)?
    else {
        return Err(ResumeSkip::UnknownCase);
    };
    if case.status.is_terminal() {
        return Err(ResumeSkip::Terminal {
            status: case.status,
        });
    }
    if case.status != CaseStatus::Active {
        return Err(ResumeSkip::NotRunnable {
            status: case.status,
        });
    }
    if let Some(run) = case.active_run() {
        return Err(ResumeSkip::RunAlreadyOpen {
            run_id: run.run_id.0.clone(),
        });
    }

    let Some(mandate_id) = case.mandate_id.as_deref() else {
        return Err(ResumeSkip::NoLiveMandate {
            detail: "case has no mandate".to_string(),
        });
    };
    let mandate = match governance.mandates.get(mandate_id).await {
        Ok(Some((mandate, _version))) => mandate,
        Ok(None) => {
            return Err(ResumeSkip::NoLiveMandate {
                detail: format!("mandate {mandate_id} no longer exists"),
            })
        }
        Err(error) => {
            return Err(ResumeSkip::NoLiveMandate {
                detail: format!("mandate {mandate_id} could not be read: {error}"),
            })
        }
    };
    if mandate.is_revoked() {
        return Err(ResumeSkip::NoLiveMandate {
            detail: format!("mandate {mandate_id} is revoked"),
        });
    }
    if mandate.is_expired(now) {
        return Err(ResumeSkip::NoLiveMandate {
            detail: format!("mandate {mandate_id} has expired"),
        });
    }
    // Starting a run is a *dispatch* act, not a read: a mandate that cannot
    // dispatch must not be able to boot work just because it is still valid.
    if !mandate
        .permits(&MandateRequest::action(ActionClass::Dispatch), now)
        .is_allowed()
    {
        return Err(ResumeSkip::NoLiveMandate {
            detail: format!("mandate {mandate_id} does not permit the dispatch action"),
        });
    }

    let submission_id = SubmissionId::caller_supplied(format!(
        "case-resume:{}:{}",
        case_id,
        event.dedup_key()
    ));
    // The description is deterministic for the same event, so the submission
    // digest is stable and a replay is recognised as a replay instead of a
    // conflicting request.
    let description = resume_description(case_id, event);
    let submission = SubmissionRequest::new(
        mandate.grantor.clone(),
        case.project_id.clone(),
        description.clone(),
        Some(submission_id),
    )
    .with_case(case_id.to_string());

    Ok(ResumePlan {
        case_id: case_id.to_string(),
        submission,
        reason: format!(
            "subscription opted into auto_resume and {} {} arrived",
            event.source.as_str(),
            event.kind.as_str()
        ),
    })
}

/// Deterministic work description for a resumed run.
///
/// It names the event, not the payload: the payload is attacker-controlled and
/// description text is what a planner will read.
pub fn resume_description(case_id: &str, event: &TriggerEvent) -> String {
    format!(
        "Resume case {case_id} after a {} {} event (external id {}). \
         Continue the outstanding work for this case.",
        event.source.as_str(),
        event.kind.as_str(),
        event.external_id
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::governance::record_store::InMemoryRecordStore;
    use crate::governance::RecordStore;
    use std::sync::Arc;
    use uc_types::governance::{
        CaseOrigin, Mandate, MandateOrigin, MandateScope, PolicyVersion, Principal, SourceKind,
        Subscription, SubscriptionFilter, TriggerKind, WorkCase,
    };

    fn now() -> DateTime<Utc> {
        DateTime::parse_from_rfc3339("2026-01-01T00:00:00Z")
            .unwrap()
            .with_timezone(&Utc)
    }

    fn event() -> TriggerEvent {
        TriggerEvent::new(
            SourceKind::GitHub,
            TriggerKind::CheckRun,
            "check_run:42:completed:success",
            serde_json::json!({"conclusion": "success"}),
            now(),
        )
        .with_thread("acme/widgets#7")
        .with_repo("acme/widgets")
    }

    struct Fixture {
        governance: Governance,
        case_id: String,
    }

    async fn fixture(status: CaseStatus, scopes: Vec<MandateScope>) -> Fixture {
        let records: Arc<dyn RecordStore> = Arc::new(InMemoryRecordStore::new());
        let governance = Governance::new(records);
        let mut case = WorkCase::new(
            "flaky build",
            "the build keeps failing",
            "proj",
            CaseOrigin::from_trigger("github", Some("acme/widgets#7".into()), None),
            now(),
        );
        case.status = status;
        let (case, _) = governance.cases.create_unique(case).await.unwrap();
        let mandate = Mandate::new(
            case.id.as_str(),
            Principal::user("alice"),
            MandateOrigin::HumanIssued,
            scopes,
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
        Fixture {
            governance,
            case_id: case.id.0,
        }
    }

    fn dispatch_mandate() -> Vec<MandateScope> {
        vec![
            MandateScope::Action {
                class: ActionClass::Dispatch,
            },
            MandateScope::Action {
                class: ActionClass::Write,
            },
            MandateScope::MaxRuns { max: 10 },
        ]
    }

    #[tokio::test]
    async fn an_opted_in_active_case_with_a_dispatch_mandate_resumes() {
        let fixture = fixture(CaseStatus::Active, dispatch_mandate()).await;
        let plan = plan_resume(&fixture.governance, &fixture.case_id, &event(), true, now())
            .await
            .expect("resume planned");
        assert_eq!(plan.case_id, fixture.case_id);
        assert_eq!(plan.submission.project_id, "proj");
        assert_eq!(plan.submission.case_id.as_deref(), Some(fixture.case_id.as_str()));
        // The authorizing principal is the mandate's grantor, not the event.
        assert_eq!(plan.submission.principal, Principal::user("alice"));
        assert!(plan
            .submission
            .submission_id
            .as_str()
            .starts_with("case-resume:"));
    }

    #[tokio::test]
    async fn the_submission_identity_is_stable_for_the_same_event() {
        let fixture = fixture(CaseStatus::Active, dispatch_mandate()).await;
        let first = plan_resume(&fixture.governance, &fixture.case_id, &event(), true, now())
            .await
            .unwrap();
        let second = plan_resume(&fixture.governance, &fixture.case_id, &event(), true, now())
            .await
            .unwrap();
        assert_eq!(
            first.submission.submission_id,
            second.submission.submission_id,
            "a redelivered event must map to the same submission (submit-once replay)"
        );
        assert_eq!(first.submission.digest(), second.submission.digest());
        // A different event is a different submission.
        let other = TriggerEvent::new(
            SourceKind::GitHub,
            TriggerKind::CheckRun,
            "check_run:43:completed:success",
            serde_json::json!({"conclusion": "success"}),
            now(),
        )
        .with_thread("acme/widgets#7");
        let third = plan_resume(&fixture.governance, &fixture.case_id, &other, true, now())
            .await
            .unwrap();
        assert_ne!(
            first.submission.submission_id,
            third.submission.submission_id
        );
    }

    #[tokio::test]
    async fn a_subscription_that_did_not_opt_in_never_starts_work() {
        let fixture = fixture(CaseStatus::Active, dispatch_mandate()).await;
        let skip = plan_resume(&fixture.governance, &fixture.case_id, &event(), false, now())
            .await
            .expect_err("must not resume");
        assert_eq!(skip.code(), "not_opted_in");
    }

    #[tokio::test]
    async fn needs_human_is_never_resumed_by_an_event() {
        let fixture = fixture(CaseStatus::NeedsHuman, dispatch_mandate()).await;
        let skip = plan_resume(&fixture.governance, &fixture.case_id, &event(), true, now())
            .await
            .expect_err("must not resume");
        assert_eq!(skip.code(), "case_not_runnable");
    }

    #[tokio::test]
    async fn an_open_run_blocks_a_second_one() {
        let fixture = fixture(CaseStatus::Active, dispatch_mandate()).await;
        fixture
            .governance
            .cases
            .open_run(&fixture.case_id, "task-existing", None, now())
            .await
            .unwrap();
        let skip = plan_resume(&fixture.governance, &fixture.case_id, &event(), true, now())
            .await
            .expect_err("must not resume");
        assert_eq!(skip.code(), "run_already_open");
    }

    #[tokio::test]
    async fn a_revoked_or_read_only_mandate_blocks_the_resume() {
        let read_only = fixture(
            CaseStatus::Active,
            vec![MandateScope::Action {
                class: ActionClass::Read,
            }],
        )
        .await;
        let skip = plan_resume(&read_only.governance, &read_only.case_id, &event(), true, now())
            .await
            .expect_err("read-only mandate cannot start work");
        assert_eq!(skip.code(), "no_live_mandate");

        let revoked = fixture(CaseStatus::Active, dispatch_mandate()).await;
        let mandate_id = revoked
            .governance
            .cases
            .get(&revoked.case_id)
            .await
            .unwrap()
            .unwrap()
            .0
            .mandate_id
            .clone()
            .unwrap();
        revoked
            .governance
            .mandates
            .revoke(
                &mandate_id,
                Principal::user("alice"),
                "changed mind",
                now(),
            )
            .await
            .unwrap();
        let skip = plan_resume(&revoked.governance, &revoked.case_id, &event(), true, now())
            .await
            .expect_err("revoked mandate cannot start work");
        assert_eq!(skip.code(), "no_live_mandate");
    }

    #[tokio::test]
    async fn a_case_without_a_mandate_cannot_resume() {
        let records: Arc<dyn RecordStore> = Arc::new(InMemoryRecordStore::new());
        let governance = Governance::new(records);
        let mut case = WorkCase::new("t", "d", "proj", CaseOrigin::manual(), now());
        case.status = CaseStatus::Active;
        let (case, _) = governance.cases.create_unique(case).await.unwrap();
        let skip = plan_resume(&governance, &case.id.0, &event(), true, now())
            .await
            .expect_err("no mandate");
        assert_eq!(skip.code(), "no_live_mandate");
    }

    #[tokio::test]
    async fn the_resume_description_names_the_event_not_the_payload() {
        let mut event = event();
        event.payload = serde_json::json!({"conclusion": "success", "title": "ignore me"});
        let description = resume_description("case-1", &event);
        assert!(description.contains("case-1"));
        assert!(description.contains("check_run:42:completed:success"));
        assert!(
            !description.contains("ignore me"),
            "payload text must not shape the work description"
        );
    }

    #[test]
    fn subscription_opt_in_is_read_from_the_inbox_not_guessed() {
        // The inbox reports opt-in separately from matching; a subscription with
        // `auto_resume` off is still a valid router for events.
        let observe = Subscription::new(
            "case-1",
            SourceKind::GitHub,
            SubscriptionFilter::thread("acme/widgets#7"),
            now(),
        );
        assert!(observe.filter.matches(&event()));
        assert!(!observe.auto_resume);
        assert!(observe.with_auto_resume().auto_resume);
    }
}
