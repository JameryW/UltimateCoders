//! MemSidecar scheduling primitives (ledger item #81).
//!
//! The sidecar keeps memory retrieval off the agent's critical path: hooks
//! react to tool events, foresight prefetches predicted memories, and the
//! OPSD router chooses between a verified weight memory and a text fallback.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use thiserror::Error;

pub const DEFAULT_MAX_PREFETCH: usize = 8;
pub const DEFAULT_CONFIDENCE_THRESHOLD: f64 = 0.6;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum MemoryKind {
    Weight,
    Text,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MemoryRecord {
    pub id: String,
    pub content: String,
    pub kind: MemoryKind,
    pub confidence: f64,
    pub tokens: usize,
}

impl MemoryRecord {
    pub fn new(
        id: impl Into<String>,
        content: impl Into<String>,
        kind: MemoryKind,
        confidence: f64,
        tokens: usize,
    ) -> Self {
        Self {
            id: id.into(),
            content: content.into(),
            kind,
            confidence,
            tokens,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HookRule {
    pub event: String,
    pub memory_ids: Vec<String>,
}

impl HookRule {
    pub fn new(event: impl Into<String>, memory_ids: Vec<String>) -> Self {
        Self {
            event: event.into(),
            memory_ids,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PrefetchPrediction {
    pub event: String,
    pub memory_id: String,
    pub probability: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PrefetchEntry {
    pub memory_id: String,
    pub probability: f64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum MemoryRoute {
    Weight,
    Text,
    None,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RouteReport {
    pub route: MemoryRoute,
    pub memory_id: Option<String>,
    pub reason: String,
    pub tokens: usize,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct SidecarStats {
    pub hook_hits: usize,
    pub prefetch_hits: usize,
    pub weight_routes: usize,
    pub text_routes: usize,
    pub blocked_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum MemSidecarError {
    #[error("event must not be empty")]
    EmptyEvent,
    #[error("memory id must not be empty")]
    EmptyMemoryId,
    #[error("memory content must not be empty")]
    EmptyContent,
    #[error("confidence must be finite and in [0, 1]")]
    InvalidConfidence,
    #[error("duplicate hook for event")]
    DuplicateHook,
}

#[derive(Debug, Clone)]
pub struct MemSidecar {
    max_prefetch: usize,
    confidence_threshold: f64,
    hooks: HashMap<String, Vec<String>>,
    prefetched: HashMap<String, PrefetchEntry>,
    stats: SidecarStats,
}

impl Default for MemSidecar {
    fn default() -> Self {
        Self::new()
    }
}

impl MemSidecar {
    pub fn new() -> Self {
        Self::with_limits(DEFAULT_MAX_PREFETCH, DEFAULT_CONFIDENCE_THRESHOLD)
    }

    pub fn with_limits(max_prefetch: usize, confidence_threshold: f64) -> Self {
        Self {
            max_prefetch: max_prefetch.max(1),
            confidence_threshold: confidence_threshold.clamp(0.0, 1.0),
            hooks: HashMap::new(),
            prefetched: HashMap::new(),
            stats: SidecarStats::default(),
        }
    }

    pub fn register_hook(&mut self, rule: HookRule) -> Result<(), MemSidecarError> {
        if rule.event.trim().is_empty() {
            return Err(MemSidecarError::EmptyEvent);
        }
        if rule.memory_ids.iter().any(|id| id.trim().is_empty()) {
            return Err(MemSidecarError::EmptyMemoryId);
        }
        if self.hooks.contains_key(&rule.event) {
            return Err(MemSidecarError::DuplicateHook);
        }
        self.hooks.insert(rule.event, rule.memory_ids);
        Ok(())
    }

    /// Hooks return memory identifiers that should be injected immediately.
    pub fn on_event(&mut self, event: &str) -> Vec<String> {
        let matched = self.hooks.get(event).cloned().unwrap_or_default();
        self.stats.hook_hits += matched.len();
        matched
    }

    /// Records a Foresight prediction and keeps the highest-probability
    /// memories up to the prefetch bound. Returns the accepted entries.
    pub fn observe_prediction(
        &mut self,
        prediction: PrefetchPrediction,
    ) -> Result<Vec<PrefetchEntry>, MemSidecarError> {
        if prediction.event.trim().is_empty() {
            return Err(MemSidecarError::EmptyEvent);
        }
        if prediction.memory_id.trim().is_empty() {
            return Err(MemSidecarError::EmptyMemoryId);
        }
        if !prediction.probability.is_finite() || !(0.0..=1.0).contains(&prediction.probability) {
            return Err(MemSidecarError::InvalidConfidence);
        }
        if prediction.probability >= self.confidence_threshold {
            self.prefetched.insert(
                prediction.event,
                PrefetchEntry {
                    memory_id: prediction.memory_id,
                    probability: prediction.probability,
                },
            );
        }
        let mut entries: Vec<(String, PrefetchEntry)> = self
            .prefetched
            .iter()
            .map(|(event, entry)| (event.clone(), entry.clone()))
            .collect();
        entries.sort_by(|a, b| {
            b.1.probability
                .total_cmp(&a.1.probability)
                .then_with(|| a.1.memory_id.cmp(&b.1.memory_id))
                .then_with(|| a.0.cmp(&b.0))
        });
        entries.truncate(self.max_prefetch);
        self.prefetched = entries
            .iter()
            .map(|(event, entry)| (event.clone(), entry.clone()))
            .collect();
        Ok(entries.into_iter().map(|(_, entry)| entry).collect())
    }

    pub fn take_prefetch(&mut self, event: &str) -> Option<PrefetchEntry> {
        let entry = self.prefetched.remove(event);
        if entry.is_some() {
            self.stats.prefetch_hits += 1;
        }
        entry
    }

    /// OPSD-style route: trust a verified weight memory only when confidence
    /// clears the threshold; otherwise fall back to text or nothing.
    pub fn route(
        &mut self,
        weight_memory: Option<MemoryRecord>,
        text_memory: Option<MemoryRecord>,
        verification_passed: bool,
    ) -> Result<RouteReport, MemSidecarError> {
        if let Some(memory) = weight_memory {
            if memory.id.trim().is_empty() {
                return Err(MemSidecarError::EmptyMemoryId);
            }
            if memory.content.trim().is_empty() {
                return Err(MemSidecarError::EmptyContent);
            }
            if !memory.confidence.is_finite() || !(0.0..=1.0).contains(&memory.confidence) {
                return Err(MemSidecarError::InvalidConfidence);
            }
            if verification_passed && memory.confidence >= self.confidence_threshold {
                self.stats.weight_routes += 1;
                return Ok(RouteReport {
                    route: MemoryRoute::Weight,
                    memory_id: Some(memory.id),
                    reason: "verified weight memory above confidence floor".to_string(),
                    tokens: memory.tokens,
                });
            }
        }
        if let Some(memory) = text_memory {
            if memory.id.trim().is_empty() {
                return Err(MemSidecarError::EmptyMemoryId);
            }
            if memory.content.trim().is_empty() {
                return Err(MemSidecarError::EmptyContent);
            }
            self.stats.text_routes += 1;
            return Ok(RouteReport {
                route: MemoryRoute::Text,
                memory_id: Some(memory.id),
                reason: "text fallback after weight-memory verification gate".to_string(),
                tokens: memory.tokens,
            });
        }
        Ok(RouteReport {
            route: MemoryRoute::None,
            memory_id: None,
            reason: "no memory candidate available".to_string(),
            tokens: 0,
        })
    }

    pub fn observe_blocked_ms(&mut self, millis: u64) {
        self.stats.blocked_ms = self.stats.blocked_ms.saturating_add(millis);
    }

    pub fn stats(&self) -> &SidecarStats {
        &self.stats
    }

    pub fn prefetched_count(&self) -> usize {
        self.prefetched.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn record(id: &str, kind: MemoryKind, confidence: f64) -> MemoryRecord {
        MemoryRecord::new(id, "payload", kind, confidence, 20)
    }

    #[test]
    fn hook_rules_return_memories() {
        let mut sidecar = MemSidecar::new();
        sidecar
            .register_hook(HookRule::new("read_file", vec!["cache-notes".into()]))
            .unwrap();
        assert_eq!(sidecar.on_event("read_file"), vec!["cache-notes"]);
        assert_eq!(sidecar.stats().hook_hits, 1);
    }

    #[test]
    fn duplicate_hooks_are_rejected() {
        let mut sidecar = MemSidecar::new();
        sidecar
            .register_hook(HookRule::new("read_file", vec!["a".into()]))
            .unwrap();
        assert!(matches!(
            sidecar.register_hook(HookRule::new("read_file", vec!["b".into()])),
            Err(MemSidecarError::DuplicateHook)
        ));
    }

    #[test]
    fn foresight_keeps_high_probability_prefetch() {
        let mut sidecar = MemSidecar::new();
        sidecar
            .observe_prediction(PrefetchPrediction {
                event: "edit".into(),
                memory_id: "m1".into(),
                probability: 0.9,
            })
            .unwrap();
        assert_eq!(sidecar.take_prefetch("edit").unwrap().memory_id, "m1");
        assert_eq!(sidecar.stats().prefetch_hits, 1);
    }

    #[test]
    fn low_probability_prefetch_is_ignored() {
        let mut sidecar = MemSidecar::new();
        sidecar
            .observe_prediction(PrefetchPrediction {
                event: "edit".into(),
                memory_id: "m1".into(),
                probability: 0.1,
            })
            .unwrap();
        assert!(sidecar.take_prefetch("edit").is_none());
    }

    #[test]
    fn prefetch_is_bounded() {
        let mut sidecar = MemSidecar::with_limits(2, 0.1);
        for index in 0..5 {
            sidecar
                .observe_prediction(PrefetchPrediction {
                    event: format!("e{index}"),
                    memory_id: format!("m{index}"),
                    probability: 0.9,
                })
                .unwrap();
        }
        assert_eq!(sidecar.prefetched_count(), 2);
    }

    #[test]
    fn verified_weight_memory_routes_directly() {
        let mut sidecar = MemSidecar::new();
        let report = sidecar
            .route(
                Some(record("w", MemoryKind::Weight, 0.9)),
                Some(record("t", MemoryKind::Text, 0.9)),
                true,
            )
            .unwrap();
        assert_eq!(report.route, MemoryRoute::Weight);
        assert_eq!(report.memory_id.as_deref(), Some("w"));
    }

    #[test]
    fn unverified_weight_memory_falls_back_to_text() {
        let mut sidecar = MemSidecar::new();
        let report = sidecar
            .route(
                Some(record("w", MemoryKind::Weight, 0.9)),
                Some(record("t", MemoryKind::Text, 0.9)),
                false,
            )
            .unwrap();
        assert_eq!(report.route, MemoryRoute::Text);
        assert_eq!(report.memory_id.as_deref(), Some("t"));
    }

    #[test]
    fn low_confidence_weight_memory_falls_back() {
        let mut sidecar = MemSidecar::new();
        let report = sidecar
            .route(Some(record("w", MemoryKind::Weight, 0.2)), None, true)
            .unwrap();
        assert_eq!(report.route, MemoryRoute::None);
    }

    #[test]
    fn empty_record_fields_are_rejected() {
        let mut sidecar = MemSidecar::new();
        assert!(matches!(
            sidecar.route(
                Some(MemoryRecord::new("", "x", MemoryKind::Weight, 0.9, 1)),
                None,
                true
            ),
            Err(MemSidecarError::EmptyMemoryId)
        ));
    }

    #[test]
    fn blocked_latency_accumulates() {
        let mut sidecar = MemSidecar::new();
        sidecar.observe_blocked_ms(2);
        sidecar.observe_blocked_ms(3);
        assert_eq!(sidecar.stats().blocked_ms, 5);
    }
}
