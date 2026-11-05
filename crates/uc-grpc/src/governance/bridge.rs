//! Transport for the governance plane: the NATS JSON request/reply bridge, the
//! outbox dispatcher, and the submission operations that have no gRPC method.
//!
//! ## The JSON op protocol
//!
//! ```text
//! request   uc.governance.request   {"op": "case.create", "payload": {…}}
//! reply     <reply subject>         {"ok": true,  "result": {…}}
//!                                   {"ok": false, "error": {"code", "message"}}
//! ```
//!
//! `op` names and `payload` keys are the proto message names and field names of
//! the equivalent gRPC call, and `result` carries the proto response fields (the
//! `ok` flag *is* the response's `success`, and the `error` object is its `error`).
//! The one deliberate difference is how time travels: the bridge replies with
//! RFC 3339 strings (chrono's serde form) where proto uses `int64` Unix seconds,
//! because every value crossing this bridge is a serde-serialized domain type.
//! The Python dashboard accepts both (`attention/model.py::_parse_timestamp`).
//!
//! ## Why the bridge shares the service functions
//!
//! A dashboard that could open a case through NATS under one set of rules and
//! through gRPC under another would make the governance invariants unverifiable.
//! Every operation therefore lives exactly once — in its service module — and both
//! transports call it. This module owns decoding, encoding, the transport-level
//! error envelope and the outbox loop; it owns no governance rule.
//!
//! ## Failure envelope
//!
//! * malformed envelope / unparsable payload → `invalid_request`
//! * unknown `op` → `unknown_op`
//! * everything else → the operation's own code (`missing_permission`,
//!   `case_not_found`, `illegal_case_transition`, …)
//!
//! None of these panic: one bad message must never take the bridge down.

#[cfg(feature = "messaging")]
use std::sync::Arc;
use std::time::Duration;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use uc_engine::governance::Governance;
use uc_types::governance::{
    Permission, SubmissionId, SubmissionRecord, SubmissionRequest, SubmitOnceOutcome,
};

use super::{
    approval_service, attention_service, case_service, optional_text, policy_interceptor,
    proactive_service, trigger_service, OpError, OpResult,
};

/// Subject the bridge subscribes to. Cluster-internal: the NATS connection is the
/// trust boundary, so this must not be exposed publicly.
pub const GOVERNANCE_SUBJECT: &str = "uc.governance.request";

/// Interval between outbox drain passes.
///
/// Two seconds: short enough that a submission is picked up while the caller is
/// still watching, long enough that an idle gateway does not hammer the store.
pub const OUTBOX_DRAIN_INTERVAL: Duration = Duration::from_secs(2);

/// Maximum number of outbox entries one drain pass will publish.
///
/// Bounded so a large backlog cannot block the submit path: the remainder stays
/// `Pending` and the next pass picks it up.
pub const OUTBOX_DRAIN_LIMIT: usize = 64;

/// Outcome of one outbox drain pass.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct OutboxDrain {
    /// Entries the transport accepted.
    pub delivered: usize,
    /// Entries whose publish failed; they are rescheduled with backoff.
    pub retried: usize,
}

/// One decoded bridge request.
///
/// Compiled without the `messaging` feature too (the unit tests decode it directly
/// to pin the envelope contract), where nothing reads its fields — hence the
/// `dead_code` allowance.
#[cfg(any(feature = "messaging", test))]
#[allow(dead_code)]
#[derive(Debug, Clone, Deserialize)]
struct BridgeRequest {
    op: String,
    #[serde(default)]
    payload: serde_json::Value,
}

/// The error half of a failed reply.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BridgeError {
    /// Machine-readable code (`missing_permission`, `case_not_found`, …).
    pub code: String,
    pub message: String,
}

/// One bridge reply: exactly one of `result` / `error` is present.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BridgeReply {
    pub ok: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub result: Option<serde_json::Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<BridgeError>,
}

impl BridgeReply {
    /// A successful reply carrying the operation's result object.
    pub fn ok(result: serde_json::Value) -> Self {
        Self {
            ok: true,
            result: Some(result),
            error: None,
        }
    }

    /// A failed reply carrying the machine-readable code.
    pub fn failed(error: &OpError) -> Self {
        Self {
            ok: false,
            result: None,
            error: Some(BridgeError {
                code: error.code.clone(),
                message: error.message.clone(),
            }),
        }
    }
}

/// Payload of `submission.submit`.
///
/// There is no proto message for this op (it is the bridge's version of
/// `TaskService.SubmitTask`), so the keys mirror `SubmitTaskRequest`, which is the
/// message a caller would otherwise send.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct SubmissionSubmitInput {
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub project_id: String,
    #[serde(default)]
    pub submission_id: Option<String>,
    #[serde(default)]
    pub principal_kind: Option<String>,
    #[serde(default)]
    pub principal_id: Option<String>,
    #[serde(default)]
    pub case_id: Option<String>,
}

/// Payload of `submission.get`.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct SubmissionGetInput {
    #[serde(default)]
    pub submission_id: String,
}

/// Payload of `submission.apply`.
///
/// The consumer's confirmation that the orchestration exists (PR01's `applied`
/// edge). `task_id` is optional but, when present, must agree with the record's
/// derived id — a consumer cannot stamp a submission with a task it did not get.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct SubmissionApplyInput {
    #[serde(default)]
    pub submission_id: String,
    #[serde(default)]
    pub task_id: Option<String>,
    #[serde(default)]
    pub principal_kind: Option<String>,
    #[serde(default)]
    pub principal_id: Option<String>,
}

/// Wire form of a [`SubmissionRecord`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SubmissionView {
    pub submission_id: String,
    pub task_id: String,
    pub state: String,
    pub principal_kind: String,
    pub principal_id: String,
    pub project_id: String,
    pub request_digest: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub case_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub outbox_key: Option<String>,
    pub publish_attempts: u32,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub published_at: Option<DateTime<Utc>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub applied_at: Option<DateTime<Utc>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_error: Option<String>,
    pub version: u64,
}

impl SubmissionView {
    fn from_record(record: &SubmissionRecord) -> Self {
        Self {
            submission_id: record.submission_id.0.clone(),
            task_id: record.task_id.clone(),
            state: record.state.as_str().to_string(),
            principal_kind: record.principal.kind.as_str().to_string(),
            principal_id: record.principal.id.clone(),
            project_id: record.project_id.clone(),
            request_digest: record.request_digest.clone(),
            case_id: record.case_id.clone(),
            outbox_key: record.outbox_key.clone(),
            publish_attempts: record.publish_attempts,
            created_at: record.created_at,
            updated_at: record.updated_at,
            published_at: record.published_at,
            applied_at: record.applied_at,
            last_error: record.last_error.clone(),
            version: record.version,
        }
    }
}

/// Result of [`submit_submission`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SubmitSubmissionOutcome {
    pub submission: SubmissionView,
    /// `true` = this submission id had already been recorded with the same
    /// content; nothing new was created.
    pub dedup_replayed: bool,
}

/// Record a submission exactly once and queue its publication.
///
/// This is the bridge's submit path: it records durability (submission + outbox
/// entry) and lets the caller/drain loop publish. It deliberately does **not**
/// create a task record — the Python orchestrator owns task creation and dedupes
/// on the derived `task_id`, so a redelivery cannot duplicate the work.
///
/// Like `TaskService.SubmitTask` it is not policy-gated: a caller that holds the
/// transport credential may submit work (the case's *mandate*, not the submission,
/// is what authorizes execution). What it cannot do is choose the submission
/// identity of someone else's earlier request — reusing an id with a different
/// body is a conflict.
pub async fn submit_submission(
    governance: &Governance,
    input: SubmissionSubmitInput,
) -> OpResult<SubmitSubmissionOutcome> {
    let now = super::now();
    let principal = policy_interceptor::resolve_declared_principal(
        input.principal_kind.as_deref(),
        input.principal_id.as_deref(),
    )?;
    if input.description.trim().is_empty() {
        return Err(OpError::invalid(
            "invalid_argument",
            "Task description cannot be empty",
        ));
    }
    if input.project_id.trim().is_empty() {
        return Err(OpError::invalid(
            "invalid_argument",
            "project_id cannot be empty — every task must declare a project scope",
        ));
    }
    let submission_id = match optional_text(input.submission_id.as_deref()) {
        Some(key) => SubmissionId::caller_supplied(key),
        None => SubmissionId::derive(&principal, &input.project_id, &input.description),
    };
    let mut submission = SubmissionRequest::new(
        principal,
        input.project_id.trim().to_string(),
        input.description.clone(),
        Some(submission_id.clone()),
    );
    if let Some(case_id) = optional_text(input.case_id.as_deref()) {
        // Refuse an unknown case before anything durable is written: a submission
        // that can never become a governed run must not poison its key.
        let known = governance
            .cases
            .get(&case_id)
            .await
            .map_err(case_service::case_store_error)?
            .is_some();
        if !known {
            return Err(OpError::invalid(
                "unknown_case",
                format!("unknown case_id '{case_id}'"),
            ));
        }
        submission = submission.with_case(case_id);
    }

    let outcome = governance
        .submissions
        .submit_once(&submission, &governance.outbox, now)
        .await
        .map_err(|error| OpError::internal("submission_store_failed", error.to_string()))?;
    if let SubmitOnceOutcome::Conflict {
        submission_id: conflicting,
        ..
    } = &outcome
    {
        tracing::info!(
            submission_id = %conflicting,
            "submission_id reused with a different request body"
        );
        return Err(OpError::conflict(
            "submission_conflict",
            format!(
                "submission_id '{conflicting}' was already used for a different request; use a \
                 fresh idempotency key"
            ),
        ));
    }
    let dedup_replayed = outcome.is_replayed();
    let record = outcome.record().ok_or_else(|| {
        OpError::internal("submission_record_missing", "outcome carried no record")
    })?;
    tracing::info!(
        submission_id = %record.submission_id.0,
        task_id = %record.task_id,
        dedup_replayed,
        "submission recorded"
    );
    Ok(SubmitSubmissionOutcome {
        submission: SubmissionView::from_record(record),
        dedup_replayed,
    })
}

/// Read one submission record.
pub async fn get_submission(
    governance: &Governance,
    input: SubmissionGetInput,
) -> OpResult<Option<SubmissionView>> {
    let submission_id = super::required_text(&input.submission_id, "submission_id")?;
    Ok(governance
        .submissions
        .get(&SubmissionId::caller_supplied(&submission_id))
        .await
        .map_err(|error| OpError::internal("submission_store_failed", error.to_string()))?
        .map(|(record, _version)| SubmissionView::from_record(&record)))
}

/// Advance the submission record for a delivered publication key.
///
/// A delivery that already succeeded must not be republished, and the record has
/// to say so: `published` is the audit answer to "did the transport take it?".
/// Non-submission keys (the outbox is shared with other planes) are ignored.
async fn confirm_published_submission(governance: &Governance, key: &str, now: DateTime<Utc>) {
    let Some(submission_id) = uc_engine::governance::submission::submission_id_from_outbox_key(key)
    else {
        return;
    };
    if let Err(error) = governance
        .submissions
        .mark_published(&submission_id, now)
        .await
    {
        tracing::warn!(
            error = %error,
            submission_id = %submission_id.as_str(),
            "publication delivered but the submission record could not be advanced"
        );
    }
}

/// Record that the consumer created the orchestration (PR01's `applied` edge).
///
/// `submission.submit` publishes at-least-once, so the consumer confirms once its
/// task exists. Confirming twice is idempotent; nothing is published here. The
/// caller needs `task_submit`, the same permission that allows creating work —
/// being on the cluster's NATS bus is not authority to stamp an audit record.
pub async fn apply_submission(
    governance: &Governance,
    input: SubmissionApplyInput,
) -> OpResult<Option<SubmissionView>> {
    let now = super::now();
    let principal = policy_interceptor::resolve_declared_principal(
        input.principal_kind.as_deref(),
        input.principal_id.as_deref(),
    )?;
    let decision = governance
        .policy
        .authorize(&principal, Permission::TaskSubmit, None, now);
    if decision.is_denied() {
        return Err(OpError::denied(&decision));
    }
    let submission_id = SubmissionId::caller_supplied(&super::required_text(
        &input.submission_id,
        "submission_id",
    )?);
    let record = governance
        .submissions
        .get(&submission_id)
        .await
        .map_err(|error| OpError::internal("submission_store_failed", error.to_string()))?;
    let Some((record, _version)) = record else {
        return Ok(None);
    };
    if let Some(task_id) = optional_text(input.task_id.as_deref()) {
        if record.task_id != task_id {
            return Err(OpError::invalid(
                "task_id_mismatch",
                format!(
                    "submission '{}' derives task '{}', not '{}'",
                    record.submission_id.as_str(),
                    record.task_id,
                    task_id
                ),
            ));
        }
    }
    if record.state == uc_types::governance::SubmissionState::Applied {
        return Ok(Some(SubmissionView::from_record(&record)));
    }
    let updated = governance
        .submissions
        .mark_applied(&submission_id, now)
        .await
        .map_err(|error| OpError::internal("submission_store_failed", error.to_string()))?;
    Ok(updated.as_ref().map(SubmissionView::from_record))
}

/// Drain due outbox entries onto their subjects.
///
/// This is the single publication path for the submit plane: `submit_once` writes
/// the entry first, then this function claims it (`InFlight`), publishes, and
/// records the result. A crash between claim and publish leaves an `InFlight`
/// entry that the boot recovery re-queues, so the failure mode is a duplicate
/// delivery — which the consumer dedupes — never a lost submission.
#[cfg(feature = "messaging")]
pub async fn drain_outbox(
    governance: &Governance,
    client: &async_nats::Client,
    now: DateTime<Utc>,
    limit: usize,
) -> OutboxDrain {
    let claimed = match governance.outbox.claim_due(now, limit).await {
        Ok(entries) => entries,
        Err(error) => {
            tracing::warn!(error = %error, "outbox claim failed; nothing published this pass");
            return OutboxDrain::default();
        }
    };
    let mut summary = OutboxDrain::default();
    for entry in claimed {
        match client
            .publish(
                entry.subject.clone(),
                entry.payload.clone().into_bytes().into(),
            )
            .await
        {
            Ok(()) => {
                if let Err(error) = governance.outbox.mark_delivered(&entry.key, now).await {
                    tracing::warn!(
                        error = %error,
                        key = %entry.key,
                        "outbox entry was published but could not be marked delivered"
                    );
                } else {
                    summary.delivered += 1;
                    // The publication left the gateway, so the submission record
                    // can claim `published` (the state machine's "transport 接手"
                    // edge). Without this the bridge path -- the only submit path --
                    // stayed `received` forever, and a SIGKILL between the publish
                    // and the record write left it that way even after recovery.
                    confirm_published_submission(governance, &entry.key, now).await;
                }
            }
            Err(error) => {
                if let Err(retry_error) = governance
                    .outbox
                    .schedule_retry(&entry.key, error.to_string(), now)
                    .await
                {
                    tracing::error!(
                        error = %retry_error,
                        key = %entry.key,
                        "outbox retry could not be scheduled; entry stays in flight until recovery"
                    );
                }
                summary.retried += 1;
                tracing::warn!(
                    key = %entry.key,
                    subject = %entry.subject,
                    error = %error,
                    "outbox publish failed; retry scheduled"
                );
            }
        }
    }
    summary
}

/// Re-queue outbox entries a crashed dispatcher left `InFlight`.
///
/// Called once on boot by [`spawn_nats_bridge`]: without it, a crash during a
/// publish would park that submission forever (the entry is neither due nor
/// dead), which is exactly the "no submission loss" property PR01 promises.
pub async fn recover_outbox(governance: &Governance) -> usize {
    match governance.outbox.recover(Utc::now()).await {
        Ok(0) => 0,
        Ok(recovered) => {
            tracing::info!(
                recovered,
                "recovered in-flight outbox entries after restart (at-least-once re-drive)"
            );
            recovered
        }
        Err(error) => {
            tracing::warn!(error = %error, "outbox boot recovery failed");
            0
        }
    }
}

/// Start the periodic outbox dispatcher.
///
/// Spawns a background task that drains the outbox every
/// [`OUTBOX_DRAIN_INTERVAL`]; returns immediately. [`spawn_nats_bridge`] calls this
/// for you — call it directly only when you want the dispatcher without the
/// request/reply bridge.
#[cfg(feature = "messaging")]
pub fn spawn_outbox_dispatcher(client: async_nats::Client, governance: Arc<Governance>) {
    tokio::spawn(async move {
        let mut ticker = tokio::time::interval(OUTBOX_DRAIN_INTERVAL);
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            ticker.tick().await;
            let drained = drain_outbox(&governance, &client, Utc::now(), OUTBOX_DRAIN_LIMIT).await;
            if drained.delivered > 0 || drained.retried > 0 {
                tracing::info!(
                    delivered = drained.delivered,
                    retried = drained.retried,
                    "outbox drain pass"
                );
            }
        }
    });
}

/// Start the governance NATS bridge and the outbox dispatcher.
///
/// * subscribes [`GOVERNANCE_SUBJECT`] and answers each request on its own task
///   (a slow operation cannot block the bridge for everyone else);
/// * re-queues outbox entries left in flight by a previous process
///   ([`recover_outbox`]) before any new publish is attempted;
/// * spawns the periodic drain so a submission queued while NATS was down is
///   delivered once it returns.
///
/// Returns immediately; the loops live in background tasks for the process
/// lifetime.
#[cfg(feature = "messaging")]
pub async fn spawn_nats_bridge(
    client: async_nats::Client,
    governance: Arc<Governance>,
    dispatch: Option<DispatchContext>,
) {
    // Recovery first: an entry left `InFlight` must be re-queued before the drain
    // loop or a request handler starts publishing.
    recover_outbox(&governance).await;

    // Start the dispatcher before subscribing: even if the request/reply
    // subscription cannot be established, PR01 publication must keep being driven
    // (a submission that sits `Received` forever is the failure this loop exists
    // to prevent).
    spawn_outbox_dispatcher(client.clone(), governance.clone());

    let mut subscription = match client.subscribe(GOVERNANCE_SUBJECT.to_string()).await {
        Ok(subscription) => subscription,
        Err(error) => {
            tracing::error!(
                error = %error,
                subject = GOVERNANCE_SUBJECT,
                "governance bridge could not subscribe; the dashboard will report 503 (outbox \
                 dispatch keeps running)"
            );
            return;
        }
    };
    tracing::info!(
        subject = GOVERNANCE_SUBJECT,
        "governance NATS bridge listening"
    );

    tokio::spawn(async move {
        use futures::StreamExt;
        while let Some(message) = subscription.next().await {
            let governance = governance.clone();
            let client = client.clone();
            let dispatch = dispatch.clone();
            tokio::spawn(async move {
                handle_request(&governance, &client, dispatch.as_ref(), message).await;
            });
        }
        tracing::warn!(
            subject = GOVERNANCE_SUBJECT,
            "governance NATS bridge subscription ended"
        );
    });
}

/// Answer one bridge request.
#[cfg(feature = "messaging")]
async fn handle_request(
    governance: &Arc<Governance>,
    client: &async_nats::Client,
    dispatch: Option<&DispatchContext>,
    message: async_nats::Message,
) {
    let Some(reply_subject) = message.reply.clone() else {
        tracing::warn!(
            subject = %message.subject,
            "governance request had no reply subject; ignored"
        );
        return;
    };

    let request = match serde_json::from_slice::<BridgeRequest>(&message.payload) {
        Ok(request) => request,
        Err(error) => {
            let reply = BridgeReply::failed(&OpError::invalid(
                "invalid_request",
                format!("request must be a JSON object with op and payload: {error}"),
            ));
            tracing::info!(code = "invalid_request", "governance op rejected");
            publish_reply(client, &reply_subject, &reply).await;
            return;
        }
    };

    let payload = if request.payload.is_null() {
        serde_json::json!({})
    } else {
        request.payload
    };
    let op = request.op;
    let reply = dispatch_op_with(governance, dispatch, &op, payload).await;

    // A submit must reach the transport before the caller's timeout: drain the
    // queue inline instead of waiting up to one dispatcher tick.
    if op == "submission.submit" && reply.ok {
        let drained = drain_outbox(governance, client, Utc::now(), OUTBOX_DRAIN_LIMIT).await;
        if drained.delivered > 0 || drained.retried > 0 {
            tracing::info!(
                delivered = drained.delivered,
                retried = drained.retried,
                "outbox drained for a bridge submission"
            );
        }
    }

    publish_reply(client, &reply_subject, &reply).await;
}

/// Execution-plane handles the bridge needs for work-starting operations.
///
/// Only `trigger.ingest` uses them: a subscription that opted into `auto_resume`
/// may start a run, and that needs the same task store the `SubmitTask` RPC uses.
/// Everything else in the bridge is governance-only, which is why this is an
/// optional context rather than a parameter on all 30 operations.
#[derive(Clone)]
pub struct DispatchContext {
    pub task_store: std::sync::Arc<tokio::sync::Mutex<crate::server::TaskStore>>,
    pub event_tx: tokio::sync::broadcast::Sender<crate::ultimate_coders::TaskEvent>,
    /// Live worker pool: capacity must report the slots that exist, not ones typed
    /// into the environment.
    pub worker_registry: Option<super::attention_service::CapacitySource>,
}

#[cfg(feature = "messaging")]
async fn publish_reply(
    client: &async_nats::Client,
    reply_subject: &async_nats::Subject,
    reply: &BridgeReply,
) {
    let body = match serde_json::to_vec(reply) {
        Ok(body) => body,
        Err(error) => {
            tracing::error!(error = %error, "governance reply could not be serialized");
            return;
        }
    };
    if let Err(error) = client.publish(reply_subject.clone(), body.into()).await {
        tracing::warn!(
            error = %error,
            subject = %reply_subject,
            "governance reply could not be published"
        );
    }
}

/// Dispatch one bridge operation.
///
/// The `result` is the proto response message of the equivalent RPC, encoded as a
/// JSON object. Denials keep the operation's own code; an unknown op is
/// `unknown_op` and never panics.
pub async fn dispatch_op(
    governance: &Governance,
    op: &str,
    payload: serde_json::Value,
) -> BridgeReply {
    dispatch_op_with(governance, None, op, payload).await
}

/// [`dispatch_op`] with the execution-plane handles attached, so `trigger.ingest`
/// can resume a case whose subscription opted in.
pub async fn dispatch_op_with(
    governance: &Governance,
    dispatch: Option<&DispatchContext>,
    op: &str,
    payload: serde_json::Value,
) -> BridgeReply {
    match dispatch_result(governance, dispatch, op, payload).await {
        Ok(result) => BridgeReply::ok(result),
        Err(error) => {
            tracing::info!(
                op,
                code = %error.code,
                message = %error.message,
                "governance op denied or failed"
            );
            BridgeReply::failed(&error)
        }
    }
}

/// The operation table. One arm per op; each arm decodes the payload, calls the
/// same function the gRPC service calls, and shapes the response object.
async fn dispatch_result(
    governance: &Governance,
    dispatch: Option<&DispatchContext>,
    op: &str,
    payload: serde_json::Value,
) -> OpResult<serde_json::Value> {
    use crate::ultimate_coders::*;

    match op {
        // ── Submissions (PR01) ───────────────────────────────────────
        "submission.submit" => {
            let input: SubmissionSubmitInput = decode(op, payload)?;
            let outcome = submit_submission(governance, input).await?;
            Ok(serde_json::json!({
                "submission": to_value(&outcome.submission)?,
                "dedup_replayed": outcome.dedup_replayed,
            }))
        }
        "submission.get" => {
            let input: SubmissionGetInput = decode(op, payload)?;
            let submission = get_submission(governance, input).await?;
            Ok(serde_json::json!({
                "available": submission.is_some(),
                "submission": submission.as_ref().map(to_value).transpose()?,
            }))
        }
        "submission.apply" => {
            let input: SubmissionApplyInput = decode(op, payload)?;
            let submission = apply_submission(governance, input).await?;
            Ok(serde_json::json!({
                "available": submission.is_some(),
                "submission": submission.as_ref().map(to_value).transpose()?,
            }))
        }

        // ── Cases + mandates (PR02) ──────────────────────────────────
        "case.create" => {
            let outcome = case_service::create_case(governance, decode(op, payload)?).await?;
            Ok(serde_json::json!({
                "case": to_value(&outcome.case)?,
                "mandate": outcome.mandate.as_ref().map(to_value).transpose()?,
                "dedup_replayed": outcome.dedup_replayed,
            }))
        }
        "case.get" => {
            let case = case_service::get_case(governance, decode(op, payload)?).await?;
            Ok(serde_json::json!({
                "available": case.is_some(),
                "case": case.as_ref().map(to_value).transpose()?,
            }))
        }
        "case.list" => {
            let outcome = case_service::list_cases(governance, decode(op, payload)?).await?;
            Ok(serde_json::json!({
                "cases": to_value(&outcome.cases)?,
                "total": outcome.total,
            }))
        }
        "case.transition" => {
            let case = case_service::transition_case(governance, decode(op, payload)?).await?;
            Ok(serde_json::json!({ "case": to_value(&case)? }))
        }
        "case.run.open" => {
            let run = case_service::open_case_run(governance, decode(op, payload)?).await?;
            Ok(serde_json::json!({ "run": to_value(&run)? }))
        }
        "case.run.finish" => {
            let case = case_service::finish_case_run(governance, decode(op, payload)?).await?;
            Ok(serde_json::json!({ "case": to_value(&case)? }))
        }
        "mandate.issue" => {
            let mandate = case_service::issue_mandate(governance, decode(op, payload)?).await?;
            Ok(serde_json::json!({ "mandate": to_value(&mandate)? }))
        }
        "mandate.revoke" => {
            let mandate = case_service::revoke_mandate(governance, decode(op, payload)?).await?;
            Ok(serde_json::json!({ "mandate": to_value(&mandate)? }))
        }
        "mandate.get" => {
            let mandate = case_service::get_mandate(governance, decode(op, payload)?).await?;
            Ok(serde_json::json!({
                "available": mandate.is_some(),
                "mandate": mandate.as_ref().map(to_value).transpose()?,
            }))
        }
        "authorize" => {
            let outcome = case_service::authorize(governance, decode(op, payload)?).await?;
            to_value(&outcome)
        }

        // ── Triggers + subscriptions (PR04) ──────────────────────────
        "trigger.ingest" => {
            // The only op that may start work: with a dispatch context a resumed
            // case gets a real task; without one the wake still happens and the
            // resume is declined with a logged reason.
            let outcome = trigger_service::ingest_with_dispatch(
                governance,
                dispatch.map(|context| &context.task_store),
                dispatch.map(|context| &context.event_tx),
                decode(op, payload)?,
            )
            .await?;
            to_value(&outcome)
        }
        "trigger.subscription.list" => {
            let subscriptions =
                trigger_service::list_subscriptions(governance, decode(op, payload)?).await?;
            Ok(serde_json::json!({ "subscriptions": to_value(&subscriptions)? }))
        }
        "trigger.subscription.upsert" => {
            let subscription =
                trigger_service::upsert_subscription(governance, decode(op, payload)?).await?;
            Ok(serde_json::json!({ "subscription": to_value(&subscription)? }))
        }
        "trigger.subscription.delete" => {
            let deleted =
                trigger_service::delete_subscription(governance, decode(op, payload)?).await?;
            Ok(serde_json::json!({ "success": deleted }))
        }

        // ── Attention + capacity (PR05) ──────────────────────────────
        "attention.list" => {
            let outcome = attention_service::list_attention_with(
                governance,
                dispatch.and_then(|context| context.worker_registry.as_ref()),
                decode(op, payload)?,
            )
            .await?;
            to_value(&outcome)
        }
        "attention.raise" => {
            let item = attention_service::raise_attention(governance, decode(op, payload)?).await?;
            Ok(serde_json::json!({ "item": to_value(&item)? }))
        }
        "attention.resolve" => {
            let item =
                attention_service::resolve_attention(governance, decode(op, payload)?).await?;
            Ok(serde_json::json!({ "item": to_value(&item)? }))
        }
        // ── Approvals (human-tier tools: request -> decide -> single-use permit) ──
        "approval.request" => {
            let outcome =
                approval_service::request_approval(governance, decode(op, payload)?).await?;
            to_value(&outcome)
        }
        "approval.list" => {
            let approvals =
                approval_service::list_approvals(governance, decode(op, payload)?).await?;
            Ok(serde_json::json!({ "approvals": to_value(&approvals)? }))
        }
        "approval.decide" => {
            let approval =
                approval_service::decide_approval(governance, decode(op, payload)?).await?;
            Ok(serde_json::json!({ "approval": to_value(&approval)? }))
        }

        "capacity.get" => {
            // Decoded only to reject a payload that is not an object; the op has
            // no fields.
            let _: GetCapacityRequest = decode(op, payload)?;
            let capacity = attention_service::get_capacity_with(
                governance,
                dispatch.and_then(|context| context.worker_registry.as_ref()),
            )
            .await?;
            Ok(serde_json::json!({ "capacity": to_value(&capacity)? }))
        }

        // ── Proactive + watchdog (PR06) ──────────────────────────────
        "proactive.state.get" => {
            let _: GetProactiveStateRequest = decode(op, payload)?;
            let state = proactive_service::get_state(governance).await?;
            Ok(serde_json::json!({ "state": to_value(&state)? }))
        }
        "proactive.mode.set" => {
            let state = proactive_service::set_mode(governance, decode(op, payload)?).await?;
            Ok(serde_json::json!({ "state": to_value(&state)? }))
        }
        "proactive.proposal.submit" => {
            let outcome =
                proactive_service::submit_proposal(governance, decode(op, payload)?).await?;
            to_value(&outcome)
        }
        "proactive.proposal.list" => {
            let proposals =
                proactive_service::list_proposals(governance, decode(op, payload)?).await?;
            Ok(serde_json::json!({ "proposals": to_value(&proposals)? }))
        }
        "proactive.proposal.decide" => {
            let proposal =
                proactive_service::decide_proposal(governance, decode(op, payload)?).await?;
            Ok(serde_json::json!({ "proposal": to_value(&proposal)? }))
        }
        "proactive.audit.list" => {
            let entries = proactive_service::list_audit(governance, decode(op, payload)?).await?;
            Ok(serde_json::json!({ "entries": to_value(&entries)? }))
        }
        "watchdog.stall.report" => {
            let verdict = proactive_service::report_stall(governance, decode(op, payload)?).await?;
            to_value(&verdict)
        }

        other => Err(OpError::invalid(
            "unknown_op",
            format!(
                "unknown governance op '{other}'; see the governance_client op list for the \
                 supported set"
            ),
        )),
    }
}

/// Decode a payload into the proto request type of the equivalent RPC.
fn decode<T>(op: &str, payload: serde_json::Value) -> OpResult<T>
where
    T: serde::de::DeserializeOwned,
{
    let payload = if payload.is_null() {
        serde_json::json!({})
    } else {
        payload
    };
    serde_json::from_value(payload).map_err(|error| {
        OpError::invalid(
            "invalid_request",
            format!("payload for '{op}' is not valid: {error}"),
        )
    })
}

/// Serialize a wire projection into a JSON object.
fn to_value<T: Serialize>(value: &T) -> OpResult<serde_json::Value> {
    serde_json::to_value(value)
        .map_err(|error| OpError::internal("serialization_failed", error.to_string()))
}

/// Derive `Deserialize` for a prost request message.
///
/// The bridge receives the same field names the gRPC message carries, so the
/// payload decodes straight into the generated type: one shape, two transports,
/// no hand-written field mapping to drift. Missing keys take the protobuf default
/// (`""`, `0`, `false`, `None`, empty list), which is exactly what the proto says
/// an omitted field means.
///
/// The field list is spelled out because prost types have no serde derives and
/// because it must stay exhaustive: when the proto grows a field, the struct
/// literal generated here stops compiling — a compile-time reminder to map it
/// rather than a silent runtime default.
macro_rules! bridge_request {
    ($ty:ident { $($field:ident : $field_ty:ty),* $(,)? }) => {
        impl<'de> ::serde::Deserialize<'de> for $ty {
            fn deserialize<D>(deserializer: D) -> ::std::result::Result<Self, D::Error>
            where
                D: ::serde::Deserializer<'de>,
            {
                #[derive(::serde::Deserialize)]
                #[serde(default)]
                struct Wire {
                    $($field: $field_ty,)*
                }
                impl ::std::default::Default for Wire {
                    fn default() -> Self {
                        Self { $($field: ::std::default::Default::default(),)* }
                    }
                }
                // `wire` is unused for messages with no fields (`GetCapacityRequest`).
                #[allow(unused_variables)]
                let wire = <Wire as ::serde::Deserialize>::deserialize(deserializer)?;
                Ok(Self {
                    $($field: wire.$field,)*
                })
            }
        }
    };
}

use crate::ultimate_coders::*;

bridge_request!(CreateCaseRequest {
    title: String,
    description: String,
    project_id: String,
    source: String,
    external_ref: Option<String>,
    thread_key: Option<String>,
    trigger_id: Option<String>,
    priority: Option<u32>,
    completion_conditions_json: Option<String>,
    contract_digest: Option<String>,
    principal_kind: String,
    principal_id: String,
    issue_mandate: bool,
    mandate_scopes_json: Option<String>,
    mandate_expires_at: Option<i64>,
    labels: Vec<String>,
});

bridge_request!(GetCaseRequest { case_id: String });

bridge_request!(ListCasesRequest {
    status: Option<String>,
    project_id: Option<String>,
    thread_key: Option<String>,
    limit: u32,
    include_terminal: bool,
});

bridge_request!(TransitionCaseRequest {
    case_id: String,
    status: String,
    reason: Option<String>,
    principal_kind: String,
    principal_id: String,
});

bridge_request!(OpenCaseRunRequest {
    case_id: String,
    task_id: String,
    submission_id: Option<String>,
});

bridge_request!(FinishCaseRunRequest {
    case_id: String,
    run_id: String,
    outcome: String,
    detail: Option<String>,
});

bridge_request!(IssueMandateRequest {
    case_id: String,
    scopes_json: String,
    expires_at: Option<i64>,
    principal_kind: String,
    principal_id: String,
    delegated_from: Option<String>,
    policy_version: Option<u64>,
});

bridge_request!(RevokeMandateRequest {
    mandate_id: String,
    reason: String,
    principal_kind: String,
    principal_id: String,
});

bridge_request!(GetMandateRequest {
    mandate_id: Option<String>,
    case_id: Option<String>,
});

bridge_request!(AuthorizeRequest {
    principal_kind: String,
    principal_id: String,
    permission: String,
    project_id: Option<String>,
    case_id: Option<String>,
    tool: Option<String>,
    action: Option<String>,
    repo: Option<String>,
    branch: Option<String>,
    path: Option<String>,
    tokens: Option<u64>,
    cost_usd: Option<f64>,
    sandbox_profile_json: Option<String>,
    policy_version: Option<u64>,
});

bridge_request!(IngestTriggerRequest {
    source: String,
    kind: String,
    external_id: String,
    thread_key: Option<String>,
    repo: Option<String>,
    actor: Option<String>,
    payload_json: String,
    delivery_id: Option<String>,
    principal_kind: String,
    principal_id: String,
    open_case_if_unmatched: bool,
    open_case_project_id: Option<String>,
});

bridge_request!(ListSubscriptionsRequest {
    case_id: Option<String>,
    source: Option<String>,
    limit: u32,
});

bridge_request!(UpsertSubscriptionRequest {
    subscription_id: Option<String>,
    case_id: String,
    source: String,
    repo: Option<String>,
    thread_key: Option<String>,
    kinds: Vec<String>,
    labels: Vec<String>,
    active: bool,
    auto_resume: bool,
});

bridge_request!(DeleteSubscriptionRequest {
    subscription_id: String
});

bridge_request!(RequestApprovalRequest {
    tool: String,
    case_id: Option<String>,
    project_id: Option<String>,
    action_digest: Option<String>,
    reason: Option<String>,
    principal_kind: String,
    principal_id: String,
    ttl_minutes: Option<u32>,
});

bridge_request!(ListApprovalsRequest {
    state: Option<String>,
    limit: u32,
    offset: u32,
});

bridge_request!(DecideApprovalRequest {
    approval_id: String,
    approve: bool,
    note: Option<String>,
    principal_kind: String,
    principal_id: String,
});

bridge_request!(ListAttentionRequest {
    include_resolved: bool,
    case_id: Option<String>,
    kind: Option<String>,
    limit: u32,
});

bridge_request!(RaiseAttentionRequest {
    case_id: String,
    kind: String,
    summary: String,
    detail: Option<String>,
    human_action: Option<String>,
    task_id: Option<String>,
    priority: Option<u32>,
    principal_kind: String,
    principal_id: String,
});

bridge_request!(ResolveAttentionRequest {
    attention_id: String,
    resolution_kind: String,
    by: Option<String>,
    detail: Option<String>,
    principal_kind: String,
    principal_id: String,
});

bridge_request!(GetCapacityRequest {});

bridge_request!(GetProactiveStateRequest {});

bridge_request!(SetProactiveModeRequest {
    mode: String,
    max_open_proposals: Option<u32>,
    max_tokens_per_action: Option<u64>,
    always_require_approval: Option<bool>,
    principal_kind: String,
    principal_id: String,
    reason: Option<String>,
});

bridge_request!(SubmitProposalRequest {
    case_id: Option<String>,
    kind: String,
    title: String,
    rationale: String,
    confidence: f32,
    evidence: Vec<String>,
    proposed_request: Option<String>,
    project_id: Option<String>,
    estimated_tokens: Option<u64>,
    principal_kind: String,
    principal_id: String,
});

bridge_request!(ListProposalsRequest {
    status: Option<String>,
    case_id: Option<String>,
    limit: u32,
});

bridge_request!(DecideProposalRequest {
    proposal_id: String,
    decision: String,
    by: String,
    reason: Option<String>,
    principal_kind: String,
    principal_id: String,
});

bridge_request!(ReportStallRequest {
    task_id: String,
    subtask_id: Option<String>,
    no_progress_count: u32,
    evidence_signature: Option<String>,
    last_error: Option<String>,
    confirmed_effect_ids: Vec<String>,
    attempts_used: u32,
    replans_used: u32,
    already_retried: bool,
    case_id: Option<String>,
});

bridge_request!(ListAuditRequest {
    limit: u32,
    proposal_id: Option<String>,
    case_id: Option<String>,
});

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use uc_types::governance::{Grant, Permission, PolicySnapshot, PolicyVersion, Principal};

    async fn plane() -> Governance {
        let governance = Governance::in_memory();
        let mut snapshot = PolicySnapshot::empty(PolicyVersion::initial());
        snapshot = snapshot
            .with_grant(Grant::new(
                Principal::service("dashboard"),
                vec![
                    Permission::CaseCreate,
                    Permission::CaseControl,
                    Permission::MandateIssue,
                    Permission::TriggerIngest,
                ],
            ))
            .expect("service grants are structurally valid");
        governance.policy.install(snapshot).await.unwrap();
        governance
    }

    fn case_payload() -> serde_json::Value {
        json!({
            "title": "Fix the flaky test",
            "description": "The suite is red",
            "project_id": "proj",
            "source": "api",
            "external_ref": "acme/widgets#7",
            "principal_kind": "service",
            "principal_id": "dashboard",
        })
    }

    #[tokio::test]
    async fn a_delivered_publication_advances_the_submission_record() {
        use uc_engine::governance::submission::{
            outbox_key, SubmissionStore, OUTBOX_SUBJECT_TASK_SUBMIT,
        };
        use uc_types::governance::{SubmissionId, SubmissionRequest, SubmissionState};

        let governance = plane().await;
        let request = SubmissionRequest::new(
            Principal::user("alice"),
            "proj",
            "fix it",
            Some(SubmissionId::caller_supplied("key-1")),
        );
        governance
            .submissions
            .submit_once(&request, &governance.outbox, Utc::now())
            .await
            .expect("submit");
        let key = outbox_key(&SubmissionId::caller_supplied("key-1"));
        assert_eq!(
            governance
                .outbox
                .claim_due(Utc::now(), 10)
                .await
                .expect("claim")
                .len(),
            1
        );
        governance
            .outbox
            .mark_delivered(&key, Utc::now())
            .await
            .expect("delivered");

        confirm_published_submission(&governance, &key, Utc::now()).await;

        let (record, _version) = governance
            .submissions
            .get(&SubmissionId::caller_supplied("key-1"))
            .await
            .expect("get")
            .expect("record present");
        assert_eq!(record.state, SubmissionState::Published);
        assert_eq!(record.publish_attempts, 1);

        // Keys from other planes are ignored, and confirming twice is harmless.
        confirm_published_submission(&governance, "attention:expiry:1", Utc::now()).await;
        confirm_published_submission(&governance, &key, Utc::now()).await;
        let (again, _version) = governance
            .submissions
            .get(&SubmissionId::caller_supplied("key-1"))
            .await
            .expect("get")
            .expect("record present");
        assert_eq!(again.state, SubmissionState::Published);
        assert_eq!(again.publish_attempts, 2);

        let _ = OUTBOX_SUBJECT_TASK_SUBMIT;
        let _ = SubmissionStore::task_id_for(&SubmissionId::caller_supplied("key-1"));
    }

    #[tokio::test]
    async fn the_consumer_can_confirm_a_submission_as_applied() {
        use uc_engine::governance::submission::SubmissionStore;
        use uc_types::governance::{SubmissionId, SubmissionRequest, SubmissionState};

        let governance = plane().await;
        // The consumer identity needs `task_submit`; the dev snapshot grants it
        // to `agent:uc-worker`, and a shared deployment grants a real consumer.
        let snapshot = PolicySnapshot::empty(PolicyVersion::initial())
            .with_grant(Grant::new(
                Principal::agent("uc-worker"),
                vec![Permission::TaskSubmit],
            ))
            .expect("an agent may hold task_submit");
        governance.policy.install(snapshot).await.expect("install");

        let request = SubmissionRequest::new(
            Principal::user("alice"),
            "proj",
            "fix it",
            Some(SubmissionId::caller_supplied("key-1")),
        );
        governance
            .submissions
            .submit_once(&request, &governance.outbox, Utc::now())
            .await
            .expect("submit");
        let task_id = SubmissionStore::task_id_for(&SubmissionId::caller_supplied("key-1"));

        let reply = dispatch_op(
            &governance,
            "submission.apply",
            json!({
                "submission_id": "key-1",
                "task_id": task_id,
                "principal_kind": "agent",
                "principal_id": "uc-worker",
            }),
        )
        .await;
        assert!(reply.ok, "{reply:?}");
        let result = reply.result.expect("result present");
        assert_eq!(result["available"], json!(true));
        assert_eq!(result["submission"]["state"], json!("applied"));

        let (record, _version) = governance
            .submissions
            .get(&SubmissionId::caller_supplied("key-1"))
            .await
            .expect("get")
            .expect("record present");
        assert_eq!(record.state, SubmissionState::Applied);

        // Re-applying is idempotent: the same consumer may confirm twice.
        let again = dispatch_op(
            &governance,
            "submission.apply",
            json!({
                "submission_id": "key-1",
                "task_id": task_id,
                "principal_kind": "agent",
                "principal_id": "uc-worker",
            }),
        )
        .await;
        assert!(again.ok, "{again:?}");

        // A consumer cannot stamp a submission with a task it did not get.
        let mismatch = dispatch_op(
            &governance,
            "submission.apply",
            json!({
                "submission_id": "key-1",
                "task_id": "sub-deadbeef",
                "principal_kind": "agent",
                "principal_id": "uc-worker",
            }),
        )
        .await;
        assert!(!mismatch.ok);
        assert_eq!(
            mismatch.error.expect("error present").code,
            "task_id_mismatch"
        );

        // Being on the bus is not authority: without `task_submit` it is refused.
        let denied = dispatch_op(
            &governance,
            "submission.apply",
            json!({"submission_id": "key-1", "principal_kind": "service", "principal_id": "dashboard"}),
        )
        .await;
        assert!(!denied.ok);
        assert_eq!(
            denied.error.expect("error present").code,
            "missing_permission"
        );
    }

    #[tokio::test]
    async fn an_unknown_op_is_a_structured_refusal_not_a_panic() {
        let governance = plane().await;
        let reply = dispatch_op(&governance, "case.teleport", json!({})).await;
        assert!(!reply.ok);
        let error = reply.error.expect("error present");
        assert_eq!(error.code, "unknown_op");
        assert!(error.message.contains("case.teleport"));
        assert!(reply.result.is_none());
    }

    #[tokio::test]
    async fn a_malformed_payload_is_invalid_request() {
        let governance = plane().await;
        let reply = dispatch_op(
            &governance,
            "case.create",
            json!({"title": 7, "priority": "high"}),
        )
        .await;
        assert!(!reply.ok);
        assert_eq!(reply.error.expect("error present").code, "invalid_request");
    }

    #[test]
    fn a_malformed_envelope_is_described_rather_than_trusted() {
        // The loop decodes the envelope before dispatch; this pins the contract
        // that an unparsable body becomes `invalid_request` at that boundary.
        let error = serde_json::from_slice::<BridgeRequest>(b"not json").unwrap_err();
        let op_error = OpError::invalid(
            "invalid_request",
            format!("request must be a JSON object with op and payload: {error}"),
        );
        let reply = BridgeReply::failed(&op_error);
        assert_eq!(reply.error.expect("error present").code, "invalid_request");
    }

    #[tokio::test]
    async fn case_create_through_the_bridge_matches_the_grpc_shape() {
        let governance = plane().await;
        let reply = dispatch_op(&governance, "case.create", case_payload()).await;
        assert!(reply.ok, "{reply:?}");
        let result = reply.result.expect("result present");
        assert_eq!(result["dedup_replayed"], json!(false));
        assert_eq!(result["case"]["project_id"], json!("proj"));
        // Timestamps cross the bridge as RFC 3339, not Unix seconds.
        assert!(result["case"]["created_at"].as_str().is_some());

        let replay = dispatch_op(&governance, "case.create", case_payload()).await;
        let replay_result = replay.result.expect("result present");
        assert_eq!(replay_result["dedup_replayed"], json!(true));
        assert_eq!(replay_result["case"]["id"], result["case"]["id"]);
    }

    #[tokio::test]
    async fn missing_optional_keys_behave_like_proto_defaults() {
        let governance = plane().await;
        // No source, no labels, no priority: proto defaults.
        let reply = dispatch_op(
            &governance,
            "case.create",
            json!({
                "title": "Minimal",
                "description": "d",
                "project_id": "proj",
                "principal_kind": "service",
                "principal_id": "dashboard",
            }),
        )
        .await;
        assert!(reply.ok, "{reply:?}");
        let result = reply.result.expect("result present");
        assert_eq!(result["case"]["origin"]["source"], json!("manual"));
        assert_eq!(result["case"]["priority"], json!(0));
    }

    #[tokio::test]
    async fn a_policy_denial_keeps_its_deny_code_over_the_bridge() {
        let governance = plane().await;
        let mut payload = case_payload();
        payload["principal_id"] = json!("mallory");
        let reply = dispatch_op(&governance, "case.create", payload).await;
        assert!(!reply.ok);
        assert_eq!(
            reply.error.expect("error present").code,
            "missing_permission"
        );
    }

    #[tokio::test]
    async fn submission_submit_is_recorded_once_and_replays() {
        let governance = plane().await;
        let payload = json!({
            "description": "Fix the build",
            "project_id": "proj",
            "submission_id": "key-1",
            "principal_kind": "service",
            "principal_id": "dashboard",
        });
        let first = dispatch_op(&governance, "submission.submit", payload.clone()).await;
        assert!(first.ok, "{first:?}");
        let first_result = first.result.expect("result present");
        assert_eq!(first_result["dedup_replayed"], json!(false));
        let task_id = first_result["submission"]["task_id"]
            .as_str()
            .expect("task id string")
            .to_string();
        assert_eq!(first_result["submission"]["state"], json!("received"));

        let second = dispatch_op(&governance, "submission.submit", payload).await;
        let second_result = second.result.expect("result present");
        assert_eq!(second_result["dedup_replayed"], json!(true));
        assert_eq!(second_result["submission"]["task_id"], json!(task_id));
    }

    #[tokio::test]
    async fn a_conflicting_submission_key_is_refused() {
        let governance = plane().await;
        dispatch_op(
            &governance,
            "submission.submit",
            json!({
                "description": "first",
                "project_id": "proj",
                "submission_id": "key-1",
                "principal_kind": "service",
                "principal_id": "dashboard",
            }),
        )
        .await;
        let reply = dispatch_op(
            &governance,
            "submission.submit",
            json!({
                "description": "second",
                "project_id": "proj",
                "submission_id": "key-1",
                "principal_kind": "service",
                "principal_id": "dashboard",
            }),
        )
        .await;
        assert!(!reply.ok);
        assert_eq!(
            reply.error.expect("error present").code,
            "submission_conflict"
        );
    }

    #[tokio::test]
    async fn submission_get_reports_availability() {
        let governance = plane().await;
        let reply = dispatch_op(
            &governance,
            "submission.get",
            json!({"submission_id": "nope"}),
        )
        .await;
        assert!(reply.ok);
        let result = reply.result.expect("result present");
        assert_eq!(result["available"], json!(false));
    }

    #[tokio::test]
    async fn read_ops_answer_empty_state_without_erroring() {
        let governance = plane().await;
        for (op, key) in [
            ("case.list", "cases"),
            ("attention.list", "items"),
            ("proactive.proposal.list", "proposals"),
            ("proactive.audit.list", "entries"),
            ("trigger.subscription.list", "subscriptions"),
        ] {
            let reply = dispatch_op(&governance, op, json!({})).await;
            assert!(reply.ok, "{op}: {reply:?}");
            let result = reply.result.expect("result present");
            assert!(result.get(key).is_some(), "{op} must return {key}");
        }
        for op in ["capacity.get", "proactive.state.get"] {
            let reply = dispatch_op(&governance, op, json!({})).await;
            assert!(reply.ok, "{op}: {reply:?}");
        }
    }

    #[tokio::test]
    async fn the_mode_set_op_reports_the_new_state() {
        let governance = Governance::in_memory();
        let mut snapshot = PolicySnapshot::empty(PolicyVersion::initial());
        snapshot = snapshot
            .with_grant(Grant::new(
                Principal::user("alice"),
                vec![Permission::ProactivePropose],
            ))
            .expect("user grants are structurally valid");
        governance.policy.install(snapshot).await.unwrap();

        let reply = dispatch_op(
            &governance,
            "proactive.mode.set",
            json!({"mode": "observe", "principal_kind": "user", "principal_id": "alice"}),
        )
        .await;
        assert!(reply.ok, "{reply:?}");
        assert_eq!(
            reply.result.expect("result present")["state"]["mode"],
            json!("observe")
        );
    }

    #[tokio::test]
    async fn an_unknown_op_does_not_panic_before_checking_payloads() {
        let governance = plane().await;
        let reply = dispatch_op(&governance, "", json!({})).await;
        assert!(!reply.ok);
        assert_eq!(reply.error.expect("error present").code, "unknown_op");
    }

    #[test]
    fn reply_serialization_carries_exactly_one_of_result_or_error() {
        let ok = BridgeReply::ok(json!({"x": 1}));
        assert_eq!(
            serde_json::to_value(&ok).unwrap(),
            json!({"ok": true, "result": {"x": 1}})
        );
        let failed = BridgeReply::failed(&OpError::invalid("boom", "detail"));
        assert_eq!(
            serde_json::to_value(&failed).unwrap(),
            json!({"ok": false, "error": {"code": "boom", "message": "detail"}})
        );
    }
}
