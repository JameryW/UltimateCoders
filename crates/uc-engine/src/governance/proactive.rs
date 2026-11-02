//! Proactive gate: admission, audit and lifecycle for proactive proposals.
//!
//! The gate is where "a model may suggest, the control plane decides" becomes
//! concrete:
//!
//! * the **mode** (off / observe / propose / execute) is operator-controlled and
//!   defaults to `off`;
//! * a proposal **must carry evidence** or it is refused — an unexplained
//!   suggestion cannot be audited, and an unauditable suggestion is exactly the
//!   failure mode this PR is meant to prevent;
//! * every gate decision — allow or deny — is written to the audit log, so false
//!   triggers can be reviewed after the fact;
//! * execution is a separate admission check that requires an approved proposal
//!   *and* `Execute` mode, so a mistyped environment variable alone cannot make
//!   the system act.

use std::sync::Arc;

use chrono::{DateTime, Utc};

use uc_types::governance::{
    ProactiveAuditEntry, ProactiveGateState, ProactiveMode, ProactiveProposal, ProposalDecision,
    ProposalError, ProposalStatus, PROACTIVE_AUDIT_NAMESPACE, PROACTIVE_NAMESPACE,
};

use super::record_store::{NamespacedStore, RecordError, RecordStore};

/// Failure modes of the proactive gate.
#[derive(Debug, Clone, PartialEq)]
pub enum ProactiveGateError {
    Store(RecordError),
    NotFound { proposal_id: String },
    NotActionable { proposal_id: String, status: String },
    Contended,
}

impl From<RecordError> for ProactiveGateError {
    fn from(error: RecordError) -> Self {
        ProactiveGateError::Store(error)
    }
}

impl std::fmt::Display for ProactiveGateError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ProactiveGateError::Store(error) => write!(f, "proactive gate: {error}"),
            ProactiveGateError::NotFound { proposal_id } => {
                write!(f, "proposal {proposal_id} not found")
            }
            ProactiveGateError::NotActionable {
                proposal_id,
                status,
            } => write!(
                f,
                "proposal {proposal_id} is {status} and cannot be changed"
            ),
            ProactiveGateError::Contended => f.write_str("proactive gate contention"),
        }
    }
}

impl std::error::Error for ProactiveGateError {}

/// Admission result carrying the deny reason for the caller *and* the audit
/// entry that was written for it.
/// Admission result: the gate's verdict, with a machine-readable code so the
/// caller can audit a refusal without parsing a message.
#[derive(Debug, Clone, PartialEq)]
pub struct Admission {
    pub allowed: bool,
    pub deny_code: Option<String>,
    pub deny_detail: Option<String>,
}

impl Admission {
    pub fn allowed() -> Self {
        Self {
            allowed: true,
            deny_code: None,
            deny_detail: None,
        }
    }

    pub fn denied(code: impl Into<String>, detail: impl Into<String>) -> Self {
        Self {
            allowed: false,
            deny_code: Some(code.into()),
            deny_detail: Some(detail.into()),
        }
    }
}

/// Durable proactive state: mode, proposals and the audit log.
#[derive(Clone, Debug)]
pub struct ProactiveGate {
    proposals: NamespacedStore,
    audit: NamespacedStore,
    state: NamespacedStore,
    mode: Arc<std::sync::RwLock<ProactiveGateState>>,
}

impl ProactiveGate {
    pub fn new(records: Arc<dyn RecordStore>) -> Self {
        Self {
            proposals: NamespacedStore::new(records.clone(), PROACTIVE_NAMESPACE),
            audit: NamespacedStore::new(records.clone(), PROACTIVE_AUDIT_NAMESPACE),
            state: NamespacedStore::new(records.clone(), "proactive_gate_state"),
            mode: Arc::new(std::sync::RwLock::new(ProactiveGateState::default())),
        }
    }

    pub fn with_state(self, state: ProactiveGateState) -> Self {
        if let Ok(mut guard) = self.mode.write() {
            *guard = state;
        }
        self
    }

    pub fn state(&self) -> ProactiveGateState {
        self.mode
            .read()
            .map(|state| state.clone())
            .unwrap_or_default()
    }

    pub fn mode(&self) -> ProactiveMode {
        self.state().mode
    }

    /// Load the persisted gate state at boot (mode + limits survive a restart).
    pub async fn load_persisted(&self) -> Result<ProactiveGateState, ProactiveGateError> {
        if let Some((state, _version)) = self.state.get::<ProactiveGateState>("current").await? {
            if let Ok(mut guard) = self.mode.write() {
                *guard = state.clone();
            }
            return Ok(state);
        }
        Ok(self.state())
    }

    /// Change the mode. `Execute` is accepted here only; putting it behind the
    /// `proactive_execute` permission is the caller's (gRPC layer) job, because
    /// this module has no principal context.
    pub async fn set_mode(
        &self,
        mode: ProactiveMode,
        max_open_proposals: Option<u32>,
        max_tokens_per_action: Option<u64>,
        always_require_approval: Option<bool>,
    ) -> Result<ProactiveGateState, ProactiveGateError> {
        let mut state = self.state();
        state.mode = mode;
        if let Some(value) = max_open_proposals {
            state.max_open_proposals = value.max(1);
        }
        if max_tokens_per_action.is_some() {
            state.max_tokens_per_action = max_tokens_per_action;
        }
        if let Some(value) = always_require_approval {
            state.always_require_approval = value;
        }
        self.state.put("current", &state, None).await?;
        if let Ok(mut guard) = self.mode.write() {
            *guard = state.clone();
        }
        Ok(state)
    }

    /// Admit and record a proposal. Denials are audited too.
    ///
    /// The returned tuple is `(admission, proposal)`: on denial the proposal is
    /// still returned (in `Observed`/`Proposed` form) so a caller in `Observe`
    /// mode can inspect what *would* have been proposed.
    pub async fn submit(
        &self,
        mut proposal: ProactiveProposal,
        open_proposals: u32,
        now: DateTime<Utc>,
    ) -> Result<(Admission, ProactiveProposal), ProactiveGateError> {
        let state = self.state();
        let decision = state.admit_proposal(&proposal, open_proposals);
        let audit =
            ProactiveAuditEntry::from_decision(now, state.mode, "admit_proposal", &decision)
                .for_proposal(proposal.id.clone());
        let audit = match &proposal.case_id {
            Some(case_id) => audit.for_case(case_id.clone()),
            None => audit,
        };
        let _ = self
            .audit
            .put(&audit_key(now, &proposal.id), &audit, None)
            .await;

        if decision.is_denied() {
            // In Observe mode the proposal is stored as `Observed` so a later audit
            // can tell "we saw this and said nothing" from "we never saw it".
            if proposal.status == ProposalStatus::Proposed
                && !state.mode.allows_proposal()
                && state.mode.evaluates()
            {
                proposal.status = ProposalStatus::Observed;
            }
            let admission = Admission::denied(
                decision.deny_code().unwrap_or("denied"),
                decision
                    .deny_reason()
                    .map(|reason| format!("{reason:?}"))
                    .unwrap_or_default(),
            );
            if state.mode.evaluates() {
                let _ = self.proposals.put(&proposal.id, &proposal, None).await;
            }
            return Ok((admission, proposal));
        }

        self.proposals.put(&proposal.id, &proposal, Some(0)).await?;
        Ok((Admission::allowed(), proposal))
    }

    /// Admit execution of an already-approved proposal.
    pub async fn admit_execution(
        &self,
        proposal_id: &str,
        now: DateTime<Utc>,
    ) -> Result<(Admission, Option<ProactiveProposal>), ProactiveGateError> {
        let Some((proposal, _version)) =
            self.proposals.get::<ProactiveProposal>(proposal_id).await?
        else {
            return Ok((Admission::denied("proposal_not_found", proposal_id), None));
        };
        let state = self.state();
        let decision = state.admit_execution(&proposal);
        let audit =
            ProactiveAuditEntry::from_decision(now, state.mode, "admit_execution", &decision)
                .for_proposal(proposal.id.clone());
        let _ = self
            .audit
            .put(&audit_key(now, &proposal.id), &audit, None)
            .await;
        if decision.is_denied() {
            return Ok((
                Admission::denied(
                    decision.deny_code().unwrap_or("denied"),
                    decision
                        .deny_reason()
                        .map(|reason| format!("{reason:?}"))
                        .unwrap_or_default(),
                ),
                Some(proposal),
            ));
        }
        Ok((Admission::allowed(), Some(proposal)))
    }

    pub async fn get(
        &self,
        proposal_id: &str,
    ) -> Result<Option<ProactiveProposal>, ProactiveGateError> {
        Ok(self
            .proposals
            .get::<ProactiveProposal>(proposal_id)
            .await?
            .map(|(proposal, _version)| proposal))
    }

    pub async fn list(
        &self,
        status: Option<ProposalStatus>,
        case_id: Option<&str>,
        limit: usize,
    ) -> Result<Vec<ProactiveProposal>, ProactiveGateError> {
        let proposals = self.proposals.list::<ProactiveProposal>(limit).await?;
        let mut proposals: Vec<ProactiveProposal> = proposals
            .into_iter()
            .map(|(proposal, _version)| proposal)
            .filter(|proposal| {
                status
                    .map(|status| proposal.status == status)
                    .unwrap_or(true)
                    && case_id
                        .map(|case_id| proposal.case_id.as_deref() == Some(case_id))
                        .unwrap_or(true)
            })
            .collect();
        proposals.sort_by_key(|p| std::cmp::Reverse(p.created_at));
        Ok(proposals)
    }

    pub async fn open_count(&self) -> Result<u32, ProactiveGateError> {
        Ok(self
            .list(None, None, 1000)
            .await?
            .into_iter()
            .filter(|proposal| proposal.status.is_open())
            .count() as u32)
    }

    /// Record a human decision. Only `Proposed` proposals can be decided.
    pub async fn decide(
        &self,
        proposal_id: &str,
        decision: &ProposalDecision,
        now: DateTime<Utc>,
    ) -> Result<ProactiveProposal, ProactiveGateError> {
        for _ in 0..8 {
            let Some((mut proposal, version)) =
                self.proposals.get::<ProactiveProposal>(proposal_id).await?
            else {
                return Err(ProactiveGateError::NotFound {
                    proposal_id: proposal_id.to_string(),
                });
            };
            match proposal.apply_decision(decision, now) {
                Ok(()) => {}
                Err(ProposalError::NotActionable { status }) => {
                    return Err(ProactiveGateError::NotActionable {
                        proposal_id: proposal_id.to_string(),
                        status: status.as_str().to_string(),
                    });
                }
            }
            match self
                .proposals
                .put(proposal_id, &proposal, Some(version))
                .await
            {
                Ok(_) => {
                    let entry = ProactiveAuditEntry::new(
                        now,
                        self.mode(),
                        "decision",
                        matches!(decision, ProposalDecision::Approve { .. }),
                    )
                    .for_proposal(proposal_id.to_string());
                    let _ = self
                        .audit
                        .put(&audit_key(now, proposal_id), &entry, None)
                        .await;
                    return Ok(proposal);
                }
                Err(RecordError::Conflict { .. }) => continue,
                Err(other) => return Err(ProactiveGateError::Store(other)),
            }
        }
        Err(ProactiveGateError::Contended)
    }

    /// Mark an approved proposal as executed with the case it created.
    pub async fn mark_executed(
        &self,
        proposal_id: &str,
        case_id: &str,
        now: DateTime<Utc>,
    ) -> Result<ProactiveProposal, ProactiveGateError> {
        for _ in 0..8 {
            let Some((mut proposal, version)) =
                self.proposals.get::<ProactiveProposal>(proposal_id).await?
            else {
                return Err(ProactiveGateError::NotFound {
                    proposal_id: proposal_id.to_string(),
                });
            };
            match proposal.mark_executed(case_id, now) {
                Ok(()) => {}
                Err(ProposalError::NotActionable { status }) => {
                    return Err(ProactiveGateError::NotActionable {
                        proposal_id: proposal_id.to_string(),
                        status: status.as_str().to_string(),
                    });
                }
            }
            match self
                .proposals
                .put(proposal_id, &proposal, Some(version))
                .await
            {
                Ok(_) => return Ok(proposal),
                Err(RecordError::Conflict { .. }) => continue,
                Err(other) => return Err(ProactiveGateError::Store(other)),
            }
        }
        Err(ProactiveGateError::Contended)
    }

    /// Audit entries, newest first, optionally filtered.
    pub async fn audit(
        &self,
        proposal_id: Option<&str>,
        case_id: Option<&str>,
        limit: usize,
    ) -> Result<Vec<ProactiveAuditEntry>, ProactiveGateError> {
        let entries = self
            .audit
            .list::<ProactiveAuditEntry>(limit.saturating_mul(4).max(50))
            .await?;
        let mut entries: Vec<ProactiveAuditEntry> = entries
            .into_iter()
            .map(|(entry, _version)| entry)
            .filter(|entry| {
                proposal_id
                    .map(|id| entry.proposal_id.as_deref() == Some(id))
                    .unwrap_or(true)
                    && case_id
                        .map(|id| entry.case_id.as_deref() == Some(id))
                        .unwrap_or(true)
            })
            .collect();
        entries.sort_by_key(|e| std::cmp::Reverse(e.at));
        entries.truncate(limit);
        Ok(entries)
    }
}

fn audit_key(now: DateTime<Utc>, proposal_id: &str) -> String {
    format!("{}:{}", now.timestamp_micros(), proposal_id)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::governance::record_store::InMemoryRecordStore;
    use uc_types::governance::ProposalKind;

    fn now() -> DateTime<Utc> {
        DateTime::parse_from_rfc3339("2026-01-01T00:00:00Z")
            .unwrap()
            .with_timezone(&Utc)
    }

    fn gate(mode: ProactiveMode) -> ProactiveGate {
        let records: Arc<dyn RecordStore> = Arc::new(InMemoryRecordStore::new());
        ProactiveGate::new(records).with_state(ProactiveGateState {
            mode,
            ..Default::default()
        })
    }

    fn proposal(mode: ProactiveMode) -> ProactiveProposal {
        ProactiveProposal::new(
            ProposalKind::FixFailingCi,
            "fix CI",
            "the run failed on the changed file",
            mode,
            now(),
        )
        .with_evidence(vec!["ci:run-42".to_string()])
        .with_confidence(0.8)
    }

    #[tokio::test]
    async fn off_mode_refuses_and_audits() {
        let gate = gate(ProactiveMode::Off);
        let (admission, _) = gate
            .submit(proposal(ProactiveMode::Off), 0, now())
            .await
            .unwrap();
        assert!(!admission.allowed);
        assert_eq!(
            admission.deny_code.as_deref(),
            Some("proactive_mode_disallows")
        );
        let audit = gate.audit(None, None, 10).await.unwrap();
        assert_eq!(audit.len(), 1);
        assert!(!audit[0].allowed);
    }

    #[tokio::test]
    async fn observe_records_without_surfacing() {
        let gate = gate(ProactiveMode::Observe);
        let (admission, stored) = gate
            .submit(proposal(ProactiveMode::Observe), 0, now())
            .await
            .unwrap();
        // Observe recorded it (allowed to record) but it is not proposed.
        assert!(admission.allowed);
        assert_eq!(stored.status, ProposalStatus::Observed);
        assert_eq!(gate.open_count().await.unwrap(), 0);
    }

    #[tokio::test]
    async fn evidence_free_proposal_is_refused() {
        let gate = gate(ProactiveMode::Propose);
        let bare = ProactiveProposal::new(
            ProposalKind::InvestigateAlert,
            "t",
            "r",
            ProactiveMode::Propose,
            now(),
        );
        let (admission, _) = gate.submit(bare, 0, now()).await.unwrap();
        assert!(!admission.allowed);
        assert!(admission
            .deny_detail
            .unwrap_or_default()
            .contains("propose_without_evidence"));
    }

    #[tokio::test]
    async fn propose_mode_requires_approval_and_refuses_execution() {
        let gate = gate(ProactiveMode::Propose);
        let (admission, proposal) = gate
            .submit(proposal(ProactiveMode::Propose), 0, now())
            .await
            .unwrap();
        assert!(admission.allowed);
        assert_eq!(proposal.status, ProposalStatus::Proposed);
        assert_eq!(gate.open_count().await.unwrap(), 1);

        let (exec_admission, _) = gate.admit_execution(&proposal.id, now()).await.unwrap();
        assert!(!exec_admission.allowed);

        let approved = gate
            .decide(
                &proposal.id,
                &ProposalDecision::Approve {
                    by: "alice".to_string(),
                },
                now(),
            )
            .await
            .unwrap();
        assert_eq!(approved.status, ProposalStatus::Approved);
        // Still no execution: mode is Propose.
        let (exec_admission, _) = gate.admit_execution(&proposal.id, now()).await.unwrap();
        assert!(!exec_admission.allowed);
    }

    #[tokio::test]
    async fn execute_mode_runs_approved_proposals_and_records_the_case() {
        let gate = gate(ProactiveMode::Execute);
        let (admission, proposal) = gate
            .submit(proposal(ProactiveMode::Execute), 0, now())
            .await
            .unwrap();
        assert!(admission.allowed);
        let (before, _) = gate.admit_execution(&proposal.id, now()).await.unwrap();
        assert!(!before.allowed, "unapproved proposal must not execute");
        gate.decide(
            &proposal.id,
            &ProposalDecision::Approve {
                by: "alice".to_string(),
            },
            now(),
        )
        .await
        .unwrap();
        let (after, _) = gate.admit_execution(&proposal.id, now()).await.unwrap();
        assert!(after.allowed);
        let executed = gate
            .mark_executed(&proposal.id, "case-9", now())
            .await
            .unwrap();
        assert_eq!(executed.status, ProposalStatus::Executed);
        assert_eq!(executed.executed_case_id.as_deref(), Some("case-9"));
    }

    #[tokio::test]
    async fn rejecting_a_proposal_is_terminal() {
        let gate = gate(ProactiveMode::Propose);
        let (_, proposal) = gate
            .submit(proposal(ProactiveMode::Propose), 0, now())
            .await
            .unwrap();
        let rejected = gate
            .decide(
                &proposal.id,
                &ProposalDecision::Reject {
                    by: "alice".to_string(),
                    reason: "not now".to_string(),
                },
                now(),
            )
            .await
            .unwrap();
        assert_eq!(rejected.status, ProposalStatus::Rejected);
        let error = gate
            .decide(
                &proposal.id,
                &ProposalDecision::Approve {
                    by: "alice".to_string(),
                },
                now(),
            )
            .await
            .expect_err("rejected proposals are terminal");
        assert!(matches!(error, ProactiveGateError::NotActionable { .. }));
    }

    #[tokio::test]
    async fn mode_change_persists() {
        let records: Arc<dyn RecordStore> = Arc::new(InMemoryRecordStore::new());
        let gate = ProactiveGate::new(records.clone());
        assert_eq!(gate.mode(), ProactiveMode::Off);
        gate.set_mode(ProactiveMode::Observe, Some(3), Some(1000), Some(true))
            .await
            .unwrap();
        let reloaded = ProactiveGate::new(records);
        let state = reloaded.load_persisted().await.unwrap();
        assert_eq!(state.mode, ProactiveMode::Observe);
        assert_eq!(state.max_open_proposals, 3);
        assert_eq!(state.max_tokens_per_action, Some(1000));
        assert!(state.always_require_approval);
    }
}
