//! The policy engine: authorization decisions over grants, mandates, tools and
//! sandbox profiles.
//!
//! Everything here is fail-closed, and every denial carries a typed
//! [`DenyReason`] so the audit trail can distinguish "no credential" from
//! "credential revoked" from "mandate too narrow". The engine itself is thin on
//! purpose: the interesting rules live in
//! [`uc_types::governance::policy`] (structural ceilings, tool table, sandbox
//! profile) and [`uc_types::governance::mandate`] (scope matching, delegation),
//! where they are unit-testable without I/O.

use std::sync::{Arc, RwLock};

use chrono::{DateTime, Utc};

use uc_types::governance::{
    DenyReason, Mandate, MandateDecision, MandateDenial, MandateId, MandateOrigin, MandateRequest,
    MandateScope, PolicyDecision, PolicySnapshot, PolicyVersion, Principal, SandboxSecurityProfile,
    ToolPolicyTable,
};

use super::case_store::{MandateStore, MandateStoreError};
use super::record_store::{NamespacedStore, RecordError, RecordStore};

/// Record-store namespace for the installed policy snapshot.
pub const POLICY_NAMESPACE: &str = "policy";

/// Failure modes of the policy engine.
#[derive(Debug, Clone, PartialEq)]
pub enum PolicyEngineError {
    Store(RecordError),
    Mandate(MandateStoreError),
    /// The caller is not allowed to perform the operation.
    Denied(Box<PolicyDecision>),
    /// The mandate being issued is wider than its parent.
    Widening(String),
    /// The referenced mandate does not exist.
    UnknownMandate {
        mandate_id: String,
    },
    /// The case already has a live (non-revoked, non-expired) mandate; issue a new
    /// one explicitly only after revoking (prevents accidental double authority).
    MandateConflict {
        case_id: String,
        existing: String,
    },
}

impl From<RecordError> for PolicyEngineError {
    fn from(error: RecordError) -> Self {
        PolicyEngineError::Store(error)
    }
}

impl From<MandateStoreError> for PolicyEngineError {
    fn from(error: MandateStoreError) -> Self {
        PolicyEngineError::Mandate(error)
    }
}

impl std::fmt::Display for PolicyEngineError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PolicyEngineError::Store(error) => write!(f, "policy store: {error}"),
            PolicyEngineError::Mandate(error) => write!(f, "policy mandate: {error}"),
            PolicyEngineError::Denied(decision) => {
                write!(f, "denied: {}", decision.deny_code().unwrap_or("unknown"))
            }
            PolicyEngineError::Widening(detail) => write!(f, "mandate widening refused: {detail}"),
            PolicyEngineError::UnknownMandate { mandate_id } => {
                write!(f, "unknown mandate {mandate_id}")
            }
            PolicyEngineError::MandateConflict { case_id, existing } => write!(
                f,
                "case {case_id} already has a live mandate ({existing}); revoke it first"
            ),
        }
    }
}

impl std::error::Error for PolicyEngineError {}

/// Authorization decisions plus mandate lifecycle.
#[derive(Clone)]
pub struct PolicyEngine {
    view: NamespacedStore,
    mandates: Arc<MandateStore>,
    snapshot: Arc<RwLock<PolicySnapshot>>,
    tools: Arc<ToolPolicyTable>,
    /// Human approvals for human-tier tools. `None` keeps the older "operator tier
    /// only" behaviour, which is what the focused unit tests want.
    approvals: Option<Arc<super::ApprovalStore>>,
}

impl std::fmt::Debug for PolicyEngine {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PolicyEngine")
            .field("version", &self.version())
            .field("durable", &self.view.is_durable())
            .finish_non_exhaustive()
    }
}

impl PolicyEngine {
    /// Build an engine with an **empty** snapshot: every check denies until grants
    /// are installed. That is the correct default — a gateway that has not been
    /// configured cannot authorize anything.
    pub fn new(records: Arc<dyn RecordStore>, mandates: Arc<MandateStore>) -> Self {
        Self {
            view: NamespacedStore::new(records, POLICY_NAMESPACE),
            mandates,
            snapshot: Arc::new(RwLock::new(PolicySnapshot::empty(PolicyVersion::initial()))),
            tools: Arc::new(ToolPolicyTable::builtin()),
            approvals: None,
        }
    }

    /// Attach the approval store, enabling the "a human approved this action" path
    /// for human-tier tools. Without it the operator tier is the only way in.
    pub fn with_approvals(mut self, approvals: Arc<super::ApprovalStore>) -> Self {
        self.approvals = Some(approvals);
        self
    }

    pub fn with_snapshot(
        records: Arc<dyn RecordStore>,
        mandates: Arc<MandateStore>,
        snapshot: PolicySnapshot,
    ) -> Self {
        let engine = Self::new(records, mandates);
        engine.install_in_memory(snapshot);
        engine
    }

    pub fn version(&self) -> PolicyVersion {
        self.snapshot
            .read()
            .map(|snapshot| snapshot.version)
            .unwrap_or(PolicyVersion::initial())
    }

    pub fn snapshot(&self) -> PolicySnapshot {
        self.snapshot
            .read()
            .map(|snapshot| snapshot.clone())
            .unwrap_or_else(|_| PolicySnapshot::empty(PolicyVersion::initial()))
    }

    pub fn tools(&self) -> &ToolPolicyTable {
        &self.tools
    }

    fn install_in_memory(&self, snapshot: PolicySnapshot) {
        if let Ok(mut guard) = self.snapshot.write() {
            *guard = snapshot;
        }
    }

    /// Install a new snapshot (bumps the version if the caller did not) and
    /// persist it so a restart keeps the same policy generation.
    pub async fn install(
        &self,
        snapshot: PolicySnapshot,
    ) -> Result<PolicyVersion, PolicyEngineError> {
        // Always advance the generation unless the caller supplied a snapshot
        // that is already newer (importing a policy from elsewhere).
        let version = if snapshot.version > self.version() {
            snapshot.version
        } else {
            self.version().next()
        };
        let snapshot = PolicySnapshot {
            version,
            ..snapshot
        };
        self.view.put("current", &snapshot, None).await?;
        self.install_in_memory(snapshot);
        Ok(version)
    }

    /// Load the persisted snapshot at boot (falls back to the in-memory empty
    /// snapshot when nothing has been installed yet).
    pub async fn load_persisted(&self) -> Result<Option<PolicyVersion>, PolicyEngineError> {
        if let Some((snapshot, _version)) = self.view.get::<PolicySnapshot>("current").await? {
            let version = snapshot.version;
            self.install_in_memory(snapshot);
            return Ok(Some(version));
        }
        Ok(None)
    }

    // ── Permission checks ──────────────────────────────────────────────

    pub fn authorize(
        &self,
        principal: &Principal,
        permission: uc_types::governance::Permission,
        project_id: Option<&str>,
        now: DateTime<Utc>,
    ) -> PolicyDecision {
        self.snapshot()
            .evaluate(principal, permission, project_id, now)
    }

    /// Resizing the worker pool is the highest-blast-radius operation the gateway
    /// exposes (it shells out to `docker compose`), so it gets its own entry point
    /// with the permission and the structural ceiling both enforced.
    pub fn authorize_scale_workers(
        &self,
        principal: &Principal,
        now: DateTime<Utc>,
    ) -> PolicyDecision {
        self.authorize(
            principal,
            uc_types::governance::Permission::WorkerScale,
            None,
            now,
        )
    }

    /// Validate a sandbox profile against the hardened baseline.
    pub fn validate_sandbox(&self, profile: &SandboxSecurityProfile) -> PolicyDecision {
        profile.decision()
    }

    /// Check a mandate request against a mandate, mapping mandate denials into the
    /// policy vocabulary so callers have one decision type.
    pub fn authorize_mandate(
        &self,
        mandate: &Mandate,
        request: &MandateRequest,
        now: DateTime<Utc>,
    ) -> PolicyDecision {
        match mandate.permits(request, now) {
            MandateDecision::Allowed => PolicyDecision::allow(),
            MandateDecision::Denied { denial } => PolicyDecision::deny(mandate_denial(denial)),
        }
    }

    /// Full tool-level check: tool table → structural ceiling → permission →
    /// human-tier approval → mandate scope.
    ///
    /// The steps are separate on purpose — a denial names the *first* thing that
    /// failed, which is what makes an audit readable.
    ///
    /// `ToolPolicy::human_approval` is not decoration: such a tool (today
    /// `scale_workers` and `docker_run`) additionally requires the operator
    /// permission `proactive_execute`, which the structural ceiling denies to
    /// agents and workers. There is no interactive approval round-trip yet; the
    /// "approval" is the operator tier itself, which is the honest boundary until
    /// an approval UI exists.
    pub async fn authorize_tool(
        &self,
        principal: &Principal,
        tool: &str,
        project_id: Option<&str>,
        case_id: Option<&str>,
        now: DateTime<Utc>,
    ) -> PolicyDecision {
        let requirement = self.tools.requirement(tool);
        if !requirement.known {
            return PolicyDecision::deny(DenyReason::ToolNotPermitted {
                tool: tool.to_string(),
            });
        }
        let permission_decision = self.authorize(
            principal,
            requirement.action.required_permission(),
            project_id,
            now,
        );
        if permission_decision.is_denied() {
            return permission_decision;
        }
        // Human-tier gate. The permit is *peeked* here and only spent once every
        // later check passed, so a call denied for a different reason (missing
        // mandate, out-of-scope project) never burns a human's consent.
        let mut permit_id: Option<String> = None;
        if requirement.human_approval {
            let operator = self.authorize(
                principal,
                uc_types::governance::Permission::ProactiveExecute,
                project_id,
                now,
            );
            if operator.is_denied() {
                let approved = match &self.approvals {
                    Some(approvals) => match approvals.find_approved(tool, case_id, now).await {
                        Ok(approved) => approved.map(|approval| approval.id),
                        Err(error) => {
                            tracing::warn!(
                                tool = %tool,
                                error = %error,
                                "approval lookup failed; denying the human-tier tool"
                            );
                            None
                        }
                    },
                    None => None,
                };
                match approved {
                    Some(id) => permit_id = Some(id),
                    None => {
                        return PolicyDecision::deny(DenyReason::ToolRequiresHumanApproval {
                            tool: tool.to_string(),
                        })
                    }
                }
            }
        }
        if !requirement.requires_mandate_scope {
            return self
                .settle_tool_decision(permit_id, tool, principal, now)
                .await;
        }
        let Some(case_id) = case_id else {
            return PolicyDecision::deny(DenyReason::MandateMissing);
        };
        let mandate = match self.mandates.latest_for_case(case_id, 256).await {
            Ok(Some(mandate)) => mandate,
            Ok(None) => return PolicyDecision::deny(DenyReason::MandateMissing),
            Err(error) => {
                return PolicyDecision::deny(DenyReason::MandateMissing).tap_backend(error);
            }
        };
        let request = MandateRequest {
            tool: Some(tool.to_string()),
            action: Some(requirement.action),
            repo: None,
            branch: None,
            path: None,
            tokens: None,
            cost_usd: None,
            wall_secs: None,
            runs: None,
        };
        let decision = self.authorize_mandate(&mandate, &request, now);
        if decision.is_allowed() {
            return self
                .settle_tool_decision(permit_id, tool, principal, now)
                .await;
        }
        decision
    }

    /// Spend the peeked permit (if any) and answer.
    ///
    /// The permit is consumed only when every other check already passed; if the
    /// spend loses a race (another call took it, or it expired between peek and
    /// here) the answer is a denial, because the consent is gone.
    async fn settle_tool_decision(
        &self,
        permit_id: Option<String>,
        tool: &str,
        principal: &Principal,
        now: DateTime<Utc>,
    ) -> PolicyDecision {
        let Some(id) = permit_id else {
            return PolicyDecision::allow();
        };
        let Some(approvals) = &self.approvals else {
            return PolicyDecision::deny(DenyReason::ToolRequiresHumanApproval {
                tool: tool.to_string(),
            });
        };
        match approvals.consume(&id, now).await {
            Ok(approval) => {
                tracing::info!(
                    tool = %tool,
                    approval = %approval.id,
                    requested_by = %principal.id,
                    "human-tier tool allowed by an approved request"
                );
                PolicyDecision::allow()
            }
            Err(error) => {
                tracing::warn!(
                    tool = %tool,
                    approval = %id,
                    error = %error,
                    "approval could not be consumed; denying the human-tier tool"
                );
                PolicyDecision::deny(DenyReason::ToolRequiresHumanApproval {
                    tool: tool.to_string(),
                })
            }
        }
    }

    // ── Mandate lifecycle ──────────────────────────────────────────────

    /// Issue (or delegate) a mandate.
    ///
    /// Enforced in order:
    /// 1. the grantor holds `mandate_issue` for the project;
    /// 2. a delegated mandate is provably narrower than its parent
    ///    ([`Mandate::validate_child`]);
    /// 3. the case does not already hold a different live mandate.
    #[allow(clippy::too_many_arguments)]
    pub async fn issue_mandate(
        &self,
        grantor: &Principal,
        case_id: &str,
        project_id: Option<&str>,
        scopes: Vec<MandateScope>,
        origin: MandateOrigin,
        expires_at: Option<DateTime<Utc>>,
        delegated_from: Option<&MandateId>,
        now: DateTime<Utc>,
    ) -> Result<Mandate, PolicyEngineError> {
        let decision = self.authorize(
            grantor,
            uc_types::governance::Permission::MandateIssue,
            project_id,
            now,
        );
        if decision.is_denied() {
            return Err(PolicyEngineError::Denied(Box::new(decision)));
        }
        if origin == MandateOrigin::HumanIssued && delegated_from.is_some() {
            return Err(PolicyEngineError::Widening(
                "a delegated mandate cannot claim the human origin".to_string(),
            ));
        }

        let version = self.version();
        let mut mandate = Mandate::new(case_id, grantor.clone(), origin, scopes, version, now);
        if let Some(expires_at) = expires_at {
            mandate = mandate.with_expiry(expires_at);
        }

        if let Some(parent_id) = delegated_from {
            let Some((parent, _)) = self.mandates.get(parent_id.as_str()).await? else {
                return Err(PolicyEngineError::UnknownMandate {
                    mandate_id: parent_id.0.clone(),
                });
            };
            mandate = mandate.delegate_from(&parent);
            let decision = parent.validate_child(&mandate, now, Some(version));
            if let MandateDecision::Denied { denial } = decision {
                return Err(PolicyEngineError::Widening(format!("{denial:?}")));
            }
            // A delegated mandate lives in the parent's case: authority flows with
            // the work, it does not create a second governed case.
            mandate.case_id = parent.case_id.clone();
        } else if let Some(existing) = self.mandates.latest_for_case(case_id, 256).await? {
            let live = !existing.is_revoked() && !existing.is_expired(now);
            if live && existing.id != mandate.id {
                return Err(PolicyEngineError::MandateConflict {
                    case_id: case_id.to_string(),
                    existing: existing.id.0.clone(),
                });
            }
        }

        self.mandates.put_new(&mandate).await?;
        Ok(mandate)
    }

    pub async fn revoke_mandate(
        &self,
        mandate_id: &str,
        by: &Principal,
        reason: &str,
        now: DateTime<Utc>,
    ) -> Result<Mandate, PolicyEngineError> {
        let decision = self.authorize(
            by,
            uc_types::governance::Permission::MandateRevoke,
            None,
            now,
        );
        if decision.is_denied() {
            return Err(PolicyEngineError::Denied(Box::new(decision)));
        }
        let mandate = self
            .mandates
            .revoke(mandate_id, by.clone(), reason, now)
            .await?;
        // Revocation is also recorded on the snapshot so in-flight checks fail
        // fast even before they re-read the mandate.
        if let Ok(mut guard) = self.snapshot.write() {
            guard.revoke_mandate(mandate_id, reason.to_string());
        }
        Ok(mandate)
    }

    pub async fn get_mandate(
        &self,
        mandate_id: &str,
    ) -> Result<Option<Mandate>, PolicyEngineError> {
        Ok(self
            .mandates
            .get(mandate_id)
            .await?
            .map(|(mandate, _version)| mandate))
    }

    pub async fn latest_mandate_for_case(
        &self,
        case_id: &str,
    ) -> Result<Option<Mandate>, PolicyEngineError> {
        Ok(self.mandates.latest_for_case(case_id, 256).await?)
    }

    /// A ready-made snapshot for local development: one operator principal with
    /// the full permission set. Never installed unless explicitly requested
    /// (`UC_POLICY_BOOTSTRAP=dev`).
    pub fn development_snapshot(operator: &str) -> PolicySnapshot {
        let principal = Principal::user(operator);
        let permissions = vec![
            uc_types::governance::Permission::TaskSubmit,
            uc_types::governance::Permission::TaskRead,
            uc_types::governance::Permission::TaskControl,
            uc_types::governance::Permission::CaseCreate,
            uc_types::governance::Permission::CaseRead,
            uc_types::governance::Permission::CaseControl,
            uc_types::governance::Permission::MandateIssue,
            uc_types::governance::Permission::MandateRevoke,
            uc_types::governance::Permission::TriggerIngest,
            uc_types::governance::Permission::WorkerScale,
            uc_types::governance::Permission::MemoryWrite,
            uc_types::governance::Permission::MergeGrant,
            uc_types::governance::Permission::AttentionResolve,
            uc_types::governance::Permission::ProactivePropose,
            uc_types::governance::Permission::ProactiveExecute,
        ];
        let mut snapshot = PolicySnapshot::empty(PolicyVersion::initial());
        // `with_grant` cannot fail for a User principal.
        if let Ok(next) = snapshot
            .clone()
            .with_grant(uc_types::governance::Grant::new(
                principal.clone(),
                permissions,
            ))
        {
            snapshot = next;
        }
        // The dashboard surface acts as two principals depending on the endpoint
        // family: `service:dashboard` for trigger ingestion/reads and
        // `user:dashboard` for the proactive control plane. Both get exactly the
        // permissions their handlers need — never `worker_scale`, `mandate_issue`
        // or `mandate_revoke`.
        let dashboard_permissions = vec![
            uc_types::governance::Permission::TaskSubmit,
            uc_types::governance::Permission::TaskRead,
            uc_types::governance::Permission::CaseCreate,
            uc_types::governance::Permission::CaseRead,
            uc_types::governance::Permission::CaseControl,
            uc_types::governance::Permission::AttentionResolve,
            uc_types::governance::Permission::TriggerIngest,
            uc_types::governance::Permission::ProactivePropose,
            uc_types::governance::Permission::ProactiveExecute,
        ];
        for principal in [
            Principal::service("dashboard"),
            Principal::user("dashboard"),
        ] {
            if let Ok(next) = snapshot
                .clone()
                .with_grant(uc_types::governance::Grant::new(
                    principal,
                    dashboard_permissions.clone(),
                ))
            {
                snapshot = next;
            }
        }
        snapshot
    }
}

fn mandate_denial(denial: MandateDenial) -> DenyReason {
    match denial {
        MandateDenial::Revoked { mandate_id, reason } => {
            DenyReason::MandateRevoked { mandate_id }.tap_detail(reason)
        }
        MandateDenial::Expired {
            mandate_id,
            expired_at,
        } => DenyReason::MandateExpired {
            mandate_id,
            expired_at,
        },
        MandateDenial::OutOfScope { scope, value } => {
            DenyReason::MandateOutOfScope { scope, value }
        }
        MandateDenial::WideningDenied { detail } => DenyReason::MandateWideningDenied { detail },
        MandateDenial::StalePolicyVersion {
            mandate_version,
            minimum,
        } => DenyReason::StalePolicyVersion {
            provided: mandate_version,
            minimum,
        },
        MandateDenial::OriginForbidden { origin } => DenyReason::MandateWideningDenied {
            detail: format!("forbidden mandate origin: {origin}"),
        },
    }
}

/// Tiny helper trait so a backend error can be logged without losing the
/// fail-closed denial that the caller sees.
trait TapBackend {
    fn tap_backend(self, error: MandateStoreError) -> Self;
}

impl TapBackend for PolicyDecision {
    fn tap_backend(self, error: MandateStoreError) -> Self {
        tracing::warn!(error = %error, "mandate lookup failed; denying");
        self
    }
}

trait TapDetail {
    fn tap_detail(self, detail: String) -> Self;
}

impl TapDetail for DenyReason {
    fn tap_detail(self, detail: String) -> Self {
        tracing::debug!(detail, "mandate revoked");
        self
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::governance::record_store::InMemoryRecordStore;
    use uc_types::governance::{ActionClass, Permission};

    fn now() -> DateTime<Utc> {
        DateTime::parse_from_rfc3339("2026-01-01T00:00:00Z")
            .unwrap()
            .with_timezone(&Utc)
    }

    fn engine() -> PolicyEngine {
        let records: Arc<dyn RecordStore> = Arc::new(InMemoryRecordStore::new());
        let mandates = Arc::new(MandateStore::new(records.clone()));
        PolicyEngine::with_snapshot(
            records,
            mandates,
            PolicyEngine::development_snapshot("alice"),
        )
    }

    #[tokio::test]
    async fn empty_snapshot_denies_everything() {
        let records: Arc<dyn RecordStore> = Arc::new(InMemoryRecordStore::new());
        let mandates = Arc::new(MandateStore::new(records.clone()));
        let engine = PolicyEngine::new(records, mandates);
        assert!(engine
            .authorize(
                &Principal::user("alice"),
                Permission::TaskSubmit,
                None,
                now()
            )
            .is_denied());
    }

    #[tokio::test]
    async fn operator_can_issue_and_revoke_a_mandate() {
        let engine = engine();
        let mandate = engine
            .issue_mandate(
                &Principal::user("alice"),
                "case-1",
                Some("proj"),
                vec![MandateScope::Action {
                    class: ActionClass::Write,
                }],
                MandateOrigin::HumanIssued,
                None,
                None,
                now(),
            )
            .await
            .expect("issue");
        assert_eq!(mandate.grantor, Principal::user("alice"));
        let revoked = engine
            .revoke_mandate(
                mandate.id.as_str(),
                &Principal::user("alice"),
                "done",
                now(),
            )
            .await
            .expect("revoke");
        assert!(revoked.is_revoked());
        // A revoked mandate no longer authorizes anything.
        let decision =
            engine.authorize_mandate(&revoked, &MandateRequest::action(ActionClass::Write), now());
        assert_eq!(decision.deny_code(), Some("mandate_revoked"));
    }

    #[tokio::test]
    async fn external_trigger_cannot_issue_a_mandate() {
        let engine = engine();
        let error = engine
            .issue_mandate(
                &Principal::external_trigger("github"),
                "case-1",
                Some("proj"),
                vec![],
                MandateOrigin::HumanIssued,
                None,
                None,
                now(),
            )
            .await
            .expect_err("trigger cannot issue");
        assert!(matches!(error, PolicyEngineError::Denied(_)));
    }

    #[tokio::test]
    async fn second_live_mandate_for_a_case_is_refused() {
        let engine = engine();
        let first = engine
            .issue_mandate(
                &Principal::user("alice"),
                "case-1",
                Some("proj"),
                vec![MandateScope::Action {
                    class: ActionClass::Read,
                }],
                MandateOrigin::HumanIssued,
                None,
                None,
                now(),
            )
            .await
            .expect("first");
        let second = engine
            .issue_mandate(
                &Principal::user("alice"),
                "case-1",
                Some("proj"),
                vec![MandateScope::Action {
                    class: ActionClass::Write,
                }],
                MandateOrigin::HumanIssued,
                None,
                None,
                now(),
            )
            .await;
        assert!(matches!(
            second,
            Err(PolicyEngineError::MandateConflict { .. })
        ));
        // Revoking then issuing works.
        engine
            .revoke_mandate(
                first.id.as_str(),
                &Principal::user("alice"),
                "narrow",
                now(),
            )
            .await
            .unwrap();
        assert!(engine
            .issue_mandate(
                &Principal::user("alice"),
                "case-1",
                Some("proj"),
                vec![MandateScope::Action {
                    class: ActionClass::Write,
                }],
                MandateOrigin::HumanIssued,
                None,
                None,
                now(),
            )
            .await
            .is_ok());
    }

    #[tokio::test]
    async fn delegation_cannot_widen() {
        let engine = engine();
        let parent = engine
            .issue_mandate(
                &Principal::user("alice"),
                "case-1",
                Some("proj"),
                vec![MandateScope::Action {
                    class: ActionClass::Read,
                }],
                MandateOrigin::HumanIssued,
                None,
                None,
                now(),
            )
            .await
            .unwrap();
        let wider = engine
            .issue_mandate(
                &Principal::user("alice"),
                "case-1",
                Some("proj"),
                vec![MandateScope::Action {
                    class: ActionClass::Merge,
                }],
                MandateOrigin::CaseDelegated,
                None,
                Some(&parent.id),
                now(),
            )
            .await;
        assert!(matches!(wider, Err(PolicyEngineError::Widening(_))));
    }

    #[tokio::test]
    async fn tool_check_requires_a_mandate_scope_only_for_state_changing_tools() {
        let engine = engine();
        // Read tools need no mandate.
        assert!(engine
            .authorize_tool(&Principal::user("alice"), "Read", Some("proj"), None, now())
            .await
            .is_allowed());
        // Edit needs one.
        assert_eq!(
            engine
                .authorize_tool(&Principal::user("alice"), "Edit", Some("proj"), None, now())
                .await
                .deny_code(),
            Some("mandate_missing")
        );
        // Unknown tool fails closed regardless of grants.
        assert_eq!(
            engine
                .authorize_tool(
                    &Principal::user("alice"),
                    "rm_rf",
                    Some("proj"),
                    None,
                    now()
                )
                .await
                .deny_code(),
            Some("tool_not_permitted")
        );
    }

    #[tokio::test]
    async fn human_tier_tools_require_the_operator_permission() {
        // Holding the tool's coarse action permission is not enough for a
        // human-tier tool: `scale_workers` / `docker_run` also require the
        // operator permission `proactive_execute`, which the structural ceiling
        // denies to agents and workers. Without this the `human_approval` flag
        // would be decoration.
        let records: Arc<dyn RecordStore> = Arc::new(InMemoryRecordStore::new());
        let mandates = Arc::new(MandateStore::new(records.clone()));
        let snapshot = PolicySnapshot::empty(PolicyVersion::initial())
            .with_grant(uc_types::governance::Grant::new(
                Principal::user("bob"),
                vec![Permission::WorkerScale, Permission::TaskSubmit],
            ))
            .expect("a user may hold these");
        let engine = PolicyEngine::with_snapshot(records, mandates, snapshot);

        assert_eq!(
            engine
                .authorize_tool(&Principal::user("bob"), "scale_workers", None, None, now())
                .await
                .deny_code(),
            Some("tool_requires_human_approval")
        );
        // `docker_run` is human-tier too, and `task_submit` is granted, so only
        // the operator tier stands in the way.
        assert_eq!(
            engine
                .authorize_tool(&Principal::user("bob"), "docker_run", None, None, now())
                .await
                .deny_code(),
            Some("tool_requires_human_approval")
        );
        // A non-human-tier tool with the same grants still reaches its own checks.
        assert_eq!(
            engine
                .authorize_tool(&Principal::user("bob"), "Edit", None, None, now())
                .await
                .deny_code(),
            Some("mandate_missing")
        );
    }

    #[tokio::test]
    async fn a_human_approval_unlocks_a_human_tier_tool_for_exactly_one_call() {
        use uc_types::governance::{
            ActionClass, ApprovalRequest, Mandate, MandateOrigin, MandateScope, ToolApproval,
        };

        let records: Arc<dyn RecordStore> = Arc::new(InMemoryRecordStore::new());
        let mandates = Arc::new(MandateStore::new(records.clone()));
        let approvals = Arc::new(super::super::ApprovalStore::new(records.clone()));
        let snapshot = PolicySnapshot::empty(PolicyVersion::initial())
            .with_grant(uc_types::governance::Grant::new(
                Principal::user("bob"),
                vec![Permission::WorkerScale, Permission::TaskSubmit],
            ))
            .expect("a user may hold these");
        let engine = PolicyEngine::with_snapshot(records, mandates.clone(), snapshot)
            .with_approvals(approvals.clone());

        let case_id = "case-approval";
        let mandate = Mandate::new(
            case_id,
            Principal::user("alice"),
            MandateOrigin::HumanIssued,
            vec![
                // A mandate must name the tool it covers; the action class alone is
                // not enough, which is what makes `tool_requires_mandate_scope` mean
                // "this exact tool was granted".
                MandateScope::Tool {
                    name: "scale_workers".to_string(),
                },
                MandateScope::Action {
                    class: ActionClass::Scale,
                },
                MandateScope::MaxRuns { max: 5 },
            ],
            PolicyVersion(1),
            now(),
        );
        mandates.put_new(&mandate).await.expect("mandate");

        // 1. Without an approval, an operator-tier tool is closed to bob.
        assert_eq!(
            engine
                .authorize_tool(
                    &Principal::user("bob"),
                    "scale_workers",
                    Some("proj"),
                    Some(case_id),
                    now()
                )
                .await
                .deny_code(),
            Some("tool_requires_human_approval")
        );

        // 2. An agent asks; a human approves exactly this tool+case.
        let request = ToolApproval::request(
            ApprovalRequest {
                tool: "scale_workers".to_string(),
                case_id: Some(case_id.to_string()),
                project_id: Some("proj".to_string()),
                action_digest: Some("scale:worker=3".to_string()),
                requested_by: Principal::agent("executor"),
                reason: Some("queue is backing up".to_string()),
            },
            now(),
            None,
        );
        let (approval, created) = approvals.request(request).await.expect("request recorded");
        assert!(created);
        approvals
            .approve(
                &approval.id,
                &Principal::user("alice"),
                Some("ok".into()),
                now(),
            )
            .await
            .expect("human approves");

        // 3. The same call now passes the human gate …
        let approved_decision = engine
            .authorize_tool(
                &Principal::user("bob"),
                "scale_workers",
                Some("proj"),
                Some(case_id),
                now(),
            )
            .await;
        assert!(
            approved_decision.is_allowed(),
            "an approved action must be usable, got {:?}",
            approved_decision.deny_code()
        );

        // … and the permit is spent.
        let used = approvals
            .list(Some(uc_types::governance::ApprovalState::Used), 10)
            .await
            .expect("list approvals");
        assert_eq!(used.len(), 1, "the permit must be consumed");
        assert!(used[0].used_at.is_some());

        // 4. A second call needs a fresh decision: consent is not a standing grant.
        assert_eq!(
            engine
                .authorize_tool(
                    &Principal::user("bob"),
                    "scale_workers",
                    Some("proj"),
                    Some(case_id),
                    now()
                )
                .await
                .deny_code(),
            Some("tool_requires_human_approval")
        );
    }

    #[tokio::test]
    async fn a_permit_is_not_spent_when_a_later_check_denies() {
        use uc_types::governance::{ActionClass, ApprovalRequest, MandateScope, ToolApproval};

        let records: Arc<dyn RecordStore> = Arc::new(InMemoryRecordStore::new());
        let mandates = Arc::new(MandateStore::new(records.clone()));
        let approvals = Arc::new(super::super::ApprovalStore::new(records.clone()));
        let snapshot = PolicySnapshot::empty(PolicyVersion::initial())
            .with_grant(uc_types::governance::Grant::new(
                Principal::user("bob"),
                vec![Permission::WorkerScale],
            ))
            .expect("grant");
        let engine = PolicyEngine::with_snapshot(records, mandates, snapshot)
            .with_approvals(approvals.clone());

        // Approved for a case that has NO mandate: the permission and human gates
        // pass, then the mandate check denies — and the consent must survive that.
        let request = ToolApproval::request(
            ApprovalRequest {
                tool: "scale_workers".to_string(),
                case_id: Some("case-without-mandate".to_string()),
                project_id: None,
                action_digest: None,
                requested_by: Principal::agent("executor"),
                reason: None,
            },
            now(),
            None,
        );
        let (approval, _) = approvals.request(request).await.expect("request");
        approvals
            .approve(&approval.id, &Principal::user("alice"), None, now())
            .await
            .expect("approved");

        assert_eq!(
            engine
                .authorize_tool(
                    &Principal::user("bob"),
                    "scale_workers",
                    None,
                    Some("case-without-mandate"),
                    now()
                )
                .await
                .deny_code(),
            Some("mandate_missing"),
            "the human gate passed; the mandate check is what denies"
        );
        // Still approved, not spent.
        let approved = approvals
            .list(Some(uc_types::governance::ApprovalState::Approved), 10)
            .await
            .expect("list");
        assert_eq!(
            approved.len(),
            1,
            "a denial for an unrelated reason must not burn the permit"
        );
        // A read-only mandate does not cover Scale: same story.
        let _ = MandateScope::Action {
            class: ActionClass::Read,
        };
    }

    #[tokio::test]
    async fn human_tier_gate_passes_before_the_mandate_scope_check() {
        // The dev snapshot gives `alice` `proactive_execute`, so the human-tier
        // gate passes and the denial moves on to the mandate scope — the ordering
        // is what lets an operator tell the two failures apart.
        let engine = engine();
        assert_eq!(
            engine
                .authorize_tool(
                    &Principal::user("alice"),
                    "scale_workers",
                    Some("proj"),
                    None,
                    now()
                )
                .await
                .deny_code(),
            Some("mandate_missing")
        );
    }

    #[tokio::test]
    async fn scale_workers_requires_its_own_permission() {
        let engine = engine();
        assert!(engine
            .authorize_scale_workers(&Principal::user("alice"), now())
            .is_allowed());
        // The dashboard service deliberately does not hold WorkerScale.
        assert_eq!(
            engine
                .authorize_scale_workers(&Principal::service("dashboard"), now())
                .deny_code(),
            Some("missing_permission")
        );
    }

    #[tokio::test]
    async fn sandbox_validation_blocks_docker_socket() {
        let engine = engine();
        let mut profile = SandboxSecurityProfile::hardened_worker();
        profile.docker_socket_mounted = true;
        let decision = engine.validate_sandbox(&profile);
        assert_eq!(decision.deny_code(), Some("sandbox_violation"));
    }

    #[tokio::test]
    async fn snapshot_round_trips_through_the_store_and_bumps_version() {
        let engine = engine();
        let installed = engine
            .install(PolicyEngine::development_snapshot("bob"))
            .await
            .expect("install");
        assert_eq!(installed.0, 2);
        // A fresh engine over the same records loads it back.
        let records = engine.view.raw().clone();
        let mandates = Arc::new(MandateStore::new(records.clone()));
        let reloaded = PolicyEngine::with_snapshot(
            records,
            mandates,
            PolicySnapshot::empty(PolicyVersion::initial()),
        );
        let loaded = reloaded.load_persisted().await.unwrap();
        assert_eq!(loaded, Some(PolicyVersion(2)));
        assert!(reloaded
            .authorize(&Principal::user("bob"), Permission::TaskSubmit, None, now())
            .is_allowed());
    }
}
