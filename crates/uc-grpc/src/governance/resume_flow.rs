//! Executing a resume plan: the gateway half of
//! [`uc_engine::governance::plan_resume`].
//!
//! The engine decides *whether* a case may start a new run; this module does the
//! two things only the gateway can do: create the logical task in the task store
//! and record the submission through the **same** submit-once path the
//! `SubmitTask` RPC uses. There is deliberately no second submission path here —
//! the outbox entry, the derived task id and the replay behaviour are identical,
//! so a resumed run is indistinguishable from a caller-submitted one downstream.
//!
//! ## Failure handling
//!
//! A resume that cannot complete is logged and skipped, never returned as an
//! error: the trigger event is already durably recorded, and the caller's retry
//! would be dropped by the inbox as a duplicate — which would lose the resume
//! permanently instead of retrying it.
//!
//! The one case that *does* mark the submission failed is an `open_run` refusal:
//! the task exists but cannot be governed under the case, so it must not be
//! dispatched as ungoverned work.

use std::sync::Arc;

use chrono::{DateTime, Utc};
use tokio::sync::Mutex;

use uc_engine::governance::{plan_resume, Governance, ResumeSkip};
use uc_types::governance::{SubmitOnceOutcome, TriggerEvent};

use crate::server::TaskStore;
use crate::ultimate_coders::TaskEvent;

/// Execute the resume for every case whose subscription opted into it and that
/// the inbox reported as `auto_resume`.
///
/// Returns the task ids that were created or replayed, in case order.
pub async fn execute_resumes(
    governance: &Governance,
    task_store: Option<&Arc<Mutex<TaskStore>>>,
    event_tx: Option<&tokio::sync::broadcast::Sender<TaskEvent>>,
    auto_resume_cases: &[String],
    event: &TriggerEvent,
    now: DateTime<Utc>,
) -> Vec<String> {
    if auto_resume_cases.is_empty() {
        return Vec::new();
    }
    let mut resumed = Vec::new();
    for case_id in auto_resume_cases {
        match resume_one(governance, task_store, event_tx, case_id, event, now).await {
            Ok(Some(task_id)) => resumed.push(task_id),
            Ok(None) => {}
            Err(detail) => {
                tracing::warn!(
                    case_id = %case_id,
                    event = %event.external_id,
                    detail = %detail,
                    "case resume failed; the trigger event itself is recorded"
                );
            }
        }
    }
    resumed
}

async fn resume_one(
    governance: &Governance,
    task_store: Option<&Arc<Mutex<TaskStore>>>,
    event_tx: Option<&tokio::sync::broadcast::Sender<TaskEvent>>,
    case_id: &str,
    event: &TriggerEvent,
    now: DateTime<Utc>,
) -> Result<Option<String>, String> {
    let plan = match plan_resume(governance, case_id, event, true, now).await {
        Ok(plan) => plan,
        Err(skip) => {
            tracing::info!(
                case_id = %case_id,
                event = %event.external_id,
                reason = skip.code(),
                detail = %skip.detail(),
                "auto-resume declined"
            );
            let _ = skip; // the detail moved into the log; nothing else to do
            return Ok(None);
        }
    };

    let Some(task_store) = task_store else {
        // A gateway without a task store cannot materialise the logical task, and a
        // submission recorded without one would be a task id no RPC could read.
        tracing::warn!(
            case_id = %case_id,
            "auto-resume skipped: this gateway has no task store attached"
        );
        return Ok(None);
    };

    let outcome = governance
        .submissions
        .submit_once(&plan.submission, &governance.outbox, now)
        .await
        .map_err(|error| format!("submit-once failed: {error}"))?;
    let dedup_replayed = outcome.is_replayed();
    if let SubmitOnceOutcome::Conflict { submission_id, .. } = &outcome {
        tracing::warn!(
            case_id = %case_id,
            submission_id = %submission_id,
            "auto-resume hit a conflicting submission; leaving it to a human"
        );
        return Ok(None);
    }
    let Some(record) = outcome.record().cloned() else {
        return Err("submit-once outcome carried no record".to_string());
    };

    // The logical task exists under the *derived* id, exactly like the RPC path,
    // so a replay after a crash between record and task converges here too.
    let new_events = {
        let mut store = task_store.lock().await;
        match store.get_task(&record.task_id).cloned() {
            Some(_existing) => Vec::new(),
            None => {
                let (_task, events) = store.submit_task_pending_with_id(
                    uc_types::TaskId(record.task_id.clone()),
                    plan.submission.description.clone(),
                    plan.submission.project_id.clone(),
                );
                events
            }
        }
    };
    if let Some(tx) = event_tx {
        for event in new_events {
            let _ = tx.send(event);
        }
    }

    // Link the run unless it is already linked (a replayed resume must not open a
    // second run — the case's one-open-run invariant would refuse it anyway).
    let already_linked = match governance.cases.get(case_id).await {
        Ok(Some((case, _))) => case.runs.iter().any(|run| run.task_id == record.task_id),
        Ok(None) => false,
        Err(error) => {
            tracing::warn!(case_id = %case_id, error = %error, "case lookup failed after resume");
            false
        }
    };
    if !already_linked {
        if let Err(error) = governance
            .cases
            .open_run(
                case_id,
                &record.task_id,
                Some(record.submission_id.to_string()),
                now,
            )
            .await
        {
            // The task exists but cannot be governed under this case: mark the
            // submission failed so a human sees it instead of silent ungoverned work.
            let _ = governance
                .submissions
                .mark_failed(&plan.submission.submission_id, error.to_string(), now)
                .await;
            return Err(format!("case run could not be opened: {error}"));
        }
    }

    if let Err(error) = governance
        .submissions
        .mark_applied(&plan.submission.submission_id, now)
        .await
    {
        tracing::warn!(error = %error, "mark_applied failed for a resumed submission");
    }

    tracing::info!(
        case_id = %case_id,
        task_id = %record.task_id,
        submission_id = %record.submission_id,
        dedup_replayed,
        "case resumed by its trigger event"
    );
    Ok(Some(record.task_id))
}

/// Small helper for tests and logs: the skip code of a declined plan.
pub fn skip_code(skip: &ResumeSkip) -> &'static str {
    skip.code()
}
