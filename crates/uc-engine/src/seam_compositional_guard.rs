//! SEAM compositional-harm guard (ledger item #119).
//!
//! SEAM (arXiv:2611.22971) shows that computer-use agents often execute
//! harmful actions composed from individually benign observations: each
//! source is harmless on its own, only the cross-source combination is
//! harmful, and the screen channel's warning does not block execution the
//! way a user-instruction channel would.
//!
//! This module is a deterministic, channel-aware guard. Observations carry
//! entities and a source kind. A [`HarmPattern`] declares that an entity in
//! two or more channels is a composite hazard. Once such a match exists, the
//! guard escalates the conflict to the privileged instruction channel and
//! refuses authorization. It never calls a model and never executes an
//! action.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Default maximum number of retained observations.
pub const DEFAULT_MAX_OBSERVATIONS: usize = 512;

/// Default maximum number of registered composite patterns.
pub const DEFAULT_MAX_PATTERNS: usize = 128;

/// The channel an observation came from.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub enum SourceKind {
    /// Screen / UI observation.
    Screen,
    /// Tool or MCP output.
    ToolOutput,
    /// Repository or file content.
    FileContent,
    /// A user or system instruction.
    UserInstruction,
}

/// A single observation from one channel.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Observation {
    /// Stable observation id.
    pub id: String,
    /// Source channel.
    pub source: SourceKind,
    /// Stable source instance id (for example a window or tool call id).
    pub source_id: String,
    /// Entities mentioned by the observation.
    pub entities: BTreeSet<String>,
}

impl Observation {
    /// Creates an observation.
    pub fn new(
        id: impl Into<String>,
        source: SourceKind,
        source_id: impl Into<String>,
        entities: impl IntoIterator<Item = impl Into<String>>,
    ) -> Self {
        Self {
            id: id.into(),
            source,
            source_id: source_id.into(),
            entities: entities.into_iter().map(Into::into).collect(),
        }
    }
}

/// A cross-channel composite hazard rule.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HarmPattern {
    /// Stable pattern id.
    pub id: String,
    /// Entity whose appearance across channels is hazardous.
    pub entity: String,
    /// Channels that must all carry the entity.
    pub channels: BTreeSet<SourceKind>,
    /// Human-readable rationale.
    pub rationale: String,
}

impl HarmPattern {
    /// Creates a pattern that requires at least two distinct channels.
    pub fn new(
        id: impl Into<String>,
        entity: impl Into<String>,
        channels: impl IntoIterator<Item = SourceKind>,
        rationale: impl Into<String>,
    ) -> Result<Self, SeamError> {
        let id = id.into();
        let entity = entity.into();
        if id.trim().is_empty() {
            return Err(SeamError::EmptyField("pattern id"));
        }
        if entity.trim().is_empty() {
            return Err(SeamError::EmptyField("pattern entity"));
        }
        let channels: BTreeSet<SourceKind> = channels.into_iter().collect();
        if channels.len() < 2 {
            return Err(SeamError::PatternNeedsTwoChannels);
        }
        Ok(Self {
            id,
            entity,
            channels,
            rationale: rationale.into(),
        })
    }
}

/// A matched composite hazard.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HarmMatch {
    /// Matching pattern id.
    pub pattern_id: String,
    /// Entity that triggered the rule.
    pub entity: String,
    /// Channels that carried the entity.
    pub matched_channels: BTreeSet<SourceKind>,
    /// Distinct source ids involved, sorted.
    pub source_ids: Vec<String>,
}

/// Escalation channel used when a composite hazard is found.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum EscalationChannel {
    /// Privileged system-instruction channel; observations cannot override it.
    PrivilegedInstruction,
}

/// A hard escalation decision.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Escalation {
    /// Where the conflict is escalated.
    pub channel: EscalationChannel,
    /// Human-readable reason.
    pub reason: String,
}

/// Result of evaluating all observations.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SeamReport {
    /// Whether a composite hazard matched.
    pub blocked: bool,
    /// All matching patterns.
    pub matches: Vec<HarmMatch>,
    /// Hard escalation, present exactly when `blocked` is true.
    pub escalation: Option<Escalation>,
}

/// Errors produced by the guard.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum SeamError {
    /// A required field was empty.
    #[error("empty field: {0}")]
    EmptyField(&'static str),
    /// Duplicate observation id.
    #[error("duplicate observation: {0}")]
    DuplicateObservation(String),
    /// Duplicate pattern id.
    #[error("duplicate pattern: {0}")]
    DuplicatePattern(String),
    /// Observation capacity reached.
    #[error("observation capacity reached")]
    ObservationCapacityReached,
    /// Pattern capacity reached.
    #[error("pattern capacity reached")]
    PatternCapacityReached,
    /// A pattern must cover at least two channels.
    #[error("a composite pattern must cover at least two channels")]
    PatternNeedsTwoChannels,
    /// Authorization was refused because a composite hazard matched.
    #[error("composite harm blocked by pattern {0}")]
    CompositeHarmBlocked(String),
}

/// Deterministic SEAM guard.
#[derive(Debug, Clone)]
pub struct SeamGuard {
    max_observations: usize,
    max_patterns: usize,
    observations: Vec<Observation>,
    patterns: Vec<HarmPattern>,
}

impl Default for SeamGuard {
    fn default() -> Self {
        Self::new()
    }
}

impl SeamGuard {
    /// Creates a guard with default bounds.
    pub fn new() -> Self {
        Self::with_capacity(DEFAULT_MAX_OBSERVATIONS, DEFAULT_MAX_PATTERNS)
    }

    /// Creates a guard with explicit bounds.
    pub fn with_capacity(max_observations: usize, max_patterns: usize) -> Self {
        Self {
            max_observations: max_observations.max(1),
            max_patterns: max_patterns.max(1),
            observations: Vec::new(),
            patterns: Vec::new(),
        }
    }

    /// Records an observation.
    pub fn observe(&mut self, observation: Observation) -> Result<(), SeamError> {
        if observation.id.trim().is_empty() {
            return Err(SeamError::EmptyField("observation id"));
        }
        if observation.source_id.trim().is_empty() {
            return Err(SeamError::EmptyField("source id"));
        }
        if observation
            .entities
            .iter()
            .any(|entity| entity.trim().is_empty())
        {
            return Err(SeamError::EmptyField("entity"));
        }
        if self
            .observations
            .iter()
            .any(|existing| existing.id == observation.id)
        {
            return Err(SeamError::DuplicateObservation(observation.id));
        }
        if self.observations.len() >= self.max_observations {
            return Err(SeamError::ObservationCapacityReached);
        }
        self.observations.push(observation);
        Ok(())
    }

    /// Registers a composite-harm pattern.
    pub fn add_pattern(&mut self, pattern: HarmPattern) -> Result<(), SeamError> {
        if self
            .patterns
            .iter()
            .any(|existing| existing.id == pattern.id)
        {
            return Err(SeamError::DuplicatePattern(pattern.id));
        }
        if self.patterns.len() >= self.max_patterns {
            return Err(SeamError::PatternCapacityReached);
        }
        self.patterns.push(pattern);
        Ok(())
    }

    /// Observations in insertion order.
    pub fn observations(&self) -> &[Observation] {
        &self.observations
    }

    /// Registered patterns.
    pub fn patterns(&self) -> &[HarmPattern] {
        &self.patterns
    }

    /// Evaluates every pattern against the retained observations.
    ///
    /// A pattern matches when the entity appears in every required channel
    /// and at least two distinct source instances are involved. A hazard is
    /// never waived by an instruction-channel claim; the caller can only
    /// authorize when the report is clean.
    pub fn evaluate(&self) -> SeamReport {
        let mut matches = Vec::new();
        for pattern in &self.patterns {
            let mut channels = BTreeMap::<SourceKind, BTreeSet<String>>::new();
            for observation in &self.observations {
                if observation.entities.contains(&pattern.entity) {
                    channels
                        .entry(observation.source)
                        .or_default()
                        .insert(observation.source_id.clone());
                }
            }
            let matched_channels: BTreeSet<SourceKind> = channels.keys().copied().collect();
            if !pattern.channels.is_subset(&matched_channels) {
                continue;
            }
            let source_ids: Vec<String> = channels
                .values()
                .flat_map(|ids| ids.iter().cloned())
                .collect();
            let distinct: BTreeSet<&String> = source_ids.iter().collect();
            if distinct.len() < 2 {
                continue;
            }
            let mut sorted = source_ids;
            sorted.sort();
            sorted.dedup();
            matches.push(HarmMatch {
                pattern_id: pattern.id.clone(),
                entity: pattern.entity.clone(),
                matched_channels,
                source_ids: sorted,
            });
        }
        matches.sort_by(|left, right| {
            left.pattern_id
                .cmp(&right.pattern_id)
                .then(left.entity.cmp(&right.entity))
        });
        let blocked = !matches.is_empty();
        let escalation = blocked.then(|| Escalation {
            channel: EscalationChannel::PrivilegedInstruction,
            reason: format!(
                "cross-channel composite hazard: {}",
                matches
                    .iter()
                    .map(|item| item.pattern_id.as_str())
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
        });
        SeamReport {
            blocked,
            matches,
            escalation,
        }
    }

    /// Requires a clean report before an action may execute.
    pub fn authorize(&self) -> Result<(), SeamError> {
        match self.evaluate().matches.first() {
            Some(item) => Err(SeamError::CompositeHarmBlocked(item.pattern_id.clone())),
            None => Ok(()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pattern(entity: &str, channels: [SourceKind; 2]) -> HarmPattern {
        HarmPattern::new("p1", entity, channels, "composite hazard").unwrap()
    }

    fn obs(id: &str, source: SourceKind, source_id: &str, entity: &str) -> Observation {
        Observation::new(id, source, source_id, [entity])
    }

    #[test]
    fn single_channel_is_benign() {
        let mut guard = SeamGuard::new();
        guard
            .add_pattern(pattern(
                "secret",
                [SourceKind::Screen, SourceKind::ToolOutput],
            ))
            .unwrap();
        guard
            .observe(obs("o1", SourceKind::Screen, "screen-1", "secret"))
            .unwrap();
        let report = guard.evaluate();
        assert!(!report.blocked);
        assert!(report.matches.is_empty());
        assert!(report.escalation.is_none());
        guard.authorize().unwrap();
    }

    #[test]
    fn cross_channel_match_is_blocked() {
        let mut guard = SeamGuard::new();
        guard
            .add_pattern(pattern(
                "secret",
                [SourceKind::Screen, SourceKind::ToolOutput],
            ))
            .unwrap();
        guard
            .observe(obs("o1", SourceKind::Screen, "screen-1", "secret"))
            .unwrap();
        guard
            .observe(obs("o2", SourceKind::ToolOutput, "tool-1", "secret"))
            .unwrap();
        let report = guard.evaluate();
        assert!(report.blocked);
        assert_eq!(report.matches.len(), 1);
        assert_eq!(report.matches[0].matched_channels.len(), 2);
        assert!(report.escalation.is_some());
        assert_eq!(
            report.escalation.unwrap().channel,
            EscalationChannel::PrivilegedInstruction
        );
        assert!(matches!(
            guard.authorize(),
            Err(SeamError::CompositeHarmBlocked(_))
        ));
    }

    #[test]
    fn same_source_instance_does_not_compose() {
        let mut guard = SeamGuard::new();
        guard
            .add_pattern(pattern(
                "secret",
                [SourceKind::Screen, SourceKind::ToolOutput],
            ))
            .unwrap();
        guard
            .observe(obs("o1", SourceKind::Screen, "same", "secret"))
            .unwrap();
        guard
            .observe(obs("o2", SourceKind::ToolOutput, "same", "secret"))
            .unwrap();
        assert!(!guard.evaluate().blocked);
    }

    #[test]
    fn pattern_requires_two_channels() {
        let error = HarmPattern::new("p", "entity", [SourceKind::Screen], "reason").unwrap_err();
        assert_eq!(error, SeamError::PatternNeedsTwoChannels);
    }

    #[test]
    fn duplicate_and_empty_observations_are_rejected() {
        let mut guard = SeamGuard::new();
        guard
            .observe(obs("o1", SourceKind::Screen, "s", "e"))
            .unwrap();
        assert_eq!(
            guard.observe(obs("o1", SourceKind::Screen, "s2", "e")),
            Err(SeamError::DuplicateObservation("o1".into()))
        );
        assert_eq!(
            guard.observe(Observation::new("", SourceKind::Screen, "s", ["e"])),
            Err(SeamError::EmptyField("observation id"))
        );
        assert_eq!(
            guard.observe(Observation::new("o2", SourceKind::Screen, "", ["e"])),
            Err(SeamError::EmptyField("source id"))
        );
    }

    #[test]
    fn capacity_is_bounded() {
        let mut guard = SeamGuard::with_capacity(1, 1);
        guard
            .observe(obs("o1", SourceKind::Screen, "s", "e"))
            .unwrap();
        assert_eq!(
            guard.observe(obs("o2", SourceKind::Screen, "s2", "e")),
            Err(SeamError::ObservationCapacityReached)
        );
        guard
            .add_pattern(pattern("e", [SourceKind::Screen, SourceKind::ToolOutput]))
            .unwrap();
        assert_eq!(
            guard.add_pattern(
                HarmPattern::new(
                    "p2",
                    "e",
                    [SourceKind::Screen, SourceKind::ToolOutput],
                    "another composite hazard",
                )
                .unwrap()
            ),
            Err(SeamError::PatternCapacityReached)
        );
    }

    #[test]
    fn matching_multiple_patterns_is_deterministic() {
        let mut guard = SeamGuard::new();
        guard
            .add_pattern(
                HarmPattern::new(
                    "z",
                    "token",
                    [SourceKind::Screen, SourceKind::ToolOutput],
                    "z",
                )
                .unwrap(),
            )
            .unwrap();
        guard
            .add_pattern(
                HarmPattern::new(
                    "a",
                    "token",
                    [SourceKind::FileContent, SourceKind::ToolOutput],
                    "a",
                )
                .unwrap(),
            )
            .unwrap();
        for (id, source, source_id) in [
            ("o1", SourceKind::Screen, "s1"),
            ("o2", SourceKind::ToolOutput, "t1"),
            ("o3", SourceKind::FileContent, "f1"),
        ] {
            guard.observe(obs(id, source, source_id, "token")).unwrap();
        }
        let report = guard.evaluate();
        assert_eq!(
            report
                .matches
                .iter()
                .map(|m| m.pattern_id.as_str())
                .collect::<Vec<_>>(),
            vec!["a", "z"]
        );
    }

    #[test]
    fn unrelated_entities_do_not_match() {
        let mut guard = SeamGuard::new();
        guard
            .add_pattern(pattern(
                "secret",
                [SourceKind::Screen, SourceKind::ToolOutput],
            ))
            .unwrap();
        guard
            .observe(obs("o1", SourceKind::Screen, "s1", "public"))
            .unwrap();
        guard
            .observe(obs("o2", SourceKind::ToolOutput, "t1", "secret"))
            .unwrap();
        assert!(!guard.evaluate().blocked);
    }

    #[test]
    fn instructions_cannot_waive_a_composite_hazard() {
        let mut guard = SeamGuard::new();
        guard
            .add_pattern(pattern(
                "secret",
                [SourceKind::Screen, SourceKind::ToolOutput],
            ))
            .unwrap();
        guard
            .observe(obs("o1", SourceKind::Screen, "s1", "secret"))
            .unwrap();
        guard
            .observe(obs("o2", SourceKind::ToolOutput, "t1", "secret"))
            .unwrap();
        guard
            .observe(obs("o3", SourceKind::UserInstruction, "u1", "secret"))
            .unwrap();
        assert!(guard.evaluate().blocked);
    }
}
