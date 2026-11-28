//! Continuous benchmark budgets and trajectory-QA attribution (ledger item #90).
//!
//! A benchmark is treated as software: its resource assumptions are versioned,
//! every result carries a budget fingerprint, and model/harness/budget effects
//! can be attributed separately. The module is a pure ledger; callers run the
//! actual benchmark and supply measured results.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;

pub const DEFAULT_MAX_SPECS: usize = 64;
pub const DEFAULT_MAX_RESULTS: usize = 4096;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BenchmarkBudget {
    pub wall_clock_ms: u64,
    pub cpu_cores: f64,
    pub memory_mb: u64,
    pub gpu_count: u32,
}

impl BenchmarkBudget {
    pub fn new(
        wall_clock_ms: u64,
        cpu_cores: f64,
        memory_mb: u64,
        gpu_count: u32,
    ) -> Result<Self, ContinuousBenchError> {
        if wall_clock_ms == 0 {
            return Err(ContinuousBenchError("wall clock must be positive".into()));
        }
        if !cpu_cores.is_finite() || cpu_cores <= 0.0 {
            return Err(ContinuousBenchError("cpu cores must be positive".into()));
        }
        if memory_mb == 0 {
            return Err(ContinuousBenchError("memory must be positive".into()));
        }
        Ok(Self {
            wall_clock_ms,
            cpu_cores,
            memory_mb,
            gpu_count,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BenchmarkSpec {
    pub name: String,
    pub version: String,
    pub budget: BenchmarkBudget,
    pub assumptions: Vec<String>,
}

impl BenchmarkSpec {
    pub fn new(
        name: impl Into<String>,
        version: impl Into<String>,
        budget: BenchmarkBudget,
        assumptions: Vec<String>,
    ) -> Result<Self, ContinuousBenchError> {
        let name = name.into();
        let version = version.into();
        if name.trim().is_empty() || version.trim().is_empty() {
            return Err(ContinuousBenchError(
                "spec name and version are required".into(),
            ));
        }
        Ok(Self {
            name,
            version,
            budget,
            assumptions,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BenchmarkResult {
    pub model: String,
    pub harness: String,
    pub passed: u32,
    pub total: u32,
    pub wall_clock_ms: u64,
    pub budget_fingerprint: String,
}

impl BenchmarkResult {
    pub fn new(
        model: impl Into<String>,
        harness: impl Into<String>,
        passed: u32,
        total: u32,
        wall_clock_ms: u64,
        budget_fingerprint: impl Into<String>,
    ) -> Result<Self, ContinuousBenchError> {
        if passed > total {
            return Err(ContinuousBenchError("passed cannot exceed total".into()));
        }
        let model = model.into();
        let harness = harness.into();
        let budget_fingerprint = budget_fingerprint.into();
        if model.trim().is_empty() || harness.trim().is_empty() {
            return Err(ContinuousBenchError(
                "model and harness are required".into(),
            ));
        }
        if budget_fingerprint.trim().is_empty() {
            return Err(ContinuousBenchError(
                "budget fingerprint is required".into(),
            ));
        }
        Ok(Self {
            model,
            harness,
            passed,
            total,
            wall_clock_ms,
            budget_fingerprint,
        })
    }

    pub fn score(&self) -> f64 {
        if self.total == 0 {
            0.0
        } else {
            self.passed as f64 / self.total as f64
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CalibrationReport {
    pub spec_name: String,
    pub spec_version: String,
    pub budget_fingerprint: String,
    pub result_count: usize,
    pub mismatched_results: usize,
    pub mean_score: f64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum AttributionKind {
    Model,
    Harness,
    Budget,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Attribution {
    pub kind: AttributionKind,
    pub model_gap_pp: f64,
    pub harness_gap_pp: f64,
    pub budget_gap_pp: f64,
    pub explanation: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TrajectoryQaFinding {
    pub trajectory_id: String,
    pub assumption: String,
    pub budget_mismatch: bool,
    pub harness_gap_pp: i32,
    pub model_gap_pp: i32,
}

impl TrajectoryQaFinding {
    pub fn new(
        trajectory_id: impl Into<String>,
        assumption: impl Into<String>,
        budget_mismatch: bool,
        harness_gap_pp: i32,
        model_gap_pp: i32,
    ) -> Self {
        Self {
            trajectory_id: trajectory_id.into(),
            assumption: assumption.into(),
            budget_mismatch,
            harness_gap_pp,
            model_gap_pp,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ContinuousBenchError(pub String);

impl std::fmt::Display for ContinuousBenchError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for ContinuousBenchError {}

#[derive(Debug, Clone, Default)]
pub struct ContinuousBench {
    specs: HashMap<String, BenchmarkSpec>,
    results: Vec<BenchmarkResult>,
    max_specs: usize,
    max_results: usize,
}

impl ContinuousBench {
    pub fn new() -> Self {
        Self {
            specs: HashMap::new(),
            results: Vec::new(),
            max_specs: DEFAULT_MAX_SPECS,
            max_results: DEFAULT_MAX_RESULTS,
        }
    }

    pub fn with_capacity(max_specs: usize, max_results: usize) -> Self {
        Self {
            specs: HashMap::new(),
            results: Vec::new(),
            max_specs,
            max_results,
        }
    }

    pub fn register_spec(&mut self, spec: BenchmarkSpec) -> Result<(), ContinuousBenchError> {
        if self.specs.len() >= self.max_specs {
            return Err(ContinuousBenchError("spec capacity reached".into()));
        }
        if self.specs.contains_key(&spec.name) {
            return Err(ContinuousBenchError(format!(
                "duplicate spec: {}",
                spec.name
            )));
        }
        self.specs.insert(spec.name.clone(), spec);
        Ok(())
    }

    pub fn spec(&self, name: &str) -> Option<&BenchmarkSpec> {
        self.specs.get(name)
    }

    pub fn budget_fingerprint(&self, name: &str) -> Result<String, ContinuousBenchError> {
        let spec = self
            .specs
            .get(name)
            .ok_or_else(|| ContinuousBenchError(format!("unknown spec: {name}")))?;
        let material = format!(
            "{}:{}:{}:{}:{}:{}",
            spec.name,
            spec.version,
            spec.budget.wall_clock_ms,
            spec.budget.cpu_cores,
            spec.budget.memory_mb,
            spec.budget.gpu_count
        );
        Ok(blake3::hash(material.as_bytes()).to_hex().to_string())
    }

    pub fn record(
        &mut self,
        spec_name: &str,
        result: BenchmarkResult,
    ) -> Result<(), ContinuousBenchError> {
        let fingerprint = self.budget_fingerprint(spec_name)?;
        if result.budget_fingerprint != fingerprint {
            return Err(ContinuousBenchError("budget fingerprint mismatch".into()));
        }
        if self.results.len() >= self.max_results {
            return Err(ContinuousBenchError("result capacity reached".into()));
        }
        self.results.push(result);
        Ok(())
    }

    pub fn calibrate(&self, spec_name: &str) -> Result<CalibrationReport, ContinuousBenchError> {
        let spec = self
            .specs
            .get(spec_name)
            .ok_or_else(|| ContinuousBenchError(format!("unknown spec: {spec_name}")))?;
        let fingerprint = self.budget_fingerprint(spec_name)?;
        let matching: Vec<&BenchmarkResult> = self
            .results
            .iter()
            .filter(|result| result.budget_fingerprint == fingerprint)
            .collect();
        let mismatched = self
            .results
            .iter()
            .filter(|result| result.budget_fingerprint != fingerprint)
            .count();
        let mean_score = if matching.is_empty() {
            0.0
        } else {
            matching.iter().map(|item| item.score()).sum::<f64>() / matching.len() as f64
        };
        Ok(CalibrationReport {
            spec_name: spec.name.clone(),
            spec_version: spec.version.clone(),
            budget_fingerprint: fingerprint,
            result_count: matching.len(),
            mismatched_results: mismatched,
            mean_score,
        })
    }

    pub fn attribute(
        &self,
        model_gap_pp: f64,
        harness_gap_pp: f64,
        budget_gap_pp: f64,
    ) -> Attribution {
        let values = [
            (AttributionKind::Model, model_gap_pp.abs()),
            (AttributionKind::Harness, harness_gap_pp.abs()),
            (AttributionKind::Budget, budget_gap_pp.abs()),
        ];
        let (kind, _) = values
            .iter()
            .max_by(|left, right| left.1.total_cmp(&right.1))
            .copied()
            .unwrap_or((AttributionKind::Model, 0.0));
        let explanation = match kind {
            AttributionKind::Model => "largest residual aligns with model capability",
            AttributionKind::Harness => "largest residual aligns with harness behaviour",
            AttributionKind::Budget => "largest residual aligns with resource budget",
        };
        Attribution {
            kind,
            model_gap_pp,
            harness_gap_pp,
            budget_gap_pp,
            explanation: explanation.into(),
        }
    }

    pub fn audit_trajectory(&self, finding: TrajectoryQaFinding) -> Attribution {
        self.attribute(
            finding.model_gap_pp as f64,
            finding.harness_gap_pp as f64,
            if finding.budget_mismatch { 20.0 } else { 0.0 },
        )
    }

    pub fn results(&self) -> &[BenchmarkResult] {
        &self.results
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spec() -> BenchmarkSpec {
        BenchmarkSpec::new(
            "swe-lite",
            "1",
            BenchmarkBudget::new(30_000, 4.0, 8192, 0).unwrap(),
            vec!["fixed wall clock".into()],
        )
        .unwrap()
    }

    fn bench() -> ContinuousBench {
        let mut bench = ContinuousBench::new();
        bench.register_spec(spec()).unwrap();
        bench
    }

    #[test]
    fn budget_validation_rejects_zero_values() {
        assert!(BenchmarkBudget::new(0, 1.0, 1, 0).is_err());
        assert!(BenchmarkBudget::new(1, 0.0, 1, 0).is_err());
        assert!(BenchmarkBudget::new(1, 1.0, 0, 0).is_err());
    }

    #[test]
    fn fingerprint_is_stable_and_spec_specific() {
        let bench = bench();
        let first = bench.budget_fingerprint("swe-lite").unwrap();
        let second = bench.budget_fingerprint("swe-lite").unwrap();
        assert_eq!(first, second);
        assert_eq!(first.len(), 64);
    }

    #[test]
    fn record_requires_matching_fingerprint() {
        let mut bench = bench();
        let fingerprint = bench.budget_fingerprint("swe-lite").unwrap();
        let result = BenchmarkResult::new("m", "h", 8, 10, 12_000, fingerprint).unwrap();
        bench.record("swe-lite", result).unwrap();
        assert_eq!(bench.results().len(), 1);
    }

    #[test]
    fn wrong_fingerprint_is_rejected() {
        let mut bench = bench();
        let result = BenchmarkResult::new("m", "h", 8, 10, 12_000, "wrong").unwrap();
        assert!(bench.record("swe-lite", result).is_err());
    }

    #[test]
    fn calibrate_reports_mean_score_and_mismatch() {
        let mut bench = bench();
        let fingerprint = bench.budget_fingerprint("swe-lite").unwrap();
        bench
            .record(
                "swe-lite",
                BenchmarkResult::new("m", "h", 8, 10, 12_000, fingerprint.clone()).unwrap(),
            )
            .unwrap();
        let report = bench.calibrate("swe-lite").unwrap();
        assert_eq!(report.result_count, 1);
        assert_eq!(report.mismatched_results, 0);
        assert!((report.mean_score - 0.8).abs() < 1e-9);
    }

    #[test]
    fn attribution_picks_largest_gap() {
        let bench = bench();
        assert_eq!(
            bench.attribute(1.0, 9.0, 2.0).kind,
            AttributionKind::Harness
        );
        assert_eq!(bench.attribute(9.0, 1.0, 2.0).kind, AttributionKind::Model);
        assert_eq!(bench.attribute(1.0, 2.0, 9.0).kind, AttributionKind::Budget);
    }

    #[test]
    fn trajectory_qa_budget_mismatch_counts_as_twenty_points() {
        let bench = bench();
        let attribution = bench.audit_trajectory(TrajectoryQaFinding::new(
            "t1",
            "fixed wall clock",
            true,
            2,
            1,
        ));
        assert_eq!(attribution.kind, AttributionKind::Budget);
    }

    #[test]
    fn result_score_handles_zero_total() {
        let result = BenchmarkResult::new("m", "h", 0, 0, 1, "x").unwrap();
        assert_eq!(result.score(), 0.0);
    }

    #[test]
    fn duplicate_spec_is_rejected() {
        let mut bench = bench();
        assert!(bench.register_spec(spec()).is_err());
    }

    #[test]
    fn capacity_is_enforced() {
        let mut bench = ContinuousBench::with_capacity(1, 1);
        bench.register_spec(spec()).unwrap();
        let other = BenchmarkSpec::new(
            "other",
            "1",
            BenchmarkBudget::new(1, 1.0, 1, 0).unwrap(),
            vec![],
        )
        .unwrap();
        assert!(bench.register_spec(other).is_err());
    }
}
