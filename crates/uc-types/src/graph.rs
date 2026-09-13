//! Graph-plane node state machine (T3, tracker #639).
//!
//! The nine canonical node states of the durable graph runtime, plus the
//! **pure** legal-transition table. The graph store (`uc-engine`) and the
//! gateway's sink fan-out share this single definition, so "is this edge
//! legal?" is answered in exactly one place.
//!
//! Serde uses the uppercase token strings (`"READY"`, `"SUCCEEDED"`, …) —
//! byte-identical with the `graph_nodes.state` column values written since
//! T2, so the enum round-trips against rows the importers already produced
//! (`NodeStatus::from_token` is the parser for that vocabulary).

use serde::{Deserialize, Serialize};

/// Canonical graph-node states.
///
/// Lifecycle: `CREATED → READY → SCHEDULED → RUNNING → SUCCEEDED`, with
/// `WAITING` as a suspension off `RUNNING`, `FAILED → READY` as the
/// retry-budget edge, and `CANCELLED` / `SKIPPED` as the terminal exits that
/// planning (not execution) decides.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum NodeStatus {
    /// Declared, dependencies not (yet) all satisfied.
    Created,
    /// All dependencies satisfied; schedulable.
    Ready,
    /// Dispatched to a worker, not yet reported running.
    Scheduled,
    /// An attempt is executing.
    Running,
    /// Execution suspended awaiting an external condition.
    Waiting,
    /// Committed exactly once (the `node_completions` winner).
    Succeeded,
    /// Retry budget exhausted; terminal for this graph run.
    Failed,
    /// Explicitly cancelled.
    Cancelled,
    /// Optional node skipped (planning decision, terminal).
    Skipped,
}

impl NodeStatus {
    /// The uppercase token stored in `graph_nodes.state` and serialized on
    /// the wire.
    pub fn as_str(self) -> &'static str {
        match self {
            NodeStatus::Created => "CREATED",
            NodeStatus::Ready => "READY",
            NodeStatus::Scheduled => "SCHEDULED",
            NodeStatus::Running => "RUNNING",
            NodeStatus::Waiting => "WAITING",
            NodeStatus::Succeeded => "SUCCEEDED",
            NodeStatus::Failed => "FAILED",
            NodeStatus::Cancelled => "CANCELLED",
            NodeStatus::Skipped => "SKIPPED",
        }
    }

    /// Parse an exact uppercase state token. Anything else (including the
    /// T2 shadow-mirror-only tokens `PAUSED` / `PLANNING` and unknown
    /// passthroughs) yields `None` — the state machine never invents a
    /// missing state.
    pub fn from_token(token: &str) -> Option<NodeStatus> {
        match token {
            "CREATED" => Some(NodeStatus::Created),
            "READY" => Some(NodeStatus::Ready),
            "SCHEDULED" => Some(NodeStatus::Scheduled),
            "RUNNING" => Some(NodeStatus::Running),
            "WAITING" => Some(NodeStatus::Waiting),
            "SUCCEEDED" => Some(NodeStatus::Succeeded),
            "FAILED" => Some(NodeStatus::Failed),
            "CANCELLED" => Some(NodeStatus::Cancelled),
            "SKIPPED" => Some(NodeStatus::Skipped),
            _ => None,
        }
    }

    /// Terminal states have no outgoing edges.
    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            NodeStatus::Succeeded | NodeStatus::Cancelled | NodeStatus::Skipped
        )
    }
}

impl std::fmt::Display for NodeStatus {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

impl std::str::FromStr for NodeStatus {
    type Err = String;
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        NodeStatus::from_token(s).ok_or_else(|| format!("unknown node state token: {s}"))
    }
}

/// The complete set of legal node transitions (T3 PRD, assessment D1).
///
/// Deliberately a private edge list so `can_transition` and the exhaustive
/// unit test share one source of truth. `FAILED → READY` is the retry path
/// (the timeout sweep re-arms through `RUNNING → READY` directly);
/// `RUNNING → READY` is the fence-and-rearm edge (failed/timed-out attempt,
/// retry budget left); no self-loops — a no-op is not a transition.
fn legal_edges() -> [(NodeStatus, NodeStatus); 17] {
    use NodeStatus::*;
    [
        (Created, Ready),
        (Created, Cancelled),
        (Created, Skipped),
        (Ready, Scheduled),
        (Ready, Cancelled),
        (Ready, Skipped),
        (Scheduled, Running),
        (Scheduled, Ready),
        (Scheduled, Cancelled),
        (Running, Waiting),
        (Running, Succeeded),
        (Running, Failed),
        (Running, Ready),
        (Running, Cancelled),
        (Waiting, Running),
        (Waiting, Failed),
        (Failed, Ready),
    ]
}

/// Whether `from → to` is a legal node transition. Self-transitions and every
/// edge out of a terminal state are rejected.
pub fn can_transition(from: NodeStatus, to: NodeStatus) -> bool {
    legal_edges().contains(&(from, to))
}

#[cfg(test)]
mod tests {
    use super::*;

    const ALL: [NodeStatus; 9] = [
        NodeStatus::Created,
        NodeStatus::Ready,
        NodeStatus::Scheduled,
        NodeStatus::Running,
        NodeStatus::Waiting,
        NodeStatus::Succeeded,
        NodeStatus::Failed,
        NodeStatus::Cancelled,
        NodeStatus::Skipped,
    ];

    #[test]
    fn node_status_serializes_to_uppercase_graph_tokens() {
        for status in ALL {
            let json = serde_json::to_string(&status).unwrap();
            assert_eq!(json, format!("\"{}\"", status.as_str()));
            let back: NodeStatus = serde_json::from_str(&json).unwrap();
            assert_eq!(back, status);
            // The token parser and serde agree — one vocabulary for the DB
            // column and the wire.
            assert_eq!(NodeStatus::from_token(status.as_str()), Some(status));
        }
        assert_eq!(
            serde_json::to_string(&NodeStatus::Succeeded).unwrap(),
            "\"SUCCEEDED\""
        );
        assert_eq!(
            serde_json::to_string(&NodeStatus::Created).unwrap(),
            "\"CREATED\""
        );
    }

    #[test]
    fn from_token_rejects_non_state_machine_tokens() {
        // T2 shadow vocabulary that is NOT part of the 9-state machine.
        assert_eq!(NodeStatus::from_token("PAUSED"), None);
        assert_eq!(NodeStatus::from_token("PLANNING"), None);
        // Case-sensitive: the lowercase legacy wire forms must not parse.
        assert_eq!(NodeStatus::from_token("ready"), None);
        assert_eq!(NodeStatus::from_token(""), None);
    }

    #[test]
    fn transition_table_is_exhaustive_in_both_directions() {
        let legal: std::collections::HashSet<(NodeStatus, NodeStatus)> =
            legal_edges().into_iter().collect();
        assert_eq!(
            legal.len(),
            legal_edges().len(),
            "the edge list must not contain duplicates"
        );
        for from in ALL {
            for to in ALL {
                assert_eq!(
                    can_transition(from, to),
                    legal.contains(&(from, to)),
                    "edge {:?}->{:?} disagrees with the table",
                    from,
                    to
                );
            }
        }
    }

    #[test]
    fn legal_edges_list_names_the_documented_lifecycle() {
        use NodeStatus::*;
        // The core lifecycle chain.
        assert!(can_transition(Created, Ready));
        assert!(can_transition(Ready, Scheduled));
        assert!(can_transition(Scheduled, Running));
        assert!(can_transition(Running, Succeeded));
        // Suspension + fence/retry re-arms.
        assert!(can_transition(Running, Waiting));
        assert!(can_transition(Waiting, Running));
        assert!(can_transition(Running, Failed));
        assert!(can_transition(Running, Ready), "fence: timeout re-arm");
        assert!(
            can_transition(Failed, Ready),
            "retry after exhausted-fail requeue"
        );
        assert!(can_transition(Scheduled, Ready), "dispatch failed requeue");
        // Planning exits.
        assert!(can_transition(Created, Skipped));
        assert!(can_transition(Ready, Cancelled));
    }

    #[test]
    fn illegal_edges_are_rejected() {
        use NodeStatus::*;
        // No self-loops.
        for status in ALL {
            assert!(
                !can_transition(status, status),
                "{status:?} self-loop allowed"
            );
        }
        // Terminal states have no outgoing edges.
        for terminal in [Succeeded, Cancelled, Skipped] {
            assert!(terminal.is_terminal());
            for to in ALL {
                assert!(
                    !can_transition(terminal, to),
                    "{terminal:?}->{to:?} must be rejected (terminal)"
                );
            }
        }
        // Skipping the schedule/running steps is illegal.
        assert!(!can_transition(Created, Running));
        assert!(!can_transition(Ready, Running));
        assert!(!can_transition(Ready, Succeeded));
        assert!(!can_transition(Created, Succeeded));
        assert!(!can_transition(Running, Scheduled));
        assert!(
            !can_transition(Waiting, Succeeded),
            "must resume before committing"
        );
        assert!(
            !can_transition(Failed, Succeeded),
            "no resurrection into a winner"
        );
        assert!(
            !can_transition(Waiting, Ready),
            "waiting re-arm goes via RUNNING"
        );
        assert!(!can_transition(Scheduled, Succeeded));
        assert!(!ALL
            .iter()
            .any(|s| s.is_terminal() && matches!(s, NodeStatus::Failed)));
    }

    #[test]
    fn display_and_fromstr_round_trip() {
        for status in ALL {
            assert_eq!(status.to_string(), status.as_str());
            let parsed: NodeStatus = status.as_str().parse().unwrap();
            assert_eq!(parsed, status);
        }
        assert!("NOPE".parse::<NodeStatus>().is_err());
    }
}
