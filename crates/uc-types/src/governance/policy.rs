//! Deterministic authorization: principals, permissions, grants, tool policy
//! and sandbox hardening requirements.
//!
//! The rule this module encodes is the plan's *"不越权"* acceptance criterion:
//! a trigger, a PR comment, a memory entry or a model output may **never**
//! enlarge the authority under which work runs. Two mechanisms enforce it:
//!
//! 1. **Structural** — [`PrincipalKind::may_hold`] is a hard-coded ceiling. An
//!    external trigger or an anonymous caller cannot hold an authority-granting
//!    permission no matter what a configuration file says, and an agent cannot
//!    hold `WorkerScale` / `MandateIssue`. [`PolicySnapshot::with_grant`]
//!    rejects such grants at construction time instead of hoping the evaluator
//!    notices later.
//! 2. **Evaluative** — every check is fail-closed: unknown tools are denied
//!    ([`ToolPolicyTable`]), missing/expired/revoked grants are denied with a
//!    typed reason, and stale policy versions are denied.
//!
//! Decision functions take `now` explicitly rather than reading the clock, so
//! expiry behaviour is reproducible in tests and in replay.

use std::collections::BTreeMap;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

/// Who is asking.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PrincipalKind {
    /// A human operator (dashboard, CLI, API with a user token).
    User,
    /// A trusted internal service (gateway-to-gateway, CI runner with a token).
    Service,
    /// An autonomous agent (planner, reviewer, executor LLM loop).
    Agent,
    /// A distributed worker executing a dispatched subtask.
    Worker,
    /// An inbound integration event (GitHub webhook, Lark message, cron tick).
    ExternalTrigger,
    /// No or unverifiable credentials.
    Anonymous,
}

impl PrincipalKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            PrincipalKind::User => "user",
            PrincipalKind::Service => "service",
            PrincipalKind::Agent => "agent",
            PrincipalKind::Worker => "worker",
            PrincipalKind::ExternalTrigger => "external_trigger",
            PrincipalKind::Anonymous => "anonymous",
        }
    }

    /// The structural authority ceiling for this kind of principal.
    ///
    /// This is intentionally not configurable. Widening the ceiling is a code
    /// change that has to pass review, which is the point.
    pub fn may_hold(&self, permission: Permission) -> bool {
        match self {
            // An inbound event or an unauthenticated caller can never hold
            // authority-granting permissions. They may still be the *subject*
            // of a decision (e.g. trigger ingestion needs TriggerIngest), which
            // is granted to the service that authenticates the webhook, not to
            // the event itself.
            PrincipalKind::ExternalTrigger | PrincipalKind::Anonymous => {
                !permission.is_authority_granting()
            }
            // Agents may work inside a mandate, but cannot mint or revoke one,
            // cannot resize the cluster, and cannot grant themselves autonomous
            // execution (`ProactiveExecute` raises the autonomy mode — that must
            // stay a human/operator decision).
            PrincipalKind::Agent => !matches!(
                permission,
                Permission::MandateIssue
                    | Permission::MandateRevoke
                    | Permission::WorkerScale
                    | Permission::ProactiveExecute
            ),
            PrincipalKind::Worker => !matches!(
                permission,
                Permission::MandateIssue
                    | Permission::MandateRevoke
                    | Permission::WorkerScale
                    | Permission::ProactiveExecute
            ),
            PrincipalKind::User | PrincipalKind::Service => true,
        }
    }
}

/// An authenticated (or explicitly unauthenticated) caller.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Principal {
    pub kind: PrincipalKind,
    /// Stable identity: user id, service name, agent role, or worker id.
    pub id: String,
}

impl Principal {
    pub fn new(kind: PrincipalKind, id: impl Into<String>) -> Self {
        Self {
            kind,
            id: id.into(),
        }
    }

    pub fn anonymous() -> Self {
        Self::new(PrincipalKind::Anonymous, "anonymous")
    }

    pub fn user(id: impl Into<String>) -> Self {
        Self::new(PrincipalKind::User, id)
    }

    pub fn service(id: impl Into<String>) -> Self {
        Self::new(PrincipalKind::Service, id)
    }

    pub fn agent(id: impl Into<String>) -> Self {
        Self::new(PrincipalKind::Agent, id)
    }

    pub fn worker(id: impl Into<String>) -> Self {
        Self::new(PrincipalKind::Worker, id)
    }

    pub fn external_trigger(id: impl Into<String>) -> Self {
        Self::new(PrincipalKind::ExternalTrigger, id)
    }

    pub fn is_authenticated(&self) -> bool {
        self.kind != PrincipalKind::Anonymous
    }
}

impl std::fmt::Display for Principal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}:{}", self.kind.as_str(), self.id)
    }
}

/// A capability the gateway can check.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Permission {
    /// Submit a task / open a case run.
    TaskSubmit,
    TaskRead,
    /// Pause, resume, cancel, retry.
    TaskControl,
    CaseCreate,
    CaseRead,
    CaseControl,
    /// Mint a mandate. Authority-granting.
    MandateIssue,
    /// Revoke a mandate. Authority-granting.
    MandateRevoke,
    /// Ingest an external event into the trigger inbox.
    TriggerIngest,
    /// Resize the worker pool / force-deregister workers. Authority-granting.
    WorkerScale,
    /// Write long-term memory.
    MemoryWrite,
    /// Issue a merge grant. Authority-granting (it authorizes writing `main`).
    MergeGrant,
    /// Resolve an attention item (answer a `NEEDS_HUMAN` question).
    AttentionResolve,
    /// Enqueue a proactive proposal. *Not* authority-granting: a proposal
    /// carries no execution rights until it is approved.
    ProactivePropose,
    /// Execute a proactive action without human approval. Authority-granting.
    ProactiveExecute,
}

impl Permission {
    pub fn as_str(&self) -> &'static str {
        match self {
            Permission::TaskSubmit => "task_submit",
            Permission::TaskRead => "task_read",
            Permission::TaskControl => "task_control",
            Permission::CaseCreate => "case_create",
            Permission::CaseRead => "case_read",
            Permission::CaseControl => "case_control",
            Permission::MandateIssue => "mandate_issue",
            Permission::MandateRevoke => "mandate_revoke",
            Permission::TriggerIngest => "trigger_ingest",
            Permission::WorkerScale => "worker_scale",
            Permission::MemoryWrite => "memory_write",
            Permission::MergeGrant => "merge_grant",
            Permission::AttentionResolve => "attention_resolve",
            Permission::ProactivePropose => "proactive_propose",
            Permission::ProactiveExecute => "proactive_execute",
        }
    }

    /// Permissions that grant or extend authority. These are the ones a
    /// low-trust principal kind may never hold, and the ones audited on every
    /// use.
    pub fn is_authority_granting(&self) -> bool {
        matches!(
            self,
            Permission::MandateIssue
                | Permission::MandateRevoke
                | Permission::WorkerScale
                | Permission::MergeGrant
                | Permission::ProactiveExecute
        )
    }

    pub fn from_str_lossy(value: &str) -> Option<Self> {
        Some(match value.trim().to_ascii_lowercase().as_str() {
            "task_submit" => Permission::TaskSubmit,
            "task_read" => Permission::TaskRead,
            "task_control" => Permission::TaskControl,
            "case_create" => Permission::CaseCreate,
            "case_read" => Permission::CaseRead,
            "case_control" => Permission::CaseControl,
            "mandate_issue" => Permission::MandateIssue,
            "mandate_revoke" => Permission::MandateRevoke,
            "trigger_ingest" => Permission::TriggerIngest,
            "worker_scale" => Permission::WorkerScale,
            "memory_write" => Permission::MemoryWrite,
            "merge_grant" => Permission::MergeGrant,
            "attention_resolve" => Permission::AttentionResolve,
            "proactive_propose" => Permission::ProactivePropose,
            "proactive_execute" => Permission::ProactiveExecute,
            _ => return None,
        })
    }
}

/// Coarse-grained class of side effect. Scopes are expressed in these terms so
/// a mandate can say "you may read and write, but never push or merge".
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ActionClass {
    Read,
    Write,
    Execute,
    Dispatch,
    Push,
    Merge,
    ExternalNotify,
    Scale,
    Admin,
}

impl ActionClass {
    pub fn as_str(&self) -> &'static str {
        match self {
            ActionClass::Read => "read",
            ActionClass::Write => "write",
            ActionClass::Execute => "execute",
            ActionClass::Dispatch => "dispatch",
            ActionClass::Push => "push",
            ActionClass::Merge => "merge",
            ActionClass::ExternalNotify => "external_notify",
            ActionClass::Scale => "scale",
            ActionClass::Admin => "admin",
        }
    }

    /// The gateway permission required to perform an action of this class.
    pub fn required_permission(&self) -> Permission {
        match self {
            ActionClass::Read => Permission::TaskRead,
            ActionClass::Write => Permission::TaskSubmit,
            ActionClass::Execute => Permission::TaskSubmit,
            ActionClass::Dispatch => Permission::TaskControl,
            ActionClass::Push => Permission::MergeGrant,
            ActionClass::Merge => Permission::MergeGrant,
            ActionClass::ExternalNotify => Permission::MemoryWrite,
            ActionClass::Scale => Permission::WorkerScale,
            ActionClass::Admin => Permission::WorkerScale,
        }
    }
}

/// Monotonically increasing policy generation. A mandate carries the version it
/// was issued under; if policy has since been *tightened*, the mandate is
/// re-evaluated rather than silently honoured.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct PolicyVersion(pub u64);

impl PolicyVersion {
    pub fn initial() -> Self {
        Self(1)
    }

    pub fn next(&self) -> Self {
        Self(self.0.saturating_add(1))
    }
}

impl std::fmt::Display for PolicyVersion {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "v{}", self.0)
    }
}

/// A grant of permissions to one principal, optionally time-boxed.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Grant {
    pub principal: Principal,
    pub permissions: Vec<Permission>,
    /// When set, the grant only applies to this project/scope.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub project_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub expires_at: Option<DateTime<Utc>>,
    #[serde(default)]
    pub revoked: bool,
    /// Free-form provenance ("bootstrap", "config", "operator").
    #[serde(default)]
    pub source: String,
}

impl Grant {
    pub fn new(principal: Principal, permissions: Vec<Permission>) -> Self {
        Self {
            principal,
            permissions,
            project_id: None,
            expires_at: None,
            revoked: false,
            source: String::new(),
        }
    }

    pub fn with_project(mut self, project_id: impl Into<String>) -> Self {
        self.project_id = Some(project_id.into());
        self
    }

    pub fn with_expiry(mut self, expires_at: DateTime<Utc>) -> Self {
        self.expires_at = Some(expires_at);
        self
    }

    pub fn permits(&self, permission: Permission, project_id: Option<&str>) -> bool {
        if self.revoked || !self.permissions.contains(&permission) {
            return false;
        }
        match (&self.project_id, project_id) {
            (None, _) => true,
            (Some(scope), Some(requested)) => scope == requested,
            (Some(_), None) => false,
        }
    }
}

/// Why a decision was denied. Every variant names the exact fact that failed so
/// the audit trail can distinguish "no credential" from "credential revoked".
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", tag = "code")]
pub enum DenyReason {
    /// No credentials presented.
    Unauthenticated,
    /// The principal has no grant covering the permission.
    MissingPermission { required: Permission },
    /// A matching grant exists but is marked revoked.
    GrantRevoked { permission: Permission },
    /// A matching grant exists but has expired.
    GrantExpired { permission: Permission },
    /// The principal kind may never hold this permission.
    ForbiddenPrincipalKind {
        kind: PrincipalKind,
        permission: Permission,
    },
    /// The request carries a policy version older than the accepted minimum.
    StalePolicyVersion { provided: u64, minimum: u64 },
    /// Case has no mandate.
    MandateMissing,
    MandateRevoked { mandate_id: String },
    MandateExpired {
        mandate_id: String,
        expired_at: DateTime<Utc>,
    },
    /// Mandate does not cover the requested scope.
    MandateOutOfScope { scope: String, value: String },
    /// An attempt to widen a mandate. Always denied for non-human origins.
    MandateWideningDenied { detail: String },
    /// Tool is not in the allow table (fail-closed).
    ToolNotPermitted { tool: String },
    /// Tool is known but needs a mandate scope the case does not hold.
    ToolRequiresMandateScope { tool: String, scope: String },
    /// Tool is known and permitted, but it is a human-tier action
    /// (`ToolPolicy::human_approval`) and the principal does not hold the
    /// operator permission `proactive_execute`.
    ToolRequiresHumanApproval { tool: String },
    /// Sandbox configuration violates the hardened profile.
    SandboxViolation { violation: String },
    /// Task/case budget exhausted.
    BudgetExhausted { detail: String },
    /// The proactive mode does not allow this action.
    ProactiveModeDisallows { mode: String, action: String },
}

impl DenyReason {
    pub fn code(&self) -> &'static str {
        match self {
            DenyReason::Unauthenticated => "unauthenticated",
            DenyReason::MissingPermission { .. } => "missing_permission",
            DenyReason::GrantRevoked { .. } => "grant_revoked",
            DenyReason::GrantExpired { .. } => "grant_expired",
            DenyReason::ForbiddenPrincipalKind { .. } => "forbidden_principal_kind",
            DenyReason::StalePolicyVersion { .. } => "stale_policy_version",
            DenyReason::MandateMissing => "mandate_missing",
            DenyReason::MandateRevoked { .. } => "mandate_revoked",
            DenyReason::MandateExpired { .. } => "mandate_expired",
            DenyReason::MandateOutOfScope { .. } => "mandate_out_of_scope",
            DenyReason::MandateWideningDenied { .. } => "mandate_widening_denied",
            DenyReason::ToolNotPermitted { .. } => "tool_not_permitted",
            DenyReason::ToolRequiresMandateScope { .. } => "tool_requires_mandate_scope",
            DenyReason::ToolRequiresHumanApproval { .. } => "tool_requires_human_approval",
            DenyReason::SandboxViolation { .. } => "sandbox_violation",
            DenyReason::BudgetExhausted { .. } => "budget_exhausted",
            DenyReason::ProactiveModeDisallows { .. } => "proactive_mode_disallows",
        }
    }
}

/// Result of a policy evaluation.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", tag = "decision")]
pub enum PolicyDecision {
    Allow,
    Deny { reason: DenyReason },
}

impl PolicyDecision {
    pub fn allow() -> Self {
        PolicyDecision::Allow
    }

    pub fn deny(reason: DenyReason) -> Self {
        PolicyDecision::Deny { reason }
    }

    pub fn is_allowed(&self) -> bool {
        matches!(self, PolicyDecision::Allow)
    }

    pub fn is_denied(&self) -> bool {
        !self.is_allowed()
    }

    pub fn deny_reason(&self) -> Option<&DenyReason> {
        match self {
            PolicyDecision::Deny { reason } => Some(reason),
            PolicyDecision::Allow => None,
        }
    }

    pub fn deny_code(&self) -> Option<&'static str> {
        self.deny_reason().map(|reason| reason.code())
    }
}

/// Rejection raised while *building* a policy snapshot. Separate from
/// [`PolicyDecision`] on purpose: an invalid grant is a configuration bug, not a
/// runtime authorization outcome.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GrantError {
    ForbiddenPrincipalKind {
        kind: PrincipalKind,
        permission: Permission,
    },
}

impl std::fmt::Display for GrantError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            GrantError::ForbiddenPrincipalKind { kind, permission } => write!(
                f,
                "principal kind {} may never hold {}",
                kind.as_str(),
                permission.as_str()
            ),
        }
    }
}

impl std::error::Error for GrantError {}

/// The complete authorization state at one policy version.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PolicySnapshot {
    pub version: PolicyVersion,
    pub grants: Vec<Grant>,
    /// Mandate ids revoked since the snapshot was built (fast negative lookup).
    #[serde(default)]
    pub revoked_mandates: BTreeMap<String, String>,
    /// Requests carrying a policy version below this are refused outright — used
    /// after a tightening rollout so in-flight mandates re-apply.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub minimum_accepted_version: Option<PolicyVersion>,
}

impl Default for PolicySnapshot {
    fn default() -> Self {
        Self::empty(PolicyVersion::initial())
    }
}

impl PolicySnapshot {
    pub fn empty(version: PolicyVersion) -> Self {
        Self {
            version,
            grants: Vec::new(),
            revoked_mandates: BTreeMap::new(),
            minimum_accepted_version: None,
        }
    }

    /// Add a grant, rejecting kinds that may never hold the permission.
    pub fn with_grant(mut self, grant: Grant) -> Result<Self, GrantError> {
        for permission in &grant.permissions {
            if !grant.principal.kind.may_hold(*permission) {
                return Err(GrantError::ForbiddenPrincipalKind {
                    kind: grant.principal.kind,
                    permission: *permission,
                });
            }
        }
        self.grants.push(grant);
        Ok(self)
    }

    pub fn revoke_mandate(&mut self, mandate_id: impl Into<String>, reason: impl Into<String>) {
        self.revoked_mandates.insert(mandate_id.into(), reason.into());
    }

    pub fn is_mandate_revoked(&self, mandate_id: &str) -> Option<&String> {
        self.revoked_mandates.get(mandate_id)
    }

    /// Core evaluation. `project_id` narrows project-scoped grants.
    pub fn evaluate(
        &self,
        principal: &Principal,
        permission: Permission,
        project_id: Option<&str>,
        now: DateTime<Utc>,
    ) -> PolicyDecision {
        if !principal.is_authenticated() {
            return PolicyDecision::deny(DenyReason::Unauthenticated);
        }
        if !principal.kind.may_hold(permission) {
            return PolicyDecision::deny(DenyReason::ForbiddenPrincipalKind {
                kind: principal.kind,
                permission,
            });
        }
        let mut saw_revoked = false;
        let mut saw_expired = false;
        for grant in self
            .grants
            .iter()
            .filter(|grant| &grant.principal == principal)
            .filter(|grant| grant.permissions.contains(&permission))
        {
            if grant.revoked {
                saw_revoked = true;
                continue;
            }
            if let Some(expires_at) = grant.expires_at {
                if expires_at <= now {
                    saw_expired = true;
                    continue;
                }
            }
            if grant.permits(permission, project_id) {
                return PolicyDecision::allow();
            }
            // Project scope mismatch: another grant may still cover it.
        }
        if saw_revoked {
            return PolicyDecision::deny(DenyReason::GrantRevoked { permission });
        }
        if saw_expired {
            return PolicyDecision::deny(DenyReason::GrantExpired { permission });
        }
        PolicyDecision::deny(DenyReason::MissingPermission { required: permission })
    }

    /// Reject a request that carries an old policy version after a tightening
    /// rollout.
    pub fn check_version(&self, provided: Option<u64>) -> PolicyDecision {
        let (Some(provided), Some(minimum)) = (provided, self.minimum_accepted_version) else {
            return PolicyDecision::allow();
        };
        if provided < minimum.0 {
            return PolicyDecision::deny(DenyReason::StalePolicyVersion {
                provided,
                minimum: minimum.0,
            });
        }
        PolicyDecision::allow()
    }
}

/// Allow table entry for one tool.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ToolPolicy {
    pub tool: String,
    pub action: ActionClass,
    /// When true the caller must hold a mandate whose tool/action scope covers
    /// this tool. Unknown tools are represented by `known = false`.
    pub known: bool,
    pub requires_mandate_scope: bool,
    /// When true a human approval step is required even inside a mandate.
    pub human_approval: bool,
}

impl ToolPolicy {
    pub fn allow(tool: impl Into<String>, action: ActionClass) -> Self {
        Self {
            tool: tool.into(),
            action,
            known: true,
            requires_mandate_scope: true,
            human_approval: false,
        }
    }

    pub fn read_only(tool: impl Into<String>) -> Self {
        Self {
            known: true,
            requires_mandate_scope: false,
            ..Self::allow(tool, ActionClass::Read)
        }
    }

    pub fn with_human_approval(mut self) -> Self {
        self.human_approval = true;
        self
    }

    pub fn without_mandate_scope(mut self) -> Self {
        self.requires_mandate_scope = false;
        self
    }
}

/// Fail-closed tool allow table.
///
/// A tool that is not in the table is **denied**; it is not silently treated as
/// read-only. That is what makes tool-level enforcement real rather than
/// advisory: adding a new tool to a sandbox requires adding it here with an
/// explicit [`ActionClass`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ToolPolicyTable {
    rules: BTreeMap<String, ToolPolicy>,
}

impl Default for ToolPolicyTable {
    fn default() -> Self {
        Self::builtin()
    }
}

impl ToolPolicyTable {
    /// The built-in table covering the tools UC sandboxes expose today.
    pub fn builtin() -> Self {
        let mut rules = BTreeMap::new();
        let mut insert = |policy: ToolPolicy| {
            rules.insert(policy.tool.clone(), policy);
        };
        for tool in ["Read", "Glob", "Grep", "LS", "NotebookRead", "TodoRead"] {
            insert(ToolPolicy::read_only(tool));
        }
        for tool in ["Edit", "Write", "NotebookEdit", "MultiEdit"] {
            insert(ToolPolicy::allow(tool, ActionClass::Write));
        }
        for tool in ["Bash", "BashOutput", "KillBash"] {
            insert(ToolPolicy::allow(tool, ActionClass::Execute));
        }
        for tool in ["Task", "Agent"] {
            insert(ToolPolicy::allow(tool, ActionClass::Dispatch));
        }
        insert(ToolPolicy::allow("git_push", ActionClass::Push));
        insert(ToolPolicy::allow("merge", ActionClass::Merge));
        insert(ToolPolicy::allow("github_comment", ActionClass::ExternalNotify));
        insert(ToolPolicy::allow("github_issue_update", ActionClass::ExternalNotify));
        insert(ToolPolicy::allow("memory_write", ActionClass::Write));
        insert(
            ToolPolicy::allow("scale_workers", ActionClass::Scale).with_human_approval(),
        );
        insert(ToolPolicy::allow("docker_run", ActionClass::Execute).with_human_approval());
        Self { rules }
    }

    pub fn insert(&mut self, policy: ToolPolicy) {
        self.rules.insert(policy.tool.clone(), policy);
    }

    /// Look up a tool. Unknown tools yield `known = false` and must be denied by
    /// the caller.
    pub fn requirement(&self, tool: &str) -> ToolPolicy {
        self.rules.get(tool).cloned().unwrap_or(ToolPolicy {
            tool: tool.to_string(),
            action: ActionClass::Admin,
            known: false,
            requires_mandate_scope: true,
            human_approval: true,
        })
    }

    /// Convenience evaluation for the common case: permission check against the
    /// snapshot, then the mandate-scope check is left to the caller (it needs
    /// the case's mandate). Unknown tools are denied here.
    pub fn evaluate_permission(
        &self,
        tool: &str,
        principal: &Principal,
        snapshot: &PolicySnapshot,
        project_id: Option<&str>,
        now: DateTime<Utc>,
    ) -> PolicyDecision {
        let requirement = self.requirement(tool);
        if !requirement.known {
            return PolicyDecision::deny(DenyReason::ToolNotPermitted {
                tool: tool.to_string(),
            });
        }
        snapshot.evaluate(
            principal,
            requirement.action.required_permission(),
            project_id,
            now,
        )
    }

    pub fn known_tools(&self) -> impl Iterator<Item = &str> {
        self.rules.keys().map(|key| key.as_str())
    }
}

/// A sandbox configuration, reduced to the security-relevant subset.
///
/// This mirrors what `docker/docker-compose.yml` and the Python sandbox actually
/// set, so the gateway can refuse a configuration before it is launched instead
/// of discovering the hole after the fact.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
pub struct SandboxSecurityProfile {
    #[serde(default)]
    pub image: String,
    #[serde(default)]
    pub privileged: bool,
    /// Container network mode: `none`, `bridge`, `host`.
    #[serde(default)]
    pub network_mode: String,
    #[serde(default)]
    pub docker_socket_mounted: bool,
    #[serde(default)]
    pub host_pid: bool,
    #[serde(default)]
    pub read_only_rootfs: bool,
    #[serde(default)]
    pub run_as_root: bool,
    #[serde(default)]
    pub seccomp_profile: String,
    /// Host paths bind-mounted read-write.
    #[serde(default)]
    pub writable_host_mounts: Vec<String>,
    #[serde(default)]
    pub memory_limit_bytes: u64,
    #[serde(default)]
    pub cpu_limit_millis: u64,
}

/// A violated hardening rule.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", tag = "kind")]
pub enum SandboxViolation {
    PrivilegedContainer,
    DockerSocketMounted,
    HostNetwork,
    HostPid,
    WritableHostMount { path: String },
    RunsAsRoot,
    SeccompUnconfined,
    NoResourceLimits,
}

impl SandboxViolation {
    pub fn as_str(&self) -> &'static str {
        match self {
            SandboxViolation::PrivilegedContainer => "privileged_container",
            SandboxViolation::DockerSocketMounted => "docker_socket_mounted",
            SandboxViolation::HostNetwork => "host_network",
            SandboxViolation::HostPid => "host_pid",
            SandboxViolation::WritableHostMount { .. } => "writable_host_mount",
            SandboxViolation::RunsAsRoot => "runs_as_root",
            SandboxViolation::SeccompUnconfined => "seccomp_unconfined",
            SandboxViolation::NoResourceLimits => "no_resource_limits",
        }
    }
}

impl std::fmt::Display for SandboxViolation {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SandboxViolation::WritableHostMount { path } => {
                write!(f, "writable_host_mount({path})")
            }
            other => f.write_str(other.as_str()),
        }
    }
}

impl SandboxSecurityProfile {
    /// Validate against the hardened profile.
    ///
    /// The Docker socket is the single most dangerous mount in this system (it
    /// is root-equivalent on the host), so it is refused unconditionally: the
    /// gateway's `ScaleWorkers` runs on the host side, never inside a worker
    /// sandbox.
    pub fn validate(&self) -> Result<(), Vec<SandboxViolation>> {
        let mut violations = Vec::new();
        if self.privileged {
            violations.push(SandboxViolation::PrivilegedContainer);
        }
        if self.docker_socket_mounted {
            violations.push(SandboxViolation::DockerSocketMounted);
        }
        if self.network_mode.eq_ignore_ascii_case("host") {
            violations.push(SandboxViolation::HostNetwork);
        }
        if self.host_pid {
            violations.push(SandboxViolation::HostPid);
        }
        for path in &self.writable_host_mounts {
            violations.push(SandboxViolation::WritableHostMount { path: path.clone() });
        }
        if self.run_as_root {
            violations.push(SandboxViolation::RunsAsRoot);
        }
        let seccomp = self.seccomp_profile.to_ascii_lowercase();
        if seccomp.is_empty() || seccomp == "unconfined" {
            violations.push(SandboxViolation::SeccompUnconfined);
        }
        if self.memory_limit_bytes == 0 && self.cpu_limit_millis == 0 {
            violations.push(SandboxViolation::NoResourceLimits);
        }
        if violations.is_empty() {
            Ok(())
        } else {
            Err(violations)
        }
    }

    pub fn decision(&self) -> PolicyDecision {
        match self.validate() {
            Ok(()) => PolicyDecision::allow(),
            Err(violations) => {
                let mut rendered: Vec<String> =
                    violations.iter().map(|v| v.to_string()).collect();
                rendered.sort();
                PolicyDecision::deny(DenyReason::SandboxViolation {
                    violation: rendered.join(","),
                })
            }
        }
    }

    /// A hardened default suitable for a coding worker: no socket, no host
    /// network, non-root, seccomp default, bounded resources.
    pub fn hardened_worker() -> Self {
        Self {
            image: "ultimatecoders-worker:local".to_string(),
            privileged: false,
            network_mode: "bridge".to_string(),
            docker_socket_mounted: false,
            host_pid: false,
            read_only_rootfs: false,
            run_as_root: false,
            seccomp_profile: "runtime/default".to_string(),
            writable_host_mounts: Vec::new(),
            memory_limit_bytes: 2 * 1024 * 1024 * 1024,
            cpu_limit_millis: 2000,
        }
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

    fn snapshot_with(grants: Vec<Grant>) -> PolicySnapshot {
        let mut snapshot = PolicySnapshot::empty(PolicyVersion::initial());
        for grant in grants {
            snapshot = snapshot.with_grant(grant).expect("grant is structurally valid");
        }
        snapshot
    }

    #[test]
    fn unauthenticated_is_denied() {
        let snapshot = PolicySnapshot::default();
        let decision = snapshot.evaluate(
            &Principal::anonymous(),
            Permission::TaskSubmit,
            None,
            now(),
        );
        assert_eq!(decision.deny_reason(), Some(&DenyReason::Unauthenticated));
    }

    #[test]
    fn external_trigger_cannot_hold_authority_granting_permission() {
        let result = PolicySnapshot::empty(PolicyVersion::initial()).with_grant(Grant::new(
            Principal::external_trigger("github"),
            vec![Permission::MandateIssue],
        ));
        assert_eq!(
            result,
            Err(GrantError::ForbiddenPrincipalKind {
                kind: PrincipalKind::ExternalTrigger,
                permission: Permission::MandateIssue
            })
        );
    }

    #[test]
    fn agent_cannot_scale_workers() {
        let result = PolicySnapshot::empty(PolicyVersion::initial()).with_grant(Grant::new(
            Principal::agent("planner"),
            vec![Permission::WorkerScale],
        ));
        assert!(result.is_err());
    }

    #[test]
    fn agent_cannot_enable_autonomous_execution() {
        // `ProactiveExecute` raises the autonomy mode; enabling it must stay a
        // human/operator decision, so the structural ceiling excludes it for
        // agents and workers regardless of configuration.
        let result = PolicySnapshot::empty(PolicyVersion::initial()).with_grant(Grant::new(
            Principal::agent("planner"),
            vec![Permission::ProactiveExecute],
        ));
        assert_eq!(
            result,
            Err(GrantError::ForbiddenPrincipalKind {
                kind: PrincipalKind::Agent,
                permission: Permission::ProactiveExecute
            })
        );
        // Proposing is still allowed: a suggestion carries no authority.
        assert!(PolicySnapshot::empty(PolicyVersion::initial())
            .with_grant(Grant::new(
                Principal::agent("planner"),
                vec![Permission::ProactivePropose],
            ))
            .is_ok());
    }

    #[test]
    fn external_trigger_may_hold_non_authority_permissions() {
        let snapshot = snapshot_with(vec![Grant::new(
            Principal::external_trigger("github"),
            vec![Permission::TriggerIngest],
        )]);
        assert!(snapshot
            .evaluate(
                &Principal::external_trigger("github"),
                Permission::TriggerIngest,
                None,
                now()
            )
            .is_allowed());
    }

    #[test]
    fn missing_permission_is_reported_as_missing_not_as_auth() {
        let snapshot = snapshot_with(vec![Grant::new(
            Principal::user("alice"),
            vec![Permission::TaskSubmit],
        )]);
        let decision = snapshot.evaluate(
            &Principal::user("alice"),
            Permission::WorkerScale,
            None,
            now(),
        );
        assert_eq!(
            decision.deny_reason(),
            Some(&DenyReason::MissingPermission {
                required: Permission::WorkerScale
            })
        );
    }

    #[test]
    fn revoked_and_expired_grants_are_distinguished() {
        let mut revoked = Grant::new(Principal::user("alice"), vec![Permission::TaskSubmit]);
        revoked.revoked = true;
        let snapshot = snapshot_with(vec![revoked]);
        assert_eq!(
            snapshot
                .evaluate(&Principal::user("alice"), Permission::TaskSubmit, None, now())
                .deny_code(),
            Some("grant_revoked")
        );

        let expired = Grant::new(Principal::user("bob"), vec![Permission::TaskSubmit])
            .with_expiry(now() - chrono::Duration::seconds(1));
        let snapshot = snapshot_with(vec![expired]);
        assert_eq!(
            snapshot
                .evaluate(&Principal::user("bob"), Permission::TaskSubmit, None, now())
                .deny_code(),
            Some("grant_expired")
        );
    }

    #[test]
    fn project_scoped_grant_only_covers_its_project() {
        let snapshot = snapshot_with(vec![Grant::new(
            Principal::user("alice"),
            vec![Permission::TaskSubmit],
        )
        .with_project("proj-a")]);
        assert!(snapshot
            .evaluate(
                &Principal::user("alice"),
                Permission::TaskSubmit,
                Some("proj-a"),
                now()
            )
            .is_allowed());
        assert!(snapshot
            .evaluate(
                &Principal::user("alice"),
                Permission::TaskSubmit,
                Some("proj-b"),
                now()
            )
            .is_denied());
        assert!(snapshot
            .evaluate(&Principal::user("alice"), Permission::TaskSubmit, None, now())
            .is_denied());
    }

    #[test]
    fn stale_policy_version_is_rejected_after_tightening() {
        let snapshot = PolicySnapshot {
            version: PolicyVersion(5),
            minimum_accepted_version: Some(PolicyVersion(3)),
            ..PolicySnapshot::empty(PolicyVersion(5))
        };
        assert!(snapshot.check_version(Some(3)).is_allowed());
        assert_eq!(
            snapshot.check_version(Some(2)).deny_code(),
            Some("stale_policy_version")
        );
        assert!(snapshot.check_version(None).is_allowed());
    }

    #[test]
    fn unknown_tool_is_denied_and_known_tools_have_explicit_classes() {
        let table = ToolPolicyTable::builtin();
        let unknown = table.requirement("rm_rf_everything");
        assert!(!unknown.known);
        assert_eq!(unknown.action, ActionClass::Admin);
        assert!(unknown.human_approval);

        let bash = table.requirement("Bash");
        assert!(bash.known);
        assert_eq!(bash.action, ActionClass::Execute);
        assert!(bash.requires_mandate_scope);

        let read = table.requirement("Read");
        assert!(read.known);
        assert_eq!(read.action, ActionClass::Read);
        assert!(!read.requires_mandate_scope);

        assert!(table.requirement("scale_workers").human_approval);
    }

    #[test]
    fn unknown_tool_permission_check_fails_closed() {
        let table = ToolPolicyTable::builtin();
        let snapshot = snapshot_with(vec![Grant::new(
            Principal::user("alice"),
            vec![Permission::TaskSubmit],
        )]);
        let decision =
            table.evaluate_permission("nope", &Principal::user("alice"), &snapshot, None, now());
        assert_eq!(decision.deny_code(), Some("tool_not_permitted"));
    }

    #[test]
    fn hardened_worker_profile_has_no_violations() {
        assert_eq!(SandboxSecurityProfile::hardened_worker().validate(), Ok(()));
    }

    #[test]
    fn docker_socket_and_privileged_are_always_refused() {
        let mut profile = SandboxSecurityProfile::hardened_worker();
        profile.docker_socket_mounted = true;
        profile.privileged = true;
        let violations = profile.validate().expect_err("hardening violations");
        assert!(violations.contains(&SandboxViolation::DockerSocketMounted));
        assert!(violations.contains(&SandboxViolation::PrivilegedContainer));
        assert_eq!(
            profile.decision().deny_code(),
            Some("sandbox_violation")
        );
    }

    #[test]
    fn missing_seccomp_and_limits_are_violations() {
        let profile = SandboxSecurityProfile {
            seccomp_profile: "unconfined".to_string(),
            ..SandboxSecurityProfile::hardened_worker()
        };
        let violations = profile.validate().expect_err("seccomp violation");
        assert_eq!(violations, vec![SandboxViolation::SeccompUnconfined]);

        let profile = SandboxSecurityProfile {
            memory_limit_bytes: 0,
            cpu_limit_millis: 0,
            ..SandboxSecurityProfile::hardened_worker()
        };
        assert_eq!(
            profile.validate(),
            Err(vec![SandboxViolation::NoResourceLimits])
        );
    }

    #[test]
    fn permission_wire_tokens_round_trip() {
        for permission in [
            Permission::TaskSubmit,
            Permission::MandateIssue,
            Permission::WorkerScale,
            Permission::ProactiveExecute,
        ] {
            assert_eq!(
                Permission::from_str_lossy(permission.as_str()),
                Some(permission)
            );
        }
        assert!(Permission::MandateIssue.is_authority_granting());
        assert!(!Permission::ProactivePropose.is_authority_granting());
    }
}
