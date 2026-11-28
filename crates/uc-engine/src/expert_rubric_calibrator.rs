//! Expert-authored rubric scoring with control-patch calibration.
//!
//! Implements ledger item #60 (ExpertCrowd-SWE, arXiv:2611.13246). The scorer
//! supports weighted partial credit across multi-file tasks, while the control
//! set forces the reference and legal-alternative patches to 1.0 and the
//! unmodified, inert, and adversarial patches to 0.0.

use serde::{Deserialize, Serialize};

pub const DEFAULT_MAX_CRITERIA: usize = 64;

/// One weighted rubric criterion.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RubricCriterion {
    pub id: String,
    pub weight: f32,
    pub description: String,
    pub required: bool,
}

impl RubricCriterion {
    pub fn new(id: impl Into<String>, weight: f32, description: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            weight,
            description: description.into(),
            required: false,
        }
    }

    pub fn required(mut self) -> Self {
        self.required = true;
        self
    }
}

/// The five control-patch classes from ExpertCrowd-SWE.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PatchKind {
    Reference,
    Unmodified,
    Inert,
    Alternative,
    Adversarial,
}

impl PatchKind {
    pub fn expected_score(self) -> f32 {
        match self {
            Self::Reference | Self::Alternative => 1.0,
            Self::Unmodified | Self::Inert | Self::Adversarial => 0.0,
        }
    }
}

/// Caller-supplied evidence for one candidate patch.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PatchEvidence {
    pub kind: PatchKind,
    pub satisfied: Vec<String>,
    pub bypasses_tests: bool,
    pub tampers_with_tests: bool,
}

impl PatchEvidence {
    pub fn new(kind: PatchKind, satisfied: Vec<String>) -> Self {
        Self {
            kind,
            satisfied,
            bypasses_tests: false,
            tampers_with_tests: false,
        }
    }

    pub fn with_bypass(mut self, bypasses_tests: bool) -> Self {
        self.bypasses_tests = bypasses_tests;
        self
    }

    pub fn with_test_tampering(mut self, tampers_with_tests: bool) -> Self {
        self.tampers_with_tests = tampers_with_tests;
        self
    }
}

/// The score for one patch.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RubricScore {
    pub raw: f32,
    pub normalized: f32,
    pub matched: usize,
    pub total: usize,
    pub missing: Vec<String>,
    pub zeroed_by_required: bool,
    pub zeroed_by_cheating: bool,
}

impl RubricScore {
    pub fn accepted(&self) -> bool {
        self.normalized >= 1.0 - f32::EPSILON
    }
}

/// One control-set observation.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CalibrationCase {
    pub kind: PatchKind,
    pub expected: f32,
    pub actual: f32,
    pub passed: bool,
}

/// Result of running the four-patch control calibration.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CalibrationReport {
    pub passed: bool,
    pub cases: Vec<CalibrationCase>,
}

impl CalibrationReport {
    pub fn failures(&self) -> impl Iterator<Item = &CalibrationCase> {
        self.cases.iter().filter(|case| !case.passed)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RubricError {
    EmptyField,
    InvalidWeight,
    DuplicateCriterion,
    CapacityReached,
    NoCriteria,
    EmptyControlSet,
}

impl std::fmt::Display for RubricError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::EmptyField => write!(formatter, "criterion id/description must not be empty"),
            Self::InvalidWeight => write!(formatter, "criterion weight must be positive"),
            Self::DuplicateCriterion => write!(formatter, "duplicate criterion id"),
            Self::CapacityReached => write!(formatter, "criterion capacity reached"),
            Self::NoCriteria => write!(formatter, "at least one criterion is required"),
            Self::EmptyControlSet => write!(formatter, "control set must not be empty"),
        }
    }
}

impl std::error::Error for RubricError {}

/// Weighted rubric with a bounded criterion set.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ExpertRubricCalibrator {
    criteria: Vec<RubricCriterion>,
    max_criteria: usize,
}

impl Default for ExpertRubricCalibrator {
    fn default() -> Self {
        Self::new()
    }
}

impl ExpertRubricCalibrator {
    pub fn new() -> Self {
        Self::with_capacity(DEFAULT_MAX_CRITERIA)
    }

    pub fn with_capacity(max_criteria: usize) -> Self {
        Self {
            criteria: Vec::new(),
            max_criteria: max_criteria.max(1),
        }
    }

    pub fn add(&mut self, criterion: RubricCriterion) -> Result<(), RubricError> {
        if criterion.id.trim().is_empty() || criterion.description.trim().is_empty() {
            return Err(RubricError::EmptyField);
        }
        if !criterion.weight.is_finite() || criterion.weight <= 0.0 {
            return Err(RubricError::InvalidWeight);
        }
        if self
            .criteria
            .iter()
            .any(|existing| existing.id == criterion.id)
        {
            return Err(RubricError::DuplicateCriterion);
        }
        if self.criteria.len() >= self.max_criteria {
            return Err(RubricError::CapacityReached);
        }
        self.criteria.push(criterion);
        Ok(())
    }

    pub fn criteria(&self) -> &[RubricCriterion] {
        &self.criteria
    }

    pub fn total_weight(&self) -> f32 {
        self.criteria.iter().map(|criterion| criterion.weight).sum()
    }

    pub fn score(&self, satisfied: &[String]) -> Result<RubricScore, RubricError> {
        self.score_patch(&PatchEvidence::new(
            PatchKind::Reference,
            satisfied.to_vec(),
        ))
    }

    pub fn score_patch(&self, evidence: &PatchEvidence) -> Result<RubricScore, RubricError> {
        if self.criteria.is_empty() {
            return Err(RubricError::NoCriteria);
        }
        let total_weight = self.total_weight();
        let mut raw = 0.0f32;
        let mut matched = 0usize;
        let mut missing = Vec::new();
        let mut missing_required = false;
        for criterion in &self.criteria {
            let hit = evidence.satisfied.iter().any(|id| id == &criterion.id);
            if hit {
                raw += criterion.weight;
                matched += 1;
            } else {
                missing.push(criterion.id.clone());
                if criterion.required {
                    missing_required = true;
                }
            }
        }
        let zeroed_by_cheating = evidence.bypasses_tests || evidence.tampers_with_tests;
        let normalized = if missing_required || zeroed_by_cheating {
            0.0
        } else {
            (raw / total_weight).clamp(0.0, 1.0)
        };
        Ok(RubricScore {
            raw,
            normalized,
            matched,
            total: self.criteria.len(),
            missing,
            zeroed_by_required: missing_required,
            zeroed_by_cheating,
        })
    }

    /// Run the five control patches and verify every expected score.
    pub fn calibrate(&self, patches: &[PatchEvidence]) -> Result<CalibrationReport, RubricError> {
        if patches.is_empty() {
            return Err(RubricError::EmptyControlSet);
        }
        let mut cases = Vec::with_capacity(patches.len());
        for patch in patches {
            let expected = patch.kind.expected_score();
            let actual = self.score_patch(patch)?.normalized;
            cases.push(CalibrationCase {
                kind: patch.kind,
                expected,
                actual,
                passed: (actual - expected).abs() <= 1e-6,
            });
        }
        let passed = cases.iter().all(|case| case.passed);
        Ok(CalibrationReport { passed, cases })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scored_calibrator() -> ExpertRubricCalibrator {
        let mut calibrator = ExpertRubricCalibrator::new();
        calibrator
            .add(RubricCriterion::new("compile", 0.5, "project compiles"))
            .unwrap();
        calibrator
            .add(RubricCriterion::new("behavior", 0.3, "requested behavior"))
            .unwrap();
        calibrator
            .add(RubricCriterion::new("tests", 0.2, "tests pass"))
            .unwrap();
        calibrator
    }

    fn all_ids() -> Vec<String> {
        vec!["compile".into(), "behavior".into(), "tests".into()]
    }

    #[test]
    fn partial_credit_is_weighted() {
        let calibrator = scored_calibrator();
        let score = calibrator
            .score(&["compile".into(), "behavior".into()])
            .unwrap();
        assert!((score.normalized - 0.8).abs() < 1e-6);
        assert_eq!(score.matched, 2);
        assert_eq!(score.missing, vec!["tests"]);
    }

    #[test]
    fn all_criteria_give_one() {
        let calibrator = scored_calibrator();
        assert!(calibrator.score(&all_ids()).unwrap().accepted());
    }

    #[test]
    fn no_satisfied_criteria_gives_zero() {
        let calibrator = scored_calibrator();
        assert_eq!(calibrator.score(&[]).unwrap().normalized, 0.0);
    }

    #[test]
    fn missing_required_criterion_zeroes_the_score() {
        let mut calibrator = ExpertRubricCalibrator::new();
        calibrator
            .add(RubricCriterion::new("compile", 0.5, "project compiles").required())
            .unwrap();
        let score = calibrator.score(&[]).unwrap();
        assert_eq!(score.normalized, 0.0);
        assert!(score.zeroed_by_required);
    }

    #[test]
    fn cheating_zeroes_even_a_full_rubric() {
        let calibrator = scored_calibrator();
        let patch = PatchEvidence::new(PatchKind::Adversarial, all_ids()).with_bypass(true);
        let score = calibrator.score_patch(&patch).unwrap();
        assert_eq!(score.normalized, 0.0);
        assert!(score.zeroed_by_cheating);
    }

    #[test]
    fn test_tampering_zeroes_the_patch() {
        let calibrator = scored_calibrator();
        let patch = PatchEvidence::new(PatchKind::Reference, all_ids()).with_test_tampering(true);
        assert_eq!(calibrator.score_patch(&patch).unwrap().normalized, 0.0);
    }

    #[test]
    fn five_control_classes_calibrate() {
        let calibrator = scored_calibrator();
        let patches = vec![
            PatchEvidence::new(PatchKind::Reference, all_ids()),
            PatchEvidence::new(PatchKind::Unmodified, vec![]),
            PatchEvidence::new(PatchKind::Inert, vec![]),
            PatchEvidence::new(PatchKind::Alternative, all_ids()),
            PatchEvidence::new(PatchKind::Adversarial, all_ids()).with_bypass(true),
        ];
        let report = calibrator.calibrate(&patches).unwrap();
        assert!(report.passed);
        assert_eq!(report.cases.len(), 5);
        assert_eq!(report.failures().count(), 0);
    }

    #[test]
    fn a_bad_control_patch_fails_calibration() {
        let calibrator = scored_calibrator();
        let patches = vec![PatchEvidence::new(PatchKind::Reference, vec![])];
        let report = calibrator.calibrate(&patches).unwrap();
        assert!(!report.passed);
        assert_eq!(report.failures().count(), 1);
    }

    #[test]
    fn invalid_weight_is_rejected() {
        let mut calibrator = ExpertRubricCalibrator::new();
        assert_eq!(
            calibrator.add(RubricCriterion::new("bad", 0.0, "bad")),
            Err(RubricError::InvalidWeight)
        );
        assert_eq!(
            calibrator.add(RubricCriterion::new("nan", f32::NAN, "nan")),
            Err(RubricError::InvalidWeight)
        );
    }

    #[test]
    fn duplicate_and_empty_fields_are_rejected() {
        let mut calibrator = ExpertRubricCalibrator::new();
        calibrator.add(RubricCriterion::new("a", 1.0, "a")).unwrap();
        assert_eq!(
            calibrator.add(RubricCriterion::new("a", 1.0, "a")),
            Err(RubricError::DuplicateCriterion)
        );
        assert_eq!(
            calibrator.add(RubricCriterion::new("", 1.0, "a")),
            Err(RubricError::EmptyField)
        );
        assert_eq!(
            calibrator.add(RubricCriterion::new("b", 1.0, "")),
            Err(RubricError::EmptyField)
        );
    }

    #[test]
    fn capacity_is_bounded() {
        let mut calibrator = ExpertRubricCalibrator::with_capacity(1);
        calibrator.add(RubricCriterion::new("a", 1.0, "a")).unwrap();
        assert_eq!(
            calibrator.add(RubricCriterion::new("b", 1.0, "b")),
            Err(RubricError::CapacityReached)
        );
    }
}
