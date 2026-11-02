//! `AttentionService` — the typed "why is this case not progressing" surface and
//! the capacity accounting built on it.
//!
//! Two invariants from the plan are visible here:
//!
//! * **Quiet waiting costs nothing.** [`AttentionKind::holds_worker_slot`] is the
//!   single place that decides whether a waiting case consumes a worker slot, and
//!   it is `false` for every attention kind. `CapacitySnapshot::slots_saved_by_quiet_waiting`
//!   makes that visible to the operator instead of merely true.
//! * **Only a human decision interrupts a human.** `NeedsHuman` is the one kind
//!   that reserves a human-priority slot and pages someone; the others are
//!   reported on the worklist but never interrupt.
//!
//! Attention items are a *projection*: raising one does not change the case's
//! status. The caller that knows the case is now waiting transitions it with
//! `CaseService.TransitionCase`, so there is one authority for case state.

use std::sync::Arc;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use tonic::{Request, Response, Status};
use uc_engine::governance::{AttentionBoardError, Governance};
use uc_types::governance::{
    AttentionItem, AttentionKind, AttentionResolution, CapacitySnapshot, Permission, WorkCase,
};

use super::{
    clamp_limit, encode_optional_timestamp, encode_timestamp, optional_text, required_text,
    OpError, OpResult,
};
use crate::governance::policy_interceptor::{require, resolve_declared_principal};
use crate::ultimate_coders::attention_service_server::AttentionService;

/// Worker-pool total the gateway reports capacity against.
///
/// The governance plane does not own the worker pool — the gateway does — so the
/// slot counters come from configuration until the scheduler exposes a live
/// count: `UC_WORKER_SLOTS_TOTAL` (default `0`) and `UC_WORKER_SLOTS_BUSY`
/// (default `0`). Every number governance *does* own (waiting/verifying/needs-human
/// /blocked cases and `slots_saved_by_quiet_waiting`) is computed from the
/// attention board and is independent of both.
fn capacity_config() -> (u32, u32) {
    let read = |key: &str| -> u32 {
        std::env::var(key)
            .ok()
            .and_then(|value| value.trim().parse::<u32>().ok())
            .unwrap_or(0)
    };
    (read("UC_WORKER_SLOTS_TOTAL"), read("UC_WORKER_SLOTS_BUSY"))
}

/// Wire form of [`AttentionItem`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AttentionItemView {
    pub id: String,
    pub case_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub task_id: Option<String>,
    pub kind: String,
    pub summary: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
    pub since: DateTime<Utc>,
    pub priority: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub human_action: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub resolved_at: Option<DateTime<Utc>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub resolution_summary: Option<String>,
}

impl AttentionItemView {
    fn from_item(item: &AttentionItem) -> Self {
        Self {
            id: item.id.clone(),
            case_id: item.case_id.clone(),
            task_id: item.task_id.clone(),
            kind: item.kind.as_str().to_string(),
            summary: item.summary.clone(),
            detail: item.detail.clone(),
            since: item.since,
            priority: u32::from(item.priority),
            human_action: item.human_action.clone(),
            resolved_at: item.resolved_at,
            resolution_summary: item
                .resolution
                .as_ref()
                .map(AttentionResolution::summary),
        }
    }

    fn to_proto(&self) -> crate::ultimate_coders::AttentionItemProto {
        crate::ultimate_coders::AttentionItemProto {
            id: self.id.clone(),
            case_id: self.case_id.clone(),
            task_id: self.task_id.clone(),
            kind: self.kind.clone(),
            summary: self.summary.clone(),
            detail: self.detail.clone(),
            since: encode_timestamp(self.since),
            priority: self.priority,
            human_action: self.human_action.clone(),
            resolved_at: encode_optional_timestamp(self.resolved_at),
            resolution_summary: self.resolution_summary.clone(),
        }
    }
}

/// Wire form of [`CapacitySnapshot`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CapacityView {
    pub worker_slots_total: u32,
    pub worker_slots_busy: u32,
    pub worker_slots_reserved_human: u32,
    pub waiting_event_cases: u32,
    pub verifying_cases: u32,
    pub needs_human_items: u32,
    pub blocked_cases: u32,
    pub worker_slots_available: u32,
    pub slots_saved_by_quiet_waiting: u32,
}

impl CapacityView {
    fn from_snapshot(snapshot: &CapacitySnapshot) -> Self {
        Self {
            worker_slots_total: snapshot.worker_slots_total,
            worker_slots_busy: snapshot.worker_slots_busy,
            worker_slots_reserved_human: snapshot.worker_slots_reserved_human,
            waiting_event_cases: snapshot.waiting_event_cases,
            verifying_cases: snapshot.verifying_cases,
            needs_human_items: snapshot.needs_human_items,
            blocked_cases: snapshot.blocked_cases,
            // Derived fields are recomputed, never copied from a client: the
            // snapshot's own methods are the definition.
            worker_slots_available: snapshot.worker_slots_available(),
            slots_saved_by_quiet_waiting: snapshot.slots_saved_by_quiet_waiting(),
        }
    }

    fn to_proto(&self) -> crate::ultimate_coders::CapacityProto {
        crate::ultimate_coders::CapacityProto {
            worker_slots_total: self.worker_slots_total,
            worker_slots_busy: self.worker_slots_busy,
            worker_slots_reserved_human: self.worker_slots_reserved_human,
            waiting_event_cases: self.waiting_event_cases,
            verifying_cases: self.verifying_cases,
            needs_human_items: self.needs_human_items,
            blocked_cases: self.blocked_cases,
            worker_slots_available: self.worker_slots_available,
            slots_saved_by_quiet_waiting: self.slots_saved_by_quiet_waiting,
        }
    }
}

/// Result of [`list_attention`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ListAttentionOutcome {
    pub items: Vec<AttentionItemView>,
    pub capacity: CapacityView,
}

/// The operator worklist, plus the capacity snapshot the dashboard renders beside
/// it.
/// Slot totals source: the gateway's live worker registry.
pub type CapacitySource = Arc<tokio::sync::RwLock<crate::worker_service::WorkerRegistry>>;

pub async fn list_attention(
    governance: &Governance,
    request: crate::ultimate_coders::ListAttentionRequest,
) -> OpResult<ListAttentionOutcome> {
    list_attention_with(governance, None, request).await
}

/// [`list_attention`] with the live registry attached, so the capacity strip shows
/// the pool as it is now instead of the numbers an operator typed into the env.
pub async fn list_attention_with(
    governance: &Governance,
    registry: Option<&CapacitySource>,
    request: crate::ultimate_coders::ListAttentionRequest,
) -> OpResult<ListAttentionOutcome> {
    let limit = clamp_limit(request.limit);
    let kind = match optional_text(request.kind.as_deref()) {
        Some(token) => Some(AttentionKind::from_str_lossy(&token).ok_or_else(|| {
            OpError::invalid(
                "unknown_attention_kind",
                format!("unknown attention kind '{token}'"),
            )
        })?),
        None => None,
    };
    let case_id = optional_text(request.case_id.as_deref());

    let mut items: Vec<AttentionItemView> = governance
        .attention
        .worklist(request.include_resolved, limit)
        .await
        .map_err(attention_error)?
        .iter()
        .filter(|item| case_id.as_deref().map(|id| item.case_id == id).unwrap_or(true))
        .filter(|item| kind.map(|kind| item.kind == kind).unwrap_or(true))
        .map(AttentionItemView::from_item)
        .collect();
    items.truncate(limit);

    let capacity = capacity_snapshot(governance, registry).await?;
    Ok(ListAttentionOutcome { items, capacity })
}

/// Raise one attention item.
///
/// The item supersedes any other open item for the same case (a case waits on one
/// thing at a time), which is what keeps the worklist a decision queue instead of
/// an event log.
pub async fn raise_attention(
    governance: &Governance,
    request: crate::ultimate_coders::RaiseAttentionRequest,
) -> OpResult<AttentionItemView> {
    let now = super::now();
    let principal = resolve_declared_principal(
        optional_text(Some(request.principal_kind.as_str())).as_deref(),
        optional_text(Some(request.principal_id.as_str())).as_deref(),
    )?;
    let case_id = required_text(&request.case_id, "case_id")?;
    let kind_token = required_text(&request.kind, "kind")?;
    let kind = AttentionKind::from_str_lossy(&kind_token).ok_or_else(|| {
        OpError::invalid(
            "unknown_attention_kind",
            format!(
                "unknown attention kind '{kind_token}' (expected waiting_event | needs_human | \
                 blocked | verifying)"
            ),
        )
    })?;
    let summary = required_text(&request.summary, "summary")?;

    let Some((case, _version)) = governance
        .cases
        .get(&case_id)
        .await
        .map_err(super::case_service::case_store_error)?
    else {
        return Err(OpError::not_found(
            "case_not_found",
            format!("case '{case_id}' does not exist"),
        ));
    };
    // Raising an item is an act *on the case*: it reorders the human worklist, so
    // it needs the same permission as any other case change.
    require(
        governance,
        &principal,
        Permission::CaseControl,
        Some(case.project_id.as_str()),
        now,
    )?;

    let mut item = AttentionItem::new(
        uuid::Uuid::new_v4().to_string(),
        case_id.clone(),
        kind,
        summary,
        now,
    );
    item.detail = optional_text(request.detail.as_deref());
    item.human_action = optional_text(request.human_action.as_deref());
    item.task_id = optional_text(request.task_id.as_deref());
    item.priority = request
        .priority
        .map(|priority| {
            u8::try_from(priority)
                .unwrap_or(WorkCase::MAX_PRIORITY)
                .min(WorkCase::MAX_PRIORITY)
        })
        .unwrap_or(case.priority);

    let (item, superseded) = governance
        .attention
        .raise(item, now)
        .await
        .map_err(attention_error)?;
    tracing::info!(
        attention_id = %item.id,
        case_id = %item.case_id,
        kind = item.kind.as_str(),
        needs_human = item.kind.requires_human_now(),
        superseded = superseded.as_deref().unwrap_or(""),
        "attention item raised"
    );
    Ok(AttentionItemView::from_item(&item))
}

/// Resolve one attention item.
///
/// Idempotent by contract: resolving an already-resolved item returns the original
/// resolution, so a double click cannot overwrite the human's answer.
pub async fn resolve_attention(
    governance: &Governance,
    request: crate::ultimate_coders::ResolveAttentionRequest,
) -> OpResult<AttentionItemView> {
    let now = super::now();
    let principal = resolve_declared_principal(
        optional_text(Some(request.principal_kind.as_str())).as_deref(),
        optional_text(Some(request.principal_id.as_str())).as_deref(),
    )?;
    let attention_id = required_text(&request.attention_id, "attention_id")?;
    let kind_token = required_text(&request.resolution_kind, "resolution_kind")?;

    let Some(item) = governance
        .attention
        .get(&attention_id)
        .await
        .map_err(attention_error)?
    else {
        return Err(OpError::not_found(
            "attention_not_found",
            format!("attention item '{attention_id}' does not exist"),
        ));
    };
    // Authorize against the case the item belongs to, so a project-scoped grant
    // covers exactly its own cases.
    let project = governance
        .cases
        .get(&item.case_id)
        .await
        .map_err(super::case_service::case_store_error)?
        .map(|(case, _version)| case.project_id);
    require(
        governance,
        &principal,
        Permission::AttentionResolve,
        project.as_deref(),
        now,
    )?;

    let by = optional_text(request.by.as_deref()).unwrap_or_else(|| principal.id.clone());
    let detail = optional_text(request.detail.as_deref()).unwrap_or_default();
    let resolution = match kind_token.to_ascii_lowercase().as_str() {
        "human_decision" => AttentionResolution::HumanDecision {
            by: by.clone(),
            answer: detail.clone(),
        },
        "event_received" => AttentionResolution::EventReceived {
            trigger_id: detail.clone(),
        },
        "automatic" => AttentionResolution::Automatic { detail: detail.clone() },
        "case_closed" => AttentionResolution::CaseClosed { status: detail.clone() },
        other => {
            return Err(OpError::invalid(
                "unknown_resolution_kind",
                format!(
                    "unknown resolution_kind '{other}' (expected human_decision | event_received \
                     | automatic | case_closed)"
                ),
            ))
        }
    };

    let item = governance
        .attention
        .resolve(&attention_id, resolution, now)
        .await
        .map_err(attention_error)?;
    tracing::info!(
        attention_id = %item.id,
        case_id = %item.case_id,
        resolution = item
            .resolution
            .as_ref()
            .map(AttentionResolution::summary)
            .unwrap_or_default(),
        by = %principal,
        "attention item resolved"
    );
    Ok(AttentionItemView::from_item(&item))
}

/// The capacity snapshot on its own.
pub async fn get_capacity(governance: &Governance) -> OpResult<CapacityView> {
    capacity_snapshot(governance, None).await
}

/// [`get_capacity`] with the live registry attached.
pub async fn get_capacity_with(
    governance: &Governance,
    registry: Option<&CapacitySource>,
) -> OpResult<CapacityView> {
    capacity_snapshot(governance, registry).await
}

/// Build the capacity snapshot: governance counts from the board, slot counts from
/// configuration (see [`capacity_config`]).
async fn capacity_snapshot(
    governance: &Governance,
    registry: Option<&CapacitySource>,
) -> OpResult<CapacityView> {
    // Live registry first: if it knows about at least one usable slot, those are the
    // real numbers. The env fallback exists for a gateway that has no workers yet
    // (and for the dashboard's unit tests) — an operator-supplied total must never
    // mask an actual pool, or the "saved slots" metric would be fiction.
    let registry_totals = match registry {
        Some(registry) => Some(registry.read().await.capacity_totals()),
        None => None,
    };
    let (total, busy) = match registry_totals {
        Some((total, busy)) if total > 0 => (total, busy),
        _ => capacity_config(),
    };
    let counts = governance
        .attention
        .open_counts()
        .await
        .map_err(attention_error)?;
    let needs_human = counts
        .iter()
        .find(|(kind, _)| *kind == AttentionKind::NeedsHuman)
        .map(|(_, count)| *count)
        .unwrap_or(0);
    // One reserved slot per open human decision, capped at the pool: the point of
    // the reserve is that an answer can start executing immediately.
    let reserve = needs_human.min(total);
    let snapshot = governance
        .attention
        .capacity(total, busy, reserve)
        .await
        .map_err(attention_error)?;
    Ok(CapacityView::from_snapshot(&snapshot))
}

/// Map an attention-board failure onto the operation vocabulary.
fn attention_error(error: AttentionBoardError) -> OpError {
    match &error {
        AttentionBoardError::NotFound { attention_id } => OpError::not_found(
            "attention_not_found",
            format!("attention item '{attention_id}' does not exist"),
        ),
        AttentionBoardError::Contended => OpError::conflict(
            "attention_contended",
            "the attention board is contended; retry the operation",
        ),
        AttentionBoardError::Store(_) => {
            OpError::internal("attention_store_failed", error.to_string())
        }
    }
}

/// gRPC adapter over the operations above.
#[derive(Clone)]
pub struct AttentionServiceImpl {
    governance: Arc<Governance>,
    worker_registry: Option<CapacitySource>,
}

impl AttentionServiceImpl {
    pub fn new(governance: Arc<Governance>) -> Self {
        Self {
            governance,
            worker_registry: None,
        }
    }

    /// Attach the live worker registry so capacity reflects the real pool.
    pub fn with_registry(governance: Arc<Governance>, worker_registry: CapacitySource) -> Self {
        Self {
            governance,
            worker_registry: Some(worker_registry),
        }
    }

    pub fn governance(&self) -> &Arc<Governance> {
        &self.governance
    }
}

#[tonic::async_trait]
impl AttentionService for AttentionServiceImpl {
    async fn list_attention(
        &self,
        request: Request<crate::ultimate_coders::ListAttentionRequest>,
    ) -> Result<Response<crate::ultimate_coders::ListAttentionResponse>, Status> {
        let outcome = list_attention_with(
            &self.governance,
            self.worker_registry.as_ref(),
            request.into_inner(),
        )
        .await?;
        Ok(Response::new(
            crate::ultimate_coders::ListAttentionResponse {
                items: outcome.items.iter().map(AttentionItemView::to_proto).collect(),
                capacity: Some(outcome.capacity.to_proto()),
            },
        ))
    }

    async fn raise_attention(
        &self,
        request: Request<crate::ultimate_coders::RaiseAttentionRequest>,
    ) -> Result<Response<crate::ultimate_coders::RaiseAttentionResponse>, Status> {
        let item = raise_attention(&self.governance, request.into_inner()).await?;
        Ok(Response::new(
            crate::ultimate_coders::RaiseAttentionResponse {
                success: true,
                item: Some(item.to_proto()),
                error: None,
            },
        ))
    }

    async fn resolve_attention(
        &self,
        request: Request<crate::ultimate_coders::ResolveAttentionRequest>,
    ) -> Result<Response<crate::ultimate_coders::ResolveAttentionResponse>, Status> {
        let item = resolve_attention(&self.governance, request.into_inner()).await?;
        Ok(Response::new(
            crate::ultimate_coders::ResolveAttentionResponse {
                success: true,
                item: Some(item.to_proto()),
                error: None,
            },
        ))
    }

    async fn get_capacity(
        &self,
        _request: Request<crate::ultimate_coders::GetCapacityRequest>,
    ) -> Result<Response<crate::ultimate_coders::GetCapacityResponse>, Status> {
        let capacity =
            get_capacity_with(&self.governance, self.worker_registry.as_ref()).await?;
        Ok(Response::new(
            crate::ultimate_coders::GetCapacityResponse {
                capacity: Some(capacity.to_proto()),
            },
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use uc_types::governance::{CaseOrigin, Grant, PolicySnapshot, PolicyVersion};

    async fn plane() -> Governance {
        let governance = Governance::in_memory();
        let mut snapshot = PolicySnapshot::empty(PolicyVersion::initial());
        for permission in [Permission::CaseControl, Permission::AttentionResolve] {
            snapshot = snapshot
                .with_grant(Grant::new(
                    uc_types::governance::Principal::user("alice"),
                    vec![permission],
                ))
                .expect("user grants are structurally valid");
        }
        governance.policy.install(snapshot).await.unwrap();
        let now = super::super::now();
        let case = WorkCase::new("case", "desc", "proj", CaseOrigin::manual(), now);
        governance.cases.create_unique(case).await.unwrap();
        governance
    }

    async fn only_case_id(governance: &Governance) -> String {
        governance
            .cases
            .list(10)
            .await
            .unwrap()
            .first()
            .map(|(case, _version)| case.id.0.clone())
            .expect("fixture case exists")
    }

    fn raise(case_id: &str, kind: &str) -> crate::ultimate_coders::RaiseAttentionRequest {
        crate::ultimate_coders::RaiseAttentionRequest {
            case_id: case_id.to_string(),
            kind: kind.to_string(),
            summary: "CI is red".to_string(),
            detail: Some("run-42 failed".to_string()),
            human_action: Some("decide whether to retry".to_string()),
            task_id: Some("task-1".to_string()),
            priority: Some(7),
            principal_kind: "user".to_string(),
            principal_id: "alice".to_string(),
        }
    }

    #[tokio::test]
    async fn raise_and_resolve_round_trip() {
        let governance = plane().await;
        let case_id = only_case_id(&governance).await;
        let item = raise_attention(&governance, raise(&case_id, "needs_human"))
            .await
            .unwrap();
        assert_eq!(item.kind, "needs_human");
        assert_eq!(item.priority, 7);
        assert_eq!(item.resolved_at, None);

        let resolved = resolve_attention(
            &governance,
            crate::ultimate_coders::ResolveAttentionRequest {
                attention_id: item.id.clone(),
                resolution_kind: "human_decision".to_string(),
                by: Some("alice".to_string()),
                detail: Some("release/2.0".to_string()),
                principal_kind: "user".to_string(),
                principal_id: "alice".to_string(),
            },
        )
        .await
        .unwrap();
        assert!(resolved.resolved_at.is_some());
        assert_eq!(
            resolved.resolution_summary.as_deref(),
            Some("human:alice:release/2.0")
        );

        // Idempotent: a second resolve keeps the first answer.
        let again = resolve_attention(
            &governance,
            crate::ultimate_coders::ResolveAttentionRequest {
                attention_id: item.id,
                resolution_kind: "automatic".to_string(),
                by: None,
                detail: Some("late".to_string()),
                principal_kind: "user".to_string(),
                principal_id: "alice".to_string(),
            },
        )
        .await
        .unwrap();
        assert_eq!(again.resolution_summary, resolved.resolution_summary);
    }

    #[tokio::test]
    async fn raising_supersedes_the_previous_open_item() {
        let governance = plane().await;
        let case_id = only_case_id(&governance).await;
        let first = raise_attention(&governance, raise(&case_id, "blocked"))
            .await
            .unwrap();
        let second = raise_attention(&governance, raise(&case_id, "needs_human"))
            .await
            .unwrap();
        let worklist = list_attention(
            &governance,
            crate::ultimate_coders::ListAttentionRequest {
                include_resolved: false,
                case_id: None,
                kind: None,
                limit: 10,
            },
        )
        .await
        .unwrap();
        assert_eq!(worklist.items.len(), 1);
        assert_eq!(worklist.items[0].id, second.id);
        let superseded = governance.attention.get(&first.id).await.unwrap().unwrap();
        assert!(!superseded.is_open());
    }

    #[tokio::test]
    async fn capacity_counts_quiet_waiting_without_reserving_slots() {
        let governance = plane().await;
        let case_id = only_case_id(&governance).await;
        raise_attention(&governance, raise(&case_id, "waiting_event"))
            .await
            .unwrap();
        let capacity = get_capacity(&governance).await.unwrap();
        assert_eq!(capacity.waiting_event_cases, 1);
        assert_eq!(capacity.needs_human_items, 0);
        assert_eq!(capacity.worker_slots_reserved_human, 0);
        assert_eq!(capacity.slots_saved_by_quiet_waiting, 1);
        assert_eq!(capacity.worker_slots_available, 0);
    }

    #[tokio::test]
    async fn needs_human_reserves_a_priority_slot() {
        let governance = plane().await;
        let case_id = only_case_id(&governance).await;
        raise_attention(&governance, raise(&case_id, "needs_human"))
            .await
            .unwrap();
        let capacity = governance.attention.capacity(4, 1, 1).await.unwrap();
        let view = CapacityView::from_snapshot(&capacity);
        assert_eq!(view.needs_human_items, 1);
        assert_eq!(view.worker_slots_reserved_human, 1);
        assert_eq!(view.worker_slots_available, 2);
    }

    #[tokio::test]
    async fn an_unknown_attention_kind_is_rejected() {
        let governance = plane().await;
        let case_id = only_case_id(&governance).await;
        let error = raise_attention(&governance, raise(&case_id, "not_yet_invented"))
            .await
            .unwrap_err();
        assert_eq!(error.code, "unknown_attention_kind");
    }

    #[tokio::test]
    async fn raising_for_an_unknown_case_is_not_found() {
        let governance = plane().await;
        let error = raise_attention(&governance, raise("case-missing", "blocked"))
            .await
            .unwrap_err();
        assert_eq!(error.status, tonic::Code::NotFound);
    }

    #[tokio::test]
    async fn resolving_requires_the_attention_resolve_permission() {
        let governance = plane().await;
        let case_id = only_case_id(&governance).await;
        let item = raise_attention(&governance, raise(&case_id, "needs_human"))
            .await
            .unwrap();
        let error = resolve_attention(
            &governance,
            crate::ultimate_coders::ResolveAttentionRequest {
                attention_id: item.id,
                resolution_kind: "automatic".to_string(),
                by: None,
                detail: None,
                principal_kind: "user".to_string(),
                principal_id: "mallory".to_string(),
            },
        )
        .await
        .unwrap_err();
        assert_eq!(error.code, "missing_permission");
    }
}
