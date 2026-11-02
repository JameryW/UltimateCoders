//! The attention board: the typed "why is this case not progressing" projection.
//!
//! Two rules drive the design:
//!
//! * **At most one open item per case.** A case is blocked by one thing at a time;
//!   raising a second item for the same case replaces the open one (the old one is
//!   resolved as `automatic: superseded`). Without that rule the worklist fills
//!   with duplicates of the same blocker.
//! * **Items are projections, not locks.** Resolving is idempotent and an item can
//!   always be re-raised; nothing in the scheduler depends on an item's existence.

use std::sync::Arc;

use chrono::{DateTime, Utc};

use uc_types::governance::{
    AttentionItem, AttentionKind, AttentionResolution, CapacitySnapshot, ATTENTION_NAMESPACE,
};

use super::record_store::{NamespacedStore, RecordError, RecordStore};

/// Failure modes of the attention board.
#[derive(Debug, Clone, PartialEq)]
pub enum AttentionBoardError {
    Store(RecordError),
    NotFound { attention_id: String },
    Contended,
}

impl From<RecordError> for AttentionBoardError {
    fn from(error: RecordError) -> Self {
        AttentionBoardError::Store(error)
    }
}

impl std::fmt::Display for AttentionBoardError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            AttentionBoardError::Store(error) => write!(f, "attention board: {error}"),
            AttentionBoardError::NotFound { attention_id } => {
                write!(f, "attention item {attention_id} not found")
            }
            AttentionBoardError::Contended => f.write_str("attention board contention"),
        }
    }
}

impl std::error::Error for AttentionBoardError {}

/// Durable attention items.
#[derive(Clone, Debug)]
pub struct AttentionBoard {
    view: NamespacedStore,
}

impl AttentionBoard {
    pub fn new(records: Arc<dyn RecordStore>) -> Self {
        Self {
            view: NamespacedStore::new(records, ATTENTION_NAMESPACE),
        }
    }

    pub fn is_durable(&self) -> bool {
        self.view.is_durable()
    }

    /// Raise an item, superseding any other open item for the same case.
    ///
    /// Returns `(item, superseded)` where `superseded` is the id of the item that
    /// was closed, if any.
    pub async fn raise(
        &self,
        item: AttentionItem,
        now: DateTime<Utc>,
    ) -> Result<(AttentionItem, Option<String>), AttentionBoardError> {
        let superseded = self
            .open_for_case(&item.case_id)
            .await?
            .filter(|open| open.id != item.id)
            .map(|open| open.id);
        if let Some(previous_id) = &superseded {
            let _ = self
                .resolve(
                    previous_id,
                    AttentionResolution::Automatic {
                        detail: format!("superseded by {}", item.id),
                    },
                    now,
                )
                .await;
        }
        let stored = match self.view.put(&item.id, &item, Some(0)).await {
            Ok(_) => item,
            Err(RecordError::Conflict { .. }) => {
                // Same id raised twice: update in place rather than duplicate.
                let mut existing = item;
                let Some((current, version)) = self.view.get::<AttentionItem>(&existing.id).await?
                else {
                    return Err(AttentionBoardError::Store(RecordError::Conflict {
                        expected: 0,
                        actual: None,
                    }));
                };
                existing.since = current.since;
                self.view.put(&existing.id, &existing, Some(version)).await?;
                existing
            }
            Err(other) => return Err(AttentionBoardError::Store(other)),
        };
        Ok((stored, superseded))
    }

    /// Resolve an item. Idempotent: a second resolution keeps the first one.
    pub async fn resolve(
        &self,
        attention_id: &str,
        resolution: AttentionResolution,
        now: DateTime<Utc>,
    ) -> Result<AttentionItem, AttentionBoardError> {
        for _ in 0..8 {
            let Some((mut item, version)) = self.view.get::<AttentionItem>(attention_id).await?
            else {
                return Err(AttentionBoardError::NotFound {
                    attention_id: attention_id.to_string(),
                });
            };
            if !item.is_open() {
                return Ok(item);
            }
            item.resolve(resolution.clone(), now);
            match self.view.put(attention_id, &item, Some(version)).await {
                Ok(_) => return Ok(item),
                Err(RecordError::Conflict { .. }) => continue,
                Err(other) => return Err(AttentionBoardError::Store(other)),
            }
        }
        Err(AttentionBoardError::Contended)
    }

    pub async fn get(
        &self,
        attention_id: &str,
    ) -> Result<Option<AttentionItem>, AttentionBoardError> {
        Ok(self
            .view
            .get::<AttentionItem>(attention_id)
            .await?
            .map(|(item, _version)| item))
    }

    /// The open item for a case, if any.
    pub async fn open_for_case(
        &self,
        case_id: &str,
    ) -> Result<Option<AttentionItem>, AttentionBoardError> {
        let items = self.view.list::<AttentionItem>(2000).await?;
        Ok(items
            .into_iter()
            .map(|(item, _version)| item)
            .filter(|item| item.case_id == case_id && item.is_open())
            .max_by_key(|item| item.since))
    }

    /// The operator worklist: open items, human decisions first, then priority, then
    /// age. Resolved items are excluded unless asked for.
    pub async fn worklist(
        &self,
        include_resolved: bool,
        limit: usize,
    ) -> Result<Vec<AttentionItem>, AttentionBoardError> {
        let items = self.view.list::<AttentionItem>(limit.saturating_mul(4).max(50)).await?;
        let mut items: Vec<AttentionItem> = items
            .into_iter()
            .map(|(item, _version)| item)
            .filter(|item| include_resolved || item.is_open())
            .collect();
        items.sort_by_key(|item| item.worklist_order_key());
        items.truncate(limit);
        Ok(items)
    }

    /// Count open items by kind — the raw material of [`Self::capacity`].
    pub async fn open_counts(&self) -> Result<Vec<(AttentionKind, u32)>, AttentionBoardError> {
        let items = self.view.list::<AttentionItem>(2000).await?;
        let mut counts: Vec<(AttentionKind, u32)> = Vec::new();
        for (item, _version) in items {
            if !item.is_open() {
                continue;
            }
            match counts.iter_mut().find(|(kind, _)| *kind == item.kind) {
                Some((_, count)) => *count += 1,
                None => counts.push((item.kind, 1)),
            }
        }
        Ok(counts)
    }

    /// Capacity accounting for the scheduler and the dashboard.
    ///
    /// `reserved_human` is passed in (defaulting to one slot per open
    /// `needs_human` item, capped at the configured reserve) because the policy
    /// decision about how much headroom to keep is an operator setting, not a
    /// property of the items.
    pub async fn capacity(
        &self,
        worker_slots_total: u32,
        worker_slots_busy: u32,
        reserved_human: u32,
    ) -> Result<CapacitySnapshot, AttentionBoardError> {
        let counts = self.open_counts().await?;
        let get = |kind: AttentionKind| -> u32 {
            counts
                .iter()
                .find(|(candidate, _)| *candidate == kind)
                .map(|(_, count)| *count)
                .unwrap_or_default()
        };
        Ok(CapacitySnapshot {
            worker_slots_total,
            worker_slots_busy,
            worker_slots_reserved_human: reserved_human.min(worker_slots_total),
            waiting_event_cases: get(AttentionKind::WaitingEvent),
            verifying_cases: get(AttentionKind::Verifying),
            needs_human_items: get(AttentionKind::NeedsHuman),
            blocked_cases: get(AttentionKind::Blocked),
        })
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

    fn board() -> AttentionBoard {
        let records: Arc<dyn RecordStore> = Arc::new(
            crate::governance::record_store::InMemoryRecordStore::new(),
        );
        AttentionBoard::new(records)
    }

    fn item(case: &str, kind: AttentionKind) -> AttentionItem {
        AttentionItem::new(
            format!("att-{case}-{kind}"),
            case,
            kind,
            format!("{kind} for {case}"),
            now(),
        )
    }

    #[tokio::test]
    async fn raising_a_second_item_supersedes_the_first() {
        let board = board();
        board
            .raise(item("case-1", AttentionKind::WaitingEvent), now())
            .await
            .unwrap();
        let (_, superseded) = board
            .raise(item("case-1", AttentionKind::NeedsHuman), now())
            .await
            .unwrap();
        assert_eq!(superseded.as_deref(), Some("att-case-1-waiting_event"));
        let open = board.open_for_case("case-1").await.unwrap().unwrap();
        assert_eq!(open.kind, AttentionKind::NeedsHuman);
        assert_eq!(board.worklist(false, 10).await.unwrap().len(), 1);
    }

    #[tokio::test]
    async fn resolving_is_idempotent() {
        let board = board();
        board
            .raise(item("case-1", AttentionKind::NeedsHuman), now())
            .await
            .unwrap();
        let first = board
            .resolve(
                "att-case-1-needs_human",
                AttentionResolution::HumanDecision {
                    by: "alice".to_string(),
                    answer: "go".to_string(),
                },
                now(),
            )
            .await
            .unwrap();
        assert!(first.resolution.is_some());
        let second = board
            .resolve(
                "att-case-1-needs_human",
                AttentionResolution::Automatic {
                    detail: "late".to_string(),
                },
                now(),
            )
            .await
            .unwrap();
        assert_eq!(first.resolution, second.resolution);
    }

    #[tokio::test]
    async fn worklist_puts_human_first_then_priority() {
        let board = board();
        board
            .raise(item("case-blocked", AttentionKind::Blocked).with_priority(9), now())
            .await
            .unwrap();
        board
            .raise(item("case-human", AttentionKind::NeedsHuman), now())
            .await
            .unwrap();
        let worklist = board.worklist(false, 10).await.unwrap();
        assert_eq!(worklist[0].case_id, "case-human");
        assert_eq!(worklist[1].case_id, "case-blocked");
    }

    #[tokio::test]
    async fn capacity_reports_quiet_waiting_savings_and_human_reserve() {
        let board = board();
        board
            .raise(item("case-1", AttentionKind::WaitingEvent), now())
            .await
            .unwrap();
        board
            .raise(item("case-2", AttentionKind::Verifying), now())
            .await
            .unwrap();
        board
            .raise(item("case-3", AttentionKind::NeedsHuman), now())
            .await
            .unwrap();
        let capacity = board.capacity(4, 2, 1).await.unwrap();
        assert_eq!(capacity.waiting_event_cases, 1);
        assert_eq!(capacity.verifying_cases, 1);
        assert_eq!(capacity.needs_human_items, 1);
        assert_eq!(capacity.slots_saved_by_quiet_waiting(), 2);
        assert_eq!(capacity.worker_slots_available(), 1);
        assert!(!AttentionKind::WaitingEvent.holds_worker_slot());
    }

    #[tokio::test]
    async fn unknown_item_resolution_is_an_error_not_a_silent_noop() {
        let board = board();
        let error = board
            .resolve(
                "missing",
                AttentionResolution::Automatic {
                    detail: "x".to_string(),
                },
                now(),
            )
            .await
            .expect_err("unknown item");
        assert!(matches!(error, AttentionBoardError::NotFound { .. }));
    }
}

