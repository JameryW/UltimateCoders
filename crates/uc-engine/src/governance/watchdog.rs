//! Watchdog: turns deterministic stall evidence into a bounded recovery decision.
//!
//! The division of labour matters. `python/ultimate_coders/agent/progress.py` owns
//! *detection* — it normalizes error signatures and decides "the same failure
//! happened again, nothing changed". This module owns *response*: given that
//! signal, plus how many attempts and replans the case already burned, decide one
//! of continue / retry / replan / escalate / stop.
//!
//! The Rust side keeps this decision because it is the side that owns budgets and
//! state authority; the Python evaluator may *suggest* that a replan is worth it,
//! but cannot grant itself another attempt.
//!
//! Two guarantees:
//!
//! * replans are **capped** ([`WatchdogConfig::max_replans`]) and a stalled case
//!   with no replan budget escalates to a human instead of looping;
//! * a replan carries `preserve_effects`, so re-planning cannot re-execute an
//!   external side effect that the EffectLedger already confirmed.

use std::sync::Arc;

use chrono::{DateTime, Utc};

use uc_types::governance::{WatchdogConfig, WatchdogDecision, WatchdogSignal, WatchdogVerdict};

use super::record_store::{NamespacedStore, RecordError, RecordStore};

/// Record-store namespace for watchdog decisions (audit trail).
pub const WATCHDOG_NAMESPACE: &str = "watchdog_decision";

/// Failure modes of the watchdog ledger.
#[derive(Debug, Clone, PartialEq)]
pub enum WatchdogError {
    Store(RecordError),
}

impl From<RecordError> for WatchdogError {
    fn from(error: RecordError) -> Self {
        WatchdogError::Store(error)
    }
}

impl std::fmt::Display for WatchdogError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            WatchdogError::Store(error) => write!(f, "watchdog ledger: {error}"),
        }
    }
}

impl std::error::Error for WatchdogError {}

/// One recorded decision, for audit and for the dashboard.
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct WatchdogRecord {
    pub task_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub subtask_id: Option<String>,
    pub verdict: String,
    pub reason: String,
    pub no_progress_count: u32,
    pub replans_used: u32,
    pub attempts_used: u32,
    #[serde(default)]
    pub preserve_effects: Vec<String>,
    pub at: DateTime<Utc>,
}

/// Bounded recovery decisions with an audit trail.
#[derive(Clone, Debug)]
pub struct Watchdog {
    config: WatchdogConfig,
    view: NamespacedStore,
}

impl Watchdog {
    pub fn new(records: Arc<dyn RecordStore>) -> Self {
        Self {
            config: WatchdogConfig::default(),
            view: NamespacedStore::new(records, WATCHDOG_NAMESPACE),
        }
    }

    pub fn with_config(mut self, config: WatchdogConfig) -> Self {
        self.config = config;
        self
    }

    pub fn config(&self) -> &WatchdogConfig {
        &self.config
    }

    pub fn is_durable(&self) -> bool {
        self.view.is_durable()
    }

    /// Decide, then record the decision.
    ///
    /// `already_retried` prevents an infinite retry-then-replan-then-retry cycle:
    /// the first stall gets one same-contract retry, the second goes to a replan.
    pub async fn decide(
        &self,
        signal: &WatchdogSignal,
        already_retried: bool,
        now: DateTime<Utc>,
    ) -> Result<WatchdogDecision, WatchdogError> {
        let decision = self.config.decide_with_retry(signal, already_retried);
        let record = WatchdogRecord {
            task_id: signal.task_id.clone(),
            subtask_id: signal.subtask_id.clone(),
            verdict: verdict_token(&decision.verdict).to_string(),
            reason: verdict_reason(&decision.verdict),
            no_progress_count: signal.no_progress_count,
            replans_used: signal.replans_used,
            attempts_used: signal.attempts_used,
            preserve_effects: preserve_effects(&decision.verdict),
            at: now,
        };
        let key = format!("{}:{}", signal.task_id, now.timestamp_millis());
        let _ = self.view.put(&key, &record, None).await;
        Ok(decision)
    }

    /// Recent decisions for one task (newest first).
    pub async fn history(
        &self,
        task_id: &str,
        limit: usize,
    ) -> Result<Vec<WatchdogRecord>, WatchdogError> {
        let records = self.view.list::<WatchdogRecord>(1000).await?;
        let mut records: Vec<WatchdogRecord> = records
            .into_iter()
            .map(|(record, _version)| record)
            .filter(|record| record.task_id == task_id)
            .collect();
        records.sort_by(|a, b| b.at.cmp(&a.at));
        records.truncate(limit);
        Ok(records)
    }

    pub async fn list(&self, limit: usize) -> Result<Vec<WatchdogRecord>, WatchdogError> {
        Ok(self
            .view
            .list::<WatchdogRecord>(limit)
            .await?
            .into_iter()
            .map(|(record, _version)| record)
            .collect())
    }
}

fn verdict_token(verdict: &WatchdogVerdict) -> &'static str {
    match verdict {
        WatchdogVerdict::Continue => "continue",
        WatchdogVerdict::RetrySameContract { .. } => "retry_same_contract",
        WatchdogVerdict::Replan { .. } => "replan",
        WatchdogVerdict::EscalateNeedsHuman { .. } => "escalate_needs_human",
        WatchdogVerdict::Stop { .. } => "stop",
    }
}

fn verdict_reason(verdict: &WatchdogVerdict) -> String {
    match verdict {
        WatchdogVerdict::Continue => "progress within budget".to_string(),
        WatchdogVerdict::RetrySameContract { delay_ms } => {
            format!("stalled: retry the same contract after {delay_ms}ms")
        }
        WatchdogVerdict::Replan {
            attempt,
            preserve_effects,
            ..
        } => format!(
            "stalled: bounded replan attempt {attempt}, preserving {} confirmed effect(s)",
            preserve_effects.len()
        ),
        WatchdogVerdict::EscalateNeedsHuman { reason } => reason.clone(),
        WatchdogVerdict::Stop { reason } => reason.clone(),
    }
}

fn preserve_effects(verdict: &WatchdogVerdict) -> Vec<String> {
    match verdict {
        WatchdogVerdict::Replan {
            preserve_effects, ..
        } => preserve_effects.clone(),
        _ => Vec::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::governance::record_store::InMemoryRecordStore;

    fn now() -> DateTime<Utc> {
        DateTime::parse_from_rfc3339("2026-01-01T00:00:00Z")
            .unwrap()
            .with_timezone(&Utc)
    }

    fn watchdog(config: WatchdogConfig) -> Watchdog {
        let records: Arc<dyn RecordStore> = Arc::new(InMemoryRecordStore::new());
        Watchdog::new(records).with_config(config)
    }

    #[tokio::test]
    async fn below_threshold_continues() {
        let watchdog = watchdog(WatchdogConfig::default());
        let mut signal = WatchdogSignal::new("task-1");
        signal.no_progress_count = 1;
        let decision = watchdog.decide(&signal, false, now()).await.unwrap();
        assert_eq!(decision.verdict, WatchdogVerdict::Continue);
    }

    #[tokio::test]
    async fn first_stall_retries_then_replans_then_escalates() {
        let watchdog = watchdog(WatchdogConfig {
            stall_limit: 2,
            max_replans: 1,
            max_attempts: 10,
            ..Default::default()
        });
        let mut signal = WatchdogSignal::new("task-1");
        signal.no_progress_count = 2;
        let first = watchdog.decide(&signal, false, now()).await.unwrap();
        assert!(matches!(
            first.verdict,
            WatchdogVerdict::RetrySameContract { .. }
        ));
        let second = watchdog.decide(&signal, true, now()).await.unwrap();
        assert!(matches!(second.verdict, WatchdogVerdict::Replan { attempt: 1, .. }));
        signal.replans_used = 1;
        let third = watchdog.decide(&signal, true, now()).await.unwrap();
        assert!(matches!(
            third.verdict,
            WatchdogVerdict::EscalateNeedsHuman { .. }
        ));
    }

    #[tokio::test]
    async fn replan_preserves_confirmed_effects_for_audit() {
        let watchdog = watchdog(WatchdogConfig::default());
        let mut signal = WatchdogSignal::new("task-1");
        signal.no_progress_count = 3;
        signal.confirmed_effect_ids = vec!["effect:merge-main".to_string()];
        let decision = watchdog.decide(&signal, true, now()).await.unwrap();
        assert!(matches!(
            decision.verdict,
            WatchdogVerdict::Replan { .. }
        ));
        let history = watchdog.history("task-1", 10).await.unwrap();
        assert_eq!(history.len(), 1);
        assert_eq!(history[0].preserve_effects, vec!["effect:merge-main".to_string()]);
        assert_eq!(history[0].verdict, "replan");
    }

    #[tokio::test]
    async fn exhausted_attempts_stop_instead_of_looping() {
        let watchdog = watchdog(WatchdogConfig {
            max_attempts: 2,
            ..Default::default()
        });
        let mut signal = WatchdogSignal::new("task-1");
        signal.attempts_used = 2;
        signal.no_progress_count = 9;
        let decision = watchdog.decide(&signal, false, now()).await.unwrap();
        assert!(matches!(decision.verdict, WatchdogVerdict::Stop { .. }));
        assert_eq!(decision.attempts_remaining, 0);
    }
}
