//! Conflict detection and resolution for multi-agent code editing.
//!
//! Uses intent-based locking: workers declare edit intents before modifying
//! files. When overlapping edits are detected, a tiered resolution pipeline
//! is applied:
//!
//! 1. Auto-merge (three-way diff) — ~70% success rate
//! 2. LLM-assisted merge — ~90% success rate
//! 3. Re-assign to single worker — ~98% success rate
//! 4. Human escalation — last resort

pub mod merger;

use dashmap::DashMap;
use serde::{Deserialize, Serialize};

use crate::events::LineRange;

/// Type of edit operation.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum EditType {
    Create,
    Modify,
    Delete,
}

/// A declared intent to edit a file.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EditIntent {
    pub worker_id: String,
    pub file_path: String,
    pub edit_type: EditType,
    pub regions: Vec<LineRange>,
    /// Unix timestamp (milliseconds).
    pub timestamp: i64,
}

impl EditIntent {
    pub fn new(
        worker_id: String,
        file_path: String,
        edit_type: EditType,
        regions: Vec<LineRange>,
    ) -> Self {
        Self {
            worker_id,
            file_path,
            edit_type,
            regions,
            timestamp: chrono::Utc::now().timestamp_millis(),
        }
    }
}

/// Result of a conflict check.
#[derive(Debug, Clone)]
pub enum ConflictResult {
    /// No conflict detected.
    NoConflict,
    /// Potential conflict (same file, different regions).
    PotentialConflict { conflicting_workers: Vec<String> },
    /// Conflicting edit (overlapping regions on same file).
    Conflicting {
        conflicting_workers: Vec<String>,
        resolution_tier: ResolutionTier,
    },
}

/// Resolution tier for conflicts.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum ResolutionTier {
    /// Auto-merge via three-way diff.
    AutoMerge,
    /// LLM-assisted merge.
    LlmAssisted,
    /// Re-assign to a single worker with full context.
    Reassign,
    /// Human escalation.
    Human,
}

impl ResolutionTier {
    /// Get the next tier up (more expensive resolution).
    pub fn escalate(&self) -> ResolutionTier {
        match self {
            ResolutionTier::AutoMerge => ResolutionTier::LlmAssisted,
            ResolutionTier::LlmAssisted => ResolutionTier::Reassign,
            ResolutionTier::Reassign => ResolutionTier::Human,
            ResolutionTier::Human => ResolutionTier::Human,
        }
    }
}

/// Result of a merge attempt.
#[derive(Debug, Clone)]
pub struct MergeResult {
    /// The merged content, if successful.
    pub merged: Option<String>,
    /// Conflict markers for regions that couldn't be auto-merged.
    pub conflicts: Vec<ConflictMarker>,
    /// Whether the merge was fully successful (no remaining conflicts).
    pub success: bool,
    /// The resolution tier that was applied.
    pub tier: ResolutionTier,
}

/// A conflict marker for a region that couldn't be auto-merged.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConflictMarker {
    /// Line number where the conflict starts.
    pub start_line: u32,
    /// Line number where the conflict ends.
    pub end_line: u32,
    /// Content from "ours" side.
    pub ours: String,
    /// Content from "theirs" side.
    pub theirs: String,
    /// Content from the base (original).
    pub base: String,
}

/// Intent-based conflict detector.
///
/// Workers declare edit intents before modifying files. The detector
/// checks for overlapping regions and returns conflict information.
pub struct ConflictDetector {
    /// Baseline file content hashes at task start.
    baseline_hashes: DashMap<String, String>,
    /// Currently declared edit intents, keyed by file path.
    active_intents: DashMap<String, Vec<EditIntent>>,
}

impl ConflictDetector {
    /// Create a new conflict detector.
    pub fn new() -> Self {
        Self {
            baseline_hashes: DashMap::new(),
            active_intents: DashMap::new(),
        }
    }

    /// Register the baseline hash for a file at task start.
    ///
    /// This is used to detect if a file has been modified by another
    /// worker between the time the task started and the edit is applied.
    pub fn register_baseline(&self, file_path: &str, hash: &str) {
        self.baseline_hashes
            .insert(file_path.to_string(), hash.to_string());
    }

    /// Declare an edit intent.
    ///
    /// The intent is recorded and checked against existing intents.
    /// Returns a `ConflictResult` indicating whether the edit would
    /// conflict with any existing declared intents.
    pub fn declare_intent(&self, intent: EditIntent) -> ConflictResult {
        let result = self.check_conflict(&intent.file_path, &intent.worker_id, &intent.regions);

        // Always record the intent
        self.active_intents
            .entry(intent.file_path.clone())
            .or_default()
            .push(intent);

        result
    }

    /// Check if an edit would conflict with existing intents.
    ///
    /// Does NOT record the intent; use `declare_intent` for that.
    pub fn check_conflict(
        &self,
        file_path: &str,
        worker_id: &str,
        regions: &[LineRange],
    ) -> ConflictResult {
        let intents = self.active_intents.get(file_path);

        match intents {
            Some(existing) if !existing.is_empty() => {
                // Find workers with overlapping intents
                let mut conflicting_workers = Vec::new();

                for intent in existing.iter() {
                    if intent.worker_id == worker_id {
                        continue; // Same worker, not a conflict
                    }

                    // Check if any regions overlap
                    let has_overlap = intent.regions.iter().any(|existing_region| {
                        regions
                            .iter()
                            .any(|new_region| existing_region.overlaps(new_region))
                    });

                    // Also flag if editing the same file with no specific regions
                    // (whole-file edit)
                    let whole_file_conflict =
                        (intent.regions.is_empty() || regions.is_empty()) && !has_overlap;

                    if (has_overlap || whole_file_conflict)
                        && !conflicting_workers.contains(&intent.worker_id)
                    {
                        conflicting_workers.push(intent.worker_id.clone());
                    }
                }

                if conflicting_workers.is_empty() {
                    // Same file, different regions — potential conflict
                    let other_workers: Vec<String> = existing
                        .iter()
                        .filter(|i| i.worker_id != worker_id)
                        .map(|i| i.worker_id.clone())
                        .collect();

                    if other_workers.is_empty() {
                        ConflictResult::NoConflict
                    } else {
                        ConflictResult::PotentialConflict {
                            conflicting_workers: other_workers,
                        }
                    }
                } else {
                    ConflictResult::Conflicting {
                        conflicting_workers,
                        resolution_tier: ResolutionTier::AutoMerge,
                    }
                }
            }
            _ => ConflictResult::NoConflict,
        }
    }

    /// Remove an intent after the edit is completed or abandoned.
    pub fn remove_intent(&self, file_path: &str, worker_id: &str) {
        if let Some(mut intents) = self.active_intents.get_mut(file_path) {
            intents.retain(|i| i.worker_id != worker_id);
        }
    }

    /// Clear all intents for a task (e.g., when the task completes).
    pub fn clear_intents(&self) {
        self.active_intents.clear();
    }

    /// Get all active intents for a file.
    pub fn get_intents(&self, file_path: &str) -> Vec<EditIntent> {
        self.active_intents
            .get(file_path)
            .map(|ints| ints.iter().cloned().collect())
            .unwrap_or_default()
    }

    /// Check if a file's current hash differs from the baseline.
    ///
    /// Returns true if the file has been modified since the baseline was set.
    pub fn is_file_modified(&self, file_path: &str, current_hash: &str) -> bool {
        match self.baseline_hashes.get(file_path) {
            Some(baseline) => baseline.value() != current_hash,
            None => false, // No baseline registered
        }
    }
}

impl Default for ConflictDetector {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn no_conflict_different_files() {
        let detector = ConflictDetector::new();

        let intent1 = EditIntent::new(
            "worker-1".to_string(),
            "src/main.rs".to_string(),
            EditType::Modify,
            vec![LineRange::new(1, 10)],
        );

        let intent2 = EditIntent::new(
            "worker-2".to_string(),
            "src/lib.rs".to_string(),
            EditType::Modify,
            vec![LineRange::new(1, 10)],
        );

        let result1 = detector.declare_intent(intent1);
        assert!(matches!(result1, ConflictResult::NoConflict));

        let result2 = detector.declare_intent(intent2);
        assert!(matches!(result2, ConflictResult::NoConflict));
    }

    #[test]
    fn no_conflict_same_file_different_regions() {
        let detector = ConflictDetector::new();

        let intent1 = EditIntent::new(
            "worker-1".to_string(),
            "src/main.rs".to_string(),
            EditType::Modify,
            vec![LineRange::new(1, 10)],
        );

        let intent2 = EditIntent::new(
            "worker-2".to_string(),
            "src/main.rs".to_string(),
            EditType::Modify,
            vec![LineRange::new(20, 30)],
        );

        let result1 = detector.declare_intent(intent1);
        assert!(matches!(result1, ConflictResult::NoConflict));

        let result2 = detector.declare_intent(intent2);
        // Same file, different regions -> potential conflict
        assert!(matches!(result2, ConflictResult::PotentialConflict { .. }));
    }

    #[test]
    fn conflict_overlapping_regions() {
        let detector = ConflictDetector::new();

        let intent1 = EditIntent::new(
            "worker-1".to_string(),
            "src/main.rs".to_string(),
            EditType::Modify,
            vec![LineRange::new(1, 20)],
        );

        let intent2 = EditIntent::new(
            "worker-2".to_string(),
            "src/main.rs".to_string(),
            EditType::Modify,
            vec![LineRange::new(10, 30)],
        );

        detector.declare_intent(intent1);
        let result = detector.declare_intent(intent2);

        match result {
            ConflictResult::Conflicting {
                conflicting_workers,
                resolution_tier,
            } => {
                assert!(conflicting_workers.contains(&"worker-1".to_string()));
                assert_eq!(resolution_tier, ResolutionTier::AutoMerge);
            }
            _ => panic!("Expected Conflicting result"),
        }
    }

    #[test]
    fn conflict_whole_file_edit() {
        let detector = ConflictDetector::new();

        let intent1 = EditIntent::new(
            "worker-1".to_string(),
            "src/main.rs".to_string(),
            EditType::Modify,
            vec![], // whole file
        );

        let intent2 = EditIntent::new(
            "worker-2".to_string(),
            "src/main.rs".to_string(),
            EditType::Modify,
            vec![LineRange::new(5, 15)],
        );

        detector.declare_intent(intent1);
        let result = detector.declare_intent(intent2);

        assert!(matches!(result, ConflictResult::Conflicting { .. }));
    }

    #[test]
    fn remove_intent() {
        let detector = ConflictDetector::new();

        let intent = EditIntent::new(
            "worker-1".to_string(),
            "src/main.rs".to_string(),
            EditType::Modify,
            vec![LineRange::new(1, 10)],
        );

        detector.declare_intent(intent);
        assert_eq!(detector.get_intents("src/main.rs").len(), 1);

        detector.remove_intent("src/main.rs", "worker-1");
        assert_eq!(detector.get_intents("src/main.rs").len(), 0);
    }

    #[test]
    fn baseline_hash_detection() {
        let detector = ConflictDetector::new();

        detector.register_baseline("src/main.rs", "hash123");

        assert!(!detector.is_file_modified("src/main.rs", "hash123"));
        assert!(detector.is_file_modified("src/main.rs", "hash456"));
        assert!(!detector.is_file_modified("src/other.rs", "hash456"));
    }

    #[test]
    fn resolution_tier_escalation() {
        assert_eq!(
            ResolutionTier::AutoMerge.escalate(),
            ResolutionTier::LlmAssisted
        );
        assert_eq!(
            ResolutionTier::LlmAssisted.escalate(),
            ResolutionTier::Reassign
        );
        assert_eq!(ResolutionTier::Reassign.escalate(), ResolutionTier::Human);
        assert_eq!(ResolutionTier::Human.escalate(), ResolutionTier::Human);
    }

    #[test]
    fn same_worker_no_conflict() {
        let detector = ConflictDetector::new();

        let intent1 = EditIntent::new(
            "worker-1".to_string(),
            "src/main.rs".to_string(),
            EditType::Modify,
            vec![LineRange::new(1, 20)],
        );

        let intent2 = EditIntent::new(
            "worker-1".to_string(),
            "src/main.rs".to_string(),
            EditType::Modify,
            vec![LineRange::new(10, 30)],
        );

        detector.declare_intent(intent1);
        let result = detector.declare_intent(intent2);
        // Same worker editing overlapping regions is not a conflict
        assert!(matches!(result, ConflictResult::NoConflict));
    }
}
