//! Merge-barrier grant contract (T9 #651 / D9 #646 on top of D5 #634).
//!
//! Ownership is **hybrid**: the Python `MergeArbiter` keeps executing merges
//! (git effects), while the Rust gateway authorizes them. The arbiter must
//! hold a grant BEFORE merging and report the outcome with the grant's
//! `merge_idempotency_key`. The key binds the grant to the exact graph state
//! that was merged (`graph_id + SUCCEEDED node set + per-node output shas`),
//! so a superseded/stale aggregation presents a key the gateway no longer
//! knows and its merge report is rejected — a stale merge loses exactly like
//! a late `commit_once` loser.

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// Number of leading hex characters of the sha256 digest used as the merge
/// idempotency key (same truncation family as the dispatch envelope key).
const MERGE_KEY_LEN: usize = 32;

/// Decision returned by the gateway for an `IssueMergeGrant` request.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct MergeGrantDecision {
    /// `true` = the arbiter may proceed with (or skip, on replay) the merge.
    pub granted: bool,
    /// The deterministic key binding this grant to the graph state. Empty
    /// when `granted == false`.
    pub merge_idempotency_key: String,
    /// `true` when the stored grant row already carries this exact key AND is
    /// consumed — the merge already reported; the arbiter must skip execution.
    pub idempotent_replay: bool,
    /// Human-readable refusal reason. Empty on success.
    pub error: String,
}

/// The arbiter's merge report, carried with the grant key.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct MergeOutcomeReport {
    /// Free-form terminal status (e.g. "merged", "conflict", "skipped_replay").
    pub status: String,
    pub merged_branches: Vec<String>,
    pub conflict_branches: Vec<String>,
    /// e.g. "pushed", "no_push", "push_failed".
    pub push_status: String,
}

/// Decision returned by the gateway for a `ReportMergeOutcome` request.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct MergeReportDecision {
    /// `false` = unknown/superseded key (the report is dropped loudly).
    pub accepted: bool,
    /// `true` when the key matched an already-consumed grant — the report is
    /// a no-op (D5: consumed-key replay = no-op).
    pub idempotent_replay: bool,
}

/// Full sha256 digest as lowercase hex (64 chars) — helper for the per-node
/// output hashes embedded in the merge key preimage.
pub fn sha256_hex(data: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(data);
    let digest = hasher.finalize();
    digest.iter().map(|b| format!("{b:02x}")).collect()
}

/// Derive the merge idempotency key (cross-language contract — mirrored by
/// `derive_merge_idempotency_key` in `tests/python`; if either side changes
/// the preimage format or truncation, both goldens fail together).
///
/// Canonical preimage: `merge:{graph_id}:{SUCCEEDED ids CSV}:{node=sha pairs}`
/// where `succeeded` is sorted by node_id (byte order), the CSV joins the
/// node ids with `,`, and the pairs section joins `{node_id}={output_sha256}`
/// with `;`. Truncated to [`MERGE_KEY_LEN`] hex chars.
pub fn derive_merge_idempotency_key(graph_id: &str, succeeded: &[(String, String)]) -> String {
    let mut ordered: Vec<&(String, String)> = succeeded.iter().collect();
    ordered.sort_by(|a, b| a.0.cmp(&b.0));
    let csv: Vec<&str> = ordered.iter().map(|(id, _)| id.as_str()).collect();
    let pairs: Vec<String> = ordered
        .iter()
        .map(|(id, sha)| format!("{id}={sha}"))
        .collect();
    let preimage = format!("merge:{}:{}:{}", graph_id, csv.join(","), pairs.join(";"));
    sha256_hex(preimage.as_bytes())[..MERGE_KEY_LEN].to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Cross-language golden (mirrored in
    /// tests/python/test_merge_gate.py::test_merge_key_matches_rust_golden).
    /// preimage = "merge:g-1:n-1,n-2:n-1=f44c…a6f6;n-2=bc43…ecb6".
    const GOLDEN_KEY: &str = "70944265bd1ad20766b7bfa2f7d5fc4b";

    fn golden_input() -> Vec<(String, String)> {
        vec![
            ("n-1".to_string(), sha256_hex(b"out-1")),
            ("n-2".to_string(), sha256_hex(b"out-2")),
        ]
    }

    #[test]
    fn merge_key_matches_cross_language_golden() {
        assert_eq!(
            derive_merge_idempotency_key("g-1", &golden_input()),
            GOLDEN_KEY
        );
    }

    #[test]
    fn merge_key_is_order_insensitive_over_succeeded_set() {
        // The arbiter may not see the same iteration order the store uses —
        // the canonical sort makes the key a pure function of the SET.
        let mut flipped = golden_input();
        flipped.reverse();
        assert_eq!(
            derive_merge_idempotency_key("g-1", &flipped),
            derive_merge_idempotency_key("g-1", &golden_input())
        );
    }

    #[test]
    fn merge_key_differs_per_graph_node_and_output() {
        let base = derive_merge_idempotency_key("g-1", &golden_input());
        // Different graph.
        assert_ne!(base, derive_merge_idempotency_key("g-2", &golden_input()));
        // Different SUCCEEDED set (stale aggregation loses).
        let fewer = vec![golden_input()[0].clone()];
        assert_ne!(base, derive_merge_idempotency_key("g-1", &fewer));
        // Different output bytes (same node set).
        let other_out = vec![("n-1".to_string(), sha256_hex(b"out-X"))];
        assert_ne!(
            derive_merge_idempotency_key("g-1", &fewer),
            derive_merge_idempotency_key("g-1", &other_out)
        );
    }

    #[test]
    fn merge_key_handles_empty_succeeded_set() {
        // All-failed graph: still derivable (arbiter will no-op anyway).
        let k = derive_merge_idempotency_key("g-1", &[]);
        assert_eq!(k.len(), MERGE_KEY_LEN);
        assert_eq!(k, derive_merge_idempotency_key("g-1", &[]), "deterministic");
    }

    #[test]
    fn sha256_hex_is_full_lowercase_digest() {
        assert_eq!(sha256_hex(b"").len(), 64);
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }
}
