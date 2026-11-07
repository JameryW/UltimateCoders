//! `ProactiveService` — bounded autonomy: modes, proposals, the audit trail and
//! the deterministic watchdog.
//!
//! The rule this service enforces is one line long: **the model may suggest, the
//! control plane decides**. Concretely:
//!
//! * the mode defaults to `off` and only an operator can raise it; raising it to
//!   `execute` is authority-granting (`proactive_execute`), so an agent or worker
//!   principal can never do it ([`PrincipalKind::may_hold`]);
//! * a proposal is only *recorded* — it carries no execution rights. Execution is a
//!   separate admission that requires an approved proposal **and** `execute` mode;
//! * every gate decision, allowed or denied, is written to the audit trail by the
//!   engine, so a false trigger can be reviewed after the fact.
//!
//! `ReportStall` is the one input that arrives from a non-operator principal (the
//! Python progress detector). It cannot grant anything: it produces a bounded
//! verdict, and an escalation becomes an attention item the operator sees.

use std::sync::Arc;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use tonic::{Request, Response, Status};
use uc_engine::governance::{Governance, ProactiveGateError};
use uc_types::governance::{
    AttentionItem, AttentionKind, Permission, ProactiveAuditEntry, ProactiveMode,
    ProactiveProposal, ProposalDecision, ProposalKind, ProposalStatus, WatchdogSignal,
    WatchdogVerdict,
};

use super::{
    clamp_limit, encode_optional_timestamp, encode_timestamp, optional_text, required_text,
    OpError, OpResult, MAX_LIST_LIMIT,
};
use crate::governance::policy_interceptor::{require, resolve_declared_principal};
use crate::ultimate_coders::proactive_service_server::ProactiveService;

/// Wire form of the proactive gate state plus its derived counters.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProactiveStateView {
    pub mode: String,
    pub max_open_proposals: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_tokens_per_action: Option<u64>,
    pub always_require_approval: bool,
    pub open_proposals: u32,
    /// Proposals ever recorded.
    pub evaluated_count: u64,
    pub approved_count: u64,
    pub rejected_count: u64,
    pub executed_count: u64,
}

impl ProactiveStateView {
    fn to_proto(&self) -> crate::ultimate_coders::ProactiveStateProto {
        crate::ultimate_coders::ProactiveStateProto {
            mode: self.mode.clone(),
            max_open_proposals: self.max_open_proposals,
            max_tokens_per_action: self.max_tokens_per_action,
            always_require_approval: self.always_require_approval,
            open_proposals: self.open_proposals,
            evaluated_count: self.evaluated_count,
            approved_count: self.approved_count,
            rejected_count: self.rejected_count,
            executed_count: self.executed_count,
        }
    }
}

/// Wire form of [`ProactiveProposal`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ProposalView {
    pub id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub case_id: Option<String>,
    pub kind: String,
    pub title: String,
    pub rationale: String,
    pub confidence: f32,
    #[serde(default)]
    pub evidence: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub proposed_request: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub project_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub estimated_tokens: Option<u64>,
    pub mode_at_creation: String,
    pub status: String,
    pub created_at: DateTime<Utc>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub decided_at: Option<DateTime<Utc>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub decided_by: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub executed_case_id: Option<String>,
}

impl ProposalView {
    fn from_proposal(proposal: &ProactiveProposal) -> Self {
        Self {
            id: proposal.id.clone(),
            case_id: proposal.case_id.clone(),
            kind: proposal.kind.as_str().to_string(),
            title: proposal.title.clone(),
            rationale: proposal.rationale.clone(),
            confidence: proposal.confidence,
            evidence: proposal.evidence.clone(),
            proposed_request: proposal.proposed_request.clone(),
            project_id: proposal.project_id.clone(),
            estimated_tokens: proposal.estimated_tokens,
            mode_at_creation: proposal.mode_at_creation.as_str().to_string(),
            status: proposal.status.as_str().to_string(),
            created_at: proposal.created_at,
            decided_at: proposal.decided_at,
            decided_by: proposal.decided_by.clone(),
            executed_case_id: proposal.executed_case_id.clone(),
        }
    }

    fn to_proto(&self) -> crate::ultimate_coders::ProposalProto {
        crate::ultimate_coders::ProposalProto {
            id: self.id.clone(),
            case_id: self.case_id.clone(),
            kind: self.kind.clone(),
            title: self.title.clone(),
            rationale: self.rationale.clone(),
            confidence: self.confidence,
            evidence: self.evidence.clone(),
            proposed_request: self.proposed_request.clone(),
            project_id: self.project_id.clone(),
            estimated_tokens: self.estimated_tokens,
            mode_at_creation: self.mode_at_creation.clone(),
            status: self.status.clone(),
            created_at: encode_timestamp(self.created_at),
            decided_at: encode_optional_timestamp(self.decided_at),
            decided_by: self.decided_by.clone(),
            executed_case_id: self.executed_case_id.clone(),
        }
    }
}

/// Result of [`submit_proposal`]: the gate's answer plus the recorded proposal.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SubmitProposalOutcome {
    pub accepted: bool,
    pub proposal: ProposalView,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub deny_code: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub deny_detail: Option<String>,
}

/// Wire form of [`ProactiveAuditEntry`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AuditEntryView {
    pub at: DateTime<Utc>,
    pub mode: String,
    pub action: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub proposal_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub case_id: Option<String>,
    pub allowed: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

impl AuditEntryView {
    fn from_entry(entry: &ProactiveAuditEntry) -> Self {
        Self {
            at: entry.at,
            mode: entry.mode.as_str().to_string(),
            action: entry.action.clone(),
            proposal_id: entry.proposal_id.clone(),
            case_id: entry.case_id.clone(),
            allowed: entry.allowed,
            reason: entry.reason.clone(),
            detail: entry.detail.clone(),
        }
    }

    fn to_proto(&self) -> crate::ultimate_coders::AuditEntryProto {
        crate::ultimate_coders::AuditEntryProto {
            at: encode_timestamp(self.at),
            mode: self.mode.clone(),
            action: self.action.clone(),
            proposal_id: self.proposal_id.clone(),
            case_id: self.case_id.clone(),
            allowed: self.allowed,
            reason: self.reason.clone(),
            detail: self.detail.clone(),
        }
    }
}

/// Result of [`report_stall`]: the bounded watchdog verdict.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StallVerdictView {
    /// `continue | retry_same_contract | replan | escalate_needs_human | stop`
    pub verdict: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub attempt: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub delay_ms: Option<u64>,
    #[serde(default)]
    pub preserve_effects: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    pub replans_remaining: u32,
    pub attempts_remaining: u32,
}

impl StallVerdictView {
    fn to_proto(&self) -> crate::ultimate_coders::ReportStallResponse {
        crate::ultimate_coders::ReportStallResponse {
            success: true,
            verdict: self.verdict.clone(),
            attempt: self.attempt,
            delay_ms: self.delay_ms,
            preserve_effects: self.preserve_effects.clone(),
            reason: self.reason.clone(),
            replans_remaining: self.replans_remaining,
            attempts_remaining: self.attempts_remaining,
            error: None,
        }
    }
}

/// The gate state, with the counters derived from the proposal store.
///
/// The counters are derived rather than stored so a restart cannot reset them to
/// zero while the proposals themselves survive.
pub async fn get_state(governance: &Governance) -> OpResult<ProactiveStateView> {
    let state = governance.proactive.state();
    let open_proposals = governance
        .proactive
        .open_count()
        .await
        .map_err(proactive_error)?;
    let proposals = governance
        .proactive
        .list(None, None, MAX_LIST_LIMIT as usize)
        .await
        .map_err(proactive_error)?;
    let count = |status: ProposalStatus| -> u64 {
        proposals
            .iter()
            .filter(|proposal| proposal.status == status)
            .count() as u64
    };
    Ok(ProactiveStateView {
        mode: state.mode.as_str().to_string(),
        max_open_proposals: state.max_open_proposals,
        max_tokens_per_action: state.max_tokens_per_action,
        always_require_approval: state.always_require_approval,
        open_proposals,
        evaluated_count: proposals.len() as u64,
        approved_count: count(ProposalStatus::Approved),
        rejected_count: count(ProposalStatus::Rejected),
        executed_count: count(ProposalStatus::Executed),
    })
}

/// Change the autonomy ceiling.
///
/// Raising it to `execute` is authority-granting (`proactive_execute`); any other
/// mode only changes what is recorded and shown (`proactive_propose`).
pub async fn set_mode(
    governance: &Governance,
    request: crate::ultimate_coders::SetProactiveModeRequest,
) -> OpResult<ProactiveStateView> {
    let now = super::now();
    let principal = resolve_declared_principal(
        optional_text(Some(request.principal_kind.as_str())).as_deref(),
        optional_text(Some(request.principal_id.as_str())).as_deref(),
    )?;
    let mode_token = required_text(&request.mode, "mode")?;
    // An explicit API call has no "unset" reading: a blank or unknown token is a
    // typo, and coercing it to a valid mode would make the operator believe
    // something happened that did not.
    let mode = ProactiveMode::from_str_lossy(&mode_token).ok_or_else(|| {
        OpError::invalid(
            "invalid_mode",
            format!(
                "unknown proactive mode '{mode_token}' (expected off | observe | propose | execute)"
            ),
        )
    })?;
    let permission = if mode.allows_execution() {
        Permission::ProactiveExecute
    } else {
        Permission::ProactivePropose
    };
    require(governance, &principal, permission, None, now)?;

    governance
        .proactive
        .set_mode(
            mode,
            request.max_open_proposals,
            request.max_tokens_per_action,
            request.always_require_approval,
        )
        .await
        .map_err(proactive_error)?;
    tracing::info!(
        mode = mode.as_str(),
        principal = %principal,
        reason = optional_text(request.reason.as_deref()).unwrap_or_default(),
        "proactive mode changed"
    );
    get_state(governance).await
}

/// Submit one evaluated proposal to the gate.
///
/// A refusal is a normal answer (`accepted = false` plus the deny code), not a
/// transport error: the evaluator needs the code to record why it was refused.
pub async fn submit_proposal(
    governance: &Governance,
    request: crate::ultimate_coders::SubmitProposalRequest,
) -> OpResult<SubmitProposalOutcome> {
    let now = super::now();
    let principal = resolve_declared_principal(
        optional_text(Some(request.principal_kind.as_str())).as_deref(),
        optional_text(Some(request.principal_id.as_str())).as_deref(),
    )?;
    let project_id = optional_text(request.project_id.as_deref());
    require(
        governance,
        &principal,
        Permission::ProactivePropose,
        project_id.as_deref(),
        now,
    )?;

    let kind_token = required_text(&request.kind, "kind")?;
    let kind = parse_proposal_kind(&kind_token).ok_or_else(|| {
        OpError::invalid(
            "invalid_kind",
            format!("unknown proposal kind '{kind_token}'"),
        )
    })?;
    let title = required_text(&request.title, "title")?;
    let rationale = required_text(&request.rationale, "rationale")?;

    let mut proposal =
        ProactiveProposal::new(kind, title, rationale, governance.proactive.mode(), now);
    proposal.case_id = optional_text(request.case_id.as_deref());
    // Confidence is advisory; clamp instead of refusing so an over-confident
    // evaluator (1.01 from float noise) still records its suggestion.
    proposal.confidence = if request.confidence.is_nan() {
        0.0
    } else {
        request.confidence.clamp(0.0, 1.0)
    };
    proposal.evidence = request
        .evidence
        .into_iter()
        .map(|item| item.trim().to_string())
        .filter(|item| !item.is_empty())
        .collect();
    proposal.proposed_request = optional_text(request.proposed_request.as_deref());
    proposal.project_id = project_id;
    proposal.estimated_tokens = request.estimated_tokens;

    let open_proposals = governance
        .proactive
        .open_count()
        .await
        .map_err(proactive_error)?;
    let (admission, proposal) = governance
        .proactive
        .submit(proposal, open_proposals, now)
        .await
        .map_err(proactive_error)?;

    if admission.allowed {
        tracing::info!(
            proposal_id = %proposal.id,
            kind = proposal.kind.as_str(),
            status = proposal.status.as_str(),
            case_id = proposal.case_id.as_deref().unwrap_or(""),
            "proactive proposal admitted"
        );
    } else {
        tracing::info!(
            proposal_id = %proposal.id,
            kind = proposal.kind.as_str(),
            deny_code = admission.deny_code.as_deref().unwrap_or(""),
            deny_detail = admission.deny_detail.as_deref().unwrap_or(""),
            "proactive proposal refused"
        );
    }
    Ok(SubmitProposalOutcome {
        accepted: admission.allowed,
        proposal: ProposalView::from_proposal(&proposal),
        deny_code: admission.deny_code,
        deny_detail: admission.deny_detail,
    })
}

/// List proposals, newest first.
pub async fn list_proposals(
    governance: &Governance,
    request: crate::ultimate_coders::ListProposalsRequest,
) -> OpResult<Vec<ProposalView>> {
    let limit = clamp_limit(request.limit);
    let status = match optional_text(request.status.as_deref()) {
        Some(token) => Some(parse_proposal_status(&token).ok_or_else(|| {
            OpError::invalid(
                "invalid_status",
                format!("unknown proposal status '{token}'"),
            )
        })?),
        None => None,
    };
    let case_id = optional_text(request.case_id.as_deref());
    Ok(governance
        .proactive
        .list(status, case_id.as_deref(), limit)
        .await
        .map_err(proactive_error)?
        .iter()
        .map(ProposalView::from_proposal)
        .collect())
}

/// Record a human decision on a proposal.
///
/// Approving hands the proposal its execution right, so it needs
/// `proactive_execute`; rejecting only closes the question, so
/// `proactive_propose` is enough.
pub async fn decide_proposal(
    governance: &Governance,
    request: crate::ultimate_coders::DecideProposalRequest,
) -> OpResult<ProposalView> {
    let now = super::now();
    let principal = resolve_declared_principal(
        optional_text(Some(request.principal_kind.as_str())).as_deref(),
        optional_text(Some(request.principal_id.as_str())).as_deref(),
    )?;
    let proposal_id = required_text(&request.proposal_id, "proposal_id")?;
    let decision_token = required_text(&request.decision, "decision")?;
    let by = optional_text(Some(request.by.as_str())).unwrap_or_else(|| principal.id.clone());
    let reason = optional_text(request.reason.as_deref()).unwrap_or_default();

    let decision = match decision_token.to_ascii_lowercase().as_str() {
        "approve" => ProposalDecision::Approve { by: by.clone() },
        "reject" => ProposalDecision::Reject {
            by: by.clone(),
            reason,
        },
        other => {
            return Err(OpError::invalid(
                "invalid_decision",
                format!("unknown decision '{other}' (expected approve | reject)"),
            ))
        }
    };
    let permission = match decision {
        ProposalDecision::Approve { .. } => Permission::ProactiveExecute,
        ProposalDecision::Reject { .. } => Permission::ProactivePropose,
    };
    require(governance, &principal, permission, None, now)?;

    let proposal = governance
        .proactive
        .decide(&proposal_id, &decision, now)
        .await
        .map_err(proactive_error)?;
    tracing::info!(
        proposal_id = %proposal.id,
        status = proposal.status.as_str(),
        decided_by = proposal.decided_by.as_deref().unwrap_or(""),
        principal = %principal,
        "proactive proposal decided"
    );
    Ok(ProposalView::from_proposal(&proposal))
}

/// The audit trail: every gate result, allowed or denied, newest first.
pub async fn list_audit(
    governance: &Governance,
    request: crate::ultimate_coders::ListAuditRequest,
) -> OpResult<Vec<AuditEntryView>> {
    let limit = clamp_limit(request.limit);
    let proposal_id = optional_text(request.proposal_id.as_deref());
    let case_id = optional_text(request.case_id.as_deref());
    Ok(governance
        .proactive
        .audit(proposal_id.as_deref(), case_id.as_deref(), limit)
        .await
        .map_err(proactive_error)?
        .iter()
        .map(AuditEntryView::from_entry)
        .collect())
}

/// Turn deterministic stall evidence into a bounded watchdog verdict.
///
/// The input comes from the Python progress detector and carries no identity
/// (the proto message has none), so this is not permission-gated; the verdict
/// grants nothing by itself. An `escalate_needs_human` verdict is *projected* onto
/// the attention board so the operator sees it — that is the one side effect, and
/// it only ever adds work for a human, never authority for a machine.
pub async fn report_stall(
    governance: &Governance,
    request: crate::ultimate_coders::ReportStallRequest,
) -> OpResult<StallVerdictView> {
    let now = super::now();
    let task_id = required_text(&request.task_id, "task_id")?;
    let mut signal = WatchdogSignal::new(task_id);
    signal.subtask_id = optional_text(request.subtask_id.as_deref());
    signal.no_progress_count = request.no_progress_count;
    signal.evidence_signature = optional_text(request.evidence_signature.as_deref());
    signal.last_error = optional_text(request.last_error.as_deref());
    signal.confirmed_effect_ids = request
        .confirmed_effect_ids
        .into_iter()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .collect();
    signal.attempts_used = request.attempts_used;
    signal.replans_used = request.replans_used;

    let decision = governance
        .watchdog
        .decide(&signal, request.already_retried, now)
        .await
        .map_err(|error| OpError::internal("watchdog_failed", error.to_string()))?;

    let mut view = StallVerdictView {
        verdict: watchdog_token(&decision.verdict).to_string(),
        attempt: None,
        delay_ms: None,
        preserve_effects: Vec::new(),
        reason: None,
        replans_remaining: decision.replans_remaining,
        attempts_remaining: decision.attempts_remaining,
    };
    match &decision.verdict {
        WatchdogVerdict::Continue => {}
        WatchdogVerdict::RetrySameContract { delay_ms } => {
            view.delay_ms = Some(*delay_ms);
        }
        WatchdogVerdict::Replan {
            attempt,
            delay_ms,
            preserve_effects,
        } => {
            view.attempt = Some(*attempt);
            view.delay_ms = Some(*delay_ms);
            view.preserve_effects = preserve_effects.clone();
        }
        WatchdogVerdict::EscalateNeedsHuman { reason } => {
            view.reason = Some(reason.clone());
            escalate_to_attention(governance, request.case_id.as_deref(), &signal, reason, now)
                .await;
        }
        WatchdogVerdict::Stop { reason } => {
            view.reason = Some(reason.clone());
        }
    }

    tracing::info!(
        task_id = %signal.task_id,
        no_progress_count = signal.no_progress_count,
        verdict = view.verdict,
        attempts_remaining = view.attempts_remaining,
        replans_remaining = view.replans_remaining,
        "watchdog stall report decided"
    );
    Ok(view)
}

/// Put an escalation on the operator's worklist.
///
/// Best-effort by design: a failed projection must not fail the stall report (the
/// verdict is what the caller acts on), but it is logged loudly because a silent
/// escalation is exactly the failure the attention board exists to prevent.
async fn escalate_to_attention(
    governance: &Governance,
    case_id: Option<&str>,
    signal: &WatchdogSignal,
    reason: &str,
    now: DateTime<Utc>,
) {
    let Some(case_id) = optional_text(case_id) else {
        tracing::warn!(
            task_id = %signal.task_id,
            reason,
            "watchdog escalated but the report carried no case_id; nothing to raise"
        );
        return;
    };
    let item = AttentionItem::new(
        uuid::Uuid::new_v4().to_string(),
        case_id.clone(),
        AttentionKind::NeedsHuman,
        format!("Task {} is stalled: {reason}", signal.task_id),
        now,
    )
    .with_detail(
        signal
            .last_error
            .clone()
            .unwrap_or_else(|| "no further detail from the progress detector".to_string()),
    )
    .with_human_action("decide whether to replan, retry with a narrower contract, or stop");
    match governance.attention.raise(item, now).await {
        Ok((item, superseded)) => tracing::info!(
            attention_id = %item.id,
            case_id = %item.case_id,
            superseded = superseded.as_deref().unwrap_or(""),
            "watchdog escalation raised an attention item"
        ),
        Err(error) => tracing::warn!(
            error = %error,
            case_id = %case_id,
            "watchdog escalation could not be projected onto the attention board"
        ),
    }
}

/// Parse a proposal-kind wire token.
fn parse_proposal_kind(token: &str) -> Option<ProposalKind> {
    Some(match token.trim().to_ascii_lowercase().as_str() {
        "discovered_followup" => ProposalKind::DiscoveredFollowup,
        "unblock_stalled" => ProposalKind::UnblockStalled,
        "fix_failing_ci" => ProposalKind::FixFailingCi,
        "maintenance_debt" => ProposalKind::MaintenanceDebt,
        "investigate_alert" => ProposalKind::InvestigateAlert,
        "replan" => ProposalKind::Replan,
        _ => return None,
    })
}

/// Parse a proposal-status wire token.
fn parse_proposal_status(token: &str) -> Option<ProposalStatus> {
    Some(match token.trim().to_ascii_lowercase().as_str() {
        "observed" => ProposalStatus::Observed,
        "proposed" => ProposalStatus::Proposed,
        "approved" => ProposalStatus::Approved,
        "rejected" => ProposalStatus::Rejected,
        "executed" => ProposalStatus::Executed,
        "expired" => ProposalStatus::Expired,
        "failed" => ProposalStatus::Failed,
        _ => return None,
    })
}

/// Wire token of a watchdog verdict (mirrors `WatchdogRecord::verdict`).
fn watchdog_token(verdict: &WatchdogVerdict) -> &'static str {
    match verdict {
        WatchdogVerdict::Continue => "continue",
        WatchdogVerdict::RetrySameContract { .. } => "retry_same_contract",
        WatchdogVerdict::Replan { .. } => "replan",
        WatchdogVerdict::EscalateNeedsHuman { .. } => "escalate_needs_human",
        WatchdogVerdict::Stop { .. } => "stop",
    }
}

/// Map a proactive-gate failure onto the operation vocabulary.
fn proactive_error(error: ProactiveGateError) -> OpError {
    match &error {
        ProactiveGateError::NotFound { proposal_id } => OpError::not_found(
            "proposal_not_found",
            format!("proposal '{proposal_id}' does not exist"),
        ),
        ProactiveGateError::NotActionable {
            proposal_id,
            status,
        } => OpError::conflict(
            "proposal_not_actionable",
            format!("proposal '{proposal_id}' is {status} and cannot be changed"),
        ),
        ProactiveGateError::Contended => OpError::conflict(
            "proactive_gate_contended",
            "the proactive gate is contended; retry the operation",
        ),
        ProactiveGateError::Store(_) => {
            OpError::internal("proactive_store_failed", error.to_string())
        }
    }
}

/// gRPC adapter over the operations above.
#[derive(Clone)]
pub struct ProactiveServiceImpl {
    governance: Arc<Governance>,
}

impl ProactiveServiceImpl {
    pub fn new(governance: Arc<Governance>) -> Self {
        Self { governance }
    }

    pub fn governance(&self) -> &Arc<Governance> {
        &self.governance
    }
}

#[tonic::async_trait]
impl ProactiveService for ProactiveServiceImpl {
    async fn get_proactive_state(
        &self,
        _request: Request<crate::ultimate_coders::GetProactiveStateRequest>,
    ) -> Result<Response<crate::ultimate_coders::GetProactiveStateResponse>, Status> {
        let state = get_state(&self.governance).await?;
        Ok(Response::new(
            crate::ultimate_coders::GetProactiveStateResponse {
                state: Some(state.to_proto()),
            },
        ))
    }

    async fn set_proactive_mode(
        &self,
        request: Request<crate::ultimate_coders::SetProactiveModeRequest>,
    ) -> Result<Response<crate::ultimate_coders::SetProactiveModeResponse>, Status> {
        let state = set_mode(&self.governance, request.into_inner()).await?;
        Ok(Response::new(
            crate::ultimate_coders::SetProactiveModeResponse {
                success: true,
                state: Some(state.to_proto()),
                error: None,
            },
        ))
    }

    async fn submit_proposal(
        &self,
        request: Request<crate::ultimate_coders::SubmitProposalRequest>,
    ) -> Result<Response<crate::ultimate_coders::SubmitProposalResponse>, Status> {
        let outcome = submit_proposal(&self.governance, request.into_inner()).await?;
        Ok(Response::new(
            crate::ultimate_coders::SubmitProposalResponse {
                success: true,
                accepted: outcome.accepted,
                proposal: Some(outcome.proposal.to_proto()),
                deny_code: outcome.deny_code,
                deny_detail: outcome.deny_detail,
                error: None,
            },
        ))
    }

    async fn list_proposals(
        &self,
        request: Request<crate::ultimate_coders::ListProposalsRequest>,
    ) -> Result<Response<crate::ultimate_coders::ListProposalsResponse>, Status> {
        let proposals = list_proposals(&self.governance, request.into_inner()).await?;
        Ok(Response::new(
            crate::ultimate_coders::ListProposalsResponse {
                proposals: proposals.iter().map(ProposalView::to_proto).collect(),
            },
        ))
    }

    async fn decide_proposal(
        &self,
        request: Request<crate::ultimate_coders::DecideProposalRequest>,
    ) -> Result<Response<crate::ultimate_coders::DecideProposalResponse>, Status> {
        let proposal = decide_proposal(&self.governance, request.into_inner()).await?;
        Ok(Response::new(
            crate::ultimate_coders::DecideProposalResponse {
                success: true,
                proposal: Some(proposal.to_proto()),
                error: None,
            },
        ))
    }

    async fn report_stall(
        &self,
        request: Request<crate::ultimate_coders::ReportStallRequest>,
    ) -> Result<Response<crate::ultimate_coders::ReportStallResponse>, Status> {
        let verdict = report_stall(&self.governance, request.into_inner()).await?;
        Ok(Response::new(verdict.to_proto()))
    }

    async fn list_audit(
        &self,
        request: Request<crate::ultimate_coders::ListAuditRequest>,
    ) -> Result<Response<crate::ultimate_coders::ListAuditResponse>, Status> {
        let entries = list_audit(&self.governance, request.into_inner()).await?;
        Ok(Response::new(crate::ultimate_coders::ListAuditResponse {
            entries: entries.iter().map(AuditEntryView::to_proto).collect(),
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use uc_types::governance::{
        CaseOrigin, Grant, PolicySnapshot, PolicyVersion, PrincipalKind, WorkCase,
    };

    async fn plane() -> Governance {
        let governance = Governance::in_memory();
        let mut snapshot = PolicySnapshot::empty(PolicyVersion::initial());
        snapshot = snapshot
            .with_grant(Grant::new(
                uc_types::governance::Principal::user("alice"),
                vec![
                    Permission::ProactivePropose,
                    Permission::ProactiveExecute,
                    Permission::CaseCreate,
                ],
            ))
            .expect("user grants are structurally valid");
        governance.policy.install(snapshot).await.unwrap();
        governance
    }

    fn proposal(evidence: &[&str]) -> crate::ultimate_coders::SubmitProposalRequest {
        crate::ultimate_coders::SubmitProposalRequest {
            case_id: None,
            kind: "fix_failing_ci".to_string(),
            title: "Fix CI run run-42".to_string(),
            rationale: "CI run run-42 concluded failure".to_string(),
            confidence: 0.7,
            evidence: evidence.iter().map(|item| item.to_string()).collect(),
            proposed_request: Some("Fix the failing test".to_string()),
            project_id: Some("proj".to_string()),
            estimated_tokens: Some(50_000),
            principal_kind: "user".to_string(),
            principal_id: "alice".to_string(),
        }
    }

    fn mode_request(mode: &str) -> crate::ultimate_coders::SetProactiveModeRequest {
        crate::ultimate_coders::SetProactiveModeRequest {
            mode: mode.to_string(),
            max_open_proposals: None,
            max_tokens_per_action: None,
            always_require_approval: None,
            principal_kind: "user".to_string(),
            principal_id: "alice".to_string(),
            reason: Some("operator opt-in".to_string()),
        }
    }

    #[tokio::test]
    async fn the_default_mode_is_off_and_refuses_proposals() {
        let governance = plane().await;
        let state = get_state(&governance).await.unwrap();
        assert_eq!(state.mode, "off");
        let outcome = submit_proposal(&governance, proposal(&["ci:run-42"]))
            .await
            .unwrap();
        assert!(!outcome.accepted);
        assert_eq!(
            outcome.deny_code.as_deref(),
            Some("proactive_mode_disallows")
        );
        // The refusal was audited, which is the point of recording it.
        let audit = list_audit(
            &governance,
            crate::ultimate_coders::ListAuditRequest {
                limit: 10,
                proposal_id: None,
                case_id: None,
            },
        )
        .await
        .unwrap();
        assert!(audit.iter().any(|entry| !entry.allowed));
    }

    #[tokio::test]
    async fn propose_mode_records_without_granting_execution() {
        let governance = plane().await;
        set_mode(&governance, mode_request("propose"))
            .await
            .unwrap();
        let outcome = submit_proposal(&governance, proposal(&["ci:run-42"]))
            .await
            .unwrap();
        assert!(outcome.accepted);
        assert_eq!(outcome.proposal.status, "proposed");
        assert_eq!(outcome.proposal.mode_at_creation, "propose");
        assert!(outcome.proposal.evidence.contains(&"ci:run-42".to_string()));
    }

    #[tokio::test]
    async fn a_proposal_without_evidence_is_refused_even_in_propose_mode() {
        let governance = plane().await;
        set_mode(&governance, mode_request("propose"))
            .await
            .unwrap();
        let outcome = submit_proposal(&governance, proposal(&[])).await.unwrap();
        assert!(!outcome.accepted);
        assert_eq!(
            outcome.deny_detail.as_deref(),
            Some("ProactiveModeDisallows { mode: \"propose\", action: \"propose_without_evidence\" }")
        );
    }

    #[tokio::test]
    async fn raising_the_mode_to_execute_is_authority_granting() {
        let governance = plane().await;
        let mut request = mode_request("execute");
        request.principal_id = "mallory".to_string();
        let error = set_mode(&governance, request).await.unwrap_err();
        assert_eq!(error.code, "missing_permission");
        assert_eq!(error.status, tonic::Code::PermissionDenied);
    }

    #[tokio::test]
    async fn an_unknown_mode_is_rejected_rather_than_coerced() {
        let governance = plane().await;
        let error = set_mode(&governance, mode_request("turbo"))
            .await
            .unwrap_err();
        assert_eq!(error.code, "invalid_mode");
    }

    #[tokio::test]
    async fn approve_then_decide_records_the_operator() {
        let governance = plane().await;
        set_mode(&governance, mode_request("propose"))
            .await
            .unwrap();
        let outcome = submit_proposal(&governance, proposal(&["ci:run-42"]))
            .await
            .unwrap();
        let decided = decide_proposal(
            &governance,
            crate::ultimate_coders::DecideProposalRequest {
                proposal_id: outcome.proposal.id.clone(),
                decision: "approve".to_string(),
                by: "alice".to_string(),
                reason: None,
                principal_kind: "user".to_string(),
                principal_id: "alice".to_string(),
            },
        )
        .await
        .unwrap();
        assert_eq!(decided.status, "approved");
        assert_eq!(decided.decided_by.as_deref(), Some("alice"));

        let error = decide_proposal(
            &governance,
            crate::ultimate_coders::DecideProposalRequest {
                proposal_id: decided.id,
                decision: "approve".to_string(),
                by: "alice".to_string(),
                reason: None,
                principal_kind: "user".to_string(),
                principal_id: "alice".to_string(),
            },
        )
        .await
        .unwrap_err();
        assert_eq!(error.code, "proposal_not_actionable");
    }

    #[tokio::test]
    async fn a_stall_below_the_threshold_continues() {
        let governance = plane().await;
        let verdict = report_stall(
            &governance,
            crate::ultimate_coders::ReportStallRequest {
                task_id: "task-1".to_string(),
                subtask_id: None,
                no_progress_count: 1,
                evidence_signature: None,
                last_error: None,
                confirmed_effect_ids: Vec::new(),
                attempts_used: 1,
                replans_used: 0,
                already_retried: false,
                case_id: None,
            },
        )
        .await
        .unwrap();
        assert_eq!(verdict.verdict, "continue");
    }

    #[tokio::test]
    async fn a_stalled_task_is_retried_once_then_replanned() {
        let governance = plane().await;
        let report = |already_retried: bool| crate::ultimate_coders::ReportStallRequest {
            task_id: "task-1".to_string(),
            subtask_id: Some("sub-1".to_string()),
            no_progress_count: 3,
            evidence_signature: Some("sig".to_string()),
            last_error: Some("timeout".to_string()),
            confirmed_effect_ids: vec!["effect:push-main".to_string()],
            attempts_used: 2,
            replans_used: 0,
            already_retried,
            case_id: None,
        };
        let first = report_stall(&governance, report(false)).await.unwrap();
        assert_eq!(first.verdict, "retry_same_contract");
        assert_eq!(first.attempt, None);

        let second = report_stall(&governance, report(true)).await.unwrap();
        assert_eq!(second.verdict, "replan");
        assert_eq!(second.attempt, Some(1));
        assert_eq!(
            second.preserve_effects,
            vec!["effect:push-main".to_string()]
        );
    }

    #[tokio::test]
    async fn an_escalation_is_projected_onto_the_attention_board() {
        let governance = plane().await;
        let case = WorkCase::new(
            "case",
            "desc",
            "proj",
            CaseOrigin::manual(),
            super::super::now(),
        );
        let case = governance.cases.create_unique(case).await.unwrap().0;
        let mut report = crate::ultimate_coders::ReportStallRequest {
            task_id: "task-1".to_string(),
            subtask_id: None,
            no_progress_count: 9,
            evidence_signature: None,
            last_error: Some("same error three times".to_string()),
            confirmed_effect_ids: Vec::new(),
            attempts_used: 1,
            replans_used: 2,
            already_retried: true,
            case_id: Some(case.id.0.clone()),
        };
        // Budget exhausted: with max_replans spent the watchdog escalates.
        report.replans_used = 2;
        let verdict = report_stall(&governance, report).await.unwrap();
        assert_eq!(verdict.verdict, "escalate_needs_human");
        assert!(verdict.reason.is_some());
        let worklist = governance.attention.worklist(false, 10).await.unwrap();
        assert_eq!(worklist.len(), 1);
        assert_eq!(worklist[0].case_id, case.id.0);
        assert_eq!(worklist[0].kind, AttentionKind::NeedsHuman);
    }

    #[test]
    fn proposal_tokens_round_trip() {
        for kind in [
            ProposalKind::DiscoveredFollowup,
            ProposalKind::UnblockStalled,
            ProposalKind::FixFailingCi,
            ProposalKind::MaintenanceDebt,
            ProposalKind::InvestigateAlert,
            ProposalKind::Replan,
        ] {
            assert_eq!(parse_proposal_kind(kind.as_str()), Some(kind));
        }
        for status in [
            ProposalStatus::Observed,
            ProposalStatus::Proposed,
            ProposalStatus::Approved,
            ProposalStatus::Rejected,
            ProposalStatus::Executed,
            ProposalStatus::Expired,
            ProposalStatus::Failed,
        ] {
            assert_eq!(parse_proposal_status(status.as_str()), Some(status));
        }
    }

    #[test]
    fn the_structural_ceiling_keeps_agents_out_of_cluster_and_mandate_authority() {
        // The domain layer's ceiling: an agent may *propose* (and even hold an
        // execute grant the operator explicitly issues) but it can never mint,
        // revoke or resize — those are refused when the grant is built, not when
        // it is evaluated.
        assert!(!PrincipalKind::Agent.may_hold(Permission::MandateIssue));
        assert!(!PrincipalKind::Agent.may_hold(Permission::MandateRevoke));
        assert!(!PrincipalKind::Agent.may_hold(Permission::WorkerScale));
        assert!(PrincipalKind::Agent.may_hold(Permission::ProactivePropose));
        // And the grant builder refuses a forbidden combination outright.
        let refused = PolicySnapshot::empty(PolicyVersion::initial()).with_grant(
            uc_types::governance::Grant::new(
                uc_types::governance::Principal::agent("planner"),
                vec![Permission::WorkerScale],
            ),
        );
        assert!(refused.is_err());
    }
}
