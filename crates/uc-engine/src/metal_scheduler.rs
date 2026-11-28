//! Cross-node Apple Silicon command scheduling and provenance gates
//! (ledger item #102, Metal Sandbox + Enforced Provenance).

use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use thiserror::Error;

pub const DEFAULT_PARALLEL_SLOTS: u32 = 4;
pub const DEFAULT_SERIAL_SLOTS: u32 = 1;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum CommandKind {
    Parallel,
    Serial,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct NodeSpec {
    pub node_id: String,
    pub parallel_slots: u32,
    pub serial_slots: u32,
}

impl NodeSpec {
    pub fn new(
        node_id: impl Into<String>,
        parallel_slots: u32,
        serial_slots: u32,
    ) -> Result<Self, MetalError> {
        let node_id = node_id.into();
        if node_id.trim().is_empty() {
            return Err(MetalError::EmptyNode);
        }
        if parallel_slots == 0 && serial_slots == 0 {
            return Err(MetalError::NoSlots);
        }
        Ok(Self {
            node_id,
            parallel_slots,
            serial_slots,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MetalCommand {
    pub command_id: String,
    pub kind: CommandKind,
    pub estimated_ms: u64,
}

impl MetalCommand {
    pub fn new(
        command_id: impl Into<String>,
        kind: CommandKind,
        estimated_ms: u64,
    ) -> Result<Self, MetalError> {
        let command_id = command_id.into();
        if command_id.trim().is_empty() {
            return Err(MetalError::EmptyCommand);
        }
        if estimated_ms == 0 {
            return Err(MetalError::ZeroDuration);
        }
        Ok(Self {
            command_id,
            kind,
            estimated_ms,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CommandAssignment {
    pub command_id: String,
    pub node_id: String,
    pub slot_index: u32,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SchedulePlan {
    pub assignments: Vec<CommandAssignment>,
    pub makespan_ms: u64,
    pub throughput_per_second: f64,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct MetalScheduler {
    nodes: Vec<NodeSpec>,
}

impl MetalScheduler {
    pub fn new(nodes: Vec<NodeSpec>) -> Result<Self, MetalError> {
        if nodes.is_empty() {
            return Err(MetalError::NoNodes);
        }
        let mut seen = BTreeSet::new();
        for node in &nodes {
            if !seen.insert(node.node_id.clone()) {
                return Err(MetalError::DuplicateNode(node.node_id.clone()));
            }
        }
        Ok(Self { nodes })
    }

    pub fn nodes(&self) -> &[NodeSpec] {
        &self.nodes
    }

    pub fn schedule(&self, commands: &[MetalCommand]) -> Result<SchedulePlan, MetalError> {
        if commands.is_empty() {
            return Ok(SchedulePlan {
                assignments: Vec::new(),
                makespan_ms: 0,
                throughput_per_second: 0.0,
            });
        }
        let mut parallel_load: BTreeMap<String, u64> = BTreeMap::new();
        let mut serial_load: BTreeMap<String, u64> = BTreeMap::new();
        let mut assignments = Vec::new();
        for command in commands {
            let candidate = self
                .nodes
                .iter()
                .filter(|node| match command.kind {
                    CommandKind::Parallel => node.parallel_slots > 0,
                    CommandKind::Serial => node.serial_slots > 0,
                })
                .min_by_key(|node| {
                    let load = match command.kind {
                        CommandKind::Parallel => {
                            parallel_load.get(&node.node_id).copied().unwrap_or(0)
                        }
                        CommandKind::Serial => serial_load.get(&node.node_id).copied().unwrap_or(0),
                    };
                    (load, node.node_id.clone())
                })
                .ok_or_else(|| MetalError::NoCompatibleNode(command.command_id.clone()))?;
            let load = match command.kind {
                CommandKind::Parallel => {
                    parallel_load.entry(candidate.node_id.clone()).or_insert(0)
                }
                CommandKind::Serial => serial_load.entry(candidate.node_id.clone()).or_insert(0),
            };
            let slot_index = match command.kind {
                CommandKind::Parallel => {
                    (*load / command.estimated_ms.max(1)) as u32 % candidate.parallel_slots.max(1)
                }
                CommandKind::Serial => 0,
            };
            *load += command.estimated_ms;
            assignments.push(CommandAssignment {
                command_id: command.command_id.clone(),
                node_id: candidate.node_id.clone(),
                slot_index,
            });
        }
        let mut makespan_ms = 0u64;
        for node in &self.nodes {
            makespan_ms = makespan_ms.max(parallel_load.get(&node.node_id).copied().unwrap_or(0));
            makespan_ms = makespan_ms.max(serial_load.get(&node.node_id).copied().unwrap_or(0));
        }
        let throughput_per_second = if makespan_ms == 0 {
            0.0
        } else {
            commands.len() as f64 / (makespan_ms as f64 / 1000.0)
        };
        Ok(SchedulePlan {
            assignments,
            makespan_ms,
            throughput_per_second,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReviewEvidence {
    pub provider: String,
    pub defects_found: u32,
}

impl ReviewEvidence {
    pub fn new(provider: impl Into<String>, defects_found: u32) -> Result<Self, MetalError> {
        let provider = provider.into();
        if provider.trim().is_empty() {
            return Err(MetalError::EmptyProvider);
        }
        Ok(Self {
            provider,
            defects_found,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ProvenanceReport {
    pub providers: Vec<String>,
    pub total_found: u32,
    pub coverage: f64,
    pub accepted: bool,
}

#[derive(Debug, Clone, Default)]
pub struct CrossProviderReview;

impl CrossProviderReview {
    pub fn evaluate(
        evidence: &[ReviewEvidence],
        injected_defects: u32,
    ) -> Result<ProvenanceReport, MetalError> {
        if injected_defects == 0 {
            return Err(MetalError::NoInjectedDefects);
        }
        let providers: BTreeSet<String> =
            evidence.iter().map(|item| item.provider.clone()).collect();
        if providers.len() < 2 {
            return Err(MetalError::InsufficientProviders);
        }
        let total_found: u32 = evidence.iter().map(|item| item.defects_found).sum();
        let coverage = (total_found as f64 / injected_defects as f64).min(1.0);
        Ok(ProvenanceReport {
            providers: providers.into_iter().collect(),
            total_found,
            coverage,
            accepted: coverage >= 0.5,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DecisionRecord {
    pub agent: String,
    pub approved: bool,
    pub evidence_digest: String,
}

#[derive(Debug, Clone, Default)]
pub struct CollusionMonitor {
    records: Vec<DecisionRecord>,
}

impl CollusionMonitor {
    pub fn observe(
        &mut self,
        agent: impl Into<String>,
        approved: bool,
        evidence_digest: impl Into<String>,
    ) -> Result<(), MetalError> {
        let agent = agent.into();
        if agent.trim().is_empty() {
            return Err(MetalError::EmptyProvider);
        }
        let evidence_digest = evidence_digest.into();
        if evidence_digest.trim().is_empty() {
            return Err(MetalError::EmptyDigest);
        }
        self.records.push(DecisionRecord {
            agent,
            approved,
            evidence_digest,
        });
        Ok(())
    }

    /// Fraction of agents that approved identical evidence digests while the
    /// rest approved distinct digests. High values identify suspicious
    /// lock-step approval.
    pub fn anomaly_score(&self) -> f64 {
        if self.records.is_empty() {
            return 0.0;
        }
        let mut by_digest: BTreeMap<&str, usize> = BTreeMap::new();
        for record in &self.records {
            if record.approved {
                *by_digest
                    .entry(record.evidence_digest.as_str())
                    .or_insert(0) += 1;
            }
        }
        let largest = by_digest.values().copied().max().unwrap_or(0);
        largest as f64 / self.records.len() as f64
    }

    pub fn records(&self) -> &[DecisionRecord] {
        &self.records
    }
}

#[derive(Debug, Clone, Error, PartialEq, Eq)]
pub enum MetalError {
    #[error("node id must not be empty")]
    EmptyNode,
    #[error("node must expose at least one slot")]
    NoSlots,
    #[error("command id must not be empty")]
    EmptyCommand,
    #[error("command duration must be positive")]
    ZeroDuration,
    #[error("scheduler needs at least one node")]
    NoNodes,
    #[error("duplicate node: {0}")]
    DuplicateNode(String),
    #[error("no compatible node for command: {0}")]
    NoCompatibleNode(String),
    #[error("provider name must not be empty")]
    EmptyProvider,
    #[error("injected defect count must be positive")]
    NoInjectedDefects,
    #[error("cross-provider review needs at least two distinct providers")]
    InsufficientProviders,
    #[error("decision evidence digest must not be empty")]
    EmptyDigest,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn node(id: &str, parallel: u32, serial: u32) -> NodeSpec {
        NodeSpec::new(id, parallel, serial).unwrap()
    }

    fn command(id: &str, kind: CommandKind, ms: u64) -> MetalCommand {
        MetalCommand::new(id, kind, ms).unwrap()
    }

    #[test]
    fn schedules_parallel_work_across_nodes() {
        let scheduler = MetalScheduler::new(vec![node("m1", 2, 1), node("m2", 2, 1)]).unwrap();
        let plan = scheduler
            .schedule(&[
                command("a", CommandKind::Parallel, 100),
                command("b", CommandKind::Parallel, 100),
            ])
            .unwrap();
        assert_eq!(plan.assignments.len(), 2);
        assert_eq!(plan.makespan_ms, 100);
        assert!(plan.throughput_per_second > 0.0);
    }

    #[test]
    fn serial_commands_use_serial_slots() {
        let scheduler = MetalScheduler::new(vec![node("m1", 2, 1)]).unwrap();
        let plan = scheduler
            .schedule(&[command("s", CommandKind::Serial, 50)])
            .unwrap();
        assert_eq!(plan.assignments[0].node_id, "m1");
    }

    #[test]
    fn empty_commands_produce_empty_plan() {
        let scheduler = MetalScheduler::new(vec![node("m1", 1, 1)]).unwrap();
        let plan = scheduler.schedule(&[]).unwrap();
        assert!(plan.assignments.is_empty());
        assert_eq!(plan.makespan_ms, 0);
    }

    #[test]
    fn duplicate_nodes_are_rejected() {
        assert_eq!(
            MetalScheduler::new(vec![node("m1", 1, 1), node("m1", 1, 1)]),
            Err(MetalError::DuplicateNode("m1".into()))
        );
    }

    #[test]
    fn empty_node_list_is_rejected() {
        assert_eq!(MetalScheduler::new(Vec::new()), Err(MetalError::NoNodes));
    }

    #[test]
    fn review_accepts_two_providers_with_high_coverage() {
        let report = CrossProviderReview::evaluate(
            &[
                ReviewEvidence::new("openai", 40).unwrap(),
                ReviewEvidence::new("anthropic", 24).unwrap(),
            ],
            65,
        )
        .unwrap();
        assert_eq!(report.total_found, 64);
        assert!(report.accepted);
        assert!(report.coverage > 0.98);
    }

    #[test]
    fn review_requires_two_providers() {
        assert_eq!(
            CrossProviderReview::evaluate(&[ReviewEvidence::new("openai", 10).unwrap()], 10),
            Err(MetalError::InsufficientProviders)
        );
    }

    #[test]
    fn review_requires_injected_defects() {
        assert_eq!(
            CrossProviderReview::evaluate(&[], 0),
            Err(MetalError::NoInjectedDefects)
        );
    }

    #[test]
    fn collusion_monitor_scores_lockstep_approvals() {
        let mut monitor = CollusionMonitor::default();
        monitor.observe("a", true, "same").unwrap();
        monitor.observe("b", true, "same").unwrap();
        monitor.observe("c", true, "different").unwrap();
        assert!((monitor.anomaly_score() - 2.0 / 3.0).abs() < 1e-12);
    }

    #[test]
    fn invalid_node_and_command_are_rejected() {
        assert_eq!(NodeSpec::new(" ", 1, 1), Err(MetalError::EmptyNode));
        assert_eq!(NodeSpec::new("m", 0, 0), Err(MetalError::NoSlots));
        assert_eq!(
            MetalCommand::new(" ", CommandKind::Parallel, 1),
            Err(MetalError::EmptyCommand)
        );
        assert_eq!(
            MetalCommand::new("a", CommandKind::Parallel, 0),
            Err(MetalError::ZeroDuration)
        );
    }
}
