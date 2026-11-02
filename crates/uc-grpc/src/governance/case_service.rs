//! `CaseService` — work cases, mandates and the stateless authorization check.
//!
//! Every operation is a free function ([`create_case`], [`transition_case`], …)
//! that the tonic service method and the NATS bridge both call. The functions
//! are the implementation; [`CaseServiceImpl`] is only the gRPC adapter.
//!
//! ## What is gated, and what is not
//!
//! The proto puts the caller's identity on the messages that *change governance
//! state* (`CreateCaseRequest`, `TransitionCaseRequest`, `IssueMandateRequest`,
//! `RevokeMandateRequest`), and those carry a permission gate. The read
//! operations (`GetCase`, `ListCases`, `GetMandate`) deliberately do not: they
//! project metadata, not authority, and the Python dashboard documents the same
//! choice (`attention_api.py`: "read endpoints are not token-gated here. They
//! expose governance metadata, not authority"). They are still behind the
//! transport auth interceptor. `OpenCaseRun`/`FinishCaseRun` carry no identity
//! either; the case's own invariants (mandate present, one open run, not
//! quiescent, not terminal) are the guard there.
//!
//! ## Authority is never inferred
//!
//! A case that a caller just created may be given a mandate in the same request
//! (`issue_mandate`), but a *replayed* create returns the mandate the case
//! already holds instead of minting a second one: a retried request must not
//! accumulate authority.

use std::sync::Arc;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use tonic::{Request, Response, Status};
use uc_engine::governance::case_store::MandateStoreError;
use uc_engine::governance::{CaseStoreError, Governance, PolicyEngineError};
use uc_types::governance::{
    ActionClass, CaseId, CaseOrigin, CaseRun, CaseRunOutcome, CaseStatus, DenyReason, Mandate,
    MandateId, MandateOrigin, MandateRequest, MandateScope, Permission, PolicyDecision,
    PrincipalKind, SandboxSecurityProfile, SourceKind, WorkCase,
};

use super::{
    clamp_limit, decode_optional_timestamp, encode_optional_timestamp, encode_timestamp,
    optional_text, required_text, OpError, OpResult, DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT,
};
use crate::governance::policy_interceptor::{
    require, resolve_declared_principal, resolve_principal,
};
use crate::ultimate_coders::case_service_server::CaseService;

// ── Wire projections ───────────────────────────────────────────────
//
// The projections are serde types because the NATS bridge serializes them
// verbatim, and `to_proto` builds the gRPC message from the same value. Both
// transports therefore report identical facts; only the timestamp encoding
// differs (RFC 3339 over JSON, Unix seconds over proto).

/// Wire form of [`CaseOrigin`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CaseOriginView {
    pub source: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub external_ref: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub thread_key: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub trigger_id: Option<String>,
}

impl CaseOriginView {
    fn from_origin(origin: &CaseOrigin) -> Self {
        Self {
            source: origin.source.clone(),
            external_ref: origin.external_ref.clone(),
            thread_key: origin.thread_key.clone(),
            trigger_id: origin.trigger_id.clone(),
        }
    }

    fn to_proto(&self) -> crate::ultimate_coders::CaseOriginProto {
        crate::ultimate_coders::CaseOriginProto {
            source: self.source.clone(),
            external_ref: self.external_ref.clone(),
            thread_key: self.thread_key.clone(),
            trigger_id: self.trigger_id.clone(),
        }
    }
}

/// Wire form of [`CaseRun`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CaseRunView {
    pub run_id: String,
    pub task_id: String,
    pub attempt: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub submission_id: Option<String>,
    pub started_at: DateTime<Utc>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ended_at: Option<DateTime<Utc>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub outcome: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

impl CaseRunView {
    fn from_run(run: &CaseRun) -> Self {
        Self {
            run_id: run.run_id.0.clone(),
            task_id: run.task_id.clone(),
            attempt: run.attempt,
            submission_id: run.submission_id.clone(),
            started_at: run.started_at,
            ended_at: run.ended_at,
            outcome: run.outcome.map(case_run_outcome_token).map(str::to_string),
            detail: run.detail.clone(),
        }
    }

    fn to_proto(&self) -> crate::ultimate_coders::CaseRunProto {
        crate::ultimate_coders::CaseRunProto {
            run_id: self.run_id.clone(),
            task_id: self.task_id.clone(),
            attempt: self.attempt,
            submission_id: self.submission_id.clone(),
            started_at: encode_timestamp(self.started_at),
            ended_at: encode_optional_timestamp(self.ended_at),
            outcome: self.outcome.clone(),
            detail: self.detail.clone(),
        }
    }
}

/// Wire form of [`WorkCase`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CaseView {
    pub id: String,
    pub title: String,
    pub description: String,
    pub project_id: String,
    pub status: String,
    pub origin: CaseOriginView,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub contract_digest: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub completion_conditions_json: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mandate_id: Option<String>,
    #[serde(default)]
    pub subscriptions: Vec<String>,
    #[serde(default)]
    pub runs: Vec<CaseRunView>,
    pub compacted_runs: u32,
    pub priority: u32,
    #[serde(default)]
    pub labels: Vec<String>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    pub version: u64,
}

impl CaseView {
    fn from_case(case: &WorkCase) -> Self {
        Self {
            id: case.id.0.clone(),
            title: case.title.clone(),
            description: case.description.clone(),
            project_id: case.project_id.clone(),
            status: case.status.as_str().to_string(),
            origin: CaseOriginView::from_origin(&case.origin),
            contract_digest: case.contract_digest.clone(),
            // The domain keeps completion conditions opaque (`Value`); the wire
            // carries them as JSON so the executor that evaluates them keeps one
            // canonical representation.
            completion_conditions_json: case
                .completion_conditions
                .as_ref()
                .map(|value| value.to_string()),
            mandate_id: case.mandate_id.clone(),
            subscriptions: case.subscriptions.clone(),
            runs: case.runs.iter().map(CaseRunView::from_run).collect(),
            compacted_runs: case.compacted_runs,
            priority: u32::from(case.priority),
            labels: case.labels.clone(),
            created_at: case.created_at,
            updated_at: case.updated_at,
            version: case.version,
        }
    }

    fn to_proto(&self) -> crate::ultimate_coders::CaseProto {
        crate::ultimate_coders::CaseProto {
            id: self.id.clone(),
            title: self.title.clone(),
            description: self.description.clone(),
            project_id: self.project_id.clone(),
            status: self.status.clone(),
            origin: Some(self.origin.to_proto()),
            contract_digest: self.contract_digest.clone(),
            completion_conditions_json: self.completion_conditions_json.clone(),
            mandate_id: self.mandate_id.clone(),
            subscriptions: self.subscriptions.clone(),
            runs: self.runs.iter().map(CaseRunView::to_proto).collect(),
            compacted_runs: self.compacted_runs,
            priority: self.priority,
            labels: self.labels.clone(),
            created_at: encode_timestamp(self.created_at),
            updated_at: encode_timestamp(self.updated_at),
            version: self.version,
        }
    }
}

/// Wire form of [`Mandate`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MandateView {
    pub id: String,
    pub case_id: String,
    pub grantor_kind: String,
    pub grantor_id: String,
    pub policy_version: u64,
    /// JSON-encoded `Vec<MandateScope>` — the scopes are a tagged enum, so JSON is
    /// the one representation Rust and Python cannot drift on.
    pub scopes_json: String,
    pub issued_at: DateTime<Utc>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub expires_at: Option<DateTime<Utc>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub revoked_at: Option<DateTime<Utc>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub revoke_reason: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub revoked_by: Option<String>,
    pub origin: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub delegated_from: Option<String>,
}

impl MandateView {
    fn from_mandate(mandate: &Mandate) -> Self {
        let scopes_json = serde_json::to_string(&mandate.scopes).unwrap_or_else(|error| {
            // Serializing a `MandateScope` cannot fail (no map keys, no floats
            // that fail). Degrade to an empty list and log rather than lose the
            // whole mandate view.
            tracing::error!(error = %error, mandate_id = %mandate.id.0, "mandate scopes were not serializable");
            "[]".to_string()
        });
        Self {
            id: mandate.id.0.clone(),
            case_id: mandate.case_id.clone(),
            grantor_kind: mandate.grantor.kind.as_str().to_string(),
            grantor_id: mandate.grantor.id.clone(),
            policy_version: mandate.policy_version,
            scopes_json,
            issued_at: mandate.issued_at,
            expires_at: mandate.expires_at,
            revoked_at: mandate.revoked.as_ref().map(|revocation| revocation.at),
            revoke_reason: mandate
                .revoked
                .as_ref()
                .map(|revocation| revocation.reason.clone()),
            revoked_by: mandate
                .revoked
                .as_ref()
                .map(|revocation| revocation.by.to_string()),
            origin: mandate.origin.as_str().to_string(),
            delegated_from: mandate.delegated_from.as_ref().map(|id| id.0.clone()),
        }
    }

    fn to_proto(&self) -> crate::ultimate_coders::MandateProto {
        crate::ultimate_coders::MandateProto {
            id: self.id.clone(),
            case_id: self.case_id.clone(),
            grantor_kind: self.grantor_kind.clone(),
            grantor_id: self.grantor_id.clone(),
            policy_version: self.policy_version,
            scopes_json: self.scopes_json.clone(),
            issued_at: encode_timestamp(self.issued_at),
            expires_at: encode_optional_timestamp(self.expires_at),
            revoked_at: encode_optional_timestamp(self.revoked_at),
            revoke_reason: self.revoke_reason.clone(),
            revoked_by: self.revoked_by.clone(),
            origin: self.origin.clone(),
            delegated_from: self.delegated_from.clone(),
        }
    }
}

/// Result of [`create_case`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CreateCaseOutcome {
    pub case: CaseView,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mandate: Option<MandateView>,
    /// `true` = the case already existed for this thread; nothing new was made.
    pub dedup_replayed: bool,
}

/// Result of [`list_cases`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ListCasesOutcome {
    pub cases: Vec<CaseView>,
    /// Number of cases that matched the filter, before `limit` was applied.
    pub total: u32,
}

/// Result of [`authorize`]: one check, every dimension that was evaluated.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AuthorizeOutcome {
    pub allowed: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub deny_code: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub deny_detail: Option<String>,
    pub permission_allowed: bool,
    /// `true` when no case was named (the mandate dimension was not checked).
    pub mandate_allowed: bool,
    /// `true` when no sandbox profile was supplied.
    pub sandbox_allowed: bool,
}

impl AuthorizeOutcome {
    fn from_dimensions(
        permission_allowed: bool,
        mandate: Option<&PolicyDecision>,
        sandbox: Option<&PolicyDecision>,
    ) -> Self {
        // The first *specific* denial (mandate before sandbox) is the one worth
        // reporting: it names the fact that failed, and the permission dimension
        // is folded into `permission_allowed` together with the tool check.
        let denial = mandate
            .filter(|decision| decision.is_denied())
            .or_else(|| sandbox.filter(|decision| decision.is_denied()));
        let (deny_code, deny_detail) = match denial.and_then(|decision| decision.deny_reason()) {
            Some(reason) => (Some(reason.code().to_string()), Some(format!("{reason:?}"))),
            None => (None, None),
        };
        Self {
            allowed: permission_allowed
                && mandate
                    .map(|decision| decision.is_allowed())
                    .unwrap_or(true)
                && sandbox
                    .map(|decision| decision.is_allowed())
                    .unwrap_or(true),
            deny_code,
            deny_detail,
            permission_allowed,
            mandate_allowed: mandate
                .map(|decision| decision.is_allowed())
                .unwrap_or(true),
            sandbox_allowed: sandbox
                .map(|decision| decision.is_allowed())
                .unwrap_or(true),
        }
    }

    /// A refusal that came from the request itself (stale policy generation).
    fn refused(code: &str, detail: impl Into<String>) -> Self {
        Self {
            allowed: false,
            deny_code: Some(code.to_string()),
            deny_detail: Some(detail.into()),
            permission_allowed: false,
            mandate_allowed: false,
            sandbox_allowed: false,
        }
    }
}

// ── Operations ─────────────────────────────────────────────────────

/// Create (or return the existing) case for a thread, optionally issuing its
/// mandate in the same call.
///
/// Case identity is **deterministic** when the request carries a thread
/// (`external_ref` or `thread_key`): re-delivering the same event converges on
/// the same case through [`CaseStore::create_unique`] instead of opening a second
/// one, and the response says so via `dedup_replayed`.
pub async fn create_case(
    governance: &Governance,
    request: crate::ultimate_coders::CreateCaseRequest,
) -> OpResult<CreateCaseOutcome> {
    let now = super::now();
    let principal = resolve_declared_principal(
        Some(request.principal_kind.as_str()),
        Some(request.principal_id.as_str()),
    )?;
    let project_id = required_text(&request.project_id, "project_id")?;
    let title = required_text(&request.title, "title")?;

    // Creating a case is a governance act: it must be authorized for this scope.
    require(
        governance,
        &principal,
        Permission::CaseCreate,
        Some(project_id.as_str()),
        now,
    )?;

    let source = match optional_text(Some(request.source.as_str())) {
        Some(source) => {
            if SourceKind::from_str_lossy(&source).is_none() {
                return Err(OpError::invalid(
                    "unknown_source",
                    format!(
                        "unknown case origin source '{source}' (expected github | ci | lark | \
                         dashboard | api | cron | manual)"
                    ),
                ));
            }
            source
        }
        // Absent provenance is a manually opened case, not an unknown source.
        None => "manual".to_string(),
    };
    let external_ref = optional_text(request.external_ref.as_deref());
    let thread_key = optional_text(request.thread_key.as_deref());
    let origin = CaseOrigin {
        source: source.clone(),
        external_ref: external_ref.clone(),
        thread_key: thread_key.clone(),
        trigger_id: optional_text(request.trigger_id.as_deref()),
    };

    let mut case = match external_ref.clone().or_else(|| thread_key.clone()) {
        Some(identity) => WorkCase::new(
            title,
            request.description.clone(),
            project_id.clone(),
            origin,
            now,
        )
        .with_id(CaseId::from_parts(&source, &identity)),
        None => WorkCase::new(
            title,
            request.description.clone(),
            project_id.clone(),
            origin,
            now,
        ),
    };
    if let Some(digest) = optional_text(request.contract_digest.as_deref()) {
        case = case.with_contract_digest(digest);
    }
    if let Some(raw) = optional_text(request.completion_conditions_json.as_deref()) {
        let conditions: serde_json::Value = serde_json::from_str(&raw).map_err(|error| {
            OpError::invalid(
                "invalid_completion_conditions",
                format!("completion_conditions_json must be valid JSON: {error}"),
            )
        })?;
        case.completion_conditions = Some(conditions);
    }
    if let Some(priority) = request.priority {
        let capped = u8::try_from(priority).unwrap_or(WorkCase::MAX_PRIORITY);
        case.set_priority(capped);
    }
    case.labels = request
        .labels
        .into_iter()
        .map(|label| label.trim().to_string())
        .filter(|label| !label.is_empty())
        .collect();

    let (case, dedup_replayed) = governance
        .cases
        .create_unique(case)
        .await
        .map_err(case_store_error)?;

    if !request.issue_mandate {
        return Ok(CreateCaseOutcome {
            case: CaseView::from_case(&case),
            mandate: None,
            dedup_replayed,
        });
    }

    if dedup_replayed {
        // Never mint a second authority for a retried create: return whatever the
        // case already holds so the caller can see it has one (or has none).
        let mandate = governance
            .policy
            .latest_mandate_for_case(case.id.as_str())
            .await
            .map_err(policy_store_error)?
            .filter(|mandate| !mandate.is_revoked() && !mandate.is_expired(now));
        return Ok(CreateCaseOutcome {
            case: CaseView::from_case(&case),
            mandate: mandate.as_ref().map(MandateView::from_mandate),
            dedup_replayed,
        });
    }

    let scopes = parse_scopes(request.mandate_scopes_json.as_deref())?;
    if scopes.is_empty() {
        return Err(OpError::invalid(
            "invalid_scopes",
            "issue_mandate was requested without scopes — a mandate that authorizes nothing makes \
             the case unable to execute",
        ));
    }
    let expires_at = decode_optional_timestamp(request.mandate_expires_at)?;
    // A human-issued mandate is the root of the delegation chain; a service
    // acting on the operator's behalf issues the system-default root.
    let origin = if principal.kind == PrincipalKind::User {
        MandateOrigin::HumanIssued
    } else {
        MandateOrigin::SystemDefault
    };
    let mandate = governance
        .policy
        .issue_mandate(
            &principal,
            case.id.as_str(),
            Some(project_id.as_str()),
            scopes,
            origin,
            expires_at,
            None,
            now,
        )
        .await
        .map_err(policy_issue_error)?;

    // Linking the mandate is what lets `open_run`'s "no mandate, no execution"
    // guard pass; an unlinked mandate would be live but unreachable.
    let (case, ()) = governance
        .cases
        .update(case.id.as_str(), |case| {
            case.mandate_id = Some(mandate.id.0.clone());
            Ok(())
        })
        .await
        .map_err(|error| {
            tracing::error!(
                case_id = %case.id.as_str(),
                mandate_id = %mandate.id.0,
                error = %error,
                "issued mandate could not be linked to its case"
            );
            case_store_error(error)
        })?;

    tracing::info!(
        case_id = %case.id.0,
        mandate_id = %mandate.id.0,
        grantor = %principal,
        "work case created with a mandate"
    );
    Ok(CreateCaseOutcome {
        case: CaseView::from_case(&case),
        mandate: Some(MandateView::from_mandate(&mandate)),
        dedup_replayed,
    })
}

/// Fetch one case. `None` when it does not exist (the proto reports `available`).
pub async fn get_case(
    governance: &Governance,
    request: crate::ultimate_coders::GetCaseRequest,
) -> OpResult<Option<CaseView>> {
    let case_id = required_text(&request.case_id, "case_id")?;
    Ok(governance
        .cases
        .get(&case_id)
        .await
        .map_err(case_store_error)?
        .map(|(case, _version)| CaseView::from_case(&case)))
}

/// List cases, newest activity first, non-terminal before terminal.
pub async fn list_cases(
    governance: &Governance,
    request: crate::ultimate_coders::ListCasesRequest,
) -> OpResult<ListCasesOutcome> {
    let limit = clamp_limit(if request.limit == 0 {
        DEFAULT_LIST_LIMIT
    } else {
        request.limit
    });
    let status = match optional_text(request.status.as_deref()) {
        Some(token) => Some(CaseStatus::from_str_lossy(&token).ok_or_else(|| {
            OpError::invalid("unknown_status", format!("unknown case status '{token}'"))
        })?),
        None => None,
    };
    let project = optional_text(request.project_id.as_deref());
    let thread = optional_text(request.thread_key.as_deref());

    let candidates = match &thread {
        // A thread maps to at most one case (deterministic identity), so a
        // thread filter is a lookup, not a scan.
        Some(thread) => governance
            .cases
            .find_by_thread(thread, MAX_LIST_LIMIT as usize)
            .await
            .map_err(case_store_error)?
            .map(|case| vec![(case, 0u64)])
            .unwrap_or_default(),
        None => governance
            .cases
            .list(MAX_LIST_LIMIT as usize)
            .await
            .map_err(case_store_error)?,
    };

    let mut matched: Vec<CaseView> = candidates
        .into_iter()
        .map(|(case, _version)| case)
        .filter(|case| match status {
            Some(status) => case.status == status,
            None => request.include_terminal || !case.status.is_terminal(),
        })
        .filter(|case| {
            project
                .as_deref()
                .map(|project| case.project_id == project)
                .unwrap_or(true)
        })
        .map(|case| CaseView::from_case(&case))
        .collect();

    // Most recently touched first, with terminal cases last so a finished case
    // never pushes live work off the first page.
    matched.sort_by(|left, right| {
        let left_terminal = CaseStatus::from_str_lossy(&left.status)
            .map(|status| status.is_terminal())
            .unwrap_or(false);
        let right_terminal = CaseStatus::from_str_lossy(&right.status)
            .map(|status| status.is_terminal())
            .unwrap_or(false);
        left_terminal
            .cmp(&right_terminal)
            .then_with(|| right.updated_at.cmp(&left.updated_at))
    });

    let total = matched.len() as u32;
    matched.truncate(limit);
    Ok(ListCasesOutcome {
        cases: matched,
        total,
    })
}

/// Transition a case between governance states.
///
/// The state machine lives in the domain layer ([`CaseStatus::can_transition_to`]);
/// a rejected edge is reported as `failed_precondition`, never silently ignored.
pub async fn transition_case(
    governance: &Governance,
    request: crate::ultimate_coders::TransitionCaseRequest,
) -> OpResult<CaseView> {
    let now = super::now();
    let principal = resolve_declared_principal(
        Some(request.principal_kind.as_str()),
        Some(request.principal_id.as_str()),
    )?;
    let case_id = required_text(&request.case_id, "case_id")?;
    let token = required_text(&request.status, "status")?;
    let status = CaseStatus::from_str_lossy(&token).ok_or_else(|| {
        OpError::invalid("unknown_status", format!("unknown case status '{token}'"))
    })?;

    let Some((case, _version)) = governance
        .cases
        .get(&case_id)
        .await
        .map_err(case_store_error)?
    else {
        return Err(OpError::not_found(
            "case_not_found",
            format!("case '{case_id}' does not exist"),
        ));
    };
    // Authorize against the case's own project, not a caller-supplied one: the
    // scope of the work is a property of the case.
    require(
        governance,
        &principal,
        Permission::CaseControl,
        Some(case.project_id.as_str()),
        now,
    )?;

    let updated = governance
        .cases
        .transition(&case_id, status, now)
        .await
        .map_err(case_store_error)?;
    tracing::info!(
        case_id,
        from = case.status.as_str(),
        to = %status,
        principal = %principal,
        reason = optional_text(request.reason.as_deref()).unwrap_or_default(),
        "work case transitioned"
    );
    Ok(CaseView::from_case(&updated))
}

/// Link an execution task to the case as a run.
///
/// No caller identity rides on this message (see the module docs); the case's own
/// invariants decide whether a run may start.
pub async fn open_case_run(
    governance: &Governance,
    request: crate::ultimate_coders::OpenCaseRunRequest,
) -> OpResult<CaseRunView> {
    let now = super::now();
    let case_id = required_text(&request.case_id, "case_id")?;
    let task_id = required_text(&request.task_id, "task_id")?;
    let submission_id = optional_text(request.submission_id.as_deref());
    let case = governance
        .cases
        .open_run(&case_id, &task_id, submission_id, now)
        .await
        .map_err(case_store_error)?;
    case.runs
        .iter()
        .rev()
        .find(|run| run.is_open())
        .map(CaseRunView::from_run)
        .ok_or_else(|| {
            OpError::internal(
                "run_not_recorded",
                "the store reported an opened run but none is open on the case",
            )
        })
}

/// Finish the open run of a case.
pub async fn finish_case_run(
    governance: &Governance,
    request: crate::ultimate_coders::FinishCaseRunRequest,
) -> OpResult<CaseView> {
    let now = super::now();
    let case_id = required_text(&request.case_id, "case_id")?;
    let run_id = required_text(&request.run_id, "run_id")?;
    let outcome = parse_case_run_outcome(&required_text(&request.outcome, "outcome")?)?;
    let case = governance
        .cases
        .finish_run(
            &case_id,
            &run_id,
            outcome,
            optional_text(request.detail.as_deref()),
            now,
        )
        .await
        .map_err(case_store_error)?;
    tracing::info!(
        case_id,
        run_id,
        outcome = case_run_outcome_token(outcome),
        status = case.status.as_str(),
        "case run finished"
    );
    Ok(CaseView::from_case(&case))
}

/// Issue (or narrow-delegate) a mandate for a case.
pub async fn issue_mandate(
    governance: &Governance,
    request: crate::ultimate_coders::IssueMandateRequest,
) -> OpResult<MandateView> {
    let now = super::now();
    let grantor = resolve_declared_principal(
        Some(request.principal_kind.as_str()),
        Some(request.principal_id.as_str()),
    )?;
    let case_id = required_text(&request.case_id, "case_id")?;
    let scopes = parse_scopes(Some(request.scopes_json.as_str()))?;
    if scopes.is_empty() {
        return Err(OpError::invalid(
            "invalid_scopes",
            "a mandate must carry at least one scope — an empty mandate authorizes nothing",
        ));
    }
    let Some((case, _version)) = governance
        .cases
        .get(&case_id)
        .await
        .map_err(case_store_error)?
    else {
        return Err(OpError::not_found(
            "case_not_found",
            format!("case '{case_id}' does not exist"),
        ));
    };
    // A caller acting on a policy generation older than the installed one is
    // issuing authority under rules that have since changed: refuse and let it
    // re-read policy, rather than silently widening what it believed it had.
    if let Some(provided) = request.policy_version {
        let current = governance.policy.version();
        if provided < current.0 {
            return Err(OpError::conflict(
                "stale_policy_version",
                format!(
                    "policy version v{provided} is behind the installed v{current}; re-read the \
                     policy snapshot before issuing a mandate"
                ),
            ));
        }
    }
    let expires_at = decode_optional_timestamp(request.expires_at)?;
    let delegated_from = optional_text(request.delegated_from.as_deref()).map(MandateId);
    let origin = if delegated_from.is_some() {
        MandateOrigin::CaseDelegated
    } else if grantor.kind == PrincipalKind::User {
        MandateOrigin::HumanIssued
    } else {
        MandateOrigin::SystemDefault
    };
    let mandate = governance
        .policy
        .issue_mandate(
            &grantor,
            &case_id,
            Some(case.project_id.as_str()),
            scopes,
            origin,
            expires_at,
            delegated_from.as_ref(),
            now,
        )
        .await
        .map_err(policy_issue_error)?;

    let linked_case_id = mandate.case_id.clone();
    let mandate_id = mandate.id.0.clone();
    governance
        .cases
        .update(&linked_case_id, |case| {
            case.mandate_id = Some(mandate_id.clone());
            Ok(())
        })
        .await
        .map_err(|error| {
            tracing::error!(
                case_id = %linked_case_id,
                mandate_id = %mandate_id,
                error = %error,
                "issued mandate could not be linked to its case"
            );
            case_store_error(error)
        })?;

    tracing::info!(
        mandate_id = %mandate.id.0,
        case_id = %mandate.case_id,
        grantor = %grantor,
        origin = mandate.origin.as_str(),
        "mandate issued"
    );
    Ok(MandateView::from_mandate(&mandate))
}

/// Revoke a mandate.
///
/// Revocation also clears the case's mandate link, so `open_run`'s "no mandate,
/// no execution" guard reflects the withdrawal immediately instead of waiting
/// for every caller to re-read the mandate.
pub async fn revoke_mandate(
    governance: &Governance,
    request: crate::ultimate_coders::RevokeMandateRequest,
) -> OpResult<MandateView> {
    let now = super::now();
    let revoker = resolve_declared_principal(
        Some(request.principal_kind.as_str()),
        Some(request.principal_id.as_str()),
    )?;
    let mandate_id = required_text(&request.mandate_id, "mandate_id")?;
    let reason =
        optional_text(Some(request.reason.as_str())).unwrap_or_else(|| "revoked".to_string());
    let mandate = governance
        .policy
        .revoke_mandate(&mandate_id, &revoker, &reason, now)
        .await
        .map_err(policy_revoke_error)?;

    let revoked_id = mandate.id.0.clone();
    let case_id = mandate.case_id.clone();
    if let Err(error) = governance
        .cases
        .update(&case_id, |case| {
            if case.mandate_id.as_deref() == Some(revoked_id.as_str()) {
                case.mandate_id = None;
            }
            Ok(())
        })
        .await
    {
        // The revocation stands regardless; a stale link only means `open_run`
        // keeps the case's authority check alive until the case is re-read.
        tracing::warn!(
            error = %error,
            case_id = %case_id,
            mandate_id = %revoked_id,
            "revoked mandate could not be unlinked from its case"
        );
    }
    tracing::info!(
        mandate_id = %revoked_id,
        case_id = %case_id,
        by = %revoker,
        reason,
        "mandate revoked"
    );
    Ok(MandateView::from_mandate(&mandate))
}

/// Fetch a mandate by id, or the newest one for a case.
pub async fn get_mandate(
    governance: &Governance,
    request: crate::ultimate_coders::GetMandateRequest,
) -> OpResult<Option<MandateView>> {
    if let Some(mandate_id) = optional_text(request.mandate_id.as_deref()) {
        return Ok(governance
            .policy
            .get_mandate(&mandate_id)
            .await
            .map_err(policy_store_error)?
            .map(|mandate| MandateView::from_mandate(&mandate)));
    }
    if let Some(case_id) = optional_text(request.case_id.as_deref()) {
        return Ok(governance
            .policy
            .latest_mandate_for_case(&case_id)
            .await
            .map_err(policy_store_error)?
            .map(|mandate| MandateView::from_mandate(&mandate)));
    }
    Err(OpError::invalid(
        "invalid_argument",
        "either mandate_id or case_id is required",
    ))
}

/// Stateless authorization check: permission ceiling, mandate scope, tool allow
/// table and sandbox profile. Fail-closed on every dimension.
pub async fn authorize(
    governance: &Governance,
    request: crate::ultimate_coders::AuthorizeRequest,
) -> OpResult<AuthorizeOutcome> {
    let now = super::now();
    // Here `principal_*` is the *subject* of the evaluation, not the caller, so
    // it must be explicit and it is not a declaration the transport has to
    // vouch for (the check is what refuses unverifiable kinds).
    let principal = resolve_principal(
        &required_text(&request.principal_kind, "principal_kind")?,
        &required_text(&request.principal_id, "principal_id")?,
    )
    .map_err(OpError::from)?;
    let permission_token = required_text(&request.permission, "permission")?;
    let permission = Permission::from_str_lossy(&permission_token).ok_or_else(|| {
        OpError::invalid(
            "unknown_permission",
            format!("unknown permission '{permission_token}'"),
        )
    })?;
    let project = optional_text(request.project_id.as_deref());
    let case_id = optional_text(request.case_id.as_deref());

    if let Some(provided) = request.policy_version {
        let current = governance.policy.version();
        if provided < current.0 {
            return Ok(AuthorizeOutcome::refused(
                "stale_policy_version",
                format!("policy version v{provided} is behind the installed v{current}"),
            ));
        }
    }

    let tool = optional_text(request.tool.as_deref());
    let permission_decision =
        governance
            .policy
            .authorize(&principal, permission, project.as_deref(), now);

    // A named tool must also pass the fail-closed allow table, so an unknown tool
    // is denied even when the coarse permission would be granted.
    let tool_decision = match &tool {
        Some(tool) => Some(
            governance
                .policy
                .authorize_tool(
                    &principal,
                    tool,
                    project.as_deref(),
                    case_id.as_deref(),
                    now,
                )
                .await,
        ),
        None => None,
    };

    // The mandate dimension is only evaluated when a case is named: there is no
    // meaningful "the case's mandate" otherwise, and reporting it as denied would
    // read as a failure of a check that never ran.
    let mandate_decision = match &case_id {
        Some(case_id) => match governance.policy.latest_mandate_for_case(case_id).await {
            Ok(Some(mandate)) => {
                let mandate_request = MandateRequest {
                    repo: optional_text(request.repo.as_deref()),
                    branch: optional_text(request.branch.as_deref()),
                    path: optional_text(request.path.as_deref()),
                    tool: tool.clone(),
                    action: match optional_text(request.action.as_deref()) {
                        Some(token) => Some(parse_action_class(&token).ok_or_else(|| {
                            OpError::invalid(
                                "unknown_action",
                                format!("unknown action class '{token}'"),
                            )
                        })?),
                        None => None,
                    },
                    tokens: request.tokens,
                    cost_usd: request.cost_usd,
                    wall_secs: None,
                    runs: None,
                };
                Some(
                    governance
                        .policy
                        .authorize_mandate(&mandate, &mandate_request, now),
                )
            }
            Ok(None) => Some(PolicyDecision::deny(DenyReason::MandateMissing)),
            Err(error) => return Err(policy_store_error(error)),
        },
        None => None,
    };

    let sandbox_decision = match optional_text(request.sandbox_profile_json.as_deref()) {
        Some(raw) => {
            let profile: SandboxSecurityProfile = serde_json::from_str(&raw).map_err(|error| {
                OpError::invalid(
                    "invalid_sandbox_profile",
                    format!("sandbox_profile_json must be a JSON object: {error}"),
                )
            })?;
            Some(governance.policy.validate_sandbox(&profile))
        }
        None => None,
    };

    let permission_allowed = permission_decision.is_allowed()
        && tool_decision
            .as_ref()
            .map(PolicyDecision::is_allowed)
            .unwrap_or(true);

    let mut outcome = AuthorizeOutcome::from_dimensions(
        permission_allowed,
        mandate_decision.as_ref(),
        sandbox_decision.as_ref(),
    );
    if !outcome.allowed && outcome.deny_code.is_none() {
        // The refusal came from the permission/tool dimension. The tool's own
        // decision is reported first: "unknown tool" is more actionable than
        // "missing permission", and a known tool that needs a permission it lacks
        // reports that permission anyway.
        let decision = tool_decision
            .as_ref()
            .filter(|decision| decision.is_denied())
            .or(Some(&permission_decision).filter(|decision| decision.is_denied()));
        if let Some(decision) = decision {
            outcome.deny_code = Some(super::denial_code(decision).to_string());
            outcome.deny_detail = decision.deny_reason().map(|reason| format!("{reason:?}"));
        }
    }
    Ok(outcome)
}

// ── Adapters, parsers and error mapping ────────────────────────────

/// gRPC adapter. Holds nothing but the plane; every method delegates to the free
/// functions above so the NATS bridge applies exactly the same rules.
#[derive(Clone)]
pub struct CaseServiceImpl {
    governance: Arc<Governance>,
}

impl CaseServiceImpl {
    pub fn new(governance: Arc<Governance>) -> Self {
        Self { governance }
    }

    /// The plane this service operates on (used by tests and by the bridge).
    pub fn governance(&self) -> &Arc<Governance> {
        &self.governance
    }
}

#[tonic::async_trait]
impl CaseService for CaseServiceImpl {
    async fn create_case(
        &self,
        request: Request<crate::ultimate_coders::CreateCaseRequest>,
    ) -> Result<Response<crate::ultimate_coders::CreateCaseResponse>, Status> {
        let outcome = create_case(&self.governance, request.into_inner()).await?;
        Ok(Response::new(crate::ultimate_coders::CreateCaseResponse {
            success: true,
            case: Some(outcome.case.to_proto()),
            mandate: outcome.mandate.as_ref().map(MandateView::to_proto),
            dedup_replayed: outcome.dedup_replayed,
            error: None,
        }))
    }

    async fn get_case(
        &self,
        request: Request<crate::ultimate_coders::GetCaseRequest>,
    ) -> Result<Response<crate::ultimate_coders::GetCaseResponse>, Status> {
        let case = get_case(&self.governance, request.into_inner()).await?;
        Ok(Response::new(crate::ultimate_coders::GetCaseResponse {
            available: case.is_some(),
            case: case.as_ref().map(CaseView::to_proto),
        }))
    }

    async fn list_cases(
        &self,
        request: Request<crate::ultimate_coders::ListCasesRequest>,
    ) -> Result<Response<crate::ultimate_coders::ListCasesResponse>, Status> {
        let outcome = list_cases(&self.governance, request.into_inner()).await?;
        Ok(Response::new(crate::ultimate_coders::ListCasesResponse {
            cases: outcome.cases.iter().map(CaseView::to_proto).collect(),
            total: outcome.total,
        }))
    }

    async fn transition_case(
        &self,
        request: Request<crate::ultimate_coders::TransitionCaseRequest>,
    ) -> Result<Response<crate::ultimate_coders::TransitionCaseResponse>, Status> {
        let case = transition_case(&self.governance, request.into_inner()).await?;
        Ok(Response::new(
            crate::ultimate_coders::TransitionCaseResponse {
                success: true,
                case: Some(case.to_proto()),
                error: None,
            },
        ))
    }

    async fn open_case_run(
        &self,
        request: Request<crate::ultimate_coders::OpenCaseRunRequest>,
    ) -> Result<Response<crate::ultimate_coders::OpenCaseRunResponse>, Status> {
        let run = open_case_run(&self.governance, request.into_inner()).await?;
        Ok(Response::new(crate::ultimate_coders::OpenCaseRunResponse {
            success: true,
            run: Some(run.to_proto()),
            error: None,
        }))
    }

    async fn finish_case_run(
        &self,
        request: Request<crate::ultimate_coders::FinishCaseRunRequest>,
    ) -> Result<Response<crate::ultimate_coders::FinishCaseRunResponse>, Status> {
        let case = finish_case_run(&self.governance, request.into_inner()).await?;
        Ok(Response::new(
            crate::ultimate_coders::FinishCaseRunResponse {
                success: true,
                case: Some(case.to_proto()),
                error: None,
            },
        ))
    }

    async fn issue_mandate(
        &self,
        request: Request<crate::ultimate_coders::IssueMandateRequest>,
    ) -> Result<Response<crate::ultimate_coders::IssueMandateResponse>, Status> {
        let mandate = issue_mandate(&self.governance, request.into_inner()).await?;
        Ok(Response::new(
            crate::ultimate_coders::IssueMandateResponse {
                success: true,
                mandate: Some(mandate.to_proto()),
                error: None,
            },
        ))
    }

    async fn revoke_mandate(
        &self,
        request: Request<crate::ultimate_coders::RevokeMandateRequest>,
    ) -> Result<Response<crate::ultimate_coders::RevokeMandateResponse>, Status> {
        let mandate = revoke_mandate(&self.governance, request.into_inner()).await?;
        Ok(Response::new(
            crate::ultimate_coders::RevokeMandateResponse {
                success: true,
                mandate: Some(mandate.to_proto()),
                error: None,
            },
        ))
    }

    async fn get_mandate(
        &self,
        request: Request<crate::ultimate_coders::GetMandateRequest>,
    ) -> Result<Response<crate::ultimate_coders::GetMandateResponse>, Status> {
        let mandate = get_mandate(&self.governance, request.into_inner()).await?;
        Ok(Response::new(crate::ultimate_coders::GetMandateResponse {
            available: mandate.is_some(),
            mandate: mandate.as_ref().map(MandateView::to_proto),
        }))
    }

    async fn authorize(
        &self,
        request: Request<crate::ultimate_coders::AuthorizeRequest>,
    ) -> Result<Response<crate::ultimate_coders::AuthorizeResponse>, Status> {
        let outcome = authorize(&self.governance, request.into_inner()).await?;
        Ok(Response::new(crate::ultimate_coders::AuthorizeResponse {
            allowed: outcome.allowed,
            deny_code: outcome.deny_code.clone(),
            deny_detail: outcome.deny_detail.clone(),
            permission_allowed: outcome.permission_allowed,
            mandate_allowed: outcome.mandate_allowed,
            sandbox_allowed: outcome.sandbox_allowed,
        }))
    }
}

/// Parse the JSON array of mandate scopes.
///
/// Absent / blank means "no scopes"; a malformed array is a client error, never a
/// silently empty mandate (that would authorize nothing while looking granted).
pub(super) fn parse_scopes(raw: Option<&str>) -> OpResult<Vec<MandateScope>> {
    let Some(raw) = optional_text(raw) else {
        return Ok(Vec::new());
    };
    serde_json::from_str(&raw).map_err(|error| {
        OpError::invalid(
            "invalid_scopes_json",
            format!("mandate scopes must be a JSON array of MandateScope: {error}"),
        )
    })
}

/// Wire token of a run outcome (mirrors the `snake_case` serde form).
pub(super) fn case_run_outcome_token(outcome: CaseRunOutcome) -> &'static str {
    match outcome {
        CaseRunOutcome::Succeeded => "succeeded",
        CaseRunOutcome::Failed => "failed",
        CaseRunOutcome::Cancelled => "cancelled",
        CaseRunOutcome::Superseded => "superseded",
    }
}

/// Parse a run outcome token.
pub(super) fn parse_case_run_outcome(token: &str) -> OpResult<CaseRunOutcome> {
    Ok(match token.trim().to_ascii_lowercase().as_str() {
        "succeeded" | "success" => CaseRunOutcome::Succeeded,
        "failed" | "failure" => CaseRunOutcome::Failed,
        "cancelled" | "canceled" => CaseRunOutcome::Cancelled,
        "superseded" => CaseRunOutcome::Superseded,
        other => {
            return Err(OpError::invalid(
                "unknown_outcome",
                format!("unknown run outcome '{other}'"),
            ))
        }
    })
}

/// Parse an action-class token (`read`, `write`, `execute`, …).
pub(super) fn parse_action_class(token: &str) -> Option<ActionClass> {
    Some(match token.trim().to_ascii_lowercase().as_str() {
        "read" => ActionClass::Read,
        "write" => ActionClass::Write,
        "execute" => ActionClass::Execute,
        "dispatch" => ActionClass::Dispatch,
        "push" => ActionClass::Push,
        "merge" => ActionClass::Merge,
        "external_notify" => ActionClass::ExternalNotify,
        "scale" => ActionClass::Scale,
        "admin" => ActionClass::Admin,
        _ => return None,
    })
}

/// Map a case-store failure onto the operation vocabulary.
pub(super) fn case_store_error(error: CaseStoreError) -> OpError {
    match error {
        CaseStoreError::NotFound { case_id } => {
            OpError::not_found("case_not_found", format!("case '{case_id}' does not exist"))
        }
        // A refused state-machine edge is a precondition of the current state,
        // not a malformed request — the caller asked for something real, just not
        // something possible right now.
        CaseStoreError::Transition(error) => {
            OpError::conflict("illegal_case_transition", error.to_string())
        }
        CaseStoreError::Contended { case_id } => OpError::conflict(
            "case_contended",
            format!("case '{case_id}' is contended; retry the operation"),
        ),
        CaseStoreError::Store(error) => OpError::internal("case_store_failed", error.to_string()),
    }
}

/// Map a policy-engine failure onto the operation vocabulary.
pub(super) fn policy_store_error(error: PolicyEngineError) -> OpError {
    match &error {
        PolicyEngineError::Denied(_) => OpError::internal(
            "policy_store_failed",
            format!("policy lookup failed: {error}"),
        ),
        PolicyEngineError::Store(_) => OpError::internal("policy_store_failed", error.to_string()),
        PolicyEngineError::Mandate(inner) => match inner {
            MandateStoreError::NotFound { mandate_id } => OpError::not_found(
                "mandate_not_found",
                format!("mandate '{mandate_id}' does not exist"),
            ),
            other => OpError::internal("mandate_store_failed", other.to_string()),
        },
        PolicyEngineError::UnknownMandate { mandate_id } => OpError::not_found(
            "mandate_not_found",
            format!("mandate '{mandate_id}' does not exist"),
        ),
        PolicyEngineError::Widening(detail) => {
            OpError::denied_with("mandate_widening_denied", detail.clone())
        }
        PolicyEngineError::MandateConflict { case_id, existing } => OpError::conflict(
            "mandate_conflict",
            format!(
                "case '{case_id}' already holds the live mandate '{existing}'; revoke it first"
            ),
        ),
    }
}

/// Map a mandate-issuance failure: a policy denial keeps its own deny code.
pub(super) fn policy_issue_error(error: PolicyEngineError) -> OpError {
    match &error {
        PolicyEngineError::Denied(decision) => OpError::denied(decision),
        PolicyEngineError::Widening(detail) => {
            OpError::denied_with("mandate_widening_denied", detail.clone())
        }
        PolicyEngineError::UnknownMandate { mandate_id } => OpError::not_found(
            "mandate_not_found",
            format!("parent mandate '{mandate_id}' does not exist"),
        ),
        PolicyEngineError::MandateConflict { case_id, existing } => OpError::conflict(
            "mandate_conflict",
            format!("case '{case_id}' already holds the live mandate '{existing}'"),
        ),
        other => OpError::internal("policy_failed", other.to_string()),
    }
}

/// Map a mandate-revocation failure.
pub(super) fn policy_revoke_error(error: PolicyEngineError) -> OpError {
    match &error {
        PolicyEngineError::Denied(decision) => OpError::denied(decision),
        PolicyEngineError::Mandate(MandateStoreError::NotFound { mandate_id }) => {
            OpError::not_found(
                "mandate_not_found",
                format!("mandate '{mandate_id}' does not exist"),
            )
        }
        other => OpError::internal("policy_failed", other.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ultimate_coders::CreateCaseRequest;
    use chrono::TimeZone;
    use uc_types::governance::{Grant, PolicySnapshot, PolicyVersion};

    fn at_epoch() -> DateTime<Utc> {
        Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 0).unwrap()
    }

    /// A plane whose only grant makes `alice` a full operator on `proj`.
    async fn operator_plane() -> Governance {
        let governance = Governance::in_memory();
        let mut snapshot = PolicySnapshot::empty(PolicyVersion::initial());
        for permission in [
            Permission::CaseCreate,
            Permission::CaseRead,
            Permission::CaseControl,
            Permission::MandateIssue,
            Permission::MandateRevoke,
        ] {
            snapshot = snapshot
                .with_grant(Grant::new(
                    uc_types::governance::Principal::user("alice"),
                    vec![permission],
                ))
                .expect("user grants are structurally valid");
        }
        governance.policy.install(snapshot).await.unwrap();
        governance
    }

    fn create_request(source: &str, external_ref: Option<&str>) -> CreateCaseRequest {
        CreateCaseRequest {
            title: "Fix the flaky test".to_string(),
            description: "The suite is red".to_string(),
            project_id: "proj".to_string(),
            source: source.to_string(),
            external_ref: external_ref.map(str::to_string),
            thread_key: None,
            trigger_id: None,
            priority: Some(3),
            completion_conditions_json: None,
            contract_digest: None,
            principal_kind: "user".to_string(),
            principal_id: "alice".to_string(),
            issue_mandate: false,
            mandate_scopes_json: None,
            mandate_expires_at: None,
            labels: vec!["ci".to_string(), "  ".to_string()],
        }
    }

    #[tokio::test]
    async fn case_identity_is_deterministic_for_a_thread() {
        let governance = operator_plane().await;
        let first = create_case(
            &governance,
            create_request("github", Some("acme/widgets#7")),
        )
        .await
        .unwrap();
        assert!(!first.dedup_replayed);
        let second = create_case(
            &governance,
            create_request("github", Some("acme/widgets#7")),
        )
        .await
        .unwrap();
        assert!(second.dedup_replayed);
        assert_eq!(first.case.id, second.case.id);
        assert_eq!(first.case.priority, 3);
        assert_eq!(first.case.labels, vec!["ci".to_string()]);
        assert_eq!(first.case.origin.source, "github");
    }

    #[tokio::test]
    async fn creating_a_case_requires_the_case_create_permission() {
        let governance = operator_plane().await;
        let mut request = create_request("manual", None);
        request.principal_id = "mallory".to_string();
        let error = create_case(&governance, request).await.unwrap_err();
        assert_eq!(error.code, "missing_permission");
        assert_eq!(error.status, tonic::Code::PermissionDenied);
    }

    #[tokio::test]
    async fn create_with_mandate_issues_and_links_it() {
        let governance = operator_plane().await;
        let mut request = create_request("manual", None);
        request.issue_mandate = true;
        request.mandate_scopes_json = Some(r#"[{"kind":"action","class":"read"}]"#.to_string());
        let outcome = create_case(&governance, request).await.unwrap();
        let mandate = outcome.mandate.expect("mandate issued");
        assert_eq!(
            outcome.case.mandate_id.as_deref(),
            Some(mandate.id.as_str())
        );
        assert_eq!(mandate.origin, "human_issued");
    }

    #[tokio::test]
    async fn create_with_mandate_requires_scopes() {
        let governance = operator_plane().await;
        let mut request = create_request("manual", None);
        request.issue_mandate = true;
        let error = create_case(&governance, request).await.unwrap_err();
        assert_eq!(error.code, "invalid_scopes");
    }

    #[tokio::test]
    async fn replay_returns_the_existing_mandate_instead_of_minting_another() {
        let governance = operator_plane().await;
        let mut request = create_request("github", Some("acme/widgets#9"));
        request.issue_mandate = true;
        request.mandate_scopes_json = Some(r#"[{"kind":"action","class":"read"}]"#.to_string());
        let first = create_case(&governance, request.clone()).await.unwrap();
        let second = create_case(&governance, request).await.unwrap();
        assert!(second.dedup_replayed);
        assert_eq!(
            second.mandate.map(|mandate| mandate.id),
            first.mandate.map(|mandate| mandate.id)
        );
    }

    #[tokio::test]
    async fn case_lifecycle_runs_through_verifying_to_completed() {
        let governance = operator_plane().await;
        let mut request = create_request("manual", None);
        request.issue_mandate = true;
        request.mandate_scopes_json = Some(r#"[{"kind":"action","class":"read"}]"#.to_string());
        let case = create_case(&governance, request).await.unwrap().case;

        let active = transition_case(
            &governance,
            crate::ultimate_coders::TransitionCaseRequest {
                case_id: case.id.clone(),
                status: "active".to_string(),
                reason: Some("starting".to_string()),
                principal_kind: "user".to_string(),
                principal_id: "alice".to_string(),
            },
        )
        .await
        .unwrap();
        assert_eq!(active.status, "active");

        let run = open_case_run(
            &governance,
            crate::ultimate_coders::OpenCaseRunRequest {
                case_id: case.id.clone(),
                task_id: "task-1".to_string(),
                submission_id: Some("sub-1".to_string()),
            },
        )
        .await
        .unwrap();
        assert_eq!(run.attempt, 1);
        assert_eq!(run.submission_id.as_deref(), Some("sub-1"));

        let verifying = finish_case_run(
            &governance,
            crate::ultimate_coders::FinishCaseRunRequest {
                case_id: case.id.clone(),
                run_id: run.run_id.clone(),
                outcome: "succeeded".to_string(),
                detail: None,
            },
        )
        .await
        .unwrap();
        assert_eq!(
            verifying.status, "verifying",
            "success must verify before it is declared complete"
        );

        let completed = transition_case(
            &governance,
            crate::ultimate_coders::TransitionCaseRequest {
                case_id: case.id.clone(),
                status: "completed".to_string(),
                reason: None,
                principal_kind: "user".to_string(),
                principal_id: "alice".to_string(),
            },
        )
        .await
        .unwrap();
        assert_eq!(completed.status, "completed");
        assert!(completed.runs[0].outcome.is_some());
    }

    #[tokio::test]
    async fn an_illegal_transition_is_a_failed_precondition() {
        let governance = operator_plane().await;
        let case = create_case(&governance, create_request("manual", None))
            .await
            .unwrap()
            .case;
        let error = transition_case(
            &governance,
            crate::ultimate_coders::TransitionCaseRequest {
                case_id: case.id,
                status: "completed".to_string(),
                reason: None,
                principal_kind: "user".to_string(),
                principal_id: "alice".to_string(),
            },
        )
        .await
        .unwrap_err();
        assert_eq!(error.code, "illegal_case_transition");
        assert_eq!(error.status, tonic::Code::FailedPrecondition);
    }

    #[tokio::test]
    async fn an_unknown_case_is_not_found() {
        let governance = operator_plane().await;
        let error = get_case(
            &governance,
            crate::ultimate_coders::GetCaseRequest {
                case_id: "case-missing".to_string(),
            },
        )
        .await
        .unwrap();
        assert!(error.is_none());

        let error = transition_case(
            &governance,
            crate::ultimate_coders::TransitionCaseRequest {
                case_id: "case-missing".to_string(),
                status: "active".to_string(),
                reason: None,
                principal_kind: "user".to_string(),
                principal_id: "alice".to_string(),
            },
        )
        .await
        .unwrap_err();
        assert_eq!(error.status, tonic::Code::NotFound);
    }

    #[tokio::test]
    async fn revoking_a_mandate_withdraws_the_case_authority() {
        let governance = operator_plane().await;
        let mut request = create_request("manual", None);
        request.issue_mandate = true;
        request.mandate_scopes_json = Some(r#"[{"kind":"action","class":"read"}]"#.to_string());
        let outcome = create_case(&governance, request).await.unwrap();
        let mandate = outcome.mandate.expect("mandate issued");

        let revoked = revoke_mandate(
            &governance,
            crate::ultimate_coders::RevokeMandateRequest {
                mandate_id: mandate.id.clone(),
                reason: "changed my mind".to_string(),
                principal_kind: "user".to_string(),
                principal_id: "alice".to_string(),
            },
        )
        .await
        .unwrap();
        assert!(revoked.revoked_at.is_some());
        let case = get_case(
            &governance,
            crate::ultimate_coders::GetCaseRequest {
                case_id: outcome.case.id.clone(),
            },
        )
        .await
        .unwrap()
        .expect("case exists");
        assert_eq!(case.mandate_id, None, "the case link must be withdrawn");
    }

    #[tokio::test]
    async fn authorize_reports_every_dimension_it_checked() {
        let governance = operator_plane().await;
        let outcome = authorize(
            &governance,
            crate::ultimate_coders::AuthorizeRequest {
                principal_kind: "user".to_string(),
                principal_id: "alice".to_string(),
                permission: "case_create".to_string(),
                project_id: Some("proj".to_string()),
                case_id: None,
                tool: None,
                action: None,
                repo: None,
                branch: None,
                path: None,
                tokens: None,
                cost_usd: None,
                sandbox_profile_json: None,
                policy_version: None,
            },
        )
        .await
        .unwrap();
        assert!(outcome.allowed);
        assert!(outcome.permission_allowed);
        assert!(outcome.mandate_allowed, "no case named means not checked");
        assert!(
            outcome.sandbox_allowed,
            "no profile supplied means not checked"
        );
        assert_eq!(outcome.deny_code, None);
    }

    #[tokio::test]
    async fn authorize_denies_an_unknown_tool_and_a_stale_policy_version() {
        let governance = operator_plane().await;
        let denied = authorize(
            &governance,
            crate::ultimate_coders::AuthorizeRequest {
                principal_kind: "user".to_string(),
                principal_id: "alice".to_string(),
                permission: "task_submit".to_string(),
                project_id: None,
                case_id: None,
                tool: Some("rm_rf_everything".to_string()),
                ..Default::default()
            },
        )
        .await
        .unwrap();
        assert!(!denied.allowed);
        assert!(!denied.permission_allowed);
        assert_eq!(denied.deny_code.as_deref(), Some("tool_not_permitted"));

        let stale = authorize(
            &governance,
            crate::ultimate_coders::AuthorizeRequest {
                principal_kind: "user".to_string(),
                principal_id: "alice".to_string(),
                permission: "case_create".to_string(),
                policy_version: Some(0),
                ..Default::default()
            },
        )
        .await
        .unwrap();
        assert!(!stale.allowed);
        assert_eq!(stale.deny_code.as_deref(), Some("stale_policy_version"));
    }

    #[tokio::test]
    async fn authorize_checks_the_mandate_when_a_case_is_named() {
        let governance = operator_plane().await;
        let mut request = create_request("manual", None);
        request.issue_mandate = true;
        request.mandate_scopes_json = Some(r#"[{"kind":"tool","name":"Read"}]"#.to_string());
        let outcome = create_case(&governance, request).await.unwrap();

        let allowed = authorize(
            &governance,
            crate::ultimate_coders::AuthorizeRequest {
                principal_kind: "user".to_string(),
                principal_id: "alice".to_string(),
                permission: "task_submit".to_string(),
                project_id: None,
                case_id: Some(outcome.case.id.clone()),
                tool: Some("Read".to_string()),
                ..Default::default()
            },
        )
        .await
        .unwrap();
        assert!(allowed.mandate_allowed, "{allowed:?}");

        let denied = authorize(
            &governance,
            crate::ultimate_coders::AuthorizeRequest {
                principal_kind: "user".to_string(),
                principal_id: "alice".to_string(),
                permission: "task_submit".to_string(),
                project_id: None,
                case_id: Some(outcome.case.id.clone()),
                tool: Some("Bash".to_string()),
                ..Default::default()
            },
        )
        .await
        .unwrap();
        assert!(!denied.mandate_allowed);
        assert_eq!(denied.deny_code.as_deref(), Some("mandate_out_of_scope"));
    }

    #[test]
    fn run_outcome_tokens_round_trip() {
        for outcome in [
            CaseRunOutcome::Succeeded,
            CaseRunOutcome::Failed,
            CaseRunOutcome::Cancelled,
            CaseRunOutcome::Superseded,
        ] {
            assert_eq!(
                parse_case_run_outcome(case_run_outcome_token(outcome)).unwrap(),
                outcome
            );
        }
        assert_eq!(
            parse_case_run_outcome("nonsense").unwrap_err().code,
            "unknown_outcome"
        );
    }

    #[test]
    fn scopes_default_to_empty_and_reject_malformed_json() {
        assert!(parse_scopes(None).unwrap().is_empty());
        assert!(parse_scopes(Some("  ")).unwrap().is_empty());
        assert_eq!(
            parse_scopes(Some("[{")).unwrap_err().code,
            "invalid_scopes_json"
        );
        assert_eq!(
            parse_scopes(Some(r#"[{"kind":"action","class":"read"}]"#))
                .unwrap()
                .len(),
            1
        );
    }

    #[test]
    fn timestamps_encode_as_unix_seconds_for_proto() {
        assert_eq!(encode_timestamp(at_epoch()), at_epoch().timestamp());
        assert_eq!(encode_optional_timestamp(None), None);
    }
}
