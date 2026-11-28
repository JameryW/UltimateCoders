//! Relative-safety prompt-injection fence with capability monitoring (ledger item #105).
//!
//! PlanTERS proves that permanently revoking injectable and side-effect tools
//! after the first read of untrusted content keeps an agent relatively safe.
//! This module models that fence plus the deterministic human-review budget used
//! for residual taint decisions. It is a policy object, not a sandbox runtime.

use serde::{Deserialize, Serialize};
use thiserror::Error;

pub const DEFAULT_APPROVAL_BUDGET: u32 = 3;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ToolClass {
    ReadOnly,
    Injectable,
    SideEffect,
}

impl ToolClass {
    pub fn is_privileged(self) -> bool {
        matches!(self, Self::Injectable | Self::SideEffect)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FenceDecision {
    pub allowed: bool,
    pub requires_review: bool,
    pub reason: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PendingReview {
    pub review_id: String,
    pub tool: String,
    pub reason: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FenceSnapshot {
    pub tainted: bool,
    pub reviews_used: u32,
    pub reviews_remaining: u32,
    pub approved_tool: Option<String>,
    pub pending_review: Option<String>,
}

#[derive(Debug, Clone, Error, PartialEq, Eq)]
pub enum PlanterError {
    #[error("tool must not be empty")]
    EmptyTool,
    #[error("review id must not be empty")]
    EmptyReviewId,
    #[error("unknown review")]
    UnknownReview,
    #[error("approval budget exhausted")]
    BudgetExhausted,
}

#[derive(Debug, Clone)]
pub struct PlanterFence {
    tainted: bool,
    approval_budget: u32,
    reviews_used: u32,
    approved_tool: Option<String>,
    pending: Option<PendingReview>,
    next_review: u64,
}

impl PlanterFence {
    pub fn new(approval_budget: u32) -> Self {
        Self {
            tainted: false,
            approval_budget,
            reviews_used: 0,
            approved_tool: None,
            pending: None,
            next_review: 1,
        }
    }

    pub fn default_fence() -> Self {
        Self::new(DEFAULT_APPROVAL_BUDGET)
    }

    pub fn read_untrusted(&mut self) -> bool {
        let was_clean = !self.tainted;
        self.tainted = true;
        self.approved_tool = None;
        was_clean
    }

    pub fn is_tainted(&self) -> bool {
        self.tainted
    }

    pub fn authorize(
        &self,
        tool: impl Into<String>,
        class: ToolClass,
    ) -> Result<FenceDecision, PlanterError> {
        let tool = tool.into();
        if tool.trim().is_empty() {
            return Err(PlanterError::EmptyTool);
        }
        if !self.tainted {
            return Ok(FenceDecision {
                allowed: true,
                requires_review: false,
                reason: "session is clean".to_string(),
            });
        }
        if !class.is_privileged() {
            return Ok(FenceDecision {
                allowed: true,
                requires_review: false,
                reason: "read-only tool remains allowed".to_string(),
            });
        }
        if self.approved_tool.as_deref() == Some(tool.as_str()) {
            return Ok(FenceDecision {
                allowed: true,
                requires_review: false,
                reason: "human review approved this tool".to_string(),
            });
        }
        Ok(FenceDecision {
            allowed: false,
            requires_review: self.reviews_used < self.approval_budget,
            reason: "privileged tool revoked after untrusted read".to_string(),
        })
    }

    pub fn request_human_review(
        &mut self,
        tool: impl Into<String>,
        reason: impl Into<String>,
    ) -> Result<PendingReview, PlanterError> {
        let tool = tool.into();
        if tool.trim().is_empty() {
            return Err(PlanterError::EmptyTool);
        }
        if self.reviews_used >= self.approval_budget {
            return Err(PlanterError::BudgetExhausted);
        }
        if let Some(pending) = &self.pending {
            return Ok(pending.clone());
        }
        let pending = PendingReview {
            review_id: format!("review-{}", self.next_review),
            tool,
            reason: reason.into(),
        };
        self.next_review += 1;
        self.pending = Some(pending.clone());
        Ok(pending)
    }

    pub fn approve_review(&mut self, review_id: &str) -> Result<(), PlanterError> {
        if review_id.trim().is_empty() {
            return Err(PlanterError::EmptyReviewId);
        }
        let Some(pending) = self.pending.clone() else {
            return Err(PlanterError::UnknownReview);
        };
        if pending.review_id != review_id {
            return Err(PlanterError::UnknownReview);
        }
        if self.reviews_used >= self.approval_budget {
            return Err(PlanterError::BudgetExhausted);
        }
        self.reviews_used += 1;
        self.approved_tool = Some(pending.tool);
        self.pending = None;
        Ok(())
    }

    pub fn deny_review(&mut self, review_id: &str) -> Result<(), PlanterError> {
        if review_id.trim().is_empty() {
            return Err(PlanterError::EmptyReviewId);
        }
        match &self.pending {
            Some(pending) if pending.review_id == review_id => {
                self.pending = None;
                Ok(())
            }
            _ => Err(PlanterError::UnknownReview),
        }
    }

    pub fn snapshot(&self) -> FenceSnapshot {
        FenceSnapshot {
            tainted: self.tainted,
            reviews_used: self.reviews_used,
            reviews_remaining: self.approval_budget.saturating_sub(self.reviews_used),
            approved_tool: self.approved_tool.clone(),
            pending_review: self.pending.as_ref().map(|review| review.review_id.clone()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clean_session_allows_all_tool_classes() {
        let fence = PlanterFence::default_fence();
        for class in [
            ToolClass::ReadOnly,
            ToolClass::Injectable,
            ToolClass::SideEffect,
        ] {
            assert!(fence.authorize("tool", class).unwrap().allowed);
        }
    }

    #[test]
    fn untrusted_read_revokes_privileged_tools() {
        let mut fence = PlanterFence::default_fence();
        assert!(fence.read_untrusted());
        assert!(
            fence
                .authorize("read", ToolClass::ReadOnly)
                .unwrap()
                .allowed
        );
        assert!(
            !fence
                .authorize("write", ToolClass::SideEffect)
                .unwrap()
                .allowed
        );
        assert!(
            !fence
                .authorize("browser", ToolClass::Injectable)
                .unwrap()
                .allowed
        );
    }

    #[test]
    fn second_untrusted_read_is_not_new_taint() {
        let mut fence = PlanterFence::default_fence();
        assert!(fence.read_untrusted());
        assert!(!fence.read_untrusted());
    }

    #[test]
    fn review_can_be_approved_once() {
        let mut fence = PlanterFence::default_fence();
        fence.read_untrusted();
        let review = fence
            .request_human_review("write", "needed for patch")
            .unwrap();
        fence.approve_review(&review.review_id).unwrap();
        assert!(
            fence
                .authorize("write", ToolClass::SideEffect)
                .unwrap()
                .allowed
        );
        assert_eq!(fence.snapshot().reviews_used, 1);
    }

    #[test]
    fn approval_is_tool_specific() {
        let mut fence = PlanterFence::default_fence();
        fence.read_untrusted();
        let review = fence.request_human_review("write", "needed").unwrap();
        fence.approve_review(&review.review_id).unwrap();
        assert!(
            !fence
                .authorize("rm", ToolClass::SideEffect)
                .unwrap()
                .allowed
        );
    }

    #[test]
    fn approval_budget_is_enforced() {
        let mut fence = PlanterFence::new(1);
        fence.read_untrusted();
        let review = fence.request_human_review("write", "needed").unwrap();
        fence.approve_review(&review.review_id).unwrap();
        fence.read_untrusted();
        assert_eq!(
            fence.request_human_review("write", "again"),
            Err(PlanterError::BudgetExhausted)
        );
    }

    #[test]
    fn pending_review_can_be_denied() {
        let mut fence = PlanterFence::default_fence();
        fence.read_untrusted();
        let review = fence.request_human_review("write", "needed").unwrap();
        fence.deny_review(&review.review_id).unwrap();
        assert!(fence.snapshot().pending_review.is_none());
        assert!(
            !fence
                .authorize("write", ToolClass::SideEffect)
                .unwrap()
                .allowed
        );
    }

    #[test]
    fn unknown_review_is_rejected() {
        let mut fence = PlanterFence::default_fence();
        assert_eq!(
            fence.approve_review("missing"),
            Err(PlanterError::UnknownReview)
        );
    }

    #[test]
    fn empty_tool_and_review_are_rejected() {
        let mut fence = PlanterFence::default_fence();
        assert_eq!(
            fence.authorize("", ToolClass::ReadOnly),
            Err(PlanterError::EmptyTool)
        );
        assert_eq!(fence.deny_review(""), Err(PlanterError::EmptyReviewId));
    }

    #[test]
    fn snapshot_reports_remaining_budget() {
        let fence = PlanterFence::new(2);
        let snapshot = fence.snapshot();
        assert_eq!(snapshot.reviews_remaining, 2);
        assert!(!snapshot.tainted);
    }
}
