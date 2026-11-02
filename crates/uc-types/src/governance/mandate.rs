//! Mandate — the authority under which a [`WorkCase`](super::workcase::WorkCase)
//! executes.
//!
//! A mandate is a **narrow, explicit, revocable, time-boxed** set of scopes.
//! The security property the plan demands (*"授权不可被外部消息扩大"*) rests on
//! three rules implemented here:
//!
//! 1. A mandate can only be **narrowed**. [`Mandate::validate_child`] checks a
//!    delegated mandate is a strict subset of its parent, and there is no
//!    "widen" operation at all — widening requires issuing a *new* mandate
//!    through [`super::policy::Permission::MandateIssue`], which
//!    [`super::policy::PrincipalKind::may_hold`] refuses for triggers, agents
//!    and workers.
//! 2. Every check is **fail-closed and exact**: a requested dimension with no
//!    covering scope is denied. Absence of a scope is never "unrestricted".
//! 3. Revocation and expiry are **checked before** scopes, so a stale cached
//!    mandate cannot be used just because its scopes happen to match.
//!
//! Pattern matching is deliberately conservative: `*` matches within one path
//! segment, `**` spans segments, and anything the matcher cannot prove is
//! treated as *not* covered.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

use super::policy::{ActionClass, PolicyVersion, Principal};

/// Glob matcher: `*` matches any run of characters except `/`; `**` matches any
/// run including `/`. Everything else is literal.
///
/// Used for repository ids (`owner/repo`), branches (`release/*`) and file paths
/// (`crates/uc-engine/**`).
pub fn pattern_covers(pattern: &str, value: &str) -> bool {
    let pattern: Vec<char> = pattern.chars().collect();
    let value: Vec<char> = value.chars().collect();
    let mut seen = vec![vec![false; value.len() + 1]; pattern.len() + 1];
    let mut stack: Vec<(usize, usize)> = vec![(0, 0)];
    while let Some((pi, vi)) = stack.pop() {
        if seen[pi][vi] {
            continue;
        }
        seen[pi][vi] = true;
        if pi == pattern.len() {
            if vi == value.len() {
                return true;
            }
            continue;
        }
        match pattern[pi] {
            '*' if pi + 1 < pattern.len() && pattern[pi + 1] == '*' => {
                // `**` consumes zero characters, or one character and stays put.
                if !seen[pi + 2][vi] {
                    stack.push((pi + 2, vi));
                }
                if vi < value.len() && !seen[pi][vi + 1] {
                    stack.push((pi, vi + 1));
                }
            }
            '*' => {
                // `*` consumes zero characters, or one non-`/` character.
                if !seen[pi + 1][vi] {
                    stack.push((pi + 1, vi));
                }
                if vi < value.len() && value[vi] != '/' && !seen[pi][vi + 1] {
                    stack.push((pi, vi + 1));
                }
            }
            literal => {
                if vi < value.len() && value[vi] == literal && !seen[pi + 1][vi + 1] {
                    stack.push((pi + 1, vi + 1));
                }
            }
        }
    }
    false
}

/// Conservative "parent pattern covers child pattern" used when delegating.
///
/// This is stricter than [`pattern_covers`] on purpose: two patterns can overlap
/// without either covering the other, and a delegation must be provably inside
/// its parent, so unprovable cases return `false`.
fn pattern_subsumes(parent: &str, child: &str) -> bool {
    if parent == child {
        return true;
    }
    if parent == "**" {
        return true;
    }
    if parent == "*" {
        // `*` only spans one segment, so it subsumes a child only if that child
        // also stays inside one segment.
        return !child.contains('/');
    }
    if let Some(prefix) = parent.strip_suffix("/**") {
        return child == prefix
            || child
                .strip_prefix(prefix)
                .map(|rest| rest.starts_with('/'))
                .unwrap_or(false);
    }
    if let Some(prefix) = parent.strip_suffix("/*") {
        if let Some(rest) = child.strip_prefix(prefix) {
            let rest = rest.strip_prefix('/').unwrap_or(rest);
            return !rest.is_empty() && !rest.contains('/');
        }
        return false;
    }
    false
}

/// Identifier of a mandate.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct MandateId(pub String);

impl MandateId {
    pub fn new() -> Self {
        Self(uuid::Uuid::new_v4().to_string())
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl Default for MandateId {
    fn default() -> Self {
        Self::new()
    }
}

impl std::fmt::Display for MandateId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// Who the mandate came from. Only a human-issued mandate may be the root of a
/// delegation chain; a delegated or default mandate can never be used to widen.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MandateOrigin {
    /// A human operator issued it through the dashboard/API.
    HumanIssued,
    /// Narrowed from a parent mandate (`delegated_from` is set).
    CaseDelegated,
    /// Issued by gateway bootstrap policy for a trusted, configured project.
    SystemDefault,
    /// Created by a test fixture. Never valid in production paths.
    TestFixture,
}

impl MandateOrigin {
    pub fn as_str(&self) -> &'static str {
        match self {
            MandateOrigin::HumanIssued => "human_issued",
            MandateOrigin::CaseDelegated => "case_delegated",
            MandateOrigin::SystemDefault => "system_default",
            MandateOrigin::TestFixture => "test_fixture",
        }
    }

    /// Only a human can originate the authority that all delegation derives
    /// from. A component that claims to have minted a human mandate without
    /// `Permission::MandateIssue` is rejected upstream by
    /// [`super::policy::PolicySnapshot::evaluate`].
    pub fn is_root(&self) -> bool {
        matches!(
            self,
            MandateOrigin::HumanIssued | MandateOrigin::SystemDefault
        )
    }
}

/// One dimension of authority.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", tag = "kind")]
pub enum MandateScope {
    /// Which repository (`owner/repo`, patterns allowed).
    Repo { pattern: String },
    /// Which branches may be pushed to.
    Branch { pattern: String },
    /// Which file paths may be modified.
    Path { pattern: String },
    /// Which tools may be invoked by name.
    Tool { name: String },
    /// Which coarse side-effect classes are allowed.
    Action { class: ActionClass },
    /// Budget ceilings. `None` means "not granted" for that dimension, never
    /// "unlimited": a budget scope that omits a dimension does not authorize it.
    Budget {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        max_tokens: Option<u64>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        max_cost_usd: Option<f64>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        max_wall_secs: Option<u64>,
    },
    /// Maximum number of runs this mandate may authorize.
    MaxRuns { max: u32 },
}

impl MandateScope {
    pub fn kind(&self) -> &'static str {
        match self {
            MandateScope::Repo { .. } => "repo",
            MandateScope::Branch { .. } => "branch",
            MandateScope::Path { .. } => "path",
            MandateScope::Tool { .. } => "tool",
            MandateScope::Action { .. } => "action",
            MandateScope::Budget { .. } => "budget",
            MandateScope::MaxRuns { .. } => "max_runs",
        }
    }
}

/// A single authorization question.
#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]
pub struct MandateRequest {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub repo: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub branch: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub path: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub action: Option<ActionClass>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tokens: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cost_usd: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub wall_secs: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub runs: Option<u32>,
}

impl MandateRequest {
    pub fn tool(name: impl Into<String>) -> Self {
        Self {
            tool: Some(name.into()),
            ..Default::default()
        }
    }

    pub fn action(class: ActionClass) -> Self {
        Self {
            action: Some(class),
            ..Default::default()
        }
    }

    pub fn write_path(path: impl Into<String>) -> Self {
        Self {
            path: Some(path.into()),
            action: Some(ActionClass::Write),
            ..Default::default()
        }
    }
}

/// Why a mandate check failed.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", tag = "code")]
pub enum MandateDenial {
    Revoked {
        mandate_id: String,
        reason: String,
    },
    Expired {
        mandate_id: String,
        expired_at: DateTime<Utc>,
    },
    /// A requested dimension has no covering scope — including the case where no
    /// scope of that kind exists at all.
    OutOfScope {
        scope: String,
        value: String,
    },
    /// A delegation attempted to hand out more than it holds.
    WideningDenied {
        detail: String,
    },
    /// The mandate was issued under a policy version older than the accepted
    /// minimum, so it must be re-issued.
    StalePolicyVersion {
        mandate_version: u64,
        minimum: u64,
    },
    /// A delegated mandate claimed an origin it may not claim.
    OriginForbidden {
        origin: String,
    },
}

impl MandateDenial {
    pub fn code(&self) -> &'static str {
        match self {
            MandateDenial::Revoked { .. } => "mandate_revoked",
            MandateDenial::Expired { .. } => "mandate_expired",
            MandateDenial::OutOfScope { .. } => "mandate_out_of_scope",
            MandateDenial::WideningDenied { .. } => "mandate_widening_denied",
            MandateDenial::StalePolicyVersion { .. } => "stale_policy_version",
            MandateDenial::OriginForbidden { .. } => "mandate_origin_forbidden",
        }
    }
}

/// Outcome of a mandate check.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", tag = "decision")]
pub enum MandateDecision {
    Allowed,
    Denied { denial: MandateDenial },
}

impl MandateDecision {
    pub fn is_allowed(&self) -> bool {
        matches!(self, MandateDecision::Allowed)
    }

    pub fn is_denied(&self) -> bool {
        !self.is_allowed()
    }

    pub fn denial(&self) -> Option<&MandateDenial> {
        match self {
            MandateDecision::Denied { denial } => Some(denial),
            MandateDecision::Allowed => None,
        }
    }
}

/// A revocation record. Mandates are never deleted — the audit trail keeps both
/// the authority and its withdrawal.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MandateRevocation {
    pub at: DateTime<Utc>,
    pub by: Principal,
    pub reason: String,
}

/// The authority granted to one case.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Mandate {
    pub id: MandateId,
    pub case_id: String,
    /// Who granted it. For delegated mandates this is the parent case's agent
    /// or service, never an inbound trigger.
    pub grantor: Principal,
    /// Policy generation the mandate was issued under.
    pub policy_version: u64,
    pub scopes: Vec<MandateScope>,
    pub issued_at: DateTime<Utc>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub expires_at: Option<DateTime<Utc>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub revoked: Option<MandateRevocation>,
    pub origin: MandateOrigin,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub delegated_from: Option<MandateId>,
}

impl Mandate {
    pub fn new(
        case_id: impl Into<String>,
        grantor: Principal,
        origin: MandateOrigin,
        scopes: Vec<MandateScope>,
        policy_version: PolicyVersion,
        now: DateTime<Utc>,
    ) -> Self {
        Self {
            id: MandateId::new(),
            case_id: case_id.into(),
            grantor,
            policy_version: policy_version.0,
            scopes,
            issued_at: now,
            expires_at: None,
            revoked: None,
            origin,
            delegated_from: None,
        }
    }

    pub fn with_expiry(mut self, expires_at: DateTime<Utc>) -> Self {
        self.expires_at = Some(expires_at);
        self
    }

    pub fn delegate_from(mut self, parent: &Mandate) -> Self {
        self.delegated_from = Some(parent.id.clone());
        self
    }

    pub fn is_revoked(&self) -> bool {
        self.revoked.is_some()
    }

    pub fn is_expired(&self, now: DateTime<Utc>) -> bool {
        self.expires_at.map(|at| at <= now).unwrap_or(false)
    }

    /// Revoke in place. Idempotent: a second revocation keeps the first record
    /// so the audit trail shows who actually withdrew the authority.
    pub fn revoke(&mut self, by: Principal, reason: impl Into<String>, now: DateTime<Utc>) {
        if self.revoked.is_some() {
            return;
        }
        self.revoked = Some(MandateRevocation {
            at: now,
            by,
            reason: reason.into(),
        });
    }

    /// Does this mandate authorize `request` right now?
    pub fn permits(&self, request: &MandateRequest, now: DateTime<Utc>) -> MandateDecision {
        if let Some(revocation) = &self.revoked {
            return MandateDecision::Denied {
                denial: MandateDenial::Revoked {
                    mandate_id: self.id.0.clone(),
                    reason: revocation.reason.clone(),
                },
            };
        }
        if let Some(expires_at) = self.expires_at {
            if expires_at <= now {
                return MandateDecision::Denied {
                    denial: MandateDenial::Expired {
                        mandate_id: self.id.0.clone(),
                        expired_at: expires_at,
                    },
                };
            }
        }

        if let Some(repo) = &request.repo {
            if !self.scopes.iter().any(|scope| match scope {
                MandateScope::Repo { pattern } => pattern_covers(pattern, repo),
                _ => false,
            }) {
                return out_of_scope("repo", repo);
            }
        }
        if let Some(branch) = &request.branch {
            if !self.scopes.iter().any(|scope| match scope {
                MandateScope::Branch { pattern } => pattern_covers(pattern, branch),
                _ => false,
            }) {
                return out_of_scope("branch", branch);
            }
        }
        if let Some(path) = &request.path {
            if !self.scopes.iter().any(|scope| match scope {
                MandateScope::Path { pattern } => pattern_covers(pattern, path),
                _ => false,
            }) {
                return out_of_scope("path", path);
            }
        }
        if let Some(tool) = &request.tool {
            let covered = self.scopes.iter().any(|scope| match scope {
                MandateScope::Tool { name } => name == tool || name == "*",
                _ => false,
            });
            if !covered {
                return out_of_scope("tool", tool);
            }
        }
        if let Some(action) = &request.action {
            let covered = self.scopes.iter().any(|scope| match scope {
                MandateScope::Action { class } => class == action,
                _ => false,
            });
            if !covered {
                return out_of_scope("action", action.as_str());
            }
        }
        if let Some(tokens) = request.tokens {
            match self.budget_scope() {
                Some(MandateScope::Budget {
                    max_tokens: Some(max),
                    ..
                }) if tokens <= *max => {}
                _ => return out_of_scope("budget.tokens", &tokens.to_string()),
            }
        }
        if let Some(cost) = request.cost_usd {
            match self.budget_scope() {
                Some(MandateScope::Budget {
                    max_cost_usd: Some(max),
                    ..
                }) if cost <= *max => {}
                _ => return out_of_scope("budget.cost_usd", &cost.to_string()),
            }
        }
        if let Some(wall) = request.wall_secs {
            match self.budget_scope() {
                Some(MandateScope::Budget {
                    max_wall_secs: Some(max),
                    ..
                }) if wall <= *max => {}
                _ => return out_of_scope("budget.wall_secs", &wall.to_string()),
            }
        }
        if let Some(runs) = request.runs {
            let covered = self.scopes.iter().any(|scope| match scope {
                MandateScope::MaxRuns { max } => runs <= *max,
                _ => false,
            });
            if !covered {
                return out_of_scope("max_runs", &runs.to_string());
            }
        }
        MandateDecision::Allowed
    }

    fn budget_scope(&self) -> Option<&MandateScope> {
        self.scopes
            .iter()
            .find(|scope| matches!(scope, MandateScope::Budget { .. }))
    }

    /// Verify that `child` is a strict subset of `self`. Used before storing a
    /// delegated mandate. A child may never:
    ///
    /// * claim the `HumanIssued` origin (that would let a component mint root
    ///   authority),
    /// * use an older policy generation than its parent,
    /// * outlive its parent,
    /// * introduce a scope the parent does not hold, or
    /// * raise a budget/frequency ceiling.
    pub fn validate_child(
        &self,
        child: &Mandate,
        now: DateTime<Utc>,
        minimum_policy: Option<PolicyVersion>,
    ) -> MandateDecision {
        if !self.permits(&MandateRequest::default(), now).is_allowed() {
            return self.permits(&MandateRequest::default(), now);
        }
        if child.origin == MandateOrigin::HumanIssued {
            return MandateDecision::Denied {
                denial: MandateDenial::OriginForbidden {
                    origin: child.origin.as_str().to_string(),
                },
            };
        }
        if child.policy_version < self.policy_version {
            return MandateDecision::Denied {
                denial: MandateDenial::StalePolicyVersion {
                    mandate_version: child.policy_version,
                    minimum: self.policy_version,
                },
            };
        }
        if let Some(minimum) = minimum_policy {
            if child.policy_version < minimum.0 {
                return MandateDecision::Denied {
                    denial: MandateDenial::StalePolicyVersion {
                        mandate_version: child.policy_version,
                        minimum: minimum.0,
                    },
                };
            }
        }
        if child.case_id != self.case_id && child.delegated_from.as_ref() != Some(&self.id) {
            return widen("delegated mandate must reference its parent mandate");
        }
        match (child.expires_at, self.expires_at) {
            (Some(child_expiry), Some(parent_expiry)) if child_expiry > parent_expiry => {
                return widen("delegated mandate outlives its parent");
            }
            (Some(_), None) => {}
            (None, Some(_)) => {
                return widen("delegated mandate removed the parent's expiry");
            }
            _ => {}
        }
        for scope in &child.scopes {
            let covered = match scope {
                MandateScope::Repo { pattern } => self
                    .scopes
                    .iter()
                    .any(|parent| matches!(parent, MandateScope::Repo { pattern: p } if pattern_subsumes(p, pattern))),
                MandateScope::Branch { pattern } => self
                    .scopes
                    .iter()
                    .any(|parent| matches!(parent, MandateScope::Branch { pattern: p } if pattern_subsumes(p, pattern))),
                MandateScope::Path { pattern } => self
                    .scopes
                    .iter()
                    .any(|parent| matches!(parent, MandateScope::Path { pattern: p } if pattern_subsumes(p, pattern))),
                MandateScope::Tool { name } => self
                    .scopes
                    .iter()
                    .any(|parent| matches!(parent, MandateScope::Tool { name: p } if p == name || p == "*")),
                MandateScope::Action { class } => self
                    .scopes
                    .iter()
                    .any(|parent| matches!(parent, MandateScope::Action { class: c } if c == class)),
                MandateScope::Budget {
                    max_tokens,
                    max_cost_usd,
                    max_wall_secs,
                } => {
                    let parent = self.budget_scope();
                    match parent {
                        Some(MandateScope::Budget {
                            max_tokens: parent_tokens,
                            max_cost_usd: parent_cost,
                            max_wall_secs: parent_wall,
                        }) => {
                            budget_within(*max_tokens, *parent_tokens)
                                && cost_within(*max_cost_usd, *parent_cost)
                                && budget_within(*max_wall_secs, *parent_wall)
                        }
                        _ => false,
                    }
                }
                MandateScope::MaxRuns { max } => self
                    .scopes
                    .iter()
                    .any(|parent| matches!(parent, MandateScope::MaxRuns { max: pmax } if max <= pmax)),
            };
            if !covered {
                return widen(format!(
                    "delegated mandate introduces a scope the parent does not hold: {}",
                    scope.kind()
                ));
            }
        }
        MandateDecision::Allowed
    }

    /// Narrowest of two mandates (same case). Used when a case accumulates
    /// authority from more than one source: the intersection is the only safe
    /// combination, because it can never grant more than either input.
    pub fn intersect(&self, other: &Mandate, now: DateTime<Utc>) -> Mandate {
        let mut scopes: Vec<MandateScope> = self
            .scopes
            .iter()
            .filter(|scope| match scope {
                MandateScope::Repo { pattern } => other
                    .scopes
                    .iter()
                    .any(|o| matches!(o, MandateScope::Repo { pattern: p } if p == pattern)),
                MandateScope::Branch { pattern } => other
                    .scopes
                    .iter()
                    .any(|o| matches!(o, MandateScope::Branch { pattern: p } if p == pattern)),
                MandateScope::Path { pattern } => other
                    .scopes
                    .iter()
                    .any(|o| matches!(o, MandateScope::Path { pattern: p } if p == pattern)),
                MandateScope::Tool { name } => other
                    .scopes
                    .iter()
                    .any(|o| matches!(o, MandateScope::Tool { name: p } if p == name)),
                MandateScope::Action { class } => other
                    .scopes
                    .iter()
                    .any(|o| matches!(o, MandateScope::Action { class: c } if c == class)),
                // Budgets are merged below, not filtered.
                MandateScope::Budget { .. } => false,
                MandateScope::MaxRuns { max } => other
                    .scopes
                    .iter()
                    .any(|o| matches!(o, MandateScope::MaxRuns { max: pmax } if max <= pmax)),
            })
            .cloned()
            .collect();
        if let (
            Some(MandateScope::Budget {
                max_tokens: at,
                max_cost_usd: ac,
                max_wall_secs: aw,
            }),
            Some(MandateScope::Budget {
                max_tokens: bt,
                max_cost_usd: bc,
                max_wall_secs: bw,
            }),
        ) = (self.budget_scope(), other.budget_scope())
        {
            scopes.push(MandateScope::Budget {
                max_tokens: narrowest_u64(*at, *bt),
                max_cost_usd: narrowest_f64(*ac, *bc),
                max_wall_secs: narrowest_u64(*aw, *bw),
            });
        }
        let expires_at = match (self.expires_at, other.expires_at) {
            (Some(a), Some(b)) => Some(a.min(b)),
            (Some(a), None) => Some(a),
            (None, Some(b)) => Some(b),
            (None, None) => None,
        };
        Mandate {
            id: MandateId::new(),
            case_id: self.case_id.clone(),
            grantor: self.grantor.clone(),
            policy_version: self.policy_version.max(other.policy_version),
            scopes,
            issued_at: now,
            expires_at,
            revoked: None,
            origin: if self.origin == other.origin {
                self.origin
            } else {
                MandateOrigin::CaseDelegated
            },
            delegated_from: None,
        }
    }
}

/// Narrowest of two optional ceilings. `None` means unbounded, so the bounded
/// side wins.
fn narrowest_u64(a: Option<u64>, b: Option<u64>) -> Option<u64> {
    match (a, b) {
        (Some(a), Some(b)) => Some(a.min(b)),
        (Some(a), None) => Some(a),
        (None, Some(b)) => Some(b),
        (None, None) => None,
    }
}

fn narrowest_f64(a: Option<f64>, b: Option<f64>) -> Option<f64> {
    match (a, b) {
        (Some(a), Some(b)) => Some(a.min(b)),
        (Some(a), None) => Some(a),
        (None, Some(b)) => Some(b),
        (None, None) => None,
    }
}

fn budget_within(child: Option<u64>, parent: Option<u64>) -> bool {
    match (child, parent) {
        // A child asking for a bounded value is fine when the parent is
        // unbounded; asking for unbounded when the parent is bounded is not.
        (Some(c), Some(p)) => c <= p,
        (Some(_), None) => true,
        (None, Some(_)) => false,
        (None, None) => true,
    }
}

fn cost_within(child: Option<f64>, parent: Option<f64>) -> bool {
    match (child, parent) {
        (Some(c), Some(p)) => c <= p + f64::EPSILON,
        (Some(_), None) => true,
        (None, Some(_)) => false,
        (None, None) => true,
    }
}

fn out_of_scope(scope: &str, value: &str) -> MandateDecision {
    MandateDecision::Denied {
        denial: MandateDenial::OutOfScope {
            scope: scope.to_string(),
            value: value.to_string(),
        },
    }
}

fn widen(detail: impl Into<String>) -> MandateDecision {
    MandateDecision::Denied {
        denial: MandateDenial::WideningDenied {
            detail: detail.into(),
        },
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

    fn human_mandate(scopes: Vec<MandateScope>) -> Mandate {
        Mandate::new(
            "case-1",
            Principal::user("alice"),
            MandateOrigin::HumanIssued,
            scopes,
            PolicyVersion(1),
            now(),
        )
    }

    fn code_edit_mandate() -> Mandate {
        human_mandate(vec![
            MandateScope::Repo {
                pattern: "JameryW/UltimateCoders".to_string(),
            },
            MandateScope::Branch {
                pattern: "uc/case-*".to_string(),
            },
            MandateScope::Path {
                pattern: "crates/uc-engine/**".to_string(),
            },
            MandateScope::Tool {
                name: "Read".to_string(),
            },
            MandateScope::Tool {
                name: "Edit".to_string(),
            },
            MandateScope::Action {
                class: ActionClass::Read,
            },
            MandateScope::Action {
                class: ActionClass::Write,
            },
            MandateScope::MaxRuns { max: 5 },
        ])
    }

    #[test]
    fn glob_matches_segments_and_spans() {
        assert!(pattern_covers(
            "crates/uc-engine/**",
            "crates/uc-engine/src/lib.rs"
        ));
        assert!(pattern_covers("crates/*/src", "crates/uc-engine/src"));
        assert!(!pattern_covers("crates/*", "crates/uc-engine/src"));
        assert!(pattern_covers("uc/case-*", "uc/case-123"));
        assert!(!pattern_covers("uc/case-*", "uc/other-123"));
        assert!(pattern_covers("**", "anything/at/all"));
    }

    #[test]
    fn mandate_allows_only_covered_requests() {
        let mandate = code_edit_mandate();
        assert!(mandate
            .permits(
                &MandateRequest::write_path("crates/uc-engine/src/lib.rs"),
                now()
            )
            .is_allowed());
        assert!(mandate
            .permits(&MandateRequest::tool("Edit"), now())
            .is_allowed());
    }

    #[test]
    fn mandate_denies_unlisted_tool_and_action() {
        let mandate = code_edit_mandate();
        let denied = mandate.permits(&MandateRequest::tool("Bash"), now());
        assert_eq!(
            denied.denial().map(|d| d.code()),
            Some("mandate_out_of_scope")
        );
        let denied = mandate.permits(&MandateRequest::action(ActionClass::Merge), now());
        assert!(denied.denial().is_some());
    }

    #[test]
    fn missing_scope_kind_is_denied_not_unrestricted() {
        let mandate = human_mandate(vec![MandateScope::Action {
            class: ActionClass::Read,
        }]);
        let decision = mandate.permits(&MandateRequest::tool("Read"), now());
        assert!(decision.denial().is_some(), "absent tool scope must deny");
    }

    #[test]
    fn expired_and_revoked_mandates_are_denied_before_scope_check() {
        let mut expired = code_edit_mandate().with_expiry(now() - chrono::Duration::seconds(1));
        assert_eq!(
            expired
                .permits(&MandateRequest::tool("Read"), now())
                .denial()
                .map(|d| d.code()),
            Some("mandate_expired")
        );
        expired.revoke(Principal::user("alice"), "changed mind", now());
        assert_eq!(
            expired
                .permits(&MandateRequest::tool("Read"), now())
                .denial()
                .map(|d| d.code()),
            Some("mandate_revoked")
        );
    }

    #[test]
    fn delegation_cannot_add_authority() {
        let parent = code_edit_mandate();
        let wider = human_mandate(vec![
            MandateScope::Repo {
                pattern: "JameryW/**".to_string(),
            },
            MandateScope::Action {
                class: ActionClass::Merge,
            },
        ])
        .delegate_from(&parent);
        let decision = parent.validate_child(&wider, now(), None);
        assert!(
            matches!(
                decision.denial(),
                Some(MandateDenial::WideningDenied { .. })
                    | Some(MandateDenial::OriginForbidden { .. })
            ),
            "unexpected decision: {decision:?}"
        );
    }

    #[test]
    fn delegated_child_claiming_human_origin_is_refused() {
        let parent = code_edit_mandate();
        let child = Mandate::new(
            "case-1",
            Principal::agent("planner"),
            MandateOrigin::HumanIssued,
            parent.scopes.clone(),
            PolicyVersion(1),
            now(),
        )
        .delegate_from(&parent);
        assert_eq!(
            parent
                .validate_child(&child, now(), None)
                .denial()
                .map(|d| d.code()),
            Some("mandate_origin_forbidden")
        );
    }

    #[test]
    fn narrower_child_is_accepted() {
        let parent = code_edit_mandate();
        let child = Mandate::new(
            "case-1",
            Principal::agent("planner"),
            MandateOrigin::CaseDelegated,
            vec![
                MandateScope::Repo {
                    pattern: "JameryW/UltimateCoders".to_string(),
                },
                MandateScope::Path {
                    pattern: "crates/uc-engine/src/lib.rs".to_string(),
                },
                MandateScope::Tool {
                    name: "Read".to_string(),
                },
                MandateScope::Action {
                    class: ActionClass::Read,
                },
                MandateScope::MaxRuns { max: 2 },
            ],
            PolicyVersion(1),
            now(),
        )
        .delegate_from(&parent);
        assert!(parent.validate_child(&child, now(), None).is_allowed());
    }

    #[test]
    fn child_cannot_outlive_parent_or_use_older_policy() {
        let parent = code_edit_mandate().with_expiry(now() + chrono::Duration::hours(1));
        let longer = Mandate::new(
            "case-1",
            Principal::agent("planner"),
            MandateOrigin::CaseDelegated,
            parent.scopes.clone(),
            PolicyVersion(1),
            now(),
        )
        .delegate_from(&parent)
        .with_expiry(now() + chrono::Duration::hours(2));
        assert!(parent
            .validate_child(&longer, now(), None)
            .denial()
            .is_some());

        let stale = Mandate::new(
            "case-1",
            Principal::agent("planner"),
            MandateOrigin::CaseDelegated,
            parent.scopes.clone(),
            PolicyVersion(0),
            now(),
        )
        .delegate_from(&parent);
        assert_eq!(
            parent
                .validate_child(&stale, now(), None)
                .denial()
                .map(|d| d.code()),
            Some("stale_policy_version")
        );
    }

    #[test]
    fn intersection_is_never_wider_than_either_input() {
        let a = code_edit_mandate();
        let b = human_mandate(vec![
            MandateScope::Tool {
                name: "Read".to_string(),
            },
            MandateScope::Action {
                class: ActionClass::Read,
            },
            MandateScope::Tool {
                name: "Bash".to_string(),
            },
        ]);
        let combined = a.intersect(&b, now());
        assert!(combined
            .permits(&MandateRequest::tool("Read"), now())
            .is_allowed());
        assert!(combined
            .permits(&MandateRequest::tool("Bash"), now())
            .is_denied());
        assert!(combined
            .permits(&MandateRequest::action(ActionClass::Merge), now())
            .is_denied());
    }

    #[test]
    fn budget_request_requires_a_budget_scope() {
        let mandate = human_mandate(vec![MandateScope::Budget {
            max_tokens: Some(1000),
            max_cost_usd: None,
            max_wall_secs: None,
        }]);
        assert!(mandate
            .permits(
                &MandateRequest {
                    tokens: Some(500),
                    ..Default::default()
                },
                now()
            )
            .is_allowed());
        assert!(mandate
            .permits(
                &MandateRequest {
                    tokens: Some(5000),
                    ..Default::default()
                },
                now()
            )
            .is_denied());
        assert!(mandate
            .permits(
                &MandateRequest {
                    cost_usd: Some(1.0),
                    ..Default::default()
                },
                now()
            )
            .is_denied());
    }
}
