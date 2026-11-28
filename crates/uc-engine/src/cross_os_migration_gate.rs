//! OS-Bench (#53): cross-OS coverage diffing and the dual-platform
//! migration gate.
//!
//! The gate never executes either platform's tests. It consumes the caller's
//! already-measured target and counter-platform outcomes, compares line-region
//! coverage to identify the five platform-specific region classes from
//! OS-Bench, and refuses a migration unless both test dimensions pass.

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Default upper bound for one coverage snapshot.
pub const DEFAULT_MAX_REGIONS: usize = 4096;

/// The two operating-system substrates the gate compares.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Platform {
    /// Linux running under the Docker sandbox.
    Linux,
    /// Windows running under PyWinpty.
    Windows,
}

impl Platform {
    /// The opposite platform.
    pub fn counter(self) -> Self {
        match self {
            Platform::Linux => Platform::Windows,
            Platform::Windows => Platform::Linux,
        }
    }
}

/// The five platform-specific code-region classes isolated by OS-Bench.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum RegionKind {
    /// Path separator or drive-letter handling.
    PathSeparator,
    /// Line-ending translation.
    LineEnding,
    /// Process creation, signals, or terminal control.
    ProcessControl,
    /// Case sensitivity, locking, or filesystem semantics.
    FileSystemSemantics,
    /// Native API or platform runtime binding.
    NativeApi,
}

/// One executed or unexecuted line region.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CoverageRegion {
    /// Source path relative to the repository root.
    pub path: String,
    /// Inclusive first line.
    pub start_line: u32,
    /// Inclusive last line.
    pub end_line: u32,
    /// The platform-specific class this region represents.
    pub kind: RegionKind,
    /// Hit count observed on Linux.
    pub linux_hits: u32,
    /// Hit count observed on Windows.
    pub windows_hits: u32,
}

impl CoverageRegion {
    /// Build one region record.
    pub fn new(
        path: impl Into<String>,
        start_line: u32,
        end_line: u32,
        kind: RegionKind,
        linux_hits: u32,
        windows_hits: u32,
    ) -> Result<Self, CrossOsError> {
        let path = path.into();
        if path.trim().is_empty() {
            return Err(CrossOsError::EmptyField("path"));
        }
        if start_line == 0 || end_line < start_line {
            return Err(CrossOsError::InvalidRange(start_line, end_line));
        }
        Ok(Self {
            path,
            start_line,
            end_line,
            kind,
            linux_hits,
            windows_hits,
        })
    }

    /// Whether the Linux run reached this region.
    pub fn linux_covered(&self) -> bool {
        self.linux_hits > 0
    }

    /// Whether the Windows run reached this region.
    pub fn windows_covered(&self) -> bool {
        self.windows_hits > 0
    }
}

/// A region whose coverage differs between the two substrates.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CoverageGap {
    /// Source path.
    pub path: String,
    /// Region class.
    pub kind: RegionKind,
    /// Linux hit count.
    pub linux_hits: u32,
    /// Windows hit count.
    pub windows_hits: u32,
}

impl CoverageGap {
    /// The gap is platform-specific when exactly one run reached the region.
    pub fn platform_specific(&self) -> bool {
        (self.linux_hits > 0) != (self.windows_hits > 0)
    }
}

/// Already-measured test outcome for one platform.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct OsTestOutcome {
    /// Tests that passed.
    pub passed: u32,
    /// Tests that failed.
    pub failed: u32,
}

impl OsTestOutcome {
    /// At least one test ran and none failed.
    pub fn clean(self) -> bool {
        self.passed > 0 && self.failed == 0
    }
}

/// A migration request with both sides' test evidence.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MigrationRequest {
    /// Stable task id.
    pub task_id: String,
    /// Platform the patch is migrating toward.
    pub target: Platform,
    /// Plain-text patch (or a deterministic patch digest wrapper).
    pub patch: String,
    /// Target-platform test outcome.
    pub target_tests: OsTestOutcome,
    /// Counter-platform regression-test outcome.
    pub counter_tests: OsTestOutcome,
    /// Coverage snapshot from the same run.
    pub regions: Vec<CoverageRegion>,
}

impl MigrationRequest {
    /// Build a request, validating the required fields.
    pub fn new(
        task_id: impl Into<String>,
        target: Platform,
        patch: impl Into<String>,
        target_tests: OsTestOutcome,
        counter_tests: OsTestOutcome,
        regions: Vec<CoverageRegion>,
    ) -> Result<Self, CrossOsError> {
        let task_id = task_id.into();
        let patch = patch.into();
        if task_id.trim().is_empty() {
            return Err(CrossOsError::EmptyField("task_id"));
        }
        if patch.trim().is_empty() {
            return Err(CrossOsError::EmptyField("patch"));
        }
        Ok(Self {
            task_id,
            target,
            patch,
            target_tests,
            counter_tests,
            regions,
        })
    }

    /// The platform that must be checked for counter-regressions.
    pub fn counter(&self) -> Platform {
        self.target.counter()
    }
}

/// The complete migration gate report.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MigrationReport {
    /// Task id copied from the request.
    pub task_id: String,
    /// Requested target platform.
    pub target: Platform,
    /// Counter platform that must be regression-tested.
    pub counter: Platform,
    /// Target tests passed.
    pub target_passed: bool,
    /// Counter-platform tests passed.
    pub counter_passed: bool,
    /// The migration may merge only when both dimensions pass.
    pub resolved: bool,
    /// Regions whose coverage differs between substrates.
    pub gaps: Vec<CoverageGap>,
    /// The subset of gaps that are platform-specific (one side unexecuted).
    pub platform_specific: Vec<CoverageGap>,
    /// Human-readable blocking reasons.
    pub reasons: Vec<String>,
}

impl MigrationReport {
    /// Both platforms were independently verified.
    pub fn dual_platform_verified(&self) -> bool {
        self.target_passed && self.counter_passed
    }

    /// Platform-specific regions that the patch did not exercise on both sides.
    pub fn unverified_platform_regions(&self) -> &[CoverageGap] {
        &self.platform_specific
    }
}

/// Errors raised while constructing the gate or its inputs.
#[derive(Debug, Error, PartialEq, Eq)]
pub enum CrossOsError {
    /// A required field was empty.
    #[error("field `{0}` must not be empty")]
    EmptyField(&'static str),
    /// A line range was inverted or started at zero.
    #[error("invalid line range {0}..={1}")]
    InvalidRange(u32, u32),
    /// The coverage snapshot exceeds the configured capacity.
    #[error("coverage capacity reached")]
    CapacityReached,
}

/// The gate over one fixed coverage snapshot and test policy.
#[derive(Debug, Clone)]
pub struct CrossOsMigrationGate {
    max_regions: usize,
    require_counter_regression: bool,
}

impl CrossOsMigrationGate {
    /// Build a gate with the default capacity and mandatory counter-regression.
    pub fn new() -> Self {
        Self {
            max_regions: DEFAULT_MAX_REGIONS,
            require_counter_regression: true,
        }
    }

    /// Build a gate with an explicit region capacity.
    pub fn with_capacity(max_regions: usize) -> Self {
        Self {
            max_regions,
            require_counter_regression: true,
        }
    }

    /// Allow the caller to waive the counter-regression suite. Even when
    /// waived, the report still records both outcomes.
    pub fn without_counter_regression(mut self) -> Self {
        self.require_counter_regression = false;
        self
    }

    /// Compare Linux and Windows coverage for one region snapshot.
    pub fn compare_coverage(
        &self,
        regions: &[CoverageRegion],
    ) -> Result<Vec<CoverageGap>, CrossOsError> {
        if regions.len() > self.max_regions {
            return Err(CrossOsError::CapacityReached);
        }
        Ok(regions
            .iter()
            .filter(|region| region.linux_hits != region.windows_hits)
            .map(|region| CoverageGap {
                path: region.path.clone(),
                kind: region.kind,
                linux_hits: region.linux_hits,
                windows_hits: region.windows_hits,
            })
            .collect())
    }

    /// Evaluate both test dimensions and the coverage diff.
    pub fn evaluate(&self, request: &MigrationRequest) -> Result<MigrationReport, CrossOsError> {
        if request.regions.len() > self.max_regions {
            return Err(CrossOsError::CapacityReached);
        }
        let target_passed = request.target_tests.clean();
        let counter_passed = request.counter_tests.clean();
        let gaps = self.compare_coverage(&request.regions)?;
        let platform_specific: Vec<CoverageGap> = gaps
            .iter()
            .filter(|gap| gap.platform_specific())
            .cloned()
            .collect();

        let mut reasons = Vec::new();
        if !target_passed {
            reasons.push(format!(
                "target {:?} tests did not pass ({}/{} passed)",
                request.target,
                request.target_tests.passed,
                request.target_tests.passed + request.target_tests.failed
            ));
        }
        if self.require_counter_regression && !counter_passed {
            reasons.push(format!(
                "counter-platform {:?} regression tests did not pass ({}/{} passed)",
                request.counter(),
                request.counter_tests.passed,
                request.counter_tests.passed + request.counter_tests.failed
            ));
        }
        if request.patch.trim().is_empty() {
            reasons.push("empty patch".to_string());
        }
        if !platform_specific.is_empty() {
            reasons.push(format!(
                "{} platform-specific region(s) were not exercised on both substrates",
                platform_specific.len()
            ));
        }

        let resolved = target_passed
            && (!self.require_counter_regression || counter_passed)
            && !request.patch.trim().is_empty()
            && platform_specific.is_empty();

        Ok(MigrationReport {
            task_id: request.task_id.clone(),
            target: request.target,
            counter: request.counter(),
            target_passed,
            counter_passed,
            resolved,
            gaps,
            platform_specific,
            reasons,
        })
    }
}

impl Default for CrossOsMigrationGate {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn region(kind: RegionKind, linux_hits: u32, windows_hits: u32) -> CoverageRegion {
        CoverageRegion::new("src/io.rs", 10, 20, kind, linux_hits, windows_hits).expect("region")
    }

    fn request(regions: Vec<CoverageRegion>) -> MigrationRequest {
        MigrationRequest::new(
            "os-task",
            Platform::Linux,
            "--- a/src/io.rs\n+++ b/src/io.rs\n@@ -1 +1 @@\n-old\n+new\n",
            OsTestOutcome {
                passed: 4,
                failed: 0,
            },
            OsTestOutcome {
                passed: 3,
                failed: 0,
            },
            regions,
        )
        .expect("request")
    }

    #[test]
    fn gate_defaults_and_platform_mapping() {
        let gate = CrossOsMigrationGate::new();
        assert!(gate.require_counter_regression);
        assert_eq!(Platform::Linux.counter(), Platform::Windows);
        assert_eq!(Platform::Windows.counter(), Platform::Linux);
    }

    #[test]
    fn coverage_diff_identifies_all_five_region_classes() {
        let gate = CrossOsMigrationGate::new();
        let kinds = [
            RegionKind::PathSeparator,
            RegionKind::LineEnding,
            RegionKind::ProcessControl,
            RegionKind::FileSystemSemantics,
            RegionKind::NativeApi,
        ];
        let regions: Vec<_> = kinds.iter().map(|kind| region(*kind, 1, 0)).collect();
        let gaps = gate.compare_coverage(&regions).expect("gaps");
        assert_eq!(gaps.len(), 5);
        assert!(gaps.iter().all(CoverageGap::platform_specific));
    }

    #[test]
    fn equal_coverage_is_not_a_gap() {
        let gate = CrossOsMigrationGate::new();
        let gaps = gate
            .compare_coverage(&[region(RegionKind::NativeApi, 2, 2)])
            .expect("gaps");
        assert!(gaps.is_empty());
    }

    #[test]
    fn one_sided_coverage_is_platform_specific() {
        let gap = CoverageGap {
            path: "src/io.rs".to_string(),
            kind: RegionKind::ProcessControl,
            linux_hits: 3,
            windows_hits: 0,
        };
        assert!(gap.platform_specific());
        let both = CoverageGap {
            linux_hits: 1,
            windows_hits: 2,
            ..gap.clone()
        };
        assert!(!both.platform_specific());
    }

    #[test]
    fn dual_platform_success_resolves_a_patch() {
        let gate = CrossOsMigrationGate::new();
        let report = gate
            .evaluate(&request(vec![region(RegionKind::LineEnding, 1, 1)]))
            .expect("report");
        assert!(report.resolved);
        assert!(report.dual_platform_verified());
        assert!(report.gaps.is_empty());
        assert!(report.reasons.is_empty());
    }

    #[test]
    fn target_test_failure_blocks_resolution() {
        let gate = CrossOsMigrationGate::new();
        let mut req = request(vec![region(RegionKind::NativeApi, 1, 1)]);
        req.target_tests = OsTestOutcome {
            passed: 2,
            failed: 1,
        };
        let report = gate.evaluate(&req).expect("report");
        assert!(!report.resolved);
        assert!(report
            .reasons
            .iter()
            .any(|reason| reason.contains("target")));
    }

    #[test]
    fn counter_platform_regression_failure_blocks_resolution() {
        let gate = CrossOsMigrationGate::new();
        let mut req = request(vec![region(RegionKind::NativeApi, 1, 1)]);
        req.counter_tests = OsTestOutcome {
            passed: 2,
            failed: 1,
        };
        let report = gate.evaluate(&req).expect("report");
        assert!(!report.resolved);
        assert!(!report.counter_passed);
        assert!(report
            .reasons
            .iter()
            .any(|reason| reason.contains("counter-platform")));
    }

    #[test]
    fn counter_regression_can_be_waived_explicitly() {
        let gate = CrossOsMigrationGate::new().without_counter_regression();
        let mut req = request(vec![region(RegionKind::NativeApi, 1, 1)]);
        req.counter_tests = OsTestOutcome {
            passed: 0,
            failed: 0,
        };
        let report = gate.evaluate(&req).expect("report");
        assert!(report.resolved);
        assert!(!report.counter_passed);
    }

    #[test]
    fn empty_patch_and_platform_specific_gaps_block_resolution() {
        let req = MigrationRequest::new(
            "os-task",
            Platform::Windows,
            "",
            OsTestOutcome {
                passed: 1,
                failed: 0,
            },
            OsTestOutcome {
                passed: 1,
                failed: 0,
            },
            vec![region(RegionKind::PathSeparator, 0, 1)],
        )
        .expect_err("empty patch");
        assert_eq!(req, CrossOsError::EmptyField("patch"));
    }

    #[test]
    fn platform_specific_gap_is_reported_and_blocks() {
        let gate = CrossOsMigrationGate::new();
        let report = gate
            .evaluate(&request(vec![region(RegionKind::PathSeparator, 0, 2)]))
            .expect("report");
        assert!(!report.resolved);
        assert_eq!(report.platform_specific.len(), 1);
        assert_eq!(report.unverified_platform_regions().len(), 1);
        assert!(report
            .reasons
            .iter()
            .any(|reason| reason.contains("platform-specific")));
    }

    #[test]
    fn capacity_and_range_are_validated() {
        let gate = CrossOsMigrationGate::with_capacity(1);
        let regions = vec![
            region(RegionKind::NativeApi, 1, 1),
            region(RegionKind::LineEnding, 1, 1),
        ];
        assert_eq!(
            gate.compare_coverage(&regions),
            Err(CrossOsError::CapacityReached)
        );
        assert_eq!(
            CoverageRegion::new("src/io.rs", 4, 3, RegionKind::NativeApi, 0, 0),
            Err(CrossOsError::InvalidRange(4, 3))
        );
    }
}
