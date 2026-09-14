//! Execution envelope contract (T1 / #637).
//!
//! The envelope rides on every `uc.subtask.execute` dispatch payload and is
//! the identity skeleton of the future durable graph runtime (T2+): a
//! `(graph_id, node_id, attempt_id)` triple addresses exactly one execution
//! attempt, and `idempotency_key` is a deterministic digest of that triple so
//! re-sends of the same dispatch carry an identical key (unlike `message_id`,
//! which embeds wall-clock millis).
//!
//! `contract_version` is the gateway↔worker handshake token: a worker that
//! does not declare the gateway's [`CONTRACT_VERSION`] is refused at
//! registration (non-empty mismatch) or accepted-but-not-dispatchable (empty,
//! i.e. a pre-handshake legacy worker). Gateway and workers MUST be upgraded
//! in lockstep — see AGENTS.md "Contract version lockstep".

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// Current execution contract version. Bumped only on breaking wire-contract
/// changes; mixed-version deployments fail loudly (register refused / no
/// dispatch), never silently. Mirrored by `CONTRACT_VERSION` in
/// `python/ultimate_coders/nats_worker.py` — bump both together.
pub const CONTRACT_VERSION: &str = "v1";

/// Number of leading hex characters of the sha256 digest used as the
/// idempotency key (32 × 4 = 128 bits — plenty for dedup, keeps payloads small).
const IDEMPOTENCY_KEY_LEN: usize = 32;

/// Hard cap on the serialized `context_block` (T10 #652 / D10 #647): an
/// 8 KiB budget keeps dispatch payloads small no matter how chatty the
/// dependency summaries are. Overflow drops whole entries (never splits one)
/// and sets `ContextBlock::truncated` — never a failed dispatch.
pub const CONTEXT_BLOCK_MAX_BYTES: usize = 8 * 1024;

/// One committed dependency output in a gateway-composed context block
/// (T10 #652). `summary` is the dependency's committed `result_ref` (the
/// winning attempt's result summary); empty when the node committed nothing.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ContextEntry {
    pub node_id: String,
    pub success: bool,
    #[serde(default)]
    pub summary: String,
}

/// Gateway-composed dependency context riding on `uc.subtask.execute`
/// (additive — legacy workers ignore unknown keys, same path as effect_class
/// in T5). Entries are ordered by `node_id` (byte order) so the block is a
/// deterministic function of the committed graph state.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct ContextBlock {
    pub entries: Vec<ContextEntry>,
    /// `true` when at least one entry was dropped to honour the
    /// [`CONTEXT_BLOCK_MAX_BYTES`] budget.
    pub truncated: bool,
}

impl ContextBlock {
    /// Compose a context block from dependency outputs. Entries are sorted
    /// by `node_id` and greedily packed under the 8 KiB serialized budget;
    /// entries that would overflow are dropped and `truncated` is set.
    /// Returns `None` for an empty input (the field is omitted on the wire —
    /// no context, no block).
    pub fn compose(mut entries: Vec<ContextEntry>) -> Option<Self> {
        if entries.is_empty() {
            return None;
        }
        entries.sort_by(|a, b| a.node_id.cmp(&b.node_id));
        let mut block = ContextBlock {
            entries: Vec::new(),
            truncated: false,
        };
        for entry in entries {
            let mut candidate = block.entries.clone();
            candidate.push(entry);
            let probe = ContextBlock {
                entries: candidate,
                truncated: false,
            };
            let size = serde_json::to_vec(&probe)
                .map(|v| v.len())
                .unwrap_or(usize::MAX);
            if size > CONTEXT_BLOCK_MAX_BYTES {
                block.truncated = true;
                break;
            }
            block.entries = probe.entries;
        }
        Some(block)
    }
}

/// Envelope attached to every subtask dispatch.
///
/// Transitional identity mapping until the graph runtime lands (T2/T3):
/// `graph_id = task_id`, `node_id = subtask_id`, `attempt_id =
/// dispatch_retry_count`, `worker_epoch = ""`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct ExecutionEnvelope {
    /// Graph (task) this execution belongs to.
    pub graph_id: String,
    /// Node (subtask) being executed.
    pub node_id: String,
    /// Attempt number within `(graph_id, node_id)`, as a decimal string.
    pub attempt_id: String,
    /// Deterministic digest of `{graph}:{node}:{attempt}` — sha256 hex, first
    /// [`IDEMPOTENCY_KEY_LEN`] chars. No timestamp component.
    pub idempotency_key: String,
    /// Worker fencing epoch. Empty until T3 introduces epochs.
    pub worker_epoch: String,
    /// Execution contract the sender/worker speaks ([`CONTRACT_VERSION`]).
    pub contract_version: String,
    /// Gateway-composed dependency context (T10 #652 / D10 #647). Additive —
    /// `None` on legacy dispatches and when the gateway has nothing to say;
    /// workers fall back to their local injector. Omitted on the wire when
    /// `None`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub context_block: Option<ContextBlock>,
}

impl ExecutionEnvelope {
    /// Build an envelope for a fresh dispatch of `(graph_id, node_id)` at
    /// attempt `attempt_id`, deriving the deterministic idempotency key and
    /// stamping the current contract version.
    pub fn new(graph_id: &str, node_id: &str, attempt_id: &str) -> Self {
        Self {
            graph_id: graph_id.to_string(),
            node_id: node_id.to_string(),
            attempt_id: attempt_id.to_string(),
            idempotency_key: Self::derive_idempotency_key(graph_id, node_id, attempt_id),
            worker_epoch: String::new(),
            contract_version: CONTRACT_VERSION.to_string(),
            context_block: None,
        }
    }

    /// Transitional constructor for gateway dispatch: `attempt` is the
    /// subtask's current `dispatch_retry_count`.
    pub fn for_dispatch(graph_id: &str, node_id: &str, attempt: u32) -> Self {
        Self::new(graph_id, node_id, &attempt.to_string())
    }

    /// `sha256("{graph_id}:{node_id}:{attempt_id}")` hex, truncated to 32
    /// chars. Deterministic in the identity triple — re-publishing the same
    /// dispatch yields the same key.
    pub fn derive_idempotency_key(graph_id: &str, node_id: &str, attempt_id: &str) -> String {
        let mut hasher = Sha256::new();
        hasher.update(format!("{graph_id}:{node_id}:{attempt_id}").as_bytes());
        let digest = hasher.finalize();
        let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
        hex[..IDEMPOTENCY_KEY_LEN].to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn contract_version_is_v1() {
        assert_eq!(CONTRACT_VERSION, "v1");
    }

    #[test]
    fn idempotency_key_is_deterministic_and_truncated() {
        let a = ExecutionEnvelope::derive_idempotency_key("g-1", "n-1", "0");
        let b = ExecutionEnvelope::derive_idempotency_key("g-1", "n-1", "0");
        assert_eq!(a, b, "same triple must derive the same key");
        assert_eq!(a.len(), IDEMPOTENCY_KEY_LEN);
        assert!(a
            .chars()
            .all(|c| c.is_ascii_digit() || ('a'..='f').contains(&c)));
        // Full sha256 hex of "g-1:n-1:0", first 32 chars — guards the exact
        // preimage format "{graph}:{node}:{attempt}".
        assert_eq!(a, "394e2a22ec4c60361079d90a62b8a9b7");
    }

    #[test]
    fn idempotency_key_differs_per_component() {
        let base = ExecutionEnvelope::derive_idempotency_key("g-1", "n-1", "0");
        assert_ne!(
            base,
            ExecutionEnvelope::derive_idempotency_key("g-2", "n-1", "0")
        );
        assert_ne!(
            base,
            ExecutionEnvelope::derive_idempotency_key("g-1", "n-2", "0")
        );
        assert_ne!(
            base,
            ExecutionEnvelope::derive_idempotency_key("g-1", "n-1", "1")
        );
    }

    #[test]
    fn for_dispatch_maps_identity_fields() {
        let env = ExecutionEnvelope::for_dispatch("t-9", "st-3", 2);
        assert_eq!(env.graph_id, "t-9");
        assert_eq!(env.node_id, "st-3");
        assert_eq!(env.attempt_id, "2");
        assert_eq!(env.worker_epoch, "");
        assert_eq!(env.contract_version, CONTRACT_VERSION);
        assert_eq!(
            env.idempotency_key,
            ExecutionEnvelope::derive_idempotency_key("t-9", "st-3", "2")
        );
        // Two envelopes built for the same triple are byte-identical on the
        // wire (the determinism the T4 dedup gate will rely on).
        let again = ExecutionEnvelope::for_dispatch("t-9", "st-3", 2);
        assert_eq!(
            serde_json::to_vec(&env).unwrap(),
            serde_json::to_vec(&again).unwrap()
        );
    }

    #[test]
    fn envelope_deserializes_from_empty_json() {
        // All-envelope-fields-optional on the wire: a payload without any of
        // the keys still parses (legacy publishers during rollout).
        let env: ExecutionEnvelope = serde_json::from_str("{}").unwrap();
        assert_eq!(env, ExecutionEnvelope::default());
        assert!(env.context_block.is_none());
    }

    // ── T10 #652 — context block (compose + wire round-trip) ───────────

    fn entry(id: &str, success: bool, summary: &str) -> ContextEntry {
        ContextEntry {
            node_id: id.to_string(),
            success,
            summary: summary.to_string(),
        }
    }

    #[test]
    fn context_block_compose_is_none_for_empty_and_sorted_for_content() {
        assert!(ContextBlock::compose(Vec::new()).is_none());
        let block = ContextBlock::compose(vec![
            entry("n-2", true, "second"),
            entry("n-1", true, "first"),
        ])
        .unwrap();
        let ids: Vec<&str> = block.entries.iter().map(|e| e.node_id.as_str()).collect();
        assert_eq!(ids, ["n-1", "n-2"], "entries ordered by node_id");
        assert!(!block.truncated);
    }

    #[test]
    fn context_block_compose_truncates_at_budget_and_never_splits_an_entry() {
        // Each entry is ~1 KiB of payload; ten of them blow the 8 KiB budget.
        let big = "x".repeat(1000);
        let entries: Vec<ContextEntry> = (0..10)
            .map(|i| entry(&format!("n-{i:02}"), true, &big))
            .collect();
        let block = ContextBlock::compose(entries).unwrap();
        assert!(block.truncated, "overflow must set the marker");
        assert!(
            block.entries.len() < 10,
            "overflow must drop entries, not fail"
        );
        let serialized = serde_json::to_vec(&block).unwrap();
        assert!(
            serialized.len() <= CONTEXT_BLOCK_MAX_BYTES,
            "composed block must fit the budget (got {} bytes)",
            serialized.len()
        );
    }

    #[test]
    fn context_block_wire_round_trip_and_legacy_absence() {
        let mut env = ExecutionEnvelope::for_dispatch("t-1", "n-1", 0);
        env.context_block = ContextBlock::compose(vec![entry("n-0", true, "done")]);
        let json = serde_json::to_string(&env).unwrap();
        assert!(json.contains("context_block"));
        let back: ExecutionEnvelope = serde_json::from_str(&json).unwrap();
        assert_eq!(back.context_block, env.context_block);

        // A pre-T10 dispatch without the key parses with context_block=None
        // (additive upgrade path — same as effect_class in T5).
        let legacy: ExecutionEnvelope =
            serde_json::from_str(r#"{"graph_id":"t-1","node_id":"n-1"}"#).unwrap();
        assert!(legacy.context_block.is_none());
        // ...and serializes back without the key (skip_serializing_if).
        let legacy_json = serde_json::to_string(&legacy).unwrap();
        assert!(!legacy_json.contains("context_block"));
    }
}
