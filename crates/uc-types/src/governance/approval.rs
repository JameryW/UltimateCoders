//! Human approval for human-tier actions.
//!
//! `ToolPolicy::human_approval` used to mean "operator tier only": an agent could
//! never call `scale_workers`, and an operator could — with no record of *why*.
//! This module adds the middle path: a request that names the exact action, a
//! human decision, and a single-use permit.
//!
//! ## The loop
//!
//! ```text
//! agent: request scale_workers on case X   ->  ToolApproval::Pending  (expires in 15 min)
//! human: approve (or reject) with a note    ->  ToolApproval::Approved
//! agent: authorize_tool(scale_workers)      ->  permit consumed (state Used)
//! ```
//!
//! ## Why each rule exists
//!
//! * **Deterministic id** — `(tool, case, requester, action digest)`. An agent that
//!   retries its request converges on the same pending item instead of stacking
//!   twelve of them in the human's worklist.
//! * **Short TTL** — an approval is consent to *this* action now, not a standing
//!   grant. Fifteen minutes by default, capped at a day.
//! * **Humans decide, and never their own request** — a `User` may decide; the
//!   requester may not decide their own request. Otherwise "approval" is a
//!   self-serve checkbox, which is exactly what the deny code was protecting
//!   against.
//! * **Single use** — an approved action is consumed by the first authorized call
//!   (`Used`). Replaying it would turn one consent into a blank cheque.
//! * **The action digest is carried, not interpreted** — the governance plane does
//!   not parse tool arguments; it records what the requester claimed so a human can
//!   compare and so a mutated request cannot ride an old approval.

use chrono::{DateTime, Duration, Utc};
use serde::{Deserialize, Serialize};

use super::policy::{Principal, PrincipalKind};
use crate::merge::sha256_hex;

/// Default lifetime of a request. Short on purpose: consent is for now.
pub const APPROVAL_TTL_MINUTES: i64 = 15;

/// Hard cap on a requested lifetime. A day is already generous for an interactive
/// decision; anything longer is a standing grant, which this type is not.
pub const MAX_APPROVAL_TTL_MINUTES: i64 = 24 * 60;

/// Where an approval is in its life.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ApprovalState {
    /// Waiting for a human. Appears on the attention worklist.
    Pending,
    /// A human allowed it. Usable exactly once.
    Approved,
    /// A human refused it.
    Rejected,
    /// Nobody decided before `expires_at`.
    Expired,
    /// The permit was consumed by an authorized call.
    Used,
}

impl ApprovalState {
    pub fn as_str(self) -> &'static str {
        match self {
            ApprovalState::Pending => "pending",
            ApprovalState::Approved => "approved",
            ApprovalState::Rejected => "rejected",
            ApprovalState::Expired => "expired",
            ApprovalState::Used => "used",
        }
    }

    /// States nothing can move out of.
    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            ApprovalState::Rejected | ApprovalState::Expired | ApprovalState::Used
        )
    }
}

/// What is being asked for. This is *data about the request*, never a substitute
/// for authorization: the policy engine still checks the permission and ceiling.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ApprovalRequest {
    /// Tool name from the policy table (`scale_workers`, `docker_run`, …).
    pub tool: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub case_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub project_id: Option<String>,
    /// Digest of the concrete action's arguments, supplied by the requester.
    ///
    /// Deliberately opaque: the governance plane does not parse tool arguments, so
    /// pretending to validate them would be theatre. It exists so an approval is
    /// visibly bound to one action and so a human can compare.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub action_digest: Option<String>,
    /// Who is asking. An `Agent` or `Worker` requester is the normal case.
    pub requested_by: Principal,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

/// One request and its decision.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ToolApproval {
    pub id: String,
    pub request: ApprovalRequest,
    pub state: ApprovalState,
    pub requested_at: DateTime<Utc>,
    pub expires_at: DateTime<Utc>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub decided_at: Option<DateTime<Utc>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub decided_by: Option<Principal>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub used_at: Option<DateTime<Utc>>,
}

/// Why a decision or consumption was refused.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ApprovalError {
    /// Only a pending request can be decided.
    NotPending { state: ApprovalState },
    /// Only a human may decide. Services and agents are not humans, however
    /// privileged their token is.
    NotAHumanDecision { kind: PrincipalKind },
    /// The requester tried to decide their own request.
    SelfDecision,
    /// The request outlived its `expires_at`.
    Expired { expires_at: DateTime<Utc> },
    /// The permit was already consumed.
    NotUsable { state: ApprovalState },
}

impl std::fmt::Display for ApprovalError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ApprovalError::NotPending { state } => {
                write!(
                    f,
                    "approval is {} — only a pending request can be decided",
                    state.as_str()
                )
            }
            ApprovalError::NotAHumanDecision { kind } => {
                write!(f, "a {:?} principal cannot decide an approval", kind)
            }
            ApprovalError::SelfDecision => {
                write!(f, "the requester cannot decide their own approval request")
            }
            ApprovalError::Expired { expires_at } => {
                write!(f, "approval expired at {expires_at}")
            }
            ApprovalError::NotUsable { state } => {
                write!(f, "approval is {} — not usable", state.as_str())
            }
        }
    }
}

impl ToolApproval {
    /// Create a pending request.
    ///
    /// The id is deterministic in `(tool, case, requester, action digest)` so a
    /// retrying requester reuses one worklist item instead of flooding it.
    pub fn request(request: ApprovalRequest, now: DateTime<Utc>, ttl: Option<Duration>) -> Self {
        let ttl = ttl
            .map(|ttl| {
                ttl.clamp(
                    Duration::minutes(1),
                    Duration::minutes(MAX_APPROVAL_TTL_MINUTES),
                )
            })
            .unwrap_or_else(|| Duration::minutes(APPROVAL_TTL_MINUTES));
        let id = approval_id(&request);
        Self {
            id,
            request,
            state: ApprovalState::Pending,
            requested_at: now,
            expires_at: now + ttl,
            decided_at: None,
            decided_by: None,
            note: None,
            used_at: None,
        }
    }

    /// Whether this approval is still waiting for a human.
    pub fn is_pending(&self) -> bool {
        self.state == ApprovalState::Pending
    }

    /// Whether a human decision is still meaningful: pending and not expired.
    pub fn is_decidable(&self, now: DateTime<Utc>) -> bool {
        self.is_pending() && now < self.expires_at
    }

    /// Whether the permit can be consumed: approved and not expired.
    ///
    /// `Pending` is not usable on purpose — a request is a question, not a permit.
    pub fn is_usable(&self, now: DateTime<Utc>) -> bool {
        self.state == ApprovalState::Approved && now < self.expires_at
    }

    /// Whether this approval was granted for exactly this tool (and case, when the
    /// caller names one — an approval requested without a case covers only calls
    /// without a case).
    pub fn covers(&self, tool: &str, case_id: Option<&str>) -> bool {
        self.request.tool == tool && self.request.case_id.as_deref() == case_id
    }

    /// A human allows it.
    pub fn approve(
        &self,
        by: &Principal,
        note: Option<String>,
        now: DateTime<Utc>,
    ) -> Result<Self, ApprovalError> {
        let mut approved = self.decide(by, ApprovalState::Approved, note, now)?;
        approved.decided_at = Some(now);
        Ok(approved)
    }

    /// A human refuses it.
    pub fn reject(
        &self,
        by: &Principal,
        note: Option<String>,
        now: DateTime<Utc>,
    ) -> Result<Self, ApprovalError> {
        self.decide(by, ApprovalState::Rejected, note, now)
    }

    fn decide(
        &self,
        by: &Principal,
        next: ApprovalState,
        note: Option<String>,
        now: DateTime<Utc>,
    ) -> Result<Self, ApprovalError> {
        if !self.is_pending() {
            return Err(ApprovalError::NotPending { state: self.state });
        }
        if now >= self.expires_at {
            return Err(ApprovalError::Expired {
                expires_at: self.expires_at,
            });
        }
        // A `User` is the only kind that can answer for a human. A `Service` may
        // hold `proactive_execute`, but "the CI runner said yes" is not consent.
        if by.kind != PrincipalKind::User {
            return Err(ApprovalError::NotAHumanDecision { kind: by.kind });
        }
        if by == &self.request.requested_by {
            return Err(ApprovalError::SelfDecision);
        }
        let mut decided = self.clone();
        decided.state = next;
        decided.decided_at = Some(now);
        decided.decided_by = Some(by.clone());
        decided.note = note;
        Ok(decided)
    }

    /// Mark the permit as spent. Called by the authorizer on the first allowed call.
    pub fn consume(&self, now: DateTime<Utc>) -> Result<Self, ApprovalError> {
        if !self.is_usable(now) {
            return Err(ApprovalError::NotUsable { state: self.state });
        }
        let mut used = self.clone();
        used.state = ApprovalState::Used;
        used.used_at = Some(now);
        Ok(used)
    }

    /// Mark an undecided request as expired. Idempotent for anything else.
    pub fn expire(&self, now: DateTime<Utc>) -> Result<Self, ApprovalError> {
        if !self.is_pending() {
            return Err(ApprovalError::NotPending { state: self.state });
        }
        if now < self.expires_at {
            // Not expired yet: refusing is the honest answer, and the caller (a
            // sweeper) can just try again later.
            return Err(ApprovalError::NotPending { state: self.state });
        }
        let mut expired = self.clone();
        expired.state = ApprovalState::Expired;
        Ok(expired)
    }
}

/// Stable id for a request. The same request always lands on the same id.
pub fn approval_id(request: &ApprovalRequest) -> String {
    let material = format!(
        "{}|{}|{}|{}|{}",
        request.tool,
        request.case_id.as_deref().unwrap_or(""),
        request.requested_by.kind.as_str(),
        request.requested_by.id,
        request.action_digest.as_deref().unwrap_or("")
    );
    format!("apr-{}", &sha256_hex(material.as_bytes())[..32])
}

#[cfg(test)]
mod tests {
    use super::*;

    fn now() -> DateTime<Utc> {
        DateTime::parse_from_rfc3339("2026-01-01T00:00:00Z")
            .unwrap()
            .with_timezone(&Utc)
    }

    fn request(tool: &str, case: Option<&str>) -> ApprovalRequest {
        ApprovalRequest {
            tool: tool.to_string(),
            case_id: case.map(str::to_string),
            project_id: Some("proj".to_string()),
            action_digest: Some("args-digest".to_string()),
            requested_by: Principal::agent("executor"),
            reason: Some("the build is wedged".to_string()),
        }
    }

    #[test]
    fn a_request_is_pending_and_not_yet_a_permit() {
        let approval = ToolApproval::request(request("scale_workers", Some("case-1")), now(), None);
        assert!(approval.is_pending());
        assert!(approval.is_decidable(now()));
        assert!(!approval.is_usable(now()), "a question is not a permit");
        assert_eq!(approval.state, ApprovalState::Pending);
        assert_eq!(
            approval.expires_at,
            now() + Duration::minutes(APPROVAL_TTL_MINUTES)
        );
    }

    #[test]
    fn the_id_is_deterministic_so_retries_do_not_flood_the_worklist() {
        let first = ToolApproval::request(request("scale_workers", Some("case-1")), now(), None);
        let later = ToolApproval::request(
            request("scale_workers", Some("case-1")),
            now() + Duration::minutes(3),
            None,
        );
        assert_eq!(first.id, later.id);
        // A different tool, case, requester or action is a different approval.
        let other_tool = ToolApproval::request(request("docker_run", Some("case-1")), now(), None);
        assert_ne!(first.id, other_tool.id);
        let mut mutated = request("scale_workers", Some("case-1"));
        mutated.action_digest = Some("different-args".to_string());
        let other_action = ToolApproval::request(mutated, now(), None);
        assert_ne!(first.id, other_action.id);
    }

    #[test]
    fn only_a_human_who_is_not_the_requester_can_decide() {
        let approval = ToolApproval::request(request("scale_workers", Some("case-1")), now(), None);

        let by_agent = approval
            .approve(&Principal::agent("executor"), None, now())
            .expect_err("an agent cannot approve");
        assert_eq!(
            by_agent,
            ApprovalError::NotAHumanDecision {
                kind: PrincipalKind::Agent
            }
        );
        let by_service = approval
            .approve(&Principal::service("ci-runner"), None, now())
            .expect_err("a service cannot approve");
        assert_eq!(
            by_service,
            ApprovalError::NotAHumanDecision {
                kind: PrincipalKind::Service
            }
        );

        // A human CAN, but not their own request.
        let own = ApprovalRequest {
            requested_by: Principal::user("alice"),
            ..request("scale_workers", Some("case-1"))
        };
        let own = ToolApproval::request(own, now(), None);
        assert_eq!(
            own.approve(&Principal::user("alice"), None, now())
                .expect_err("no self-approval"),
            ApprovalError::SelfDecision
        );

        let approved = approval
            .approve(
                &Principal::user("alice"),
                Some("go ahead".to_string()),
                now(),
            )
            .expect("a human can approve");
        assert_eq!(approved.state, ApprovalState::Approved);
        assert_eq!(approved.decided_by, Some(Principal::user("alice")));
        assert_eq!(approved.note.as_deref(), Some("go ahead"));
        assert!(approved.is_usable(now()));
    }

    #[test]
    fn an_expired_request_cannot_be_approved_late() {
        let approval = ToolApproval::request(request("scale_workers", None), now(), None);
        let too_late = now() + Duration::minutes(APPROVAL_TTL_MINUTES + 1);
        assert_eq!(
            approval
                .approve(&Principal::user("alice"), None, too_late)
                .expect_err("expired"),
            ApprovalError::Expired {
                expires_at: approval.expires_at
            }
        );
    }

    #[test]
    fn the_lifetime_is_bounded_in_both_directions() {
        let short = ToolApproval::request(
            request("scale_workers", None),
            now(),
            Some(Duration::seconds(1)),
        );
        assert_eq!(
            short.expires_at,
            now() + Duration::minutes(1),
            "clamped up to 1 min"
        );

        let long = ToolApproval::request(
            request("scale_workers", None),
            now(),
            Some(Duration::days(30)),
        );
        assert_eq!(
            long.expires_at,
            now() + Duration::minutes(MAX_APPROVAL_TTL_MINUTES),
            "clamped down to a day"
        );
    }

    #[test]
    fn a_permit_is_single_use() {
        let approval = ToolApproval::request(request("docker_run", Some("case-2")), now(), None);
        let approved = approval
            .approve(&Principal::user("alice"), None, now())
            .expect("approved");
        let used = approved.consume(now()).expect("first call consumes");
        assert_eq!(used.state, ApprovalState::Used);
        assert!(used.used_at.is_some());
        assert_eq!(
            used.consume(now()).expect_err("second call must fail"),
            ApprovalError::NotUsable {
                state: ApprovalState::Used
            }
        );
        // And an already-used approval cannot be re-decided either.
        assert_eq!(
            used.approve(&Principal::user("bob"), None, now())
                .expect_err("terminal"),
            ApprovalError::NotPending {
                state: ApprovalState::Used
            }
        );
    }

    #[test]
    fn a_pending_request_is_never_usable() {
        let approval = ToolApproval::request(request("scale_workers", None), now(), None);
        assert_eq!(
            approval.consume(now()).expect_err("pending is a question"),
            ApprovalError::NotUsable {
                state: ApprovalState::Pending
            }
        );
    }

    #[test]
    fn coverage_is_tool_and_case_exact() {
        let with_case =
            ToolApproval::request(request("scale_workers", Some("case-1")), now(), None);
        assert!(with_case.covers("scale_workers", Some("case-1")));
        assert!(!with_case.covers("scale_workers", Some("case-2")));
        assert!(
            !with_case.covers("scale_workers", None),
            "an approval for a case does not cover a case-less call"
        );
        assert!(!with_case.covers("docker_run", Some("case-1")));

        let without_case = ToolApproval::request(request("docker_run", None), now(), None);
        assert!(without_case.covers("docker_run", None));
        assert!(!without_case.covers("docker_run", Some("case-1")));
    }

    #[test]
    fn a_sweeper_expires_what_nobody_answered() {
        let approval = ToolApproval::request(request("scale_workers", None), now(), None);
        // Too early: refuse rather than lie about the state.
        assert!(approval.expire(now()).is_err());
        let later = now() + Duration::minutes(APPROVAL_TTL_MINUTES + 1);
        let expired = approval.expire(later).expect("now it is late enough");
        assert_eq!(expired.state, ApprovalState::Expired);
        assert!(expired.state.is_terminal());
    }
}
