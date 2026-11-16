//! Flow-aware cumulative regression obligations (#17).
//!
//! A task graph is not a flat list of independent nodes: a node executed
//! downstream must not be allowed to silently regress tests that an ancestor
//! already made pass. [`CumulativeRegressionPlan`] models this by propagating
//! every node's own fail-to-pass (F2P) tests to all of its descendants.
//!
//! ```text
//!   A (f2p: a1, a2)
//!   ├── B (f2p: b1)
//!   │   └── D (f2p: d1)   D inherits {a1, a2, b1} + d1
//!   └── C (f2p: c1)
//!       └── D              D inherits {a1, a2, c1} + d1
//! ```
//!
//! The plan is purely structural — it does not execute tests. Its job is to
//! answer three questions deterministically:
//!
//! * [`CumulativeRegressionPlan::obligations_for`] — which tests an executing
//!   node must keep green, and which ancestors each obligation came from.
//! * [`CumulativeRegressionPlan::blocks_completion`] — whether the node's
//!   failed-test set intersects a blocking obligation.
//! * [`CumulativeRegressionPlan::inject_contracts`] — the per-ancestor test
//!   contract to inject into the node's prompt/handoff, so the downstream
//!   agent sees exactly what it must not break.
//!
//! Parent edges may be registered before their parent node exists (forward
//! references); such parents are treated as leaves until added. Every
//! collection is a [`BTreeSet`]/[`BTreeMap`], so cycle detection, inheritance
//! and error reporting are stable across runs.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// The cumulative regression obligation attached to one node.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RegressionObligation {
    /// Node this obligation belongs to.
    pub node_id: String,
    /// Own F2P tests plus every ancestor's F2P tests (deduplicated).
    pub test_ids: BTreeSet<String>,
    /// Ancestor node ids that contributed at least one test to `test_ids`.
    pub inherited_from: BTreeSet<String>,
    /// Whether a failure in `test_ids` must block completion.
    pub blocking: bool,
}

impl RegressionObligation {
    /// Whether `failed_tests` intersects the blocking obligation.
    pub fn violated_by(&self, failed_tests: &BTreeSet<String>) -> bool {
        self.blocking && !failed_tests.is_disjoint(&self.test_ids)
    }
}

/// Why a node could not be added to a [`CumulativeRegressionPlan`].
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum RegressionPlanError {
    #[error("regression plan node id must not be empty")]
    EmptyNodeId,
    #[error("regression plan already contains node `{node_id}`")]
    DuplicateNode { node_id: String },
    #[error("adding node `{node_id}` under parent `{parent}` would create a cycle")]
    CycleDetected { node_id: String, parent: String },
}

#[derive(Debug, Clone, Default)]
struct PlanNode {
    parents: BTreeSet<String>,
    own_f2p_tests: BTreeSet<String>,
}

/// DAG of nodes and their own F2P tests; derives cumulative obligations.
#[derive(Debug, Clone, Default)]
pub struct CumulativeRegressionPlan {
    nodes: BTreeMap<String, PlanNode>,
}

impl CumulativeRegressionPlan {
    pub fn new() -> Self {
        Self::default()
    }

    /// Register one node with its parent edges and own F2P tests.
    ///
    /// Parents may reference nodes not yet added (forward edges). Returns
    /// [`RegressionPlanError::CycleDetected`] deterministically when the new
    /// edges would close a cycle, including a direct self-edge.
    pub fn add_node<P, T>(
        &mut self,
        node_id: impl Into<String>,
        parents: P,
        own_f2p_tests: T,
    ) -> Result<(), RegressionPlanError>
    where
        P: IntoIterator,
        P::Item: Into<String>,
        T: IntoIterator,
        T::Item: Into<String>,
    {
        let node_id = node_id.into();
        if node_id.trim().is_empty() {
            return Err(RegressionPlanError::EmptyNodeId);
        }
        if self.nodes.contains_key(&node_id) {
            return Err(RegressionPlanError::DuplicateNode { node_id });
        }

        let parents: BTreeSet<String> = parents.into_iter().map(Into::into).collect();
        let own_f2p_tests: BTreeSet<String> = own_f2p_tests.into_iter().map(Into::into).collect();

        // Validate in sorted parent order so the reported cycle is stable.
        for parent in &parents {
            if parent == &node_id {
                return Err(RegressionPlanError::CycleDetected {
                    node_id,
                    parent: parent.clone(),
                });
            }
            // A cycle exists when `node_id` is already reachable from `parent`
            // through the existing (forward-reference tolerant) edges.
            if self.reaches(parent, &node_id) {
                return Err(RegressionPlanError::CycleDetected {
                    node_id,
                    parent: parent.clone(),
                });
            }
        }

        self.nodes.insert(
            node_id,
            PlanNode {
                parents,
                own_f2p_tests,
            },
        );
        Ok(())
    }

    /// Whether `from` can reach `target` by walking parent edges.
    fn reaches(&self, from: &str, target: &str) -> bool {
        let mut stack = vec![from];
        let mut visited = BTreeSet::new();
        while let Some(current) = stack.pop() {
            if current == target {
                return true;
            }
            if !visited.insert(current.to_string()) {
                continue;
            }
            if let Some(node) = self.nodes.get(current) {
                for parent in &node.parents {
                    stack.push(parent.as_str());
                }
            }
        }
        false
    }

    /// All topological ancestors of `node_id` (transitively), in sorted order.
    /// Returns `None` for an unknown node.
    pub fn ancestors_of(&self, node_id: &str) -> Option<BTreeSet<String>> {
        self.nodes.get(node_id)?;
        let mut ancestors = BTreeSet::new();
        let mut stack: Vec<&str> = self
            .nodes
            .get(node_id)
            .map(|node| node.parents.iter().map(String::as_str).collect())
            .unwrap_or_default();
        while let Some(current) = stack.pop() {
            if !ancestors.insert(current.to_string()) {
                continue;
            }
            if let Some(node) = self.nodes.get(current) {
                for parent in &node.parents {
                    stack.push(parent.as_str());
                }
            }
        }
        Some(ancestors)
    }

    /// The cumulative regression obligation for `node_id`.
    ///
    /// `test_ids` = own F2P tests ∪ every ancestor's F2P tests.
    /// `inherited_from` = ancestors that contributed at least one test.
    /// `blocking` = the obligation contains at least one test.
    /// Returns `None` for an unknown node.
    pub fn obligations_for(&self, node_id: &str) -> Option<RegressionObligation> {
        let node = self.nodes.get(node_id)?;
        let mut test_ids = node.own_f2p_tests.clone();
        let mut inherited_from = BTreeSet::new();
        for ancestor_id in self.ancestors_of(node_id)? {
            if let Some(ancestor) = self.nodes.get(&ancestor_id) {
                if !ancestor.own_f2p_tests.is_empty() {
                    test_ids.extend(ancestor.own_f2p_tests.iter().cloned());
                    inherited_from.insert(ancestor_id);
                }
            }
        }
        Some(RegressionObligation {
            node_id: node_id.to_string(),
            blocking: !test_ids.is_empty(),
            test_ids,
            inherited_from,
        })
    }

    /// Whether `node_id` must not complete: one of its blocking obligations
    /// intersects `failed_tests`. Unknown nodes return `false`.
    pub fn blocks_completion(&self, node_id: &str, failed_tests: &BTreeSet<String>) -> bool {
        self.obligations_for(node_id)
            .map(|obligation| obligation.violated_by(failed_tests))
            .unwrap_or(false)
    }

    /// The ancestor test contracts to inject into `node_id`'s execution:
    /// ancestor node id → that ancestor's own F2P tests. Ancestors with no
    /// own tests are omitted. Unknown nodes yield an empty map.
    pub fn inject_contracts(&self, node_id: &str) -> BTreeMap<String, BTreeSet<String>> {
        let mut contracts = BTreeMap::new();
        let Some(ancestors) = self.ancestors_of(node_id) else {
            return contracts;
        };
        for ancestor_id in ancestors {
            if let Some(ancestor) = self.nodes.get(&ancestor_id) {
                if !ancestor.own_f2p_tests.is_empty() {
                    contracts.insert(ancestor_id, ancestor.own_f2p_tests.clone());
                }
            }
        }
        contracts
    }

    /// Number of registered nodes.
    pub fn len(&self) -> usize {
        self.nodes.len()
    }

    pub fn is_empty(&self) -> bool {
        self.nodes.is_empty()
    }

    pub fn contains(&self, node_id: &str) -> bool {
        self.nodes.contains_key(node_id)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn set(items: &[&str]) -> BTreeSet<String> {
        items.iter().map(|item| item.to_string()).collect()
    }

    #[test]
    fn chain_inherits_all_ancestor_tests() {
        let mut plan = CumulativeRegressionPlan::new();
        plan.add_node("A", Vec::<String>::new(), ["a1", "a2"])
            .unwrap();
        plan.add_node("B", ["A"], ["b1"]).unwrap();
        plan.add_node("C", ["B"], ["c1"]).unwrap();

        let obligation = plan.obligations_for("C").unwrap();
        assert_eq!(obligation.node_id, "C");
        assert_eq!(obligation.test_ids, set(&["a1", "a2", "b1", "c1"]));
        assert_eq!(obligation.inherited_from, set(&["A", "B"]));
        assert!(obligation.blocking);

        let b = plan.obligations_for("B").unwrap();
        assert_eq!(b.test_ids, set(&["a1", "a2", "b1"]));
        assert_eq!(b.inherited_from, set(&["A"]));

        let a = plan.obligations_for("A").unwrap();
        assert_eq!(a.test_ids, set(&["a1", "a2"]));
        assert!(a.inherited_from.is_empty());

        assert_eq!(plan.obligations_for("missing"), None);
        assert_eq!(plan.len(), 3);
    }

    #[test]
    fn diamond_inherits_every_branch_once() {
        let mut plan = CumulativeRegressionPlan::new();
        plan.add_node("A", Vec::<String>::new(), ["a1"]).unwrap();
        plan.add_node("B", ["A"], ["b1"]).unwrap();
        plan.add_node("C", ["A"], ["c1"]).unwrap();
        plan.add_node("D", ["C", "B"], ["d1"]).unwrap();

        let obligation = plan.obligations_for("D").unwrap();
        assert_eq!(obligation.test_ids, set(&["a1", "b1", "c1", "d1"]));
        assert_eq!(obligation.inherited_from, set(&["A", "B", "C"]));
        assert_eq!(
            plan.inject_contracts("D"),
            BTreeMap::from([
                ("A".to_string(), set(&["a1"])),
                ("B".to_string(), set(&["b1"])),
                ("C".to_string(), set(&["c1"])),
            ])
        );
    }

    #[test]
    fn duplicate_and_self_parent_are_rejected() {
        let mut plan = CumulativeRegressionPlan::new();
        plan.add_node("A", Vec::<String>::new(), ["a1"]).unwrap();
        assert_eq!(
            plan.add_node("A", Vec::<String>::new(), ["a2"]),
            Err(RegressionPlanError::DuplicateNode {
                node_id: "A".to_string()
            })
        );
        assert!(matches!(
            plan.add_node("B", ["B"], Vec::<String>::new()),
            Err(RegressionPlanError::CycleDetected { .. })
        ));
        assert_eq!(
            plan.add_node("", Vec::<String>::new(), Vec::<String>::new()),
            Err(RegressionPlanError::EmptyNodeId)
        );
    }

    #[test]
    fn cycle_is_detected_deterministically_even_with_forward_edges() {
        let mut plan = CumulativeRegressionPlan::new();
        // A references C before C exists.
        plan.add_node("A", ["C"], ["a1"]).unwrap();
        plan.add_node("B", ["A"], ["b1"]).unwrap();
        let error = plan.add_node("C", ["B"], ["c1"]).unwrap_err();
        assert_eq!(
            error,
            RegressionPlanError::CycleDetected {
                node_id: "C".to_string(),
                parent: "B".to_string()
            }
        );
        assert_eq!(plan.len(), 2);
    }

    #[test]
    fn blocking_uses_cumulative_failed_tests() {
        let mut plan = CumulativeRegressionPlan::new();
        plan.add_node("A", Vec::<String>::new(), ["a1"]).unwrap();
        plan.add_node("B", ["A"], ["b1"]).unwrap();

        assert!(
            plan.blocks_completion("B", &set(&["a1"])),
            "ancestor test must block"
        );
        assert!(
            plan.blocks_completion("B", &set(&["b1"])),
            "own test must block"
        );
        assert!(
            !plan.blocks_completion("B", &set(&["unrelated"])),
            "non-obligation must not block"
        );
        assert!(!plan.blocks_completion("B", &BTreeSet::new()));
        assert!(!plan.blocks_completion("missing", &set(&["a1"])));
    }

    #[test]
    fn inject_contracts_only_returns_ancestors_with_tests() {
        let mut plan = CumulativeRegressionPlan::new();
        plan.add_node("root", Vec::<String>::new(), Vec::<String>::new())
            .unwrap();
        plan.add_node("mid", ["root"], ["m1"]).unwrap();
        plan.add_node("leaf", ["mid"], ["l1"]).unwrap();

        let contracts = plan.inject_contracts("leaf");
        assert_eq!(contracts.len(), 1);
        assert_eq!(contracts.get("mid"), Some(&set(&["m1"])));
        assert!(plan.inject_contracts("missing").is_empty());
    }
}
