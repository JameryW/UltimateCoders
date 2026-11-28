//! Cross-repository contract coordination (#46).
//!
//! Multi-repository workspaces are represented as a directed dependency graph:
//! an edge `consumer -> provider` means the consumer depends on a symbol
//! exported by the provider. Mutating an exported symbol forces every downstream
//! consumer to re-check the signature it compiled against, before the change is
//! allowed to flow into an integration checkpoint.
//!
//! The module is deterministic and in-memory. It does not run a build, clone a
//! repository, or query a package registry.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Default bound on registered repositories.
pub const DEFAULT_MAX_REPOS: usize = 64;
/// Default bound on dependency edges.
pub const DEFAULT_MAX_EDGES: usize = 512;

/// One symbol exported by a repository.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExportedSymbol {
    pub name: String,
    pub signature: String,
    pub version: u32,
}

/// A repository and its exported surface.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RepoContract {
    pub repo: String,
    pub exports: Vec<ExportedSymbol>,
}

impl RepoContract {
    fn export(&self, symbol: &str) -> Option<&ExportedSymbol> {
        self.exports.iter().find(|export| export.name == symbol)
    }
}

/// Why one repository depends on another.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DependencyKind {
    Source,
    Build,
    Configuration,
    TestContract,
}

/// A consumer's compile-time expectation about a provider symbol.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ConsumerDependency {
    pub consumer: String,
    pub provider: String,
    pub symbol: String,
    pub expected_signature: String,
    pub kind: DependencyKind,
    pub required: bool,
}

/// A provider-side exported-symbol mutation.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ContractChange {
    pub provider: String,
    pub symbol: String,
    pub old_signature: String,
    pub new_signature: String,
}

/// Result of re-checking one consumer against a provider change.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ContractCheckState {
    Compatible,
    Incompatible,
    MissingSymbol,
}

/// One downstream contract check.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ContractCheck {
    pub consumer: String,
    pub provider: String,
    pub symbol: String,
    pub expected_signature: String,
    pub observed_signature: String,
    pub kind: DependencyKind,
    pub required: bool,
    pub state: ContractCheckState,
}

impl ContractCheck {
    /// Whether this check blocks integration.
    pub fn blocked(&self) -> bool {
        self.required && self.state != ContractCheckState::Compatible
    }
}

/// The full propagation report for one exported-symbol change.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PropagationReport {
    pub change: ContractChange,
    pub applied_version: u32,
    pub affected_repos: Vec<String>,
    pub checks: Vec<ContractCheck>,
    pub all_required_compatible: bool,
}

impl PropagationReport {
    /// Whether the change may enter an integration checkpoint.
    pub fn clean(&self) -> bool {
        self.all_required_compatible
    }

    /// Consumers that must be updated before integration.
    pub fn blocked_consumers(&self) -> Vec<String> {
        self.checks
            .iter()
            .filter(|check| check.blocked())
            .map(|check| check.consumer.clone())
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect()
    }
}

/// Errors from the cross-repository contract graph.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum CrossRepoError {
    #[error("field `{field}` must not be empty")]
    EmptyField { field: &'static str },
    #[error("unknown repository `{0}`")]
    UnknownRepo(String),
    #[error("duplicate repository `{0}`")]
    DuplicateRepo(String),
    #[error("unknown exported symbol `{symbol}` in repository `{repo}`")]
    UnknownSymbol { repo: String, symbol: String },
    #[error("duplicate dependency edge `{consumer}` -> `{provider}` for `{symbol}`")]
    DuplicateDependency {
        consumer: String,
        provider: String,
        symbol: String,
    },
    #[error("a repository may not depend on itself")]
    SelfDependency,
    #[error("adding dependency `{consumer}` -> `{provider}` would create a cycle")]
    CycleDetected { consumer: String, provider: String },
    #[error("stale contract: expected `{expected}`, observed `{observed}`")]
    StaleContract { expected: String, observed: String },
    #[error("repository capacity reached")]
    CapacityReached,
    #[error("dependency capacity reached")]
    EdgeCapacityReached,
}

fn non_empty(field: &'static str, value: &str) -> Result<(), CrossRepoError> {
    if value.trim().is_empty() {
        Err(CrossRepoError::EmptyField { field })
    } else {
        Ok(())
    }
}

/// Bounded multi-repository contract graph.
#[derive(Debug, Clone)]
pub struct CrossRepoCoordinator {
    repos: BTreeMap<String, RepoContract>,
    dependencies: Vec<ConsumerDependency>,
    max_repos: usize,
    max_edges: usize,
}

impl Default for CrossRepoCoordinator {
    fn default() -> Self {
        Self::with_capacity(DEFAULT_MAX_REPOS, DEFAULT_MAX_EDGES)
    }
}

impl CrossRepoCoordinator {
    /// Build a coordinator with default bounds.
    pub fn new() -> Self {
        Self::default()
    }

    /// Build a coordinator with explicit repository and edge bounds.
    pub fn with_capacity(max_repos: usize, max_edges: usize) -> Self {
        Self {
            repos: BTreeMap::new(),
            dependencies: Vec::new(),
            max_repos,
            max_edges,
        }
    }

    /// Register a repository.
    pub fn register_repo(&mut self, repo: impl Into<String>) -> Result<(), CrossRepoError> {
        let repo = repo.into();
        non_empty("repo", &repo)?;
        if self.repos.contains_key(&repo) {
            return Err(CrossRepoError::DuplicateRepo(repo));
        }
        if self.repos.len() >= self.max_repos {
            return Err(CrossRepoError::CapacityReached);
        }
        self.repos.insert(
            repo.clone(),
            RepoContract {
                repo,
                exports: Vec::new(),
            },
        );
        Ok(())
    }

    /// Repositories in deterministic order.
    pub fn repos(&self) -> Vec<&str> {
        self.repos.keys().map(String::as_str).collect()
    }

    /// Dependency edges in insertion order.
    pub fn dependencies(&self) -> &[ConsumerDependency] {
        &self.dependencies
    }

    /// Export a symbol from an already-registered repository.
    pub fn declare_export(
        &mut self,
        repo: &str,
        symbol: impl Into<String>,
        signature: impl Into<String>,
    ) -> Result<(), CrossRepoError> {
        let symbol = symbol.into();
        let signature = signature.into();
        non_empty("symbol", &symbol)?;
        non_empty("signature", &signature)?;
        let contract = self
            .repos
            .get_mut(repo)
            .ok_or_else(|| CrossRepoError::UnknownRepo(repo.to_string()))?;
        if contract.export(&symbol).is_some() {
            return Err(CrossRepoError::DuplicateDependency {
                consumer: repo.to_string(),
                provider: repo.to_string(),
                symbol,
            });
        }
        contract.exports.push(ExportedSymbol {
            name: symbol,
            signature,
            version: 1,
        });
        Ok(())
    }

    /// Declare that `consumer` expects `expected_signature` from `provider`.
    pub fn add_dependency(
        &mut self,
        consumer: impl Into<String>,
        provider: impl Into<String>,
        symbol: impl Into<String>,
        expected_signature: impl Into<String>,
        kind: DependencyKind,
    ) -> Result<(), CrossRepoError> {
        let consumer = consumer.into();
        let provider = provider.into();
        let symbol = symbol.into();
        let expected_signature = expected_signature.into();
        non_empty("consumer", &consumer)?;
        non_empty("provider", &provider)?;
        non_empty("symbol", &symbol)?;
        non_empty("expected_signature", &expected_signature)?;
        if consumer == provider {
            return Err(CrossRepoError::SelfDependency);
        }
        if !self.repos.contains_key(&consumer) {
            return Err(CrossRepoError::UnknownRepo(consumer));
        }
        if !self.repos.contains_key(&provider) {
            return Err(CrossRepoError::UnknownRepo(provider));
        }
        if self.repos[&provider].export(&symbol).is_none() {
            return Err(CrossRepoError::UnknownSymbol {
                repo: provider,
                symbol,
            });
        }
        if self.dependencies.iter().any(|dependency| {
            dependency.consumer == consumer
                && dependency.provider == provider
                && dependency.symbol == symbol
        }) {
            return Err(CrossRepoError::DuplicateDependency {
                consumer,
                provider,
                symbol,
            });
        }
        if self.dependencies.len() >= self.max_edges {
            return Err(CrossRepoError::EdgeCapacityReached);
        }
        if self.reaches(&provider, &consumer) {
            return Err(CrossRepoError::CycleDetected { consumer, provider });
        }
        self.dependencies.push(ConsumerDependency {
            consumer,
            provider,
            symbol,
            expected_signature,
            kind,
            required: true,
        });
        Ok(())
    }

    /// Mark one edge optional (for backwards-compatible, soft migrations).
    pub fn mark_optional(
        &mut self,
        consumer: &str,
        provider: &str,
        symbol: &str,
    ) -> Result<(), CrossRepoError> {
        let dependency = self
            .dependencies
            .iter_mut()
            .find(|dependency| {
                dependency.consumer == consumer
                    && dependency.provider == provider
                    && dependency.symbol == symbol
            })
            .ok_or_else(|| CrossRepoError::UnknownSymbol {
                repo: provider.to_string(),
                symbol: symbol.to_string(),
            })?;
        dependency.required = false;
        Ok(())
    }

    fn reaches(&self, start: &str, target: &str) -> bool {
        let mut stack = vec![start.to_string()];
        let mut seen = BTreeSet::new();
        while let Some(node) = stack.pop() {
            if node == target {
                return true;
            }
            if !seen.insert(node.clone()) {
                continue;
            }
            for dependency in &self.dependencies {
                if dependency.consumer == node && !seen.contains(&dependency.provider) {
                    stack.push(dependency.provider.clone());
                }
            }
        }
        false
    }

    fn current_signature(&self, provider: &str, symbol: &str) -> Option<&ExportedSymbol> {
        self.repos
            .get(provider)
            .and_then(|repo| repo.export(symbol))
    }

    /// Apply an exported-symbol change and re-check every downstream consumer.
    pub fn propagate(
        &mut self,
        change: ContractChange,
    ) -> Result<PropagationReport, CrossRepoError> {
        non_empty("provider", &change.provider)?;
        non_empty("symbol", &change.symbol)?;
        non_empty("new_signature", &change.new_signature)?;
        let current = self
            .current_signature(&change.provider, &change.symbol)
            .ok_or_else(|| CrossRepoError::UnknownSymbol {
                repo: change.provider.clone(),
                symbol: change.symbol.clone(),
            })?;
        if current.signature != change.old_signature {
            return Err(CrossRepoError::StaleContract {
                expected: change.old_signature,
                observed: current.signature.clone(),
            });
        }
        let next_version = current.version.saturating_add(1);
        {
            let repo = self
                .repos
                .get_mut(&change.provider)
                .ok_or_else(|| CrossRepoError::UnknownRepo(change.provider.clone()))?;
            let export = repo
                .exports
                .iter_mut()
                .find(|export| export.name == change.symbol)
                .ok_or_else(|| CrossRepoError::UnknownSymbol {
                    repo: change.provider.clone(),
                    symbol: change.symbol.clone(),
                })?;
            export.signature = change.new_signature.clone();
            export.version = next_version;
        }
        let mut checks = Vec::new();
        for dependency in &self.dependencies {
            if dependency.provider != change.provider || dependency.symbol != change.symbol {
                continue;
            }
            let state = if dependency.expected_signature == change.new_signature {
                ContractCheckState::Compatible
            } else {
                ContractCheckState::Incompatible
            };
            checks.push(ContractCheck {
                consumer: dependency.consumer.clone(),
                provider: dependency.provider.clone(),
                symbol: dependency.symbol.clone(),
                expected_signature: dependency.expected_signature.clone(),
                observed_signature: change.new_signature.clone(),
                kind: dependency.kind,
                required: dependency.required,
                state,
            });
        }
        let mut affected_repos = checks
            .iter()
            .map(|check| check.consumer.clone())
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect::<Vec<_>>();
        affected_repos.sort();
        let all_required_compatible = checks.iter().all(|check| !check.blocked());
        Ok(PropagationReport {
            change,
            applied_version: next_version,
            affected_repos,
            checks,
            all_required_compatible,
        })
    }

    /// A deterministic topological order of repositories, if the graph is acyclic.
    pub fn topological_order(&self) -> Option<Vec<String>> {
        let mut indegree = BTreeMap::new();
        for repo in self.repos.keys() {
            indegree.insert(repo.clone(), 0usize);
        }
        for dependency in &self.dependencies {
            *indegree.entry(dependency.consumer.clone()).or_default() += 1;
        }
        let mut ready = indegree
            .iter()
            .filter(|(_, degree)| **degree == 0)
            .map(|(repo, _)| repo.clone())
            .collect::<BTreeSet<_>>();
        let mut order = Vec::with_capacity(self.repos.len());
        while let Some(repo) = ready.pop_first() {
            order.push(repo.clone());
            for dependency in &self.dependencies {
                if dependency.provider != repo {
                    continue;
                }
                if let Some(degree) = indegree.get_mut(&dependency.consumer) {
                    *degree = degree.saturating_sub(1);
                    if *degree == 0 {
                        ready.insert(dependency.consumer.clone());
                    }
                }
            }
        }
        (order.len() == self.repos.len()).then_some(order)
    }

    /// Number of exported symbols across all repositories.
    pub fn export_count(&self) -> usize {
        self.repos.values().map(|repo| repo.exports.len()).sum()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn coordinator() -> CrossRepoCoordinator {
        let mut coordinator = CrossRepoCoordinator::new();
        for repo in ["api", "service", "web"] {
            coordinator.register_repo(repo).expect("repo");
        }
        coordinator
            .declare_export("api", "fetch_user", "fn fetch_user(id: u64) -> User")
            .expect("api export");
        coordinator
            .declare_export("service", "render", "fn render(user: &User) -> String")
            .expect("service export");
        coordinator
    }

    fn change(old: &str, new: &str) -> ContractChange {
        ContractChange {
            provider: "api".to_string(),
            symbol: "fetch_user".to_string(),
            old_signature: old.to_string(),
            new_signature: new.to_string(),
        }
    }

    #[test]
    fn compatible_change_passes_all_consumers() {
        let mut coordinator = coordinator();
        coordinator
            .add_dependency(
                "service",
                "api",
                "fetch_user",
                "fn fetch_user(id: u64) -> User",
                DependencyKind::Source,
            )
            .expect("edge");
        let report = coordinator
            .propagate(change(
                "fn fetch_user(id: u64) -> User",
                "fn fetch_user(id: u64) -> User",
            ))
            .expect("propagate");
        assert!(report.clean());
        assert_eq!(report.applied_version, 2);
        assert_eq!(report.affected_repos, vec!["service"]);
        assert_eq!(report.checks[0].state, ContractCheckState::Compatible);
    }

    #[test]
    fn incompatible_change_blocks_the_consumer() {
        let mut coordinator = coordinator();
        coordinator
            .add_dependency(
                "service",
                "api",
                "fetch_user",
                "fn fetch_user(id: u64) -> User",
                DependencyKind::Source,
            )
            .expect("edge");
        let report = coordinator
            .propagate(change(
                "fn fetch_user(id: u64) -> User",
                "fn fetch_user(id: u64, tenant: &str) -> User",
            ))
            .expect("propagate");
        assert!(!report.clean());
        assert_eq!(report.blocked_consumers(), vec!["service"]);
    }

    #[test]
    fn optional_edges_do_not_block_integration() {
        let mut coordinator = coordinator();
        coordinator
            .add_dependency(
                "service",
                "api",
                "fetch_user",
                "fn fetch_user(id: u64) -> User",
                DependencyKind::TestContract,
            )
            .expect("edge");
        coordinator
            .mark_optional("service", "api", "fetch_user")
            .expect("optional");
        let report = coordinator
            .propagate(change(
                "fn fetch_user(id: u64) -> User",
                "fn fetch_user(id: u64, tenant: &str) -> User",
            ))
            .expect("propagate");
        assert!(report.clean());
        assert_eq!(report.checks[0].state, ContractCheckState::Incompatible);
    }

    #[test]
    fn stale_old_signature_is_rejected() {
        let mut coordinator = coordinator();
        let err = coordinator
            .propagate(change("fn old()", "fn new()"))
            .unwrap_err();
        assert!(matches!(err, CrossRepoError::StaleContract { .. }));
    }

    #[test]
    fn duplicate_and_self_dependencies_are_rejected() {
        let mut coordinator = coordinator();
        coordinator
            .add_dependency(
                "service",
                "api",
                "fetch_user",
                "sig",
                DependencyKind::Source,
            )
            .expect("edge");
        assert!(matches!(
            coordinator.add_dependency(
                "service",
                "api",
                "fetch_user",
                "sig",
                DependencyKind::Build
            ),
            Err(CrossRepoError::DuplicateDependency { .. })
        ));
        assert_eq!(
            coordinator
                .add_dependency("api", "api", "fetch_user", "sig", DependencyKind::Source)
                .unwrap_err(),
            CrossRepoError::SelfDependency
        );
    }

    #[test]
    fn cycles_are_rejected() {
        let mut coordinator = coordinator();
        coordinator
            .add_dependency(
                "service",
                "api",
                "fetch_user",
                "sig",
                DependencyKind::Source,
            )
            .expect("edge");
        let err = coordinator
            .add_dependency("api", "service", "render", "sig", DependencyKind::Source)
            .unwrap_err();
        assert!(matches!(err, CrossRepoError::CycleDetected { .. }));
    }

    #[test]
    fn unknown_repositories_and_symbols_are_rejected() {
        let mut coordinator = coordinator();
        assert!(matches!(
            coordinator.register_repo("api"),
            Err(CrossRepoError::DuplicateRepo(_))
        ));
        assert!(matches!(
            coordinator.add_dependency(
                "missing",
                "api",
                "fetch_user",
                "sig",
                DependencyKind::Source
            ),
            Err(CrossRepoError::UnknownRepo(_))
        ));
        assert!(matches!(
            coordinator.add_dependency(
                "service",
                "api",
                "missing_symbol",
                "sig",
                DependencyKind::Source
            ),
            Err(CrossRepoError::UnknownSymbol { .. })
        ));
    }

    #[test]
    fn topological_order_is_deterministic() {
        let mut coordinator = coordinator();
        coordinator
            .add_dependency("web", "service", "render", "sig", DependencyKind::Source)
            .expect("web edge");
        coordinator
            .add_dependency(
                "service",
                "api",
                "fetch_user",
                "sig",
                DependencyKind::Source,
            )
            .expect("service edge");
        let order = coordinator.topological_order().expect("acyclic");
        assert_eq!(order.first().map(String::as_str), Some("api"));
        assert_eq!(order.last().map(String::as_str), Some("web"));
    }

    #[test]
    fn capacity_bounds_are_enforced() {
        let mut coordinator = CrossRepoCoordinator::with_capacity(1, 1);
        coordinator.register_repo("api").expect("api");
        assert_eq!(
            coordinator.register_repo("service").unwrap_err(),
            CrossRepoError::CapacityReached
        );
        coordinator
            .declare_export("api", "fetch", "sig")
            .expect("export");
        coordinator
            .add_dependency("api", "api", "fetch", "sig", DependencyKind::Source)
            .expect_err("self edge does not consume capacity");
        let mut two = CrossRepoCoordinator::with_capacity(2, 1);
        two.register_repo("a").expect("a");
        two.register_repo("b").expect("b");
        two.declare_export("a", "x", "sig").expect("x");
        two.add_dependency("b", "a", "x", "sig", DependencyKind::Source)
            .expect("edge");
        assert_eq!(
            two.add_dependency("b", "a", "x", "sig2", DependencyKind::Build)
                .unwrap_err(),
            CrossRepoError::DuplicateDependency {
                consumer: "b".to_string(),
                provider: "a".to_string(),
                symbol: "x".to_string(),
            }
        );
    }
}
