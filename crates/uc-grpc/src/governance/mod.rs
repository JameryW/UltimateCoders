//! Governance plane at the transport boundary: the four gRPC services, the
//! principal/policy gate, and the NATS JSON bridge.
//!
//! The domain rules do not live here. They live in [`uc_types::governance`]
//! (pure data + invariants) and [`uc_engine::governance`] (durable stores +
//! deterministic decisions). This module is the *edge*: it turns proto / JSON
//! payloads into those calls and turns their answers back into proto / JSON.
//!
//! ## One implementation per operation
//!
//! Every operation is a free function in its service module
//! ([`case_service::create_case`], [`trigger_service::ingest`], …). The tonic
//! service method and the NATS bridge handler both call that function, so the
//! rules cannot fork between the two transports. The function owns the policy
//! check, the store call and the wire projection; the transports own nothing but
//! decoding and encoding.
//!
//! ## Failure mapping
//!
//! * unknown / malformed input → `invalid_argument`
//! * policy denial → `permission_denied`, message prefixed with the deny code
//! * missing record → `not_found`
//! * rejected state-machine transition or submission conflict → `failed_precondition`
//! * store or serialization failure → `internal`
//!
//! The bridge maps the same failures onto `{"ok": false, "error": {"code",
//! "message"}}` with the very same code strings, so a dashboard cannot tell
//! which transport answered — which is the point.

pub mod attention_service;
pub mod bridge;
pub mod case_service;
pub mod policy_interceptor;
pub mod resume_flow;
pub mod proactive_service;
pub mod trigger_service;

use chrono::{DateTime, Utc};
use tonic::{Code, Status};
use uc_engine::governance::Governance;
use uc_types::governance::PolicyDecision;

pub use attention_service::{AttentionServiceImpl, AttentionItemView, CapacityView};
pub use bridge::OUTBOX_DRAIN_LIMIT;
pub use case_service::{CaseRunView, CaseServiceImpl, CaseView, MandateView};
pub use policy_interceptor::{
    denial_code, deny_status, require, require_decision, require_scale_workers, resolve_principal,
    resolve_principal_from_metadata,
};
pub use proactive_service::{ProactiveServiceImpl, ProactiveStateView, ProposalView};
pub use trigger_service::{SubscriptionView, TriggerServiceImpl};

/// Page size used when a caller does not ask for one.
pub const DEFAULT_LIST_LIMIT: u32 = 50;

/// Hard cap on a page size — mirrors
/// [`uc_engine::governance::GOVERNANCE_LIST_LIMIT`] so the transport cannot ask
/// the store for more than it is willing to return.
pub const MAX_LIST_LIMIT: u32 = uc_engine::governance::GOVERNANCE_LIST_LIMIT as u32;

/// The four governance services, ready to be served.
///
/// Kept as a separate factory from [`crate::server::GrpcServer::into_services`]
/// so the execution-plane services keep their own tuple (widening that one would
/// break every existing caller) and so a test can build the governance plane
/// without constructing an engine.
pub fn services(
    governance: std::sync::Arc<Governance>,
) -> (
    crate::ultimate_coders::case_service_server::CaseServiceServer<CaseServiceImpl>,
    crate::ultimate_coders::trigger_service_server::TriggerServiceServer<TriggerServiceImpl>,
    crate::ultimate_coders::attention_service_server::AttentionServiceServer<AttentionServiceImpl>,
    crate::ultimate_coders::proactive_service_server::ProactiveServiceServer<ProactiveServiceImpl>,
) {
    use crate::ultimate_coders::attention_service_server::AttentionServiceServer;
    use crate::ultimate_coders::case_service_server::CaseServiceServer;
    use crate::ultimate_coders::proactive_service_server::ProactiveServiceServer;
    use crate::ultimate_coders::trigger_service_server::TriggerServiceServer;

    (
        CaseServiceServer::new(CaseServiceImpl::new(governance.clone())),
        TriggerServiceServer::new(TriggerServiceImpl::new(governance.clone())),
        AttentionServiceServer::new(AttentionServiceImpl::new(governance.clone())),
        ProactiveServiceServer::new(ProactiveServiceImpl::new(governance)),
    )
}

/// Same as [`services`], but the trigger service may start work: ingestion of an
/// opted-in event can resume a case, which needs the task store and the task-event
/// channel the `SubmitTask` RPC writes to.
#[allow(clippy::type_complexity)]
pub fn services_with_dispatch(
    governance: std::sync::Arc<Governance>,
    task_store: std::sync::Arc<tokio::sync::Mutex<crate::server::TaskStore>>,
    event_tx: tokio::sync::broadcast::Sender<crate::ultimate_coders::TaskEvent>,
) -> (
    crate::ultimate_coders::case_service_server::CaseServiceServer<CaseServiceImpl>,
    crate::ultimate_coders::trigger_service_server::TriggerServiceServer<TriggerServiceImpl>,
    crate::ultimate_coders::attention_service_server::AttentionServiceServer<AttentionServiceImpl>,
    crate::ultimate_coders::proactive_service_server::ProactiveServiceServer<ProactiveServiceImpl>,
) {
    use crate::ultimate_coders::attention_service_server::AttentionServiceServer;
    use crate::ultimate_coders::case_service_server::CaseServiceServer;
    use crate::ultimate_coders::proactive_service_server::ProactiveServiceServer;
    use crate::ultimate_coders::trigger_service_server::TriggerServiceServer;

    (
        CaseServiceServer::new(CaseServiceImpl::new(governance.clone())),
        TriggerServiceServer::new(TriggerServiceImpl::with_dispatch(
            governance.clone(),
            task_store,
            event_tx,
        )),
        AttentionServiceServer::new(AttentionServiceImpl::new(governance.clone())),
        ProactiveServiceServer::new(ProactiveServiceImpl::new(governance)),
    )
}

/// A governance operation failure, in the vocabulary both transports speak.
///
/// Operations return this instead of `tonic::Status` so the NATS bridge can
/// report the same machine-readable `code` the gRPC status message carries: a
/// dashboard must not be able to tell which transport rejected it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OpError {
    /// Machine-readable code. For a policy denial this is the policy engine's own
    /// deny code (`missing_permission`, `mandate_expired`, …).
    pub code: String,
    /// Human-readable explanation, including the evaluated facts.
    pub message: String,
    /// gRPC status code the failure maps onto.
    pub status: Code,
}

impl OpError {
    fn new(status: Code, code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            status,
        }
    }

    /// Malformed or unknown input.
    pub fn invalid(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self::new(Code::InvalidArgument, code, message)
    }

    /// The caller is not allowed to perform the operation.
    pub fn denied(decision: &PolicyDecision) -> Self {
        match decision.deny_reason() {
            Some(reason) => Self::new(
                Code::PermissionDenied,
                reason.code(),
                format!("{reason:?}"),
            ),
            None => Self::new(
                Code::PermissionDenied,
                "policy_denied",
                "the policy engine refused the request",
            ),
        }
    }

    /// A denial that did not come from a [`PolicyDecision`] (e.g. an identity
    /// claim the transport refuses to accept).
    pub fn denied_with(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self::new(Code::PermissionDenied, code, message)
    }

    /// The referenced record does not exist.
    pub fn not_found(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self::new(Code::NotFound, code, message)
    }

    /// The request is well-formed but the current state refuses it.
    pub fn conflict(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self::new(Code::FailedPrecondition, code, message)
    }

    /// A store or serialization failure. Never the caller's fault.
    pub fn internal(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self::new(Code::Internal, code, message)
    }

    /// The gRPC representation: `"<code>: <message>"`, so the deny code survives
    /// the transport and a client can branch on it without parsing prose.
    pub fn to_status(&self) -> Status {
        Status::new(self.status, format!("{}: {}", self.code, self.message))
    }
}

impl std::fmt::Display for OpError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for OpError {}

impl From<OpError> for Status {
    fn from(error: OpError) -> Self {
        error.to_status()
    }
}

impl From<Status> for OpError {
    /// Re-interpret a transport status (produced by the principal resolver) as an
    /// operation error, keeping the code prefix the resolver put in the message.
    fn from(status: Status) -> Self {
        let message = status.message().to_string();
        let (code, detail) = match message.split_once(": ") {
            Some((code, detail)) => (code.to_string(), detail.to_string()),
            None => (status.code().to_string(), message),
        };
        Self {
            code,
            message: detail,
            status: status.code(),
        }
    }
}

/// One operation's honest result: what it did, for logging and tests.
///
/// Deliberately carries nothing the wire does not also carry — an operation must
/// not report different facts to the two transports.
pub type OpResult<T> = Result<T, OpError>;

/// The clock for one operation.
///
/// Read once per call so every store write in that call sees the same instant;
/// the domain layer takes `now` explicitly for exactly this reason.
pub fn now() -> DateTime<Utc> {
    Utc::now()
}

/// Clamp a page size, treating `0` as "use the default" (the proto default).
pub fn clamp_limit(limit: u32) -> usize {
    let limit = if limit == 0 { DEFAULT_LIST_LIMIT } else { limit };
    limit.min(MAX_LIST_LIMIT) as usize
}

/// Decode an `int64` proto timestamp (Unix seconds) into a UTC instant.
///
/// Out-of-range values are refused rather than clamped: a timestamp a client
/// invented is a malformed request, not "now".
pub fn decode_timestamp(seconds: i64) -> OpResult<DateTime<Utc>> {
    DateTime::<Utc>::from_timestamp(seconds, 0).ok_or_else(|| {
        OpError::invalid(
            "invalid_timestamp",
            format!("{seconds} is not a representable Unix timestamp"),
        )
    })
}

/// Decode an optional `int64` proto timestamp.
pub fn decode_optional_timestamp(seconds: Option<i64>) -> OpResult<Option<DateTime<Utc>>> {
    match seconds {
        Some(seconds) => decode_timestamp(seconds).map(Some),
        None => Ok(None),
    }
}

/// Encode an instant as `int64` Unix seconds (the proto convention in this crate,
/// see `conversions::*_to_proto`).
pub fn encode_timestamp(value: DateTime<Utc>) -> i64 {
    value.timestamp()
}

/// Encode an optional instant as an optional `int64`.
pub fn encode_optional_timestamp(value: Option<DateTime<Utc>>) -> Option<i64> {
    value.map(encode_timestamp)
}

/// Read a non-blank string, or `None`. Used for every optional proto field so a
/// blank value behaves like an absent one.
pub fn optional_text(value: Option<&str>) -> Option<String> {
    value
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
}

/// Require a non-blank string field.
pub fn required_text(value: &str, field: &str) -> OpResult<String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Err(OpError::invalid(
            "invalid_argument",
            format!("{field} must not be empty"),
        ));
    }
    Ok(trimmed.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use uc_types::governance::DenyReason;

    #[test]
    fn page_sizes_follow_the_proto_default_and_the_store_cap() {
        assert_eq!(clamp_limit(0), DEFAULT_LIST_LIMIT as usize);
        assert_eq!(clamp_limit(7), 7);
        assert_eq!(clamp_limit(u32::MAX), MAX_LIST_LIMIT as usize);
    }

    #[test]
    fn timestamps_round_trip_through_unix_seconds() {
        let instant = DateTime::parse_from_rfc3339("2026-01-01T00:00:00Z")
            .unwrap()
            .with_timezone(&Utc);
        assert_eq!(decode_timestamp(encode_timestamp(instant)).unwrap(), instant);
        assert_eq!(encode_optional_timestamp(None), None);
    }

    #[test]
    fn an_unrepresentable_timestamp_is_an_invalid_argument() {
        let error = decode_timestamp(i64::MAX).unwrap_err();
        assert_eq!(error.code, "invalid_timestamp");
        assert_eq!(error.status, Code::InvalidArgument);
    }

    #[test]
    fn policy_denials_keep_the_deny_code_in_the_status_message() {
        let decision = PolicyDecision::deny(DenyReason::MissingPermission {
            required: uc_types::governance::Permission::WorkerScale,
        });
        let error = OpError::denied(&decision);
        assert_eq!(error.code, "missing_permission");
        assert_eq!(error.status, Code::PermissionDenied);
        assert!(error.to_status().message().starts_with("missing_permission: "));
    }

    #[test]
    fn a_status_round_trips_through_the_operation_error() {
        let status = Status::permission_denied("principal_kind_not_declarable: nope");
        let error = OpError::from(status);
        assert_eq!(error.code, "principal_kind_not_declarable");
        assert_eq!(error.message, "nope");
        assert_eq!(error.status, Code::PermissionDenied);
        assert_eq!(
            error.to_status().message(),
            "principal_kind_not_declarable: nope"
        );
    }

    #[test]
    fn optional_text_treats_blank_like_absent() {
        assert_eq!(optional_text(None), None);
        assert_eq!(optional_text(Some("   ")), None);
        assert_eq!(optional_text(Some(" v ")), Some("v".to_string()));
    }
}
