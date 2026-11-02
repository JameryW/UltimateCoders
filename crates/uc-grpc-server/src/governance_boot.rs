//! Gateway bootstrap for the governance plane.
//!
//! The stores live in `uc-engine`; this module is the only place that reads the
//! environment and decides *which* stores the process gets. Keeping it out of
//! `main.rs` matters for two reasons:
//!
//! 1. `main.rs` is already long and mostly about wiring transports; the governance
//!    boot sequence (durability choice → policy bootstrap → crash recovery) is a
//!    separate concern with its own failure policy.
//! 2. Every step here is **fail-closed**: a misconfigured governance plane must
//!    deny operations, never silently fall back to "allow". Degrading to an
//!    in-memory store is acceptable (it is explicit in the logs and `is_durable()`
//!    reports it); degrading to an empty *policy* is only acceptable because an
//!    empty policy denies everything.
//!
//! ## Environment
//!
//! | Variable | Default | Meaning |
//! |---|---|---|
//! | `UC_GOVERNANCE_BACKEND` | `memory` | `memory` or `postgres` |
//! | `UC_DATABASE_URL` | _(unset)_ | required for `postgres` |
//! | `UC_POLICY_BOOTSTRAP` | _(unset)_ | `dev` installs [`PolicyEngine::development_snapshot`] |
//! | `UC_OPERATOR_ID` | `operator` | principal id used by the `dev` snapshot |
//! | `UC_POLICY_GRANTS_JSON` | _(unset)_ | JSON array of grants (see [`parse_grants`]) |
//! | `UC_PROACTIVE_MODE` | `off` | `off`/`observe`/`propose`/`execute` |
//!
//! Precedence: a persisted policy snapshot always wins over environment
//! bootstrap, so restarting a configured gateway cannot silently reset policy to
//! the dev snapshot. Revocation and grants therefore survive restarts.

use std::sync::Arc;

use chrono::Utc;

use uc_engine::governance::{Governance, InMemoryRecordStore, PolicyEngine, RecordStore};
#[cfg(feature = "storage")]
use uc_engine::governance::PostgresRecordStore;
use uc_types::governance::{
    Grant, Permission, PolicySnapshot, PolicyVersion, Principal, PrincipalKind, ProactiveMode,
};

/// Build the governance plane from the environment and run boot recovery.
///
/// Recovery order matters: the outbox is recovered *before* submissions are
/// re-queued, so an entry orphaned mid-publish is re-delivered rather than
/// duplicating the queue entry a submission recovery would add.
pub async fn create_governance() -> Arc<Governance> {
    let governance = Arc::new(Governance::new(choose_record_store().await));
    tracing::info!(
        durable = governance.is_durable(),
        "Governance plane initialized"
    );

    recover(&governance).await;
    bootstrap_policy(&governance).await;
    bootstrap_proactive(&governance).await;
    governance
}

/// Choose the record store. A configured-but-unreachable PostgreSQL degrades to
/// the in-memory store with a loud warning rather than refusing to start: a
/// gateway that cannot serve is worse than a gateway that serves non-durably for
/// the duration of the outage, and `is_durable()` keeps the degradation visible.
async fn choose_record_store() -> Arc<dyn RecordStore> {
    let backend = std::env::var("UC_GOVERNANCE_BACKEND")
        .unwrap_or_else(|_| "memory".to_string())
        .trim()
        .to_ascii_lowercase();
    if backend == "memory" || backend.is_empty() {
        tracing::info!(
            "UC_GOVERNANCE_BACKEND=memory — governance records are process-local \
             (submissions, outbox and cases do not survive a restart)"
        );
        return Arc::new(InMemoryRecordStore::new());
    }
    if backend != "postgres" && backend != "pg" {
        tracing::warn!(
            value = %backend,
            "Unknown UC_GOVERNANCE_BACKEND — falling back to in-memory governance records"
        );
        return Arc::new(InMemoryRecordStore::new());
    }

    let Ok(url) = std::env::var("UC_DATABASE_URL") else {
        tracing::warn!(
            "UC_GOVERNANCE_BACKEND=postgres but UC_DATABASE_URL is unset — \
             falling back to in-memory governance records"
        );
        return Arc::new(InMemoryRecordStore::new());
    };

    #[cfg(feature = "storage")]
    {
        match PostgresRecordStore::connect(&url, 4).await {
            Ok(store) => {
                tracing::info!("Governance records backed by PostgreSQL");
                return Arc::new(store);
            }
            Err(error) => {
                tracing::warn!(
                    error = %error,
                    "PostgreSQL governance store unavailable — falling back to in-memory"
                );
            }
        }
    }
    #[cfg(not(feature = "storage"))]
    {
        tracing::warn!(
            "UC_GOVERNANCE_BACKEND=postgres requires the `storage` feature; \
             this build falls back to in-memory governance records"
        );
    }
    Arc::new(InMemoryRecordStore::new())
}

/// Crash recovery: re-queue orphaned outbox entries and unfinished submissions.
pub async fn recover(governance: &Governance) {
    let now = Utc::now();
    match governance.outbox.recover(now).await {
        Ok(0) => {}
        Ok(count) => tracing::info!(count, "Re-queued in-flight outbox entries after restart"),
        Err(error) => tracing::warn!(error = %error, "Outbox recovery failed"),
    }
    match governance
        .submissions
        .recover_unfinished(&governance.outbox, now, 500)
        .await
    {
        Ok(summary) if summary.requeued > 0 => tracing::info!(
            requeued = summary.requeued,
            already_applied = summary.already_applied,
            failed = summary.failed,
            "Recovered unfinished submissions (same derived task ids)"
        ),
        Ok(_) => {}
        Err(error) => tracing::warn!(error = %error, "Submission recovery failed"),
    }
}

/// Install policy. Persisted snapshot wins; otherwise `UC_POLICY_BOOTSTRAP=dev`
/// installs the development snapshot or `UC_POLICY_GRANTS_JSON` installs explicit
/// grants. With none of those, the plane stays empty — which denies everything.
pub async fn bootstrap_policy(governance: &Governance) {
    match governance.policy.load_persisted().await {
        Ok(Some(version)) => {
            tracing::info!(
                version = version.0,
                "Loaded persisted policy snapshot (environment bootstrap skipped)"
            );
            return;
        }
        Ok(None) => {}
        Err(error) => {
            tracing::warn!(
                error = %error,
                "Could not read the persisted policy snapshot; falling back to environment bootstrap"
            );
        }
    }

    let bootstrap = std::env::var("UC_POLICY_BOOTSTRAP")
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase();
    let mut snapshot: Option<PolicySnapshot> = None;

    if bootstrap == "dev" {
        let operator = std::env::var("UC_OPERATOR_ID").unwrap_or_else(|_| "operator".to_string());
        tracing::warn!(
            operator = %operator,
            "UC_POLICY_BOOTSTRAP=dev — installing the development policy snapshot. \
             Do not use this in a shared deployment: it grants the operator every permission"
        );
        snapshot = Some(PolicyEngine::development_snapshot(&operator));
    }

    if let Ok(raw) = std::env::var("UC_POLICY_GRANTS_JSON") {
        match parse_grants(&raw) {
            Ok(grants) => {
                let mut built = snapshot.unwrap_or_else(|| PolicySnapshot::empty(PolicyVersion::initial()));
                let mut applied = 0usize;
                for grant in grants {
                    match built.clone().with_grant(grant) {
                        Ok(next) => {
                            built = next;
                            applied += 1;
                        }
                        Err(error) => {
                            // A forbidden grant is a configuration error; refuse the
                            // grant, keep the rest. Never widen to accommodate it.
                            tracing::error!(
                                error = %error,
                                "UC_POLICY_GRANTS_JSON grant refused by the structural ceiling"
                            );
                        }
                    }
                }
                tracing::info!(grants = applied, "Loaded policy grants from UC_POLICY_GRANTS_JSON");
                snapshot = Some(built);
            }
            Err(error) => tracing::error!(
                error = %error,
                "UC_POLICY_GRANTS_JSON is invalid — no grants loaded from it"
            ),
        }
    }

    match snapshot {
        Some(snapshot) => match governance.policy.install(snapshot).await {
            Ok(version) => tracing::info!(version = version.0, "Installed governance policy snapshot"),
            Err(error) => tracing::error!(
                error = %error,
                "Failed to install the governance policy snapshot — all governance \
                 operations will be denied (fail-closed)"
            ),
        },
        None => tracing::warn!(
            "No governance policy configured (set UC_POLICY_BOOTSTRAP=dev or \
             UC_POLICY_GRANTS_JSON) — every governance RPC and ScaleWorkers will be \
             denied with `missing_permission` (fail-closed)"
        ),
    }
}

/// Restore the proactive mode (default off) and never enable execution from an
/// unset variable.
pub async fn bootstrap_proactive(governance: &Governance) {
    if let Err(error) = governance.proactive.load_persisted().await {
        tracing::warn!(error = %error, "Could not read persisted proactive state");
    }
    let Ok(raw) = std::env::var("UC_PROACTIVE_MODE") else {
        return;
    };
    let Some(mode) = ProactiveMode::from_str_lossy(&raw) else {
        tracing::warn!(
            value = %raw,
            "Unknown UC_PROACTIVE_MODE — staying in `off` (fail-closed)"
        );
        return;
    };
    match governance
        .proactive
        .set_mode(mode, None, None, None)
        .await
    {
        Ok(state) => tracing::info!(
            mode = state.mode.as_str(),
            "Proactive mode configured"
        ),
        Err(error) => tracing::warn!(error = %error, "Failed to persist the proactive mode"),
    }
}

/// One grant entry in `UC_POLICY_GRANTS_JSON`.
///
/// ```json
/// [{"kind": "service", "id": "dashboard",
///   "permissions": ["case_read", "attention_resolve", "trigger_ingest"]}]
/// ```
#[derive(Debug, serde::Deserialize)]
pub struct GrantSpec {
    pub kind: String,
    pub id: String,
    pub permissions: Vec<String>,
    #[serde(default)]
    pub project_id: Option<String>,
}

/// Parse `UC_POLICY_GRANTS_JSON` into grants.
///
/// Unknown permission or principal-kind tokens are **errors**, not skips: a typo
/// silently dropping a permission is exactly the kind of config bug that looks
/// like a policy denial later.
pub fn parse_grants(raw: &str) -> Result<Vec<Grant>, String> {
    let specs: Vec<GrantSpec> =
        serde_json::from_str(raw).map_err(|error| format!("invalid JSON: {error}"))?;
    let mut grants = Vec::with_capacity(specs.len());
    for spec in specs {
        let kind = match spec.kind.trim().to_ascii_lowercase().as_str() {
            "user" => PrincipalKind::User,
            "service" => PrincipalKind::Service,
            "agent" => PrincipalKind::Agent,
            "worker" => PrincipalKind::Worker,
            other => {
                return Err(format!(
                    "principal kind `{other}` is not grantable (external_trigger/anonymous may never hold grants)"
                ))
            }
        };
        let mut permissions = Vec::with_capacity(spec.permissions.len());
        for token in &spec.permissions {
            let permission = Permission::from_str_lossy(token)
                .ok_or_else(|| format!("unknown permission `{token}`"))?;
            permissions.push(permission);
        }
        let mut grant = Grant::new(Principal::new(kind, spec.id), permissions);
        if let Some(project_id) = spec.project_id {
            grant = grant.with_project(project_id);
        }
        grants.push(grant);
    }
    Ok(grants)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn grants_parse_into_a_valid_snapshot() {
        let grants = parse_grants(
            r#"[{"kind":"service","id":"dashboard",
                 "permissions":["case_read","attention_resolve","trigger_ingest"]},
                {"kind":"user","id":"alice","permissions":["mandate_issue"],
                 "project_id":"proj-a"}]"#,
        )
        .expect("valid grants");
        assert_eq!(grants.len(), 2);
        let mut snapshot = PolicySnapshot::empty(PolicyVersion::initial());
        for grant in grants {
            snapshot = snapshot.clone().with_grant(grant).expect("structurally valid");
        }
        let now = Utc::now();
        assert!(snapshot
            .evaluate(
                &Principal::service("dashboard"),
                Permission::CaseRead,
                None,
                now
            )
            .is_allowed());
        assert!(snapshot
            .evaluate(
                &Principal::user("alice"),
                Permission::MandateIssue,
                Some("proj-a"),
                now
            )
            .is_allowed());
        assert!(snapshot
            .evaluate(
                &Principal::user("alice"),
                Permission::MandateIssue,
                Some("proj-b"),
                now
            )
            .is_denied());
    }

    #[test]
    fn a_typo_in_a_permission_is_an_error_not_a_silent_skip() {
        let error = parse_grants(r#"[{"kind":"user","id":"alice","permissions":["task_submitt"]}]"#)
            .expect_err("unknown permission must be rejected");
        assert!(error.contains("task_submitt"), "error names the bad token: {error}");
    }

    #[test]
    fn an_ungrantable_principal_kind_is_refused() {
        let error = parse_grants(
            r#"[{"kind":"external_trigger","id":"github","permissions":["mandate_issue"]}]"#,
        )
        .expect_err("trigger cannot be granted authority");
        assert!(error.contains("not grantable"));
    }

    #[test]
    fn an_authority_granting_permission_for_an_agent_is_refused_by_the_ceiling() {
        let grants = parse_grants(r#"[{"kind":"agent","id":"planner","permissions":["worker_scale"]}]"#)
            .expect("parses");
        let result = PolicySnapshot::empty(PolicyVersion::initial()).with_grant(grants[0].clone());
        assert!(
            result.is_err(),
            "the structural ceiling must reject worker_scale for an agent"
        );
    }

    #[test]
    fn invalid_json_is_an_error() {
        assert!(parse_grants("{not json").is_err());
    }
}
