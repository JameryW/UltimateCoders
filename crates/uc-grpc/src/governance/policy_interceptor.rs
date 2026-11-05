//! Principal resolution and the authorization gate every governance RPC passes
//! through.
//!
//! ## Why a separate module
//!
//! Authorization is the one thing that must not be re-implemented per service.
//! Every gRPC service in [`super`] starts by resolving the caller here and then
//! calls [`require`] with the permission the operation needs; the NATS bridge
//! does the same because it shares the service operation functions. If a new RPC
//! forgets to call [`require`] it is visible in review as a missing line with a
//! known name, not as an ad-hoc check that looks different.
//!
//! ## Identity rules
//!
//! The transport (bearer token / cluster-internal NATS) authenticates *the
//! channel*. This module resolves *the principal* — who the caller claims to be
//! inside that channel — and enforces two rules:
//!
//! 1. **Only verifiable principals may be declared.** `external_trigger` and
//!    `anonymous` are refused outright. An inbound webhook is authenticated by
//!    the API layer and then acts as the `service` principal that verified it; it
//!    is never allowed to present itself to the gateway as the trigger itself.
//!    Letting a caller declare `anonymous` would also be a way to *escalate* past
//!    per-principal grants, because "unauthenticated" is a distinct state the
//!    policy engine denies by default — declaring it would be a downgrade, but
//!    the honest failure is a refusal, not a silent re-interpretation.
//! 2. **Absent identity means the operator surface.** A gRPC request without a
//!    declared principal is the dashboard: the only client that holds the
//!    transport credential and speaks for the operator. It resolves to
//!    `service/dashboard`, the same identity the Python dashboard uses for its
//!    own governance ops, so a policy written once covers both paths.
//!
//! ## Denials
//!
//! A policy denial is always `Status::permission_denied` whose message starts
//! with the deny code (`missing_permission`, `mandate_expired`, …) followed by
//! the evaluated reason. Dashboards and the Python bridge surface that code
//! verbatim, so it must not be translated into prose.

// `tonic::Status` is the crate-wide transport error type and the generated service
// traits return it by value; newer Clippy flags it as a large `Err` variant. The
// crate already allows this for the generated code (see `crate::ultimate_coders`),
// and these helpers exist to produce exactly that type.
#![allow(clippy::result_large_err)]

use chrono::{DateTime, Utc};
use tonic::{Request, Status};
use uc_engine::governance::Governance;
use uc_types::governance::{Permission, PolicyDecision, Principal, PrincipalKind};

/// Metadata key carrying a declared principal kind (`user`, `service`, `agent`,
/// `worker`). Set by callers that need to act on behalf of another principal.
pub const PRINCIPAL_KIND_METADATA: &str = "x-uc-principal-kind";

/// Metadata key carrying the declared principal id.
pub const PRINCIPAL_ID_METADATA: &str = "x-uc-principal-id";

/// Identity assumed when a request declares none: the dashboard operator
/// surface. See the module docs for why this is a `service`, not `anonymous`.
pub const DEFAULT_PRINCIPAL_KIND: PrincipalKind = PrincipalKind::Service;

/// Default id for [`DEFAULT_PRINCIPAL_KIND`].
pub const DEFAULT_PRINCIPAL_ID: &str = "dashboard";

/// Deny code used when a caller tries to declare an unverifiable identity.
pub const DECLARATION_REFUSED: &str = "principal_kind_not_declarable";

/// Deny code used when a declared identity is structurally invalid (empty id).
pub const INVALID_PRINCIPAL: &str = "invalid_principal";

/// Parse a wire token into a [`PrincipalKind`].
///
/// `PrincipalKind` deliberately has no lossy parser in `uc-types` (a typo must
/// not silently become a different kind), so the mapping lives with the parser
/// that owns the "what may be declared" rule.
fn parse_kind(token: &str) -> Option<PrincipalKind> {
    Some(match token.trim().to_ascii_lowercase().as_str() {
        "user" => PrincipalKind::User,
        "service" => PrincipalKind::Service,
        "agent" => PrincipalKind::Agent,
        "worker" => PrincipalKind::Worker,
        "external_trigger" | "externaltrigger" => PrincipalKind::ExternalTrigger,
        "anonymous" => PrincipalKind::Anonymous,
        _ => return None,
    })
}

/// Resolve an explicitly declared `(kind, id)` pair.
///
/// Refuses a kind that cannot be verified at this boundary ([`PrincipalKind::ExternalTrigger`],
/// [`PrincipalKind::Anonymous`]) with `permission_denied`, because the caller is
/// asking for an identity the gateway is not able to vouch for. A syntactically
/// unknown kind or an empty id is `invalid_argument`: the request is malformed,
/// not merely disallowed.
pub fn resolve_principal(kind: &str, id: &str) -> Result<Principal, Status> {
    let kind = parse_kind(kind).ok_or_else(|| {
        Status::invalid_argument(format!(
            "unknown principal_kind '{kind}' (expected user | service | agent | worker)"
        ))
    })?;
    let id = id.trim();
    if id.is_empty() {
        return Err(Status::invalid_argument("principal_id must not be empty"));
    }
    if !kind.may_hold(Permission::TaskSubmit) && kind != PrincipalKind::Anonymous {
        // Unreachable today; kept so a future widening of `may_hold` cannot
        // silently admit a principal kind here.
        return Err(Status::permission_denied(format!(
            "{DECLARATION_REFUSED}: principal kind {} may not act over gRPC",
            kind.as_str()
        )));
    }
    match kind {
        PrincipalKind::ExternalTrigger | PrincipalKind::Anonymous => {
            Err(Status::permission_denied(format!(
                "{DECLARATION_REFUSED}: principal kind {} cannot be declared at this boundary \
                 (a webhook acts as the service that verified it)",
                kind.as_str()
            )))
        }
        _ => Ok(Principal::new(kind, id)),
    }
}

/// Resolve a caller that may or may not have declared an identity.
///
/// A message with neither field set resolves to [`DEFAULT_PRINCIPAL_KIND`] /
/// [`DEFAULT_PRINCIPAL_ID`]. A message that sets one but not the other is
/// malformed: half an identity cannot be checked, so it is rejected rather than
/// guessed.
pub fn resolve_declared_principal(
    kind: Option<&str>,
    id: Option<&str>,
) -> Result<Principal, Status> {
    let declared_kind = kind.map(str::trim).filter(|value| !value.is_empty());
    let declared_id = id.map(str::trim).filter(|value| !value.is_empty());
    match (declared_kind, declared_id) {
        (None, None) => Ok(Principal::new(DEFAULT_PRINCIPAL_KIND, DEFAULT_PRINCIPAL_ID)),
        (Some(_), None) => Err(Status::invalid_argument(format!(
            "{INVALID_PRINCIPAL}: principal_kind was set without principal_id"
        ))),
        (None, Some(_)) => Err(Status::invalid_argument(format!(
            "{INVALID_PRINCIPAL}: principal_id was set without principal_kind"
        ))),
        (Some(kind), Some(id)) => resolve_principal(kind, id),
    }
}

/// Resolve the caller of a request that carries no identity field of its own.
///
/// Used by RPCs whose proto message predates the governance plane
/// (`ScaleWorkers`). Metadata is the only place such a caller can name itself;
/// absent metadata it is the operator surface, exactly as
/// [`resolve_declared_principal`] treats an empty message.
pub fn resolve_principal_from_metadata<T>(request: &Request<T>) -> Result<Principal, Status> {
    let metadata = request.metadata();
    let read = |key: &str| -> Option<String> {
        metadata
            .get(key)
            .and_then(|value| value.to_str().ok())
            .map(str::to_string)
    };
    let kind = read(PRINCIPAL_KIND_METADATA);
    let id = read(PRINCIPAL_ID_METADATA);
    resolve_declared_principal(kind.as_deref(), id.as_deref())
}

/// The machine-readable deny code of a denied decision, or `"policy_denied"`.
pub fn denial_code(decision: &PolicyDecision) -> &'static str {
    decision.deny_code().unwrap_or("policy_denied")
}

/// Translate a policy decision into a gRPC status.
///
/// The message starts with the deny code so a client can act on it without
/// parsing English, and carries the full evaluated reason for the operator.
pub fn deny_status(decision: &PolicyDecision) -> Status {
    match decision.deny_reason() {
        Some(reason) => Status::permission_denied(format!("{}: {reason:?}", reason.code())),
        None => Status::permission_denied("policy_denied: the policy engine refused the request"),
    }
}

/// Return `Ok(())` when `decision` allows the operation, otherwise the denial.
///
/// `action` names the operation for the operator log; it never replaces the deny
/// code in the status message.
pub fn require_decision(decision: &PolicyDecision, action: &str) -> Result<(), Status> {
    if decision.is_allowed() {
        return Ok(());
    }
    let status = deny_status(decision);
    tracing::warn!(
        action,
        deny_code = denial_code(decision),
        "governance operation denied by policy"
    );
    Err(status)
}

/// The authorization gate: evaluate `permission` for `principal` and fail closed.
pub fn require(
    governance: &Governance,
    principal: &Principal,
    permission: Permission,
    project: Option<&str>,
    now: DateTime<Utc>,
) -> Result<(), Status> {
    let decision = governance
        .policy
        .authorize(principal, permission, project, now);
    require_decision(
        &decision,
        &format!(
            "{}:{} {}",
            principal.kind.as_str(),
            principal.id,
            permission.as_str()
        ),
    )
}

/// Gate for the highest-blast-radius operation the gateway exposes.
///
/// `ScaleWorkers` shells out to `docker compose`, so it has its own entry point
/// on the policy engine ([`uc_engine::governance::PolicyEngine::authorize_scale_workers`])
/// which applies both the permission and the structural ceiling. Routing through
/// here keeps `worker_service` from open-coding the check.
pub async fn require_scale_workers(
    governance: &Governance,
    principal: &Principal,
    case_id: Option<&str>,
    now: DateTime<Utc>,
) -> Result<(), Status> {
    // The action-level human-tier gate: the coarse permission alone is not
    // consent. A caller below the operator tier must name the case whose
    // approved single-use permit authorizes this resize (the permit is spent
    // here, on success); the operator tier passes on `proactive_execute`.
    let decision = governance
        .policy
        .authorize_human_tier_tool(principal, "scale_workers", None, case_id, now)
        .await;
    require_decision(&decision, "worker_scale")
}

#[cfg(test)]
mod tests {
    use super::*;
    use uc_types::governance::{DenyReason, Grant, PolicySnapshot, PolicyVersion};

    async fn governance_with(grants: Vec<Grant>) -> Governance {
        let governance = Governance::in_memory();
        let mut snapshot = PolicySnapshot::empty(PolicyVersion::initial());
        for grant in grants {
            snapshot = snapshot
                .with_grant(grant)
                .expect("grant is structurally valid");
        }
        // `install` is the only way to swap a snapshot on the aggregate, and it
        // persists it, so the test exercises the same path the gateway uses.
        governance.policy.install(snapshot).await.unwrap();
        governance
    }

    #[test]
    fn declared_user_principal_is_accepted() {
        let principal = resolve_principal("user", "alice").unwrap();
        assert_eq!(principal.kind, PrincipalKind::User);
        assert_eq!(principal.id, "alice");
    }

    #[test]
    fn external_trigger_cannot_be_declared() {
        let error = resolve_principal("external_trigger", "github").unwrap_err();
        assert_eq!(error.code(), tonic::Code::PermissionDenied);
        assert!(error.message().starts_with(DECLARATION_REFUSED));
    }

    #[test]
    fn anonymous_cannot_be_declared() {
        let error = resolve_principal("anonymous", "nobody").unwrap_err();
        assert_eq!(error.code(), tonic::Code::PermissionDenied);
    }

    #[test]
    fn unknown_kind_and_empty_id_are_invalid_arguments() {
        assert_eq!(
            resolve_principal("robot", "x").unwrap_err().code(),
            tonic::Code::InvalidArgument
        );
        assert_eq!(
            resolve_principal("user", "  ").unwrap_err().code(),
            tonic::Code::InvalidArgument
        );
    }

    #[test]
    fn absent_identity_is_the_dashboard_service() {
        let principal = resolve_declared_principal(None, None).unwrap();
        assert_eq!(principal, Principal::service(DEFAULT_PRINCIPAL_ID));
        // Blank strings behave like absent fields: the protobuf default.
        let blank = resolve_declared_principal(Some(""), Some("")).unwrap();
        assert_eq!(blank, principal);
    }

    #[test]
    fn half_an_identity_is_rejected() {
        assert_eq!(
            resolve_declared_principal(Some("user"), None)
                .unwrap_err()
                .code(),
            tonic::Code::InvalidArgument
        );
        assert_eq!(
            resolve_declared_principal(None, Some("alice"))
                .unwrap_err()
                .code(),
            tonic::Code::InvalidArgument
        );
    }

    #[test]
    fn metadata_identity_overrides_the_default() {
        let mut request = Request::new(());
        request
            .metadata_mut()
            .insert(PRINCIPAL_KIND_METADATA, "agent".parse().unwrap());
        request
            .metadata_mut()
            .insert(PRINCIPAL_ID_METADATA, "planner".parse().unwrap());
        let principal = resolve_principal_from_metadata(&request).unwrap();
        assert_eq!(principal, Principal::agent("planner"));
    }

    #[tokio::test]
    async fn missing_permission_is_a_permission_denied_status_with_the_code() {
        let governance = governance_with(vec![Grant::new(
            Principal::user("alice"),
            vec![Permission::CaseRead],
        )])
        .await;
        let error = require(
            &governance,
            &Principal::user("alice"),
            Permission::WorkerScale,
            None,
            Utc::now(),
        )
        .unwrap_err();
        assert_eq!(error.code(), tonic::Code::PermissionDenied);
        assert!(
            error.message().starts_with("missing_permission"),
            "status must carry the deny code, got: {}",
            error.message()
        );
    }

    #[tokio::test]
    async fn granted_permission_passes() {
        let governance = governance_with(vec![Grant::new(
            Principal::user("alice"),
            vec![Permission::CaseCreate],
        )])
        .await;
        assert!(require(
            &governance,
            &Principal::user("alice"),
            Permission::CaseCreate,
            Some("proj"),
            Utc::now(),
        )
        .is_ok());
    }

    #[tokio::test]
    async fn anonymous_is_denied_before_any_grant_is_consulted() {
        let governance = governance_with(vec![Grant::new(
            Principal::user("alice"),
            vec![Permission::CaseCreate],
        )])
        .await;
        let error = require(
            &governance,
            &Principal::anonymous(),
            Permission::CaseCreate,
            None,
            Utc::now(),
        )
        .unwrap_err();
        assert!(error.message().starts_with("unauthenticated"));
    }

    #[tokio::test]
    async fn scale_workers_denial_is_a_permission_denied_status() {
        let governance = governance_with(vec![Grant::new(
            Principal::service("dashboard"),
            vec![Permission::TaskSubmit],
        )])
        .await;
        let error = require_scale_workers(
            &governance,
            &Principal::service("dashboard"),
            None,
            Utc::now(),
        )
        .await
        .unwrap_err();
        assert_eq!(error.code(), tonic::Code::PermissionDenied);
        assert!(error.message().starts_with("missing_permission"));
    }

    #[test]
    fn deny_status_renders_the_reason_without_the_code_prefix() {
        let decision = PolicyDecision::deny(DenyReason::Unauthenticated);
        assert_eq!(denial_code(&decision), "unauthenticated");
        assert!(deny_status(&decision)
            .message()
            .starts_with("unauthenticated"));
    }
}
