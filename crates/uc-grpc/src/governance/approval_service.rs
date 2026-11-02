//! Approval operations: request a human decision, list what is waiting, decide.
//!
//! The three operations exist so a human-tier tool has a path other than "be an
//! operator": an agent asks, a human answers, and the authorizer spends the permit
//! exactly once (see `uc_engine::governance::ApprovalStore`).
//!
//! ## Who may do what
//!
//! | op | permission | extra gate |
//! | --- | --- | --- |
//! | `request` | `task_submit` (asking the system to act) | the requester is recorded verbatim |
//! | `list` | none (read) | — |
//! | `decide` | `proactive_execute` (operator tier) | the decider must be a `user`; the request's own author may not decide |
//!
//! The two independent gates on `decide` are deliberate: holding an operator token
//! is not the same as being the human the request is waiting on, and a service
//! principal is never a human.
//!
//! ## The worklist link
//!
//! A request also raises a `needs_human` attention item for its case, so it shows up
//! where humans already look. The item is a *pointer*: resolving it does not change
//! the approval's state, and losing it does not grant anything. The decision
//! resolves it, best effort.

use std::sync::Arc;

use chrono::{DateTime, Duration, Utc};
use serde::{Deserialize, Serialize};

use uc_engine::governance::{ApprovalStoreError, Governance};
use uc_types::governance::{
    ApprovalRequest, ApprovalState, AttentionItem, AttentionKind, AttentionResolution, Permission,
    PrincipalKind, ToolApproval,
};

use super::policy_interceptor::resolve_declared_principal;
use super::{
    clamp_limit, encode_optional_timestamp, encode_timestamp, optional_text, required_text,
    OpError, OpResult,
};
use crate::ultimate_coders::approval_service_server::ApprovalService;

/// Wire form of [`ToolApproval`]. Serialized to JSON for the NATS bridge and mapped
/// to the proto message for gRPC, so both transports carry the same fields.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ApprovalView {
    pub id: String,
    pub tool: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub case_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub project_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub action_digest: Option<String>,
    pub requested_by_kind: String,
    pub requested_by_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    pub state: String,
    pub requested_at: DateTime<Utc>,
    pub expires_at: DateTime<Utc>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub decided_at: Option<DateTime<Utc>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub decided_by_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub used_at: Option<DateTime<Utc>>,
}

impl ApprovalView {
    fn from_approval(approval: &ToolApproval) -> Self {
        Self {
            id: approval.id.clone(),
            tool: approval.request.tool.clone(),
            case_id: approval.request.case_id.clone(),
            project_id: approval.request.project_id.clone(),
            action_digest: approval.request.action_digest.clone(),
            requested_by_kind: approval.request.requested_by.kind.as_str().to_string(),
            requested_by_id: approval.request.requested_by.id.clone(),
            reason: approval.request.reason.clone(),
            state: approval.state.as_str().to_string(),
            requested_at: approval.requested_at,
            expires_at: approval.expires_at,
            decided_at: approval.decided_at,
            decided_by_id: approval
                .decided_by
                .as_ref()
                .map(|principal| principal.id.clone()),
            note: approval.note.clone(),
            used_at: approval.used_at,
        }
    }

    fn to_proto(&self) -> crate::ultimate_coders::ApprovalProto {
        crate::ultimate_coders::ApprovalProto {
            id: self.id.clone(),
            tool: self.tool.clone(),
            case_id: self.case_id.clone(),
            project_id: self.project_id.clone(),
            action_digest: self.action_digest.clone(),
            requested_by_kind: self.requested_by_kind.clone(),
            requested_by_id: self.requested_by_id.clone(),
            reason: self.reason.clone(),
            state: self.state.clone(),
            requested_at: encode_timestamp(self.requested_at),
            expires_at: encode_timestamp(self.expires_at),
            decided_at: encode_optional_timestamp(self.decided_at),
            decided_by_id: self.decided_by_id.clone(),
            note: self.note.clone(),
            used_at: encode_optional_timestamp(self.used_at),
        }
    }
}

/// Result of [`request_approval`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RequestApprovalOutcome {
    pub approval: ApprovalView,
    /// False when the same request already existed (an idempotent retry).
    pub created: bool,
}

fn approval_error(error: ApprovalStoreError) -> OpError {
    match error {
        ApprovalStoreError::Refused(refusal) => {
            OpError::denied_with("approval_refused", refusal.to_string())
        }
        ApprovalStoreError::NotFound { id } => OpError::not_found(
            "approval_not_found",
            format!("approval '{id}' does not exist"),
        ),
        ApprovalStoreError::Store(error) => {
            OpError::internal("approval_store_failed", error.to_string())
        }
    }
}

/// Ask a human to allow one tool action.
pub async fn request_approval(
    governance: &Governance,
    request: crate::ultimate_coders::RequestApprovalRequest,
) -> OpResult<RequestApprovalOutcome> {
    let now = super::now();
    let principal = resolve_declared_principal(
        optional_text(Some(request.principal_kind.as_str())).as_deref(),
        optional_text(Some(request.principal_id.as_str())).as_deref(),
    )?;
    let tool = required_text(&request.tool, "tool")?;
    let requirement = governance.policy.tools().requirement(&tool);
    if !requirement.known {
        // An unknown tool can never be authorized, so recording a request for it
        // would only put an unanswerable question in front of a human.
        return Err(OpError::invalid(
            "unknown_tool",
            format!("tool '{tool}' is not in the policy table"),
        ));
    }
    let case_id = optional_text(request.case_id.as_deref());
    let project_id = optional_text(request.project_id.as_deref());

    if !requirement.human_approval {
        // Asking for something that does not need a human is a client bug, and
        // silently recording it would teach the caller that it did need one.
        return Err(OpError::invalid(
            "tool_does_not_require_approval",
            format!("tool '{tool}' does not require human approval"),
        ));
    }

    let ttl = request
        .ttl_minutes
        .map(|minutes| Duration::minutes(i64::from(minutes.max(1))));
    let approval = ToolApproval::request(
        ApprovalRequest {
            tool: tool.clone(),
            case_id: case_id.clone(),
            project_id,
            action_digest: optional_text(request.action_digest.as_deref()),
            requested_by: principal.clone(),
            reason: optional_text(request.reason.as_deref()),
        },
        now,
        ttl,
    );
    let (approval, created) = governance
        .approvals
        .request(approval)
        .await
        .map_err(approval_error)?;

    if created {
        // Best effort: the worklist is how a human finds out. A failure here must
        // not fail the request — the approval document is the authority.
        if let Some(case_id) = &case_id {
            let item = AttentionItem::new(
                format!(
                    "att-approval-{}",
                    &approval.id[approval.id.len().saturating_sub(12)..]
                ),
                case_id,
                AttentionKind::NeedsHuman,
                format!("approve {}?", approval.request.tool),
                now,
            )
            .with_detail(format!(
                "approval {} · requested by {}:{} · {}",
                approval.id,
                approval.request.requested_by.kind.as_str(),
                approval.request.requested_by.id,
                approval
                    .request
                    .reason
                    .as_deref()
                    .unwrap_or("no reason given")
            ))
            .with_human_action(format!("POST /api/approvals/{}/decide", approval.id))
            .with_priority(8);
            if let Err(error) = governance.attention.raise(item, now).await {
                tracing::warn!(error = %error, approval = %approval.id, "approval worklist item could not be raised");
            }
        }
    }

    Ok(RequestApprovalOutcome {
        approval: ApprovalView::from_approval(&approval),
        created,
    })
}

/// List approvals, newest first.
pub async fn list_approvals(
    governance: &Governance,
    request: crate::ultimate_coders::ListApprovalsRequest,
) -> OpResult<Vec<ApprovalView>> {
    let state = match optional_text(request.state.as_deref()) {
        Some(token) => Some(parse_state(&token)?),
        None => None,
    };
    let limit = clamp_limit(request.limit);
    let approvals = governance
        .approvals
        .list(state, limit)
        .await
        .map_err(approval_error)?;
    Ok(approvals.iter().map(ApprovalView::from_approval).collect())
}

fn parse_state(token: &str) -> OpResult<ApprovalState> {
    match token.trim().to_ascii_lowercase().as_str() {
        "pending" => Ok(ApprovalState::Pending),
        "approved" => Ok(ApprovalState::Approved),
        "rejected" => Ok(ApprovalState::Rejected),
        "expired" => Ok(ApprovalState::Expired),
        "used" => Ok(ApprovalState::Used),
        other => Err(OpError::invalid(
            "unknown_approval_state",
            format!("unknown approval state '{other}'"),
        )),
    }
}

/// Approve or reject a request. Humans only.
pub async fn decide_approval(
    governance: &Governance,
    request: crate::ultimate_coders::DecideApprovalRequest,
) -> OpResult<ApprovalView> {
    let now = super::now();
    let principal = resolve_declared_principal(
        optional_text(Some(request.principal_kind.as_str())).as_deref(),
        optional_text(Some(request.principal_id.as_str())).as_deref(),
    )?;
    // Gate 1: operator-tier authority.
    let decision = governance
        .policy
        .authorize(&principal, Permission::ProactiveExecute, None, now);
    if decision.is_denied() {
        return Err(OpError::denied(&decision));
    }
    // Gate 2: a human answers. A service token, however privileged, is not consent.
    if principal.kind != PrincipalKind::User {
        return Err(OpError::denied_with(
            "approval_requires_human",
            format!(
                "a {} principal cannot decide an approval; only a user can",
                principal.kind.as_str()
            ),
        ));
    }

    let id = required_text(&request.approval_id, "approval_id")?;
    let note = optional_text(request.note.as_deref());
    let approval = if request.approve {
        governance
            .approvals
            .approve(&id, &principal, note.clone(), now)
            .await
    } else {
        governance
            .approvals
            .reject(&id, &principal, note.clone(), now)
            .await
    }
    .map_err(approval_error)?;

    // Best effort: close the worklist item this request raised.
    if let Some(case_id) = &approval.request.case_id {
        if let Ok(Some(item)) = governance.attention.open_for_case(case_id).await {
            if item.kind == AttentionKind::NeedsHuman
                && item
                    .detail
                    .as_deref()
                    .is_some_and(|detail| detail.contains(&approval.id))
            {
                if let Err(error) = governance
                    .attention
                    .resolve(
                        &item.id,
                        AttentionResolution::HumanDecision {
                            by: principal.id.clone(),
                            answer: approval.state.as_str().to_string(),
                        },
                        now,
                    )
                    .await
                {
                    tracing::warn!(error = %error, "approval worklist item could not be resolved");
                }
            }
        }
    }

    Ok(ApprovalView::from_approval(&approval))
}

/// gRPC adapter over the operations above.
#[derive(Clone)]
pub struct ApprovalServiceImpl {
    governance: Arc<Governance>,
}

impl ApprovalServiceImpl {
    pub fn new(governance: Arc<Governance>) -> Self {
        Self { governance }
    }

    pub fn governance(&self) -> &Arc<Governance> {
        &self.governance
    }
}

#[tonic::async_trait]
impl ApprovalService for ApprovalServiceImpl {
    async fn request_approval(
        &self,
        request: tonic::Request<crate::ultimate_coders::RequestApprovalRequest>,
    ) -> Result<tonic::Response<crate::ultimate_coders::RequestApprovalResponse>, tonic::Status>
    {
        let outcome = request_approval(&self.governance, request.into_inner()).await?;
        Ok(tonic::Response::new(
            crate::ultimate_coders::RequestApprovalResponse {
                success: true,
                approval: Some(outcome.approval.to_proto()),
                created: outcome.created,
                error: None,
            },
        ))
    }

    async fn list_approvals(
        &self,
        request: tonic::Request<crate::ultimate_coders::ListApprovalsRequest>,
    ) -> Result<tonic::Response<crate::ultimate_coders::ListApprovalsResponse>, tonic::Status> {
        let approvals = list_approvals(&self.governance, request.into_inner()).await?;
        Ok(tonic::Response::new(
            crate::ultimate_coders::ListApprovalsResponse {
                approvals: approvals.iter().map(ApprovalView::to_proto).collect(),
            },
        ))
    }

    async fn decide_approval(
        &self,
        request: tonic::Request<crate::ultimate_coders::DecideApprovalRequest>,
    ) -> Result<tonic::Response<crate::ultimate_coders::DecideApprovalResponse>, tonic::Status>
    {
        let approval = decide_approval(&self.governance, request.into_inner()).await?;
        Ok(tonic::Response::new(
            crate::ultimate_coders::DecideApprovalResponse {
                success: true,
                approval: Some(approval.to_proto()),
                error: None,
            },
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use uc_types::governance::Principal;

    fn request(tool: &str) -> crate::ultimate_coders::RequestApprovalRequest {
        crate::ultimate_coders::RequestApprovalRequest {
            tool: tool.to_string(),
            case_id: None,
            project_id: Some("proj".to_string()),
            action_digest: None,
            reason: Some("the queue is wedged".to_string()),
            principal_kind: "agent".to_string(),
            principal_id: "executor".to_string(),
            ttl_minutes: None,
        }
    }

    fn governance() -> Governance {
        let records: Arc<dyn uc_engine::governance::RecordStore> =
            Arc::new(uc_engine::governance::InMemoryRecordStore::new());
        Governance::new(records)
    }

    #[tokio::test]
    async fn requesting_an_unknown_tool_is_refused_before_anything_is_recorded() {
        let governance = governance();
        let error = request_approval(&governance, request("rm_rf"))
            .await
            .expect_err("unknown tools cannot be requested");
        assert_eq!(error.code, "unknown_tool");
        assert!(governance
            .approvals
            .list(None, 10)
            .await
            .unwrap()
            .is_empty());
    }

    #[tokio::test]
    async fn requesting_an_ordinary_tool_is_refused() {
        let governance = governance();
        let error = request_approval(&governance, request("Read"))
            .await
            .expect_err("a tool that does not need approval cannot be requested");
        assert_eq!(error.code, "tool_does_not_require_approval");
    }

    #[tokio::test]
    async fn a_human_tier_tool_can_be_requested_and_listed() {
        let governance = governance();
        let outcome = request_approval(&governance, request("scale_workers"))
            .await
            .expect("scale_workers is human-tier");
        assert!(outcome.created);
        assert_eq!(outcome.approval.state, "pending");
        assert_eq!(outcome.approval.tool, "scale_workers");
        assert_eq!(outcome.approval.requested_by_kind, "agent");

        let listed = list_approvals(
            &governance,
            crate::ultimate_coders::ListApprovalsRequest {
                state: Some("pending".to_string()),
                limit: 10,
            },
        )
        .await
        .expect("list works");
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].id, outcome.approval.id);

        // A retry is idempotent rather than a second worklist question.
        let retry = request_approval(&governance, request("scale_workers"))
            .await
            .expect("retry is fine");
        assert!(!retry.created);
        assert_eq!(retry.approval.id, outcome.approval.id);
    }

    #[tokio::test]
    async fn only_a_human_with_operator_authority_can_decide() {
        let governance = governance();
        let outcome = request_approval(&governance, request("scale_workers"))
            .await
            .unwrap();
        let decide = |kind: &str, id: &str| crate::ultimate_coders::DecideApprovalRequest {
            approval_id: outcome.approval.id.clone(),
            approve: true,
            note: None,
            principal_kind: kind.to_string(),
            principal_id: id.to_string(),
        };

        // No grants at all: the empty snapshot denies everything, including the
        // operator tier.
        let denied = decide_approval(&governance, decide("user", "alice"))
            .await
            .expect_err("empty policy denies");
        assert_eq!(denied.code, "missing_permission");

        // Grant the operator tier to alice, but keep the agent out.
        let records: Arc<dyn uc_engine::governance::RecordStore> =
            Arc::new(uc_engine::governance::InMemoryRecordStore::new());
        let governance = Governance::new(records);
        let snapshot = uc_types::governance::PolicySnapshot::empty(
            uc_types::governance::PolicyVersion::initial(),
        )
        .with_grant(uc_types::governance::Grant::new(
            Principal::user("alice"),
            vec![
                Permission::ProactiveExecute,
                Permission::TaskSubmit,
                Permission::CaseCreate,
            ],
        ))
        .expect("user may hold these");
        governance.policy.install(snapshot).await.expect("install");
        let outcome = request_approval(&governance, request("scale_workers"))
            .await
            .unwrap();
        let decide = |kind: &str, id: &str| crate::ultimate_coders::DecideApprovalRequest {
            approval_id: outcome.approval.id.clone(),
            approve: true,
            note: Some("ok".to_string()),
            principal_kind: kind.to_string(),
            principal_id: id.to_string(),
        };

        let by_agent = decide_approval(&governance, decide("agent", "executor"))
            .await
            .expect_err("an agent cannot decide");
        // The structural ceiling refuses before any grant is even consulted: an
        // agent can never hold the operator permission, so it can never decide.
        assert_eq!(by_agent.code, "forbidden_principal_kind");

        let decided = decide_approval(&governance, decide("user", "alice"))
            .await
            .expect("a human with the operator tier can decide");
        assert_eq!(decided.state, "approved");
        assert_eq!(decided.decided_by_id.as_deref(), Some("alice"));
        assert_eq!(decided.note.as_deref(), Some("ok"));
    }
}
