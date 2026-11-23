//! Crustify-style Wavefront planning and Miri safety gate (ledger #112).
//!
//! This is a pure planning module: it orders FFI symbols by their hidden
//! pointer dependencies, reports binding coverage, and blocks a binding set
//! when Miri reports a reachable undefined behaviour. It never invokes a C
//! compiler, Miri, or a repository.

use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use thiserror::Error;

pub const DEFAULT_MAX_SYMBOLS: usize = 4096;

#[derive(Debug, Error, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum CrustifyError {
    #[error("symbol name must not be empty")]
    EmptyName,
    #[error("duplicate symbol: {0}")]
    DuplicateSymbol(String),
    #[error("symbol capacity reached")]
    CapacityReached,
    #[error("unknown dependency: {0}")]
    UnknownDependency(String),
    #[error("dependency cycle detected")]
    CycleDetected,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FfiSymbol {
    pub name: String,
    pub signature: String,
    pub dependencies: Vec<String>,
    pub returns_pointer: bool,
}

impl FfiSymbol {
    pub fn new(name: impl Into<String>, signature: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            signature: signature.into(),
            dependencies: Vec::new(),
            returns_pointer: false,
        }
    }

    pub fn with_dependencies(
        mut self,
        dependencies: impl IntoIterator<Item = impl Into<String>>,
    ) -> Self {
        self.dependencies = dependencies.into_iter().map(Into::into).collect();
        self
    }

    pub fn returning_pointer(mut self) -> Self {
        self.returns_pointer = true;
        self
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WavefrontPlan {
    pub waves: Vec<Vec<String>>,
}

impl WavefrontPlan {
    pub fn flattened(&self) -> Vec<String> {
        self.waves.iter().flatten().cloned().collect()
    }

    pub fn wave_of(&self, symbol: &str) -> Option<usize> {
        self.waves
            .iter()
            .position(|wave| wave.iter().any(|item| item == symbol))
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum UbKind {
    Aliasing,
    NullDeref,
    OutOfBounds,
    Uninitialized,
    Leak,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MiriFinding {
    pub symbol: String,
    pub kind: UbKind,
    pub evidence: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BindingInventory {
    pub public_api: usize,
    pub covered: usize,
    pub unsafe_surface: usize,
}

impl BindingInventory {
    pub fn coverage(&self) -> f64 {
        if self.public_api == 0 {
            0.0
        } else {
            self.covered as f64 / self.public_api as f64
        }
    }

    pub fn unsafe_ratio(&self) -> f64 {
        if self.covered == 0 {
            0.0
        } else {
            self.unsafe_surface as f64 / self.covered as f64
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CrustifyReport {
    pub plan: WavefrontPlan,
    pub inventory: BindingInventory,
    pub findings: Vec<MiriFinding>,
    pub passed: bool,
    pub reasons: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CrustifyWavefront {
    symbols: BTreeMap<String, FfiSymbol>,
    capacity: usize,
}

impl CrustifyWavefront {
    pub fn new() -> Self {
        Self::with_capacity(DEFAULT_MAX_SYMBOLS)
    }

    pub fn with_capacity(capacity: usize) -> Self {
        Self {
            symbols: BTreeMap::new(),
            capacity,
        }
    }

    pub fn register(&mut self, symbol: FfiSymbol) -> Result<(), CrustifyError> {
        if symbol.name.trim().is_empty() {
            return Err(CrustifyError::EmptyName);
        }
        if self.symbols.contains_key(&symbol.name) {
            return Err(CrustifyError::DuplicateSymbol(symbol.name));
        }
        if self.symbols.len() >= self.capacity {
            return Err(CrustifyError::CapacityReached);
        }
        self.symbols.insert(symbol.name.clone(), symbol);
        Ok(())
    }

    pub fn symbols(&self) -> impl Iterator<Item = &FfiSymbol> {
        self.symbols.values()
    }

    pub fn symbol_count(&self) -> usize {
        self.symbols.len()
    }

    pub fn plan(&self) -> Result<WavefrontPlan, CrustifyError> {
        let mut indegree: BTreeMap<String, usize> = BTreeMap::new();
        let mut dependents: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
        for symbol in self.symbols.values() {
            indegree.entry(symbol.name.clone()).or_insert(0);
            for dependency in &symbol.dependencies {
                if !self.symbols.contains_key(dependency) {
                    return Err(CrustifyError::UnknownDependency(dependency.clone()));
                }
                *indegree.entry(symbol.name.clone()).or_insert(0) += 1;
                dependents
                    .entry(dependency.clone())
                    .or_default()
                    .insert(symbol.name.clone());
            }
        }

        let mut waves = Vec::new();
        let mut ready: Vec<String> = indegree
            .iter()
            .filter(|(_, degree)| **degree == 0)
            .map(|(name, _)| name.clone())
            .collect();
        ready.sort();
        let mut visited = 0usize;
        while !ready.is_empty() {
            let wave = ready.clone();
            visited += wave.len();
            waves.push(wave.clone());
            let mut next = Vec::new();
            for name in wave {
                if let Some(children) = dependents.get(&name) {
                    for child in children {
                        if let Some(degree) = indegree.get_mut(child) {
                            *degree -= 1;
                            if *degree == 0 {
                                next.push(child.clone());
                            }
                        }
                    }
                }
            }
            next.sort();
            ready = next;
        }
        if visited != self.symbols.len() {
            return Err(CrustifyError::CycleDetected);
        }
        Ok(WavefrontPlan { waves })
    }
}

impl Default for CrustifyWavefront {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CrustifyVerifier {
    min_coverage: u32,
}

impl CrustifyVerifier {
    pub fn new(min_coverage_percent: u32) -> Self {
        Self {
            min_coverage: min_coverage_percent,
        }
    }

    pub fn default_verifier() -> Self {
        Self::new(95)
    }

    pub fn verify(
        &self,
        symbols: &[FfiSymbol],
        inventory: BindingInventory,
        findings: &[MiriFinding],
    ) -> Result<CrustifyReport, CrustifyError> {
        let mut wavefront = CrustifyWavefront::new();
        for symbol in symbols {
            wavefront.register(symbol.clone())?;
        }
        let plan = wavefront.plan()?;
        let mut reasons = Vec::new();
        if (inventory.coverage() * 100.0).round() < self.min_coverage as f64 {
            reasons.push(format!(
                "binding coverage {:.2}% is below {}%",
                inventory.coverage() * 100.0,
                self.min_coverage
            ));
        }
        if !findings.is_empty() {
            reasons.push(format!("{} reachable Miri finding(s)", findings.len()));
        }
        Ok(CrustifyReport {
            plan,
            inventory,
            findings: findings.to_vec(),
            passed: reasons.is_empty(),
            reasons,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn symbol(name: &str, deps: &[&str]) -> FfiSymbol {
        FfiSymbol::new(name, format!("fn {name}()")).with_dependencies(deps.iter().copied())
    }

    #[test]
    fn wavefront_orders_dependencies() {
        let mut wave = CrustifyWavefront::new();
        wave.register(symbol("a", &[])).unwrap();
        wave.register(symbol("b", &["a"])).unwrap();
        wave.register(symbol("c", &["b"])).unwrap();
        let plan = wave.plan().unwrap();
        assert_eq!(plan.waves, vec![vec!["a"], vec!["b"], vec!["c"]]);
    }

    #[test]
    fn independent_symbols_share_a_wave() {
        let mut wave = CrustifyWavefront::new();
        wave.register(symbol("b", &[])).unwrap();
        wave.register(symbol("a", &[])).unwrap();
        let plan = wave.plan().unwrap();
        assert_eq!(plan.waves, vec![vec!["a", "b"]]);
    }

    #[test]
    fn unknown_dependency_is_rejected() {
        let mut wave = CrustifyWavefront::new();
        wave.register(symbol("a", &["missing"])).unwrap();
        assert_eq!(
            wave.plan(),
            Err(CrustifyError::UnknownDependency("missing".to_string()))
        );
    }

    #[test]
    fn cycle_is_rejected() {
        let mut wave = CrustifyWavefront::new();
        wave.register(symbol("a", &["b"])).unwrap();
        wave.register(symbol("b", &["a"])).unwrap();
        assert_eq!(wave.plan(), Err(CrustifyError::CycleDetected));
    }

    #[test]
    fn duplicate_and_empty_names_are_rejected() {
        let mut wave = CrustifyWavefront::new();
        wave.register(symbol("a", &[])).unwrap();
        assert!(matches!(
            wave.register(symbol("a", &[])),
            Err(CrustifyError::DuplicateSymbol(_))
        ));
        assert_eq!(
            wave.register(symbol("", &[])),
            Err(CrustifyError::EmptyName)
        );
    }

    #[test]
    fn capacity_is_enforced() {
        let mut wave = CrustifyWavefront::with_capacity(1);
        wave.register(symbol("a", &[])).unwrap();
        assert_eq!(
            wave.register(symbol("b", &[])),
            Err(CrustifyError::CapacityReached)
        );
    }

    #[test]
    fn coverage_and_unsafe_ratio() {
        let inventory = BindingInventory {
            public_api: 100,
            covered: 99,
            unsafe_surface: 1,
        };
        assert_eq!(inventory.coverage(), 0.99);
        assert_eq!(inventory.unsafe_ratio(), 1.0 / 99.0);
    }

    #[test]
    fn clean_binding_passes() {
        let verifier = CrustifyVerifier::default_verifier();
        let report = verifier
            .verify(
                &[symbol("a", &[])],
                BindingInventory {
                    public_api: 10,
                    covered: 10,
                    unsafe_surface: 0,
                },
                &[],
            )
            .unwrap();
        assert!(report.passed);
        assert!(report.reasons.is_empty());
    }

    #[test]
    fn missing_coverage_fails() {
        let verifier = CrustifyVerifier::new(99);
        let report = verifier
            .verify(
                &[symbol("a", &[])],
                BindingInventory {
                    public_api: 10,
                    covered: 9,
                    unsafe_surface: 0,
                },
                &[],
            )
            .unwrap();
        assert!(!report.passed);
        assert!(report.reasons[0].contains("coverage"));
    }

    #[test]
    fn miri_finding_blocks_pass() {
        let verifier = CrustifyVerifier::new(0);
        let finding = MiriFinding {
            symbol: "a".into(),
            kind: UbKind::Aliasing,
            evidence: "two mutable pointers".into(),
        };
        let report = verifier
            .verify(
                &[symbol("a", &[])],
                BindingInventory {
                    public_api: 1,
                    covered: 1,
                    unsafe_surface: 0,
                },
                &[finding],
            )
            .unwrap();
        assert!(!report.passed);
        assert!(report.reasons[0].contains("Miri"));
    }

    #[test]
    fn plan_reports_wave_of_symbol() {
        let mut wave = CrustifyWavefront::new();
        wave.register(symbol("a", &[])).unwrap();
        wave.register(symbol("b", &["a"])).unwrap();
        let plan = wave.plan().unwrap();
        assert_eq!(plan.wave_of("b"), Some(1));
        assert_eq!(plan.flattened(), vec!["a", "b"]);
    }
}
