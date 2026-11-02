//! Proactive evaluation and the watchdog — bounded autonomy.
//!
//! Two related concerns live here because they share one hard rule: **the model
//! may suggest, the control plane decides**.
//!
//! * [`ProactiveMode`] gates whether a suggestion may even be recorded
//!   (`Observe`), shown (`Propose`), or executed (`Execute`). The default is
//!   `Off`, and `Observe` is the only mode the plan recommends enabling without
//!   a measurement phase. A suggestion carries no authority of its own: it is
//!   admitted only through the same [`Mandate`](super::mandate::Mandate) and
//!   [`Permission`](super::policy::Permission) checks as any other work.
//! * [`WatchdogConfig`] turns "this task is stalled" into a *bounded* decision.
//!   The deterministic inputs come from the existing Python `progress.py`
//!   detector (evidence-delta stall signatures); this module only decides what
//!   to do about them. Replans are capped, and a replan carries the list of
//!   already-confirmed external effects so recovery does not repeat them.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

use super::policy::{DenyReason, PolicyDecision};

/// Record-store namespace for proactive proposals.
pub const PROACTIVE_NAMESPACE: &str = "proactive_proposal";
/// Record-store namespace for the audit trail.
pub const PROACTIVE_AUDIT_NAMESPACE: &str = "proactive_audit";

/// How much autonomy the system has.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProactiveMode {
    /// Nothing is evaluated. Default.
    #[default]
    Off,
    /// Suggestions are evaluated and recorded, never shown prominently and never
    /// executed. This is the measurement mode.
    Observe,
    /// Suggestions are surfaced for a human to approve or reject. Execution
    /// requires approval.
    Propose,
    /// Approved suggestions execute without a further human step. Requires an
    /// explicit operator decision to enable.
    Execute,
}

impl ProactiveMode {
    pub fn as_str(&self) -> &'static str {
        match self {
            ProactiveMode::Off => "off",
            ProactiveMode::Observe => "observe",
            ProactiveMode::Propose => "propose",
            ProactiveMode::Execute => "execute",
        }
    }

    pub fn from_str_lossy(value: &str) -> Option<Self> {
        Some(match value.trim().to_ascii_lowercase().as_str() {
            "off" | "disabled" | "" => ProactiveMode::Off,
            "observe" => ProactiveMode::Observe,
            "propose" => ProactiveMode::Propose,
            "execute" => ProactiveMode::Execute,
            _ => return None,
        })
    }

    /// Is evaluation performed at all?
    pub fn evaluates(&self) -> bool {
        !matches!(self, ProactiveMode::Off)
    }

    /// May a proposal be recorded and shown to a human?
    pub fn allows_proposal(&self) -> bool {
        matches!(self, ProactiveMode::Propose | ProactiveMode::Execute)
    }

    /// May an *approved* proposal start work without a further human step?
    pub fn allows_execution(&self) -> bool {
        matches!(self, ProactiveMode::Execute)
    }
}

/// What kind of work a proposal suggests.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProposalKind {
    /// Follow-up work discovered while completing another case.
    DiscoveredFollowup,
    /// A stalled task that looks recoverable.
    UnblockStalled,
    /// A failing CI run that looks caused by the current change.
    FixFailingCi,
    /// A maintenance/refactor opportunity.
    MaintenanceDebt,
    /// A regression or alert worth investigating.
    InvestigateAlert,
}

impl ProposalKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            ProposalKind::DiscoveredFollowup => "discovered_followup",
            ProposalKind::UnblockStalled => "unblock_stalled",
            ProposalKind::FixFailingCi => "fix_failing_ci",
            ProposalKind::MaintenanceDebt => "maintenance_debt",
            ProposalKind::InvestigateAlert => "investigate_alert",
        }
    }
}

/// Lifecycle of a proposal.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProposalStatus {
    /// Recorded in `Observe` mode; never surfaced, never actionable.
    Observed,
    /// Surfaced, waiting for a human decision.
    Proposed,
    Approved,
    Rejected,
    /// Work was created from it (the created case/task id is recorded).
    Executed,
    /// Timed out without a decision.
    Expired,
    Failed,
}

impl ProposalStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            ProposalStatus::Observed => "observed",
            ProposalStatus::Proposed => "proposed",
            ProposalStatus::Approved => "approved",
            ProposalStatus::Rejected => "rejected",
            ProposalStatus::Executed => "executed",
            ProposalStatus::Expired => "expired",
            ProposalStatus::Failed => "failed",
        }
    }

    pub fn is_terminal(&self) -> bool {
        matches!(
            self,
            ProposalStatus::Rejected
                | ProposalStatus::Executed
                | ProposalStatus::Expired
                | ProposalStatus::Failed
        )
    }

    pub fn is_open(&self) -> bool {
        matches!(self, ProposalStatus::Proposed | ProposalStatus::Approved)
    }
}

/// A decision on a proposal.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", tag = "decision")]
pub enum ProposalDecision {
    Approve { by: String },
    Reject { by: String, reason: String },
}

/// A proactive suggestion produced by the Python evaluator.
///
/// It is a *suggestion*: `proposed_request` is a task description the operator
/// would be approving, and `requested_scopes` are advisory. Actual authority
/// comes from the mandate issued when (and if) the proposal is executed.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ProactiveProposal {
    pub id: String,
    /// Case the suggestion was derived from, if any.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub case_id: Option<String>,
    pub kind: ProposalKind,
    pub title: String,
    pub rationale: String,
    /// Evaluator confidence in `[0, 1]`. Advisory only; there is no automatic
    /// threshold that grants execution.
    #[serde(default)]
    pub confidence: f32,
    /// Concrete evidence (trigger ids, CI run ids, file paths). Empty evidence
    /// is rejected by [`ProactiveGateState::admit`] — an unexplainable
    /// suggestion is not auditable.
    #[serde(default)]
    pub evidence: Vec<String>,
    /// The request that would be submitted if approved.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub proposed_request: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub project_id: Option<String>,
    /// Estimated token budget for the proposed work.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub estimated_tokens: Option<u64>,
    /// Mode in force when the proposal was created. Recorded so an audit can
    /// tell "observed only" from "approved for execution".
    pub mode_at_creation: ProactiveMode,
    pub status: ProposalStatus,
    pub created_at: DateTime<Utc>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub decided_at: Option<DateTime<Utc>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub decided_by: Option<String>,
    /// Case id created when executed.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub executed_case_id: Option<String>,
    #[serde(default)]
    pub version: u64,
}

impl ProactiveProposal {
    pub fn new(
        kind: ProposalKind,
        title: impl Into<String>,
        rationale: impl Into<String>,
        mode: ProactiveMode,
        now: DateTime<Utc>,
    ) -> Self {
        Self {
            id: uuid::Uuid::new_v4().to_string(),
            case_id: None,
            kind,
            title: title.into(),
            rationale: rationale.into(),
            confidence: 0.0,
            evidence: Vec::new(),
            proposed_request: None,
            project_id: None,
            estimated_tokens: None,
            mode_at_creation: mode,
            status: if mode.allows_proposal() {
                ProposalStatus::Proposed
            } else {
                ProposalStatus::Observed
            },
            created_at: now,
            decided_at: None,
            decided_by: None,
            executed_case_id: None,
            version: 0,
        }
    }

    pub fn with_evidence(mut self, evidence: Vec<String>) -> Self {
        self.evidence = evidence;
        self
    }

    pub fn with_confidence(mut self, confidence: f32) -> Self {
        self.confidence = confidence.clamp(0.0, 1.0);
        self
    }

    pub fn with_request(
        mut self,
        project_id: impl Into<String>,
        request: impl Into<String>,
    ) -> Self {
        self.project_id = Some(project_id.into());
        self.proposed_request = Some(request.into());
        self
    }

    pub fn with_estimate(mut self, tokens: u64) -> Self {
        self.estimated_tokens = Some(tokens);
        self
    }

    pub fn apply_decision(
        &mut self,
        decision: &ProposalDecision,
        now: DateTime<Utc>,
    ) -> Result<(), ProposalError> {
        if self.status != ProposalStatus::Proposed {
            return Err(ProposalError::NotActionable {
                status: self.status,
            });
        }
        match decision {
            ProposalDecision::Approve { by } => {
                self.status = ProposalStatus::Approved;
                self.decided_by = Some(by.clone());
            }
            ProposalDecision::Reject { by, reason } => {
                self.status = ProposalStatus::Rejected;
                self.decided_by = Some(format!("{by}:{reason}"));
            }
        }
        self.decided_at = Some(now);
        Ok(())
    }

    pub fn mark_executed(
        &mut self,
        case_id: impl Into<String>,
        now: DateTime<Utc>,
    ) -> Result<(), ProposalError> {
        if self.status != ProposalStatus::Approved {
            return Err(ProposalError::NotActionable {
                status: self.status,
            });
        }
        self.status = ProposalStatus::Executed;
        self.executed_case_id = Some(case_id.into());
        self.decided_at = Some(now);
        Ok(())
    }
}

/// Rejected proposal transition.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProposalError {
    NotActionable { status: ProposalStatus },
}

impl std::fmt::Display for ProposalError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ProposalError::NotActionable { status } => {
                write!(
                    f,
                    "proposal in status {} is not actionable",
                    status.as_str()
                )
            }
        }
    }
}

impl std::error::Error for ProposalError {}

/// Operator-controlled gate around proactive behaviour.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ProactiveGateState {
    pub mode: ProactiveMode,
    /// Maximum open (undecided) proposals. Prevents review-queue flooding.
    #[serde(default = "default_max_open")]
    pub max_open_proposals: u32,
    /// Per-action token ceiling. A proposal above it is denied even in
    /// `Execute` mode.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_tokens_per_action: Option<u64>,
    /// When true, `Execute` mode still requires an approval (defence in depth
    /// for a mistyped environment variable).
    #[serde(default)]
    pub always_require_approval: bool,
}

fn default_max_open() -> u32 {
    5
}

impl Default for ProactiveGateState {
    fn default() -> Self {
        Self {
            mode: ProactiveMode::Off,
            max_open_proposals: default_max_open(),
            max_tokens_per_action: None,
            always_require_approval: false,
        }
    }
}

impl ProactiveGateState {
    /// Decide whether a proposal may be recorded right now.
    pub fn admit_proposal(
        &self,
        proposal: &ProactiveProposal,
        open_proposals: u32,
    ) -> PolicyDecision {
        if !self.mode.evaluates() {
            return PolicyDecision::deny(DenyReason::ProactiveModeDisallows {
                mode: self.mode.as_str().to_string(),
                action: "evaluate".to_string(),
            });
        }
        if proposal.evidence.is_empty() {
            return PolicyDecision::deny(DenyReason::ProactiveModeDisallows {
                mode: self.mode.as_str().to_string(),
                action: "propose_without_evidence".to_string(),
            });
        }
        if open_proposals >= self.max_open_proposals {
            return PolicyDecision::deny(DenyReason::BudgetExhausted {
                detail: format!(
                    "open proactive proposals at capacity ({}/{})",
                    open_proposals, self.max_open_proposals
                ),
            });
        }
        PolicyDecision::allow()
    }

    /// Decide whether an *approved* proposal may start executing.
    pub fn admit_execution(&self, proposal: &ProactiveProposal) -> PolicyDecision {
        if self.always_require_approval && proposal.status != ProposalStatus::Approved {
            return PolicyDecision::deny(DenyReason::ProactiveModeDisallows {
                mode: self.mode.as_str().to_string(),
                action: "execution_requires_approval".to_string(),
            });
        }
        if !self.mode.allows_execution() {
            return PolicyDecision::deny(DenyReason::ProactiveModeDisallows {
                mode: self.mode.as_str().to_string(),
                action: "execute".to_string(),
            });
        }
        if proposal.status != ProposalStatus::Approved {
            return PolicyDecision::deny(DenyReason::ProactiveModeDisallows {
                mode: self.mode.as_str().to_string(),
                action: format!("status_{}", proposal.status.as_str()),
            });
        }
        if let (Some(limit), Some(estimate)) =
            (self.max_tokens_per_action, proposal.estimated_tokens)
        {
            if estimate > limit {
                return PolicyDecision::deny(DenyReason::BudgetExhausted {
                    detail: format!("proposal estimates {estimate} tokens, limit is {limit}"),
                });
            }
        }
        PolicyDecision::allow()
    }
}

/// One audit record per proactive decision. Written for every gate result,
/// allowed or denied, so false triggers are reviewable after the fact.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ProactiveAuditEntry {
    pub at: DateTime<Utc>,
    pub mode: ProactiveMode,
    /// `"admit_proposal"` / `"admit_execution"` / `"decision"` / `"watchdog"`.
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

impl ProactiveAuditEntry {
    pub fn new(
        at: DateTime<Utc>,
        mode: ProactiveMode,
        action: impl Into<String>,
        allowed: bool,
    ) -> Self {
        Self {
            at,
            mode,
            action: action.into(),
            proposal_id: None,
            case_id: None,
            allowed,
            reason: None,
            detail: None,
        }
    }

    pub fn from_decision(
        at: DateTime<Utc>,
        mode: ProactiveMode,
        action: impl Into<String>,
        decision: &PolicyDecision,
    ) -> Self {
        let mut entry = Self::new(at, mode, action, decision.is_allowed());
        entry.reason = decision
            .deny_reason()
            .map(|reason| reason.code().to_string());
        entry.detail = decision.deny_reason().map(|reason| format!("{reason:?}"));
        entry
    }

    pub fn for_proposal(mut self, proposal_id: impl Into<String>) -> Self {
        self.proposal_id = Some(proposal_id.into());
        self
    }

    pub fn for_case(mut self, case_id: impl Into<String>) -> Self {
        self.case_id = Some(case_id.into());
        self
    }
}

/// Deterministic stall evidence for one task.
///
/// Produced from the Python `progress.py` detector: `no_progress_count` is the
/// number of consecutive attempts whose normalized error signature, stderr
/// signature and file set were all unchanged.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WatchdogSignal {
    pub task_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub subtask_id: Option<String>,
    #[serde(default)]
    pub no_progress_count: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub evidence_signature: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_error: Option<String>,
    /// External effects already confirmed (EffectLedger ids). A replan must not
    /// repeat them.
    #[serde(default)]
    pub confirmed_effect_ids: Vec<String>,
    /// How many attempts the case has already consumed.
    #[serde(default)]
    pub attempts_used: u32,
    /// How many replans the case has already consumed.
    #[serde(default)]
    pub replans_used: u32,
}

impl WatchdogSignal {
    pub fn new(task_id: impl Into<String>) -> Self {
        Self {
            task_id: task_id.into(),
            subtask_id: None,
            no_progress_count: 0,
            evidence_signature: None,
            last_error: None,
            confirmed_effect_ids: Vec::new(),
            attempts_used: 0,
            replans_used: 0,
        }
    }
}

/// Watchdog limits.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WatchdogConfig {
    /// Consecutive no-progress attempts before the watchdog acts. Mirrors
    /// `UC_PROGRESS_STALL_LIMIT` (default 2).
    #[serde(default = "default_stall_limit")]
    pub stall_limit: u32,
    /// Maximum bounded replans per case. After this the watchdog escalates to a
    /// human instead of trying again.
    #[serde(default = "default_max_replans")]
    pub max_replans: u32,
    /// Maximum attempts per case before it is stopped outright.
    #[serde(default = "default_max_attempts")]
    pub max_attempts: u32,
    #[serde(default = "default_base_backoff_ms")]
    pub base_backoff_ms: u64,
    #[serde(default = "default_max_backoff_ms")]
    pub max_backoff_ms: u64,
}

fn default_stall_limit() -> u32 {
    2
}
fn default_max_replans() -> u32 {
    2
}
fn default_max_attempts() -> u32 {
    5
}
fn default_base_backoff_ms() -> u64 {
    1_000
}
fn default_max_backoff_ms() -> u64 {
    60_000
}

impl Default for WatchdogConfig {
    fn default() -> Self {
        Self {
            stall_limit: default_stall_limit(),
            max_replans: default_max_replans(),
            max_attempts: default_max_attempts(),
            base_backoff_ms: default_base_backoff_ms(),
            max_backoff_ms: default_max_backoff_ms(),
        }
    }
}

/// What the watchdog decided to do.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", tag = "verdict")]
pub enum WatchdogVerdict {
    /// Not stalled; let the normal loop continue.
    Continue,
    /// Same-contract redispatch: retry the same work (transport/transient
    /// failure class), no new planning.
    RetrySameContract { delay_ms: u64 },
    /// Bounded replan: re-plan the remaining work. `preserve_effects` names the
    /// external effects already confirmed, which the new plan must not repeat.
    Replan {
        attempt: u32,
        delay_ms: u64,
        preserve_effects: Vec<String>,
    },
    /// Escalate to a human (replan budget exhausted).
    EscalateNeedsHuman { reason: String },
    /// Stop: the case is not making progress and retrying cannot help.
    Stop { reason: String },
}

/// A watchdog decision plus the remaining budget shown to the caller/audit.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WatchdogDecision {
    pub verdict: WatchdogVerdict,
    pub replans_remaining: u32,
    pub attempts_remaining: u32,
}

impl WatchdogConfig {
    /// Turn a stall signal into a bounded decision.
    ///
    /// Order of checks matters: a case that has exhausted its *attempt* budget is
    /// stopped even if it never stalled (nothing to replan), and a stalled case
    /// with no *replan* budget escalates rather than looping.
    pub fn decide(&self, signal: &WatchdogSignal) -> WatchdogDecision {
        let replans_remaining = self.max_replans.saturating_sub(signal.replans_used);
        let attempts_remaining = self.max_attempts.saturating_sub(signal.attempts_used);

        if signal.attempts_used >= self.max_attempts {
            return WatchdogDecision {
                verdict: WatchdogVerdict::Stop {
                    reason: format!(
                        "attempt budget exhausted ({}/{})",
                        signal.attempts_used, self.max_attempts
                    ),
                },
                replans_remaining,
                attempts_remaining: 0,
            };
        }
        if signal.no_progress_count < self.stall_limit {
            return WatchdogDecision {
                verdict: WatchdogVerdict::Continue,
                replans_remaining,
                attempts_remaining,
            };
        }
        if replans_remaining == 0 {
            return WatchdogDecision {
                verdict: WatchdogVerdict::EscalateNeedsHuman {
                    reason: format!(
                        "stalled for {} attempts and the replan budget is exhausted",
                        signal.no_progress_count
                    ),
                },
                replans_remaining,
                attempts_remaining,
            };
        }
        let attempt = signal.replans_used + 1;
        let delay_ms = self
            .base_backoff_ms
            .saturating_mul(1u64 << (attempt.saturating_sub(1)).min(16))
            .min(self.max_backoff_ms);
        WatchdogDecision {
            verdict: WatchdogVerdict::Replan {
                attempt,
                delay_ms,
                preserve_effects: signal.confirmed_effect_ids.clone(),
            },
            replans_remaining,
            attempts_remaining,
        }
    }

    /// A stall that is *not* a planning problem (the same error keeps appearing
    /// with no file changes) should be retried once under the same contract
    /// before replanning, because replanning costs model calls while a
    /// transient failure costs one retry.
    pub fn decide_with_retry(
        &self,
        signal: &WatchdogSignal,
        already_retried: bool,
    ) -> WatchdogDecision {
        if signal.no_progress_count >= self.stall_limit
            && !already_retried
            && signal.attempts_used < self.max_attempts
            && signal.replans_used < self.max_replans
        {
            return WatchdogDecision {
                verdict: WatchdogVerdict::RetrySameContract {
                    delay_ms: self.base_backoff_ms,
                },
                replans_remaining: self.max_replans.saturating_sub(signal.replans_used),
                attempts_remaining: self.max_attempts.saturating_sub(signal.attempts_used),
            };
        }
        self.decide(signal)
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
    fn default_mode_is_off_and_evaluates_nothing() {
        let gate = ProactiveGateState::default();
        assert_eq!(gate.mode, ProactiveMode::Off);
        let proposal = ProactiveProposal::new(
            ProposalKind::DiscoveredFollowup,
            "t",
            "r",
            ProactiveMode::Off,
            now(),
        )
        .with_evidence(vec!["trigger:1".to_string()]);
        assert_eq!(
            gate.admit_proposal(&proposal, 0).deny_code(),
            Some("proactive_mode_disallows")
        );
    }

    #[test]
    fn observe_records_but_never_proposes_or_executes() {
        let mut proposal = ProactiveProposal::new(
            ProposalKind::FixFailingCi,
            "t",
            "r",
            ProactiveMode::Observe,
            now(),
        )
        .with_evidence(vec!["ci:run-42".to_string()]);
        assert_eq!(proposal.status, ProposalStatus::Observed);
        let gate = ProactiveGateState {
            mode: ProactiveMode::Observe,
            ..Default::default()
        };
        assert!(gate.admit_proposal(&proposal, 0).is_allowed());
        assert!(!ProactiveMode::Observe.allows_proposal());
        // An observed proposal cannot be approved into execution.
        assert!(proposal
            .apply_decision(
                &ProposalDecision::Approve {
                    by: "alice".to_string()
                },
                now()
            )
            .is_err());
        assert_eq!(
            gate.admit_execution(&proposal).deny_code(),
            Some("proactive_mode_disallows")
        );
    }

    #[test]
    fn propose_allows_approval_but_not_autonomous_execution() {
        let gate = ProactiveGateState {
            mode: ProactiveMode::Propose,
            ..Default::default()
        };
        let mut proposal = ProactiveProposal::new(
            ProposalKind::DiscoveredFollowup,
            "t",
            "r",
            ProactiveMode::Propose,
            now(),
        )
        .with_evidence(vec!["trigger:9".to_string()]);
        assert!(gate.admit_proposal(&proposal, 0).is_allowed());
        proposal
            .apply_decision(
                &ProposalDecision::Approve {
                    by: "alice".to_string(),
                },
                now(),
            )
            .expect("approval allowed in propose mode");
        assert_eq!(
            gate.admit_execution(&proposal).deny_code(),
            Some("proactive_mode_disallows"),
            "propose mode must never execute without a separate operator opt-in"
        );
    }

    #[test]
    fn execute_mode_runs_only_approved_proposals_within_budget() {
        let gate = ProactiveGateState {
            mode: ProactiveMode::Execute,
            max_tokens_per_action: Some(100_000),
            ..Default::default()
        };
        let mut proposal = ProactiveProposal::new(
            ProposalKind::UnblockStalled,
            "t",
            "r",
            ProactiveMode::Execute,
            now(),
        )
        .with_evidence(vec!["task:t1".to_string()])
        .with_estimate(50_000);
        assert!(gate.admit_execution(&proposal).is_denied());
        proposal
            .apply_decision(
                &ProposalDecision::Approve {
                    by: "alice".to_string(),
                },
                now(),
            )
            .unwrap();
        assert!(gate.admit_execution(&proposal).is_allowed());
        proposal.estimated_tokens = Some(250_000);
        assert_eq!(
            gate.admit_execution(&proposal).deny_code(),
            Some("budget_exhausted")
        );
    }

    #[test]
    fn proposal_without_evidence_is_refused() {
        let gate = ProactiveGateState {
            mode: ProactiveMode::Propose,
            ..Default::default()
        };
        let proposal = ProactiveProposal::new(
            ProposalKind::InvestigateAlert,
            "t",
            "r",
            ProactiveMode::Propose,
            now(),
        );
        let decision = gate.admit_proposal(&proposal, 0);
        assert!(decision.is_denied());
        assert!(decision
            .deny_reason()
            .map(|reason| format!("{reason:?}"))
            .unwrap_or_default()
            .contains("propose_without_evidence"));
    }

    #[test]
    fn open_proposal_capacity_is_bounded() {
        let gate = ProactiveGateState {
            mode: ProactiveMode::Propose,
            max_open_proposals: 2,
            ..Default::default()
        };
        let proposal = ProactiveProposal::new(
            ProposalKind::DiscoveredFollowup,
            "t",
            "r",
            ProactiveMode::Propose,
            now(),
        )
        .with_evidence(vec!["e".to_string()]);
        assert!(gate.admit_proposal(&proposal, 1).is_allowed());
        assert_eq!(
            gate.admit_proposal(&proposal, 2).deny_code(),
            Some("budget_exhausted")
        );
    }

    #[test]
    fn audit_entry_always_records_deny_reason() {
        let decision = PolicyDecision::deny(DenyReason::ProactiveModeDisallows {
            mode: "off".to_string(),
            action: "evaluate".to_string(),
        });
        let entry = ProactiveAuditEntry::from_decision(
            now(),
            ProactiveMode::Off,
            "admit_proposal",
            &decision,
        );
        assert!(!entry.allowed);
        assert_eq!(entry.reason.as_deref(), Some("proactive_mode_disallows"));
    }

    #[test]
    fn watchdog_continues_below_the_stall_threshold() {
        let config = WatchdogConfig::default();
        let mut signal = WatchdogSignal::new("task-1");
        signal.no_progress_count = 1;
        assert_eq!(config.decide(&signal).verdict, WatchdogVerdict::Continue);
    }

    #[test]
    fn watchdog_replans_are_bounded_then_escalate() {
        let config = WatchdogConfig {
            stall_limit: 2,
            max_replans: 1,
            max_attempts: 10,
            ..Default::default()
        };
        let mut signal = WatchdogSignal::new("task-1");
        signal.no_progress_count = 2;
        let first = config.decide(&signal);
        assert!(matches!(
            first.verdict,
            WatchdogVerdict::Replan { attempt: 1, .. }
        ));
        signal.replans_used = 1;
        let second = config.decide(&signal);
        assert!(matches!(
            second.verdict,
            WatchdogVerdict::EscalateNeedsHuman { .. }
        ));
        assert_eq!(second.replans_remaining, 0);
    }

    #[test]
    fn watchdog_replan_preserves_confirmed_effects() {
        let config = WatchdogConfig::default();
        let mut signal = WatchdogSignal::new("task-1");
        signal.no_progress_count = 2;
        signal.confirmed_effect_ids = vec![
            "effect:push-main".to_string(),
            "effect:comment-7".to_string(),
        ];
        match config.decide(&signal).verdict {
            WatchdogVerdict::Replan {
                preserve_effects, ..
            } => assert_eq!(
                preserve_effects,
                vec![
                    "effect:push-main".to_string(),
                    "effect:comment-7".to_string()
                ]
            ),
            other => panic!("expected replan, got {other:?}"),
        }
    }

    #[test]
    fn watchdog_stops_when_attempt_budget_is_spent() {
        let config = WatchdogConfig {
            max_attempts: 3,
            ..Default::default()
        };
        let mut signal = WatchdogSignal::new("task-1");
        signal.attempts_used = 3;
        signal.no_progress_count = 5;
        assert!(matches!(
            config.decide(&signal).verdict,
            WatchdogVerdict::Stop { .. }
        ));
    }

    #[test]
    fn watchdog_retries_once_before_replanning() {
        let config = WatchdogConfig::default();
        let mut signal = WatchdogSignal::new("task-1");
        signal.no_progress_count = 3;
        assert!(matches!(
            config.decide_with_retry(&signal, false).verdict,
            WatchdogVerdict::RetrySameContract { .. }
        ));
        assert!(matches!(
            config.decide_with_retry(&signal, true).verdict,
            WatchdogVerdict::Replan { .. }
        ));
    }
}
