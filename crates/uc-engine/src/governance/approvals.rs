//! Durable home for tool approvals.
//!
//! One namespace (`approvals`) of [`ToolApproval`] documents keyed by the request's
//! deterministic id. Every state change is a compare-and-swap, so two humans
//! clicking "approve" at the same time cannot both win, and a single permit cannot
//! be consumed twice.
//!
//! ## Why the store is separate from the attention board
//!
//! An approval *is* a decision item, and the board is what the human sees. But the
//! board is a projection: it can be cleared, re-raised, or lost on a restart without
//! changing what is allowed. The permit itself therefore lives here, and the board
//! only carries a pointer (`attention item → approval id`). Losing the worklist item
//! must not silently grant or revoke anything.

use std::sync::Arc;

use chrono::{DateTime, Duration, Utc};

use uc_types::governance::{
    ApprovalError, ApprovalState, Principal, ToolApproval, APPROVAL_TTL_MINUTES,
};

use super::record_store::{NamespacedStore, RecordError, RecordStore};

/// Namespace holding every approval document.
pub const APPROVAL_NAMESPACE: &str = "approvals";

/// Bounded scan for "is there a usable permit for this tool/case".
///
/// Approvals are human-paced, so a few hundred live documents is already a lot;
/// the bound exists so a runaway requester cannot make authorization O(all history).
const APPROVAL_SCAN_LIMIT: usize = 512;

#[derive(Debug)]
pub enum ApprovalStoreError {
    Store(RecordError),
    /// The decision was refused by the approval's own invariants.
    Refused(ApprovalError),
    NotFound {
        id: String,
    },
}

impl std::fmt::Display for ApprovalStoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ApprovalStoreError::Store(error) => write!(f, "approval store: {error}"),
            ApprovalStoreError::Refused(error) => write!(f, "{error}"),
            ApprovalStoreError::NotFound { id } => write!(f, "approval {id} does not exist"),
        }
    }
}

impl From<RecordError> for ApprovalStoreError {
    fn from(error: RecordError) -> Self {
        ApprovalStoreError::Store(error)
    }
}

/// Approval documents over a [`RecordStore`].
#[derive(Clone)]
pub struct ApprovalStore {
    view: NamespacedStore,
}

impl std::fmt::Debug for ApprovalStore {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ApprovalStore")
            .field("durable", &self.view.is_durable())
            .finish_non_exhaustive()
    }
}

impl ApprovalStore {
    pub fn new(records: Arc<dyn RecordStore>) -> Self {
        Self {
            view: NamespacedStore::new(records, APPROVAL_NAMESPACE),
        }
    }

    pub fn is_durable(&self) -> bool {
        self.view.is_durable()
    }

    /// Record a request.
    ///
    /// Returns `(approval, created)`. A repeated request for the same action is not
    /// an error: it converges on the existing document, which is what keeps an
    /// agent's retry loop from filling the worklist with duplicates.
    pub async fn request(
        &self,
        approval: ToolApproval,
    ) -> Result<(ToolApproval, bool), ApprovalStoreError> {
        match self.view.put(&approval.id, &approval, Some(0)).await {
            Ok(_) => Ok((approval, true)),
            Err(RecordError::Conflict { .. }) => {
                let existing =
                    self.get(&approval.id)
                        .await?
                        .ok_or_else(|| ApprovalStoreError::NotFound {
                            id: approval.id.clone(),
                        })?;
                Ok((existing.0, false))
            }
            Err(error) => Err(error.into()),
        }
    }

    pub async fn get(&self, id: &str) -> Result<Option<(ToolApproval, u64)>, ApprovalStoreError> {
        Ok(self.view.get::<ToolApproval>(id).await?)
    }

    /// Every approval in `state` (all states when `None`), newest first.
    pub async fn list(
        &self,
        state: Option<ApprovalState>,
        limit: usize,
    ) -> Result<Vec<ToolApproval>, ApprovalStoreError> {
        let mut approvals: Vec<ToolApproval> = self
            .view
            .list::<ToolApproval>(APPROVAL_SCAN_LIMIT)
            .await?
            .into_iter()
            .map(|(approval, _version)| approval)
            .filter(|approval| state.is_none_or(|state| approval.state == state))
            .collect();
        approvals.sort_by_key(|approval| std::cmp::Reverse(approval.requested_at));
        approvals.truncate(limit);
        Ok(approvals)
    }

    /// A human allows the request.
    pub async fn approve(
        &self,
        id: &str,
        by: &Principal,
        note: Option<String>,
        now: DateTime<Utc>,
    ) -> Result<ToolApproval, ApprovalStoreError> {
        self.decide(id, by, note, now, true).await
    }

    /// A human refuses the request.
    pub async fn reject(
        &self,
        id: &str,
        by: &Principal,
        note: Option<String>,
        now: DateTime<Utc>,
    ) -> Result<ToolApproval, ApprovalStoreError> {
        self.decide(id, by, note, now, false).await
    }

    async fn decide(
        &self,
        id: &str,
        by: &Principal,
        note: Option<String>,
        now: DateTime<Utc>,
        allow: bool,
    ) -> Result<ToolApproval, ApprovalStoreError> {
        // Bounded CAS retry: a decision races only with another decision or a
        // consume, both of which end the document's pending state.
        for _ in 0..3 {
            let Some((approval, version)) = self.get(id).await? else {
                return Err(ApprovalStoreError::NotFound { id: id.to_string() });
            };
            let next = if allow {
                approval.approve(by, note.clone(), now)
            } else {
                approval.reject(by, note.clone(), now)
            }
            .map_err(ApprovalStoreError::Refused)?;
            match self.view.put(id, &next, Some(version)).await {
                Ok(_) => {
                    tracing::info!(
                        approval = %id,
                        state = next.state.as_str(),
                        decided_by = %by.id,
                        "tool approval decided"
                    );
                    return Ok(next);
                }
                Err(RecordError::Conflict { .. }) => continue,
                Err(error) => return Err(error.into()),
            }
        }
        Err(ApprovalStoreError::Refused(ApprovalError::NotPending {
            state: ApprovalState::Pending,
        }))
    }

    /// Find a usable permit without spending it.
    ///
    /// The policy engine looks first and consumes last, so a permit is never burned
    /// by a call that ends up denied for an unrelated reason (missing mandate,
    /// project scope, …). Oldest decision wins.
    pub async fn find_approved(
        &self,
        tool: &str,
        case_id: Option<&str>,
        now: DateTime<Utc>,
    ) -> Result<Option<ToolApproval>, ApprovalStoreError> {
        let mut candidates: Vec<ToolApproval> = self
            .view
            .list::<ToolApproval>(APPROVAL_SCAN_LIMIT)
            .await?
            .into_iter()
            .map(|(approval, _version)| approval)
            .filter(|approval| approval.is_usable(now) && approval.covers(tool, case_id))
            .collect();
        candidates.sort_by_key(|approval| approval.decided_at.unwrap_or(approval.requested_at));
        Ok(candidates.into_iter().next())
    }

    /// Spend one specific permit. Fails if it is no longer usable (expired, already
    /// spent by a concurrent call, or rejected in the meantime).
    pub async fn consume(
        &self,
        id: &str,
        now: DateTime<Utc>,
    ) -> Result<ToolApproval, ApprovalStoreError> {
        for _ in 0..3 {
            let Some((approval, version)) = self.get(id).await? else {
                return Err(ApprovalStoreError::NotFound { id: id.to_string() });
            };
            let used = approval.consume(now).map_err(ApprovalStoreError::Refused)?;
            match self.view.put(id, &used, Some(version)).await {
                Ok(_) => {
                    tracing::info!(approval = %id, "human approval consumed");
                    return Ok(used);
                }
                Err(RecordError::Conflict { .. }) => continue,
                Err(error) => return Err(error.into()),
            }
        }
        Err(ApprovalStoreError::Refused(ApprovalError::NotUsable {
            state: ApprovalState::Approved,
        }))
    }

    /// Consume one usable permit for `(tool, case_id)`, if any.
    ///
    /// This is the call the policy engine makes for a human-tier tool. It is
    /// deliberately a write: consume-and-return makes the permit single-use, and the
    /// CAS means two concurrent authorized calls cannot both spend the same consent.
    /// The oldest approved permit wins, so a human's earlier decision is used first.
    pub async fn consume_approved(
        &self,
        tool: &str,
        case_id: Option<&str>,
        now: DateTime<Utc>,
    ) -> Result<Option<ToolApproval>, ApprovalStoreError> {
        let mut candidates: Vec<(ToolApproval, u64)> = self
            .view
            .list::<ToolApproval>(APPROVAL_SCAN_LIMIT)
            .await?
            .into_iter()
            .filter(|(approval, _version)| {
                approval.is_usable(now) && approval.covers(tool, case_id)
            })
            .collect();
        candidates.sort_by_key(|(approval, _version)| {
            approval.decided_at.unwrap_or(approval.requested_at)
        });
        for (approval, version) in candidates {
            let used = match approval.consume(now) {
                Ok(used) => used,
                Err(_) => continue,
            };
            match self.view.put(&used.id, &used, Some(version)).await {
                Ok(_) => {
                    tracing::info!(
                        approval = %used.id,
                        tool = %tool,
                        case_id = case_id.unwrap_or(""),
                        "human approval consumed"
                    );
                    return Ok(Some(used));
                }
                // Another caller spent it first: try the next candidate.
                Err(RecordError::Conflict { .. }) => continue,
                Err(error) => return Err(error.into()),
            }
        }
        Ok(None)
    }

    /// Move undecided requests whose lifetime ran out to `Expired`.
    ///
    /// A sweeper (boot, or a periodic tick) calls this so the worklist does not
    /// accumulate questions nobody can answer any more.
    pub async fn expire_stale(
        &self,
        now: DateTime<Utc>,
        limit: usize,
    ) -> Result<usize, ApprovalStoreError> {
        let pending: Vec<(ToolApproval, u64)> = self
            .view
            .list::<ToolApproval>(APPROVAL_SCAN_LIMIT)
            .await?
            .into_iter()
            .filter(|(approval, _version)| approval.is_pending() && now >= approval.expires_at)
            .take(limit)
            .collect();
        let mut expired = 0;
        for (approval, version) in pending {
            let next = match approval.expire(now) {
                Ok(next) => next,
                Err(_) => continue,
            };
            match self.view.put(&next.id, &next, Some(version)).await {
                Ok(_) => expired += 1,
                Err(RecordError::Conflict { .. }) => {}
                Err(error) => return Err(error.into()),
            }
        }
        Ok(expired)
    }

    /// Default lifetime, exposed so gateways and tests agree on it.
    pub fn default_ttl() -> Duration {
        Duration::minutes(APPROVAL_TTL_MINUTES)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::governance::record_store::InMemoryRecordStore;
    use uc_types::governance::ApprovalRequest;

    fn now() -> DateTime<Utc> {
        DateTime::parse_from_rfc3339("2026-01-01T00:00:00Z")
            .unwrap()
            .with_timezone(&Utc)
    }

    fn store() -> ApprovalStore {
        let records: Arc<dyn RecordStore> = Arc::new(InMemoryRecordStore::new());
        ApprovalStore::new(records)
    }

    fn request(tool: &str, case: Option<&str>) -> ToolApproval {
        ToolApproval::request(
            ApprovalRequest {
                tool: tool.to_string(),
                case_id: case.map(str::to_string),
                project_id: Some("proj".to_string()),
                action_digest: Some("args".to_string()),
                requested_by: Principal::agent("executor"),
                reason: None,
            },
            now(),
            None,
        )
    }

    #[tokio::test]
    async fn a_repeated_request_converges_instead_of_duplicating() {
        let store = store();
        let approval = request("scale_workers", Some("case-1"));
        let (first, created) = store.request(approval.clone()).await.unwrap();
        assert!(created);
        let (second, created) = store.request(approval).await.unwrap();
        assert!(!created, "a retry must not create a second approval");
        assert_eq!(first.id, second.id);
        assert_eq!(store.list(None, 10).await.unwrap().len(), 1);
    }

    #[tokio::test]
    async fn approve_then_consume_spends_the_permit_exactly_once() {
        let store = store();
        let (approval, _) = store
            .request(request("scale_workers", Some("case-1")))
            .await
            .unwrap();
        assert!(
            store
                .consume_approved("scale_workers", Some("case-1"), now())
                .await
                .unwrap()
                .is_none(),
            "a pending request is not a permit"
        );

        store
            .approve(
                &approval.id,
                &Principal::user("alice"),
                Some("ok".into()),
                now(),
            )
            .await
            .unwrap();
        let spent = store
            .consume_approved("scale_workers", Some("case-1"), now())
            .await
            .unwrap()
            .expect("approved permits are usable");
        assert_eq!(spent.state, ApprovalState::Used);
        assert_eq!(
            store
                .consume_approved("scale_workers", Some("case-1"), now())
                .await
                .unwrap(),
            None,
            "single use"
        );
    }

    #[tokio::test]
    async fn peeking_does_not_spend_the_permit() {
        let store = store();
        let (approval, _) = store
            .request(request("scale_workers", Some("case-1")))
            .await
            .unwrap();
        store
            .approve(&approval.id, &Principal::user("alice"), None, now())
            .await
            .unwrap();
        let peeked = store
            .find_approved("scale_workers", Some("case-1"), now())
            .await
            .unwrap()
            .expect("approved permits are visible");
        assert_eq!(peeked.state, ApprovalState::Approved);
        // Still usable after a pure read.
        let spent = store
            .consume(&peeked.id, now())
            .await
            .expect("consuming the peeked permit");
        assert_eq!(spent.state, ApprovalState::Used);
        assert!(
            store.consume(&peeked.id, now()).await.is_err(),
            "second consume must fail"
        );
    }

    #[tokio::test]
    async fn a_permit_covers_only_its_own_tool_and_case() {
        let store = store();
        let (approval, _) = store
            .request(request("scale_workers", Some("case-1")))
            .await
            .unwrap();
        store
            .approve(&approval.id, &Principal::user("alice"), None, now())
            .await
            .unwrap();
        assert!(store
            .consume_approved("docker_run", Some("case-1"), now())
            .await
            .unwrap()
            .is_none());
        assert!(store
            .consume_approved("scale_workers", Some("case-2"), now())
            .await
            .unwrap()
            .is_none());
        assert!(store
            .consume_approved("scale_workers", None, now())
            .await
            .unwrap()
            .is_none());
        assert!(store
            .consume_approved("scale_workers", Some("case-1"), now())
            .await
            .unwrap()
            .is_some());
    }

    #[tokio::test]
    async fn an_agent_cannot_decide_its_own_request() {
        let store = store();
        let (approval, _) = store.request(request("scale_workers", None)).await.unwrap();
        let error = store
            .approve(&approval.id, &Principal::agent("executor"), None, now())
            .await
            .expect_err("agents cannot approve");
        assert!(matches!(error, ApprovalStoreError::Refused(_)));
        let error = store
            .approve(&approval.id, &Principal::service("ci"), None, now())
            .await
            .expect_err("services cannot approve");
        assert!(matches!(error, ApprovalStoreError::Refused(_)));
    }

    #[tokio::test]
    async fn rejecting_ends_the_request_and_never_yields_a_permit() {
        let store = store();
        let (approval, _) = store
            .request(request("docker_run", Some("case-3")))
            .await
            .unwrap();
        let rejected = store
            .reject(
                &approval.id,
                &Principal::user("alice"),
                Some("no".into()),
                now(),
            )
            .await
            .unwrap();
        assert_eq!(rejected.state, ApprovalState::Rejected);
        assert!(store
            .consume_approved("docker_run", Some("case-3"), now())
            .await
            .unwrap()
            .is_none());
        // A rejected request cannot be approved afterwards.
        assert!(store
            .approve(&approval.id, &Principal::user("alice"), None, now())
            .await
            .is_err());
    }

    #[tokio::test]
    async fn the_sweeper_expires_what_nobody_answered() {
        let store = store();
        store.request(request("scale_workers", None)).await.unwrap();
        assert_eq!(store.expire_stale(now(), 32).await.unwrap(), 0, "not yet");
        let later = now() + Duration::minutes(APPROVAL_TTL_MINUTES + 1);
        assert_eq!(store.expire_stale(later, 32).await.unwrap(), 1);
        let listed = store.list(Some(ApprovalState::Expired), 10).await.unwrap();
        assert_eq!(listed.len(), 1);
        assert!(store
            .consume_approved("scale_workers", None, later)
            .await
            .unwrap()
            .is_none());
    }

    #[tokio::test]
    async fn an_expired_permit_is_not_usable() {
        let store = store();
        let (approval, _) = store.request(request("scale_workers", None)).await.unwrap();
        store
            .approve(&approval.id, &Principal::user("alice"), None, now())
            .await
            .unwrap();
        let later = now() + Duration::minutes(APPROVAL_TTL_MINUTES + 1);
        assert!(store
            .consume_approved("scale_workers", None, later)
            .await
            .unwrap()
            .is_none());
    }
}
