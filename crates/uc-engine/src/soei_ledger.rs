//! Soei claim/proof ledger (#33): two-phase acknowledgement, hash chain and
//! W3C PROV-style edges.
//!
//! This is the pure bookkeeping half of the Soei evidence ledger. It never
//! talks to NATS, a database, or a model: callers submit already-observed
//! events, acknowledge them, and ask whether a completion claim has a
//! mechanically verifiable proof recorded after the last mutation.
//!
//! The ledger stores content hashes rather than transcripts, redacts
//! high-entropy secrets before hashing, and keeps a bounded in-memory chain.
//! Applying or persisting the ledger is the caller's responsibility.

use std::collections::{BTreeMap, HashSet};

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Hash algorithm recorded by the ledger.
pub const LEDGER_ALGORITHM: &str = "blake3";
/// Default bound on retained entries.
pub const DEFAULT_LEDGER_CAPACITY: usize = 4096;
/// Previous hash of the first entry.
pub const GENESIS_HASH: &str = "0";

/// Event classes relevant to a claim/proof handoff.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LedgerEventKind {
    Mutation,
    Verification,
    Proof,
    CompletionClaim,
    Handoff,
}

impl LedgerEventKind {
    fn as_str(self) -> &'static str {
        match self {
            Self::Mutation => "mutation",
            Self::Verification => "verification",
            Self::Proof => "proof",
            Self::CompletionClaim => "completion_claim",
            Self::Handoff => "handoff",
        }
    }
}

/// W3C PROV-aligned relation names used by the ledger.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProvRelation {
    WasGeneratedBy,
    WasDerivedFrom,
    WasAttributedTo,
}

impl ProvRelation {
    fn as_str(self) -> &'static str {
        match self {
            Self::WasGeneratedBy => "was_generated_by",
            Self::WasDerivedFrom => "was_derived_from",
            Self::WasAttributedTo => "was_attributed_to",
        }
    }
}

/// One directed provenance edge.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProvEdge {
    pub relation: ProvRelation,
    pub target: String,
}

impl ProvEdge {
    pub fn new(relation: ProvRelation, target: impl Into<String>) -> Self {
        Self {
            relation,
            target: target.into(),
        }
    }
}

/// A committed ledger entry. `content_hash` is the digest of the redacted
/// content; the content itself is not retained.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LedgerEntry {
    pub seq: u64,
    pub kind: LedgerEventKind,
    pub actor: String,
    pub content_hash: String,
    pub prev_hash: String,
    pub entry_hash: String,
    pub edges: Vec<ProvEdge>,
    pub redacted: bool,
}

impl LedgerEntry {
    pub fn is_redacted(&self) -> bool {
        self.redacted
    }

    pub fn provenance_targets(&self, relation: ProvRelation) -> Vec<&str> {
        self.edges
            .iter()
            .filter(|edge| edge.relation == relation)
            .map(|edge| edge.target.as_str())
            .collect()
    }
}

/// An event that has been ingested but not yet acknowledged.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PendingRecord {
    token: u64,
    kind: LedgerEventKind,
    actor: String,
    content_hash: String,
    edges: Vec<ProvEdge>,
    redacted: bool,
}

impl PendingRecord {
    pub fn token(&self) -> u64 {
        self.token
    }

    pub fn kind(&self) -> LedgerEventKind {
        self.kind
    }

    pub fn redacted(&self) -> bool {
        self.redacted
    }
}

/// Errors from the pure ledger layer.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum SoeiError {
    #[error("ledger actor must not be empty")]
    EmptyActor,
    #[error("ledger content must not be empty")]
    EmptyContent,
    #[error("provenance edge target must not be empty")]
    EmptyEdgeTarget,
    #[error("unknown pending ledger token")]
    UnknownPendingToken,
    #[error("ledger capacity reached")]
    CapacityReached,
    #[error("unknown ledger entry")]
    UnknownEntry,
    #[error("entry is not a completion claim")]
    NotACompletionClaim,
    #[error("completion claim has no mutation evidence")]
    NoMutationEvidence,
    #[error("completion claim is missing a mechanically verifiable proof")]
    MissingProof,
    #[error("hash chain verification failed at sequence {seq}")]
    ChainBroken { seq: u64 },
}

/// Bounded, in-memory Soei ledger.
#[derive(Debug, Clone)]
pub struct SoeiLedger {
    entries: Vec<LedgerEntry>,
    by_hash: BTreeMap<String, usize>,
    pending: BTreeMap<u64, PendingRecord>,
    next_token: u64,
    capacity: usize,
    chain_tip: String,
}

impl Default for SoeiLedger {
    fn default() -> Self {
        Self::new(DEFAULT_LEDGER_CAPACITY)
    }
}

impl SoeiLedger {
    pub fn new(capacity: usize) -> Self {
        Self {
            entries: Vec::new(),
            by_hash: BTreeMap::new(),
            pending: BTreeMap::new(),
            next_token: 1,
            capacity: capacity.max(1),
            chain_tip: GENESIS_HASH.to_string(),
        }
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    pub fn chain_tip(&self) -> &str {
        &self.chain_tip
    }

    pub fn entries(&self) -> &[LedgerEntry] {
        &self.entries
    }

    pub fn pending_len(&self) -> usize {
        self.pending.len()
    }

    /// Phase one: ingest an event and return a token that must be acknowledged.
    pub fn begin_record(
        &mut self,
        kind: LedgerEventKind,
        actor: impl Into<String>,
        content: &str,
        edges: Vec<ProvEdge>,
    ) -> Result<PendingRecord, SoeiError> {
        let actor = actor.into();
        if actor.trim().is_empty() {
            return Err(SoeiError::EmptyActor);
        }
        if content.trim().is_empty() {
            return Err(SoeiError::EmptyContent);
        }
        if edges.iter().any(|edge| edge.target.trim().is_empty()) {
            return Err(SoeiError::EmptyEdgeTarget);
        }
        if self.entries.len() + self.pending.len() >= self.capacity {
            return Err(SoeiError::CapacityReached);
        }
        let (redacted_content, redacted) = redact_secrets(content);
        let record = PendingRecord {
            token: self.next_token,
            kind,
            actor: actor.trim().to_string(),
            content_hash: content_digest(&redacted_content),
            edges,
            redacted,
        };
        self.next_token += 1;
        self.pending.insert(record.token, record.clone());
        Ok(record)
    }

    /// Phase two: acknowledge a pending record and append it to the hash chain.
    pub fn acknowledge(&mut self, record: &PendingRecord) -> Result<LedgerEntry, SoeiError> {
        let stored = self
            .pending
            .remove(&record.token)
            .ok_or(SoeiError::UnknownPendingToken)?;
        if self.entries.len() >= self.capacity {
            return Err(SoeiError::CapacityReached);
        }
        let seq = self.entries.len() as u64 + 1;
        let prev_hash = self.chain_tip.clone();
        let entry_hash = compute_entry_hash(
            seq,
            stored.kind,
            &stored.actor,
            &stored.content_hash,
            &prev_hash,
            &stored.edges,
            stored.redacted,
        );
        let entry = LedgerEntry {
            seq,
            kind: stored.kind,
            actor: stored.actor,
            content_hash: stored.content_hash,
            prev_hash,
            entry_hash: entry_hash.clone(),
            edges: stored.edges,
            redacted: stored.redacted,
        };
        self.by_hash.insert(entry_hash.clone(), self.entries.len());
        self.chain_tip = entry_hash;
        self.entries.push(entry.clone());
        Ok(entry)
    }

    /// Convenience path for callers that do not need to observe the token.
    pub fn record(
        &mut self,
        kind: LedgerEventKind,
        actor: impl Into<String>,
        content: &str,
        edges: Vec<ProvEdge>,
    ) -> Result<LedgerEntry, SoeiError> {
        let pending = self.begin_record(kind, actor, content, edges)?;
        self.acknowledge(&pending)
    }

    pub fn entry_by_hash(&self, hash: &str) -> Option<&LedgerEntry> {
        self.by_hash
            .get(hash)
            .and_then(|index| self.entries.get(*index))
    }

    /// Recompute the chain from genesis and check every link.
    pub fn verify_chain(&self) -> Result<(), SoeiError> {
        let mut prev = GENESIS_HASH.to_string();
        for (index, entry) in self.entries.iter().enumerate() {
            let seq = index as u64 + 1;
            if entry.seq != seq || entry.prev_hash != prev {
                return Err(SoeiError::ChainBroken { seq });
            }
            let expected = compute_entry_hash(
                entry.seq,
                entry.kind,
                &entry.actor,
                &entry.content_hash,
                &entry.prev_hash,
                &entry.edges,
                entry.redacted,
            );
            if entry.entry_hash != expected {
                return Err(SoeiError::ChainBroken { seq });
            }
            prev = entry.entry_hash.clone();
        }
        Ok(())
    }

    /// L5 claim gate: a completion claim must point at a mutation and have a
    /// proof/verification entry recorded after that mutation.
    pub fn verify_claim(&self, claim_hash: &str) -> Result<(), SoeiError> {
        let claim = self
            .entry_by_hash(claim_hash)
            .ok_or(SoeiError::UnknownEntry)?;
        if claim.kind != LedgerEventKind::CompletionClaim {
            return Err(SoeiError::NotACompletionClaim);
        }
        let mut mutation_hashes: Vec<&str> = claim.provenance_targets(ProvRelation::WasDerivedFrom);
        if mutation_hashes.is_empty() {
            if let Some(last_mutation) = self
                .entries
                .iter()
                .filter(|entry| entry.seq < claim.seq && entry.kind == LedgerEventKind::Mutation)
                .max_by_key(|entry| entry.seq)
            {
                mutation_hashes.push(last_mutation.entry_hash.as_str());
            }
        }
        if mutation_hashes.is_empty() {
            return Err(SoeiError::NoMutationEvidence);
        }
        let last_mutation_seq = mutation_hashes
            .iter()
            .filter_map(|hash| self.entry_by_hash(hash).map(|entry| entry.seq))
            .max()
            .ok_or(SoeiError::NoMutationEvidence)?;
        let proven = self.entries.iter().any(|entry| {
            entry.seq > last_mutation_seq
                && entry.seq < claim.seq
                && matches!(
                    entry.kind,
                    LedgerEventKind::Proof | LedgerEventKind::Verification
                )
                && entry
                    .provenance_targets(ProvRelation::WasDerivedFrom)
                    .iter()
                    .any(|target| mutation_hashes.contains(target))
        });
        if proven {
            Ok(())
        } else {
            Err(SoeiError::MissingProof)
        }
    }

    /// Record a proof bound to one mutation hash.
    pub fn record_proof(
        &mut self,
        actor: impl Into<String>,
        content: &str,
        mutation_hash: &str,
    ) -> Result<LedgerEntry, SoeiError> {
        self.record(
            LedgerEventKind::Proof,
            actor,
            content,
            vec![ProvEdge::new(ProvRelation::WasDerivedFrom, mutation_hash)],
        )
    }

    /// Record a completion claim bound to one or more mutation hashes.
    pub fn record_claim(
        &mut self,
        actor: impl Into<String>,
        content: &str,
        mutation_hashes: &[&str],
    ) -> Result<LedgerEntry, SoeiError> {
        let edges = mutation_hashes
            .iter()
            .map(|hash| ProvEdge::new(ProvRelation::WasDerivedFrom, *hash))
            .collect();
        self.record(LedgerEventKind::CompletionClaim, actor, content, edges)
    }
}

/// Digest of already-redacted content.
pub fn content_digest(content: &str) -> String {
    let mut hasher = blake3::Hasher::new();
    hash_field(&mut hasher, content.as_bytes());
    hasher.finalize().to_hex().to_string()
}

/// Redact token-shaped high-entropy secrets before they reach the ledger.
pub fn redact_secrets(content: &str) -> (String, bool) {
    let mut output = String::with_capacity(content.len());
    let mut redacted = false;
    let mut token_start: Option<usize> = None;

    for (index, character) in content.char_indices() {
        if is_token_character(character) {
            token_start.get_or_insert(index);
            continue;
        }
        if let Some(start) = token_start.take() {
            let token = &content[start..index];
            if is_secret_token(token) {
                output.push_str("[REDACTED]");
                redacted = true;
            } else {
                output.push_str(token);
            }
        }
        output.push(character);
    }
    if let Some(start) = token_start {
        let token = &content[start..];
        if is_secret_token(token) {
            output.push_str("[REDACTED]");
            redacted = true;
        } else {
            output.push_str(token);
        }
    }
    (output, redacted)
}

fn is_token_character(character: char) -> bool {
    character.is_ascii_alphanumeric() || character == '_' || character == '-'
}

fn is_secret_token(token: &str) -> bool {
    let lowered = token.to_ascii_lowercase();
    const PREFIXES: [&str; 6] = ["sk-", "sk_", "ghp_", "gho_", "xoxb-", "xoxp-"];
    if PREFIXES.iter().any(|prefix| lowered.starts_with(prefix)) {
        return true;
    }
    if token.len() < 32 {
        return false;
    }
    let has_digit = token.chars().any(|character| character.is_ascii_digit());
    let has_alpha = token
        .chars()
        .any(|character| character.is_ascii_alphabetic());
    let distinct: HashSet<char> = token.chars().collect();
    let all_hex = token.chars().all(|character| character.is_ascii_hexdigit());
    has_digit && has_alpha && distinct.len() >= 8 && !all_hex
}

fn compute_entry_hash(
    seq: u64,
    kind: LedgerEventKind,
    actor: &str,
    content_hash: &str,
    prev_hash: &str,
    edges: &[ProvEdge],
    redacted: bool,
) -> String {
    let mut hasher = blake3::Hasher::new();
    hash_field(&mut hasher, b"uc-soei-ledger-v1");
    hash_field(&mut hasher, &seq.to_le_bytes());
    hash_field(&mut hasher, kind.as_str().as_bytes());
    hash_field(&mut hasher, actor.as_bytes());
    hash_field(&mut hasher, content_hash.as_bytes());
    hash_field(&mut hasher, prev_hash.as_bytes());
    hash_field(&mut hasher, &[u8::from(redacted)]);
    for edge in edges {
        hash_field(&mut hasher, edge.relation.as_str().as_bytes());
        hash_field(&mut hasher, edge.target.as_bytes());
    }
    hasher.finalize().to_hex().to_string()
}

fn hash_field(hasher: &mut blake3::Hasher, bytes: &[u8]) {
    hasher.update(&(bytes.len() as u64).to_le_bytes());
    hasher.update(bytes);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn two_phase_acknowledgement_appends_only_after_ack() {
        let mut ledger = SoeiLedger::new(8);
        let pending = ledger
            .begin_record(
                LedgerEventKind::Mutation,
                "worker-1",
                "changed src/lib.rs",
                vec![],
            )
            .unwrap();
        assert_eq!(ledger.len(), 0);
        assert_eq!(ledger.pending_len(), 1);
        let entry = ledger.acknowledge(&pending).unwrap();
        assert_eq!(ledger.len(), 1);
        assert_eq!(ledger.pending_len(), 0);
        assert_eq!(entry.seq, 1);
        assert_eq!(entry.prev_hash, GENESIS_HASH);
        assert_eq!(ledger.chain_tip(), entry.entry_hash);
        assert_eq!(
            ledger.acknowledge(&pending),
            Err(SoeiError::UnknownPendingToken)
        );
    }

    #[test]
    fn chain_is_deterministic_and_verifiable() {
        let mut ledger = SoeiLedger::new(8);
        let first = ledger
            .record(LedgerEventKind::Mutation, "a", "one", vec![])
            .unwrap();
        let second = ledger
            .record(LedgerEventKind::Verification, "b", "two", vec![])
            .unwrap();
        assert_eq!(second.prev_hash, first.entry_hash);
        assert!(ledger.verify_chain().is_ok());
        assert_eq!(first.entry_hash.len(), 64);
    }

    #[test]
    fn secrets_are_redacted_before_hashing() {
        let (redacted, changed) = redact_secrets("token sk-abcdefghijklmnopqrstuvwxyz123456 end");
        assert!(changed);
        assert!(redacted.contains("[REDACTED]"));
        assert!(!redacted.contains("abcdefghijklmnopqrstuvwxyz"));
        let mut ledger = SoeiLedger::new(4);
        let entry = ledger
            .record(
                LedgerEventKind::Mutation,
                "worker-1",
                "token sk-abcdefghijklmnopqrstuvwxyz123456",
                vec![],
            )
            .unwrap();
        assert!(entry.is_redacted());
        assert_ne!(
            entry.content_hash,
            content_digest("token sk-abcdefghijklmnopqrstuvwxyz123456")
        );
    }

    #[test]
    fn pure_hex_digests_are_not_treated_as_secrets() {
        let digest = "0123456789abcdef0123456789abcdef";
        let (redacted, changed) = redact_secrets(digest);
        assert!(!changed);
        assert_eq!(redacted, digest);
    }

    #[test]
    fn completion_claim_requires_proof_after_last_mutation() {
        let mut ledger = SoeiLedger::new(16);
        let mutation = ledger
            .record(
                LedgerEventKind::Mutation,
                "worker-1",
                "patch applied",
                vec![],
            )
            .unwrap();
        let claim = ledger
            .record_claim("worker-1", "finished", &[mutation.entry_hash.as_str()])
            .unwrap();
        assert_eq!(
            ledger.verify_claim(&claim.entry_hash),
            Err(SoeiError::MissingProof)
        );

        let proof = ledger
            .record_proof("oracle", "tests passed", mutation.entry_hash.as_str())
            .unwrap();
        assert_eq!(proof.prev_hash, claim.entry_hash);
        assert_eq!(
            ledger.verify_claim(&claim.entry_hash),
            Err(SoeiError::MissingProof),
            "proof after the claim cannot retroactively justify it"
        );
    }

    #[test]
    fn proof_before_claim_satisfies_the_claim_gate() {
        let mut ledger = SoeiLedger::new(16);
        let mutation = ledger
            .record(
                LedgerEventKind::Mutation,
                "worker-1",
                "patch applied",
                vec![],
            )
            .unwrap();
        ledger
            .record_proof("oracle", "cargo test passed", mutation.entry_hash.as_str())
            .unwrap();
        let claim = ledger
            .record_claim("worker-1", "finished", &[mutation.entry_hash.as_str()])
            .unwrap();
        assert!(ledger.verify_claim(&claim.entry_hash).is_ok());
        assert_eq!(
            ledger.verify_claim(&mutation.entry_hash),
            Err(SoeiError::NotACompletionClaim)
        );
    }

    #[test]
    fn claim_without_mutation_is_rejected() {
        let mut ledger = SoeiLedger::new(8);
        let claim = ledger.record_claim("worker-1", "finished", &[]).unwrap();
        assert_eq!(
            ledger.verify_claim(&claim.entry_hash),
            Err(SoeiError::NoMutationEvidence)
        );
    }

    #[test]
    fn tampered_chain_is_detected() {
        let mut ledger = SoeiLedger::new(8);
        ledger
            .record(LedgerEventKind::Mutation, "a", "one", vec![])
            .unwrap();
        ledger
            .record(LedgerEventKind::Proof, "b", "two", vec![])
            .unwrap();
        ledger.entries[0].actor = "tampered".to_string();
        assert_eq!(
            ledger.verify_chain(),
            Err(SoeiError::ChainBroken { seq: 1 })
        );
    }

    #[test]
    fn capacity_and_validation_are_enforced() {
        let mut ledger = SoeiLedger::new(1);
        ledger
            .record(LedgerEventKind::Mutation, "a", "one", vec![])
            .unwrap();
        assert_eq!(
            ledger.record(LedgerEventKind::Proof, "b", "two", vec![]),
            Err(SoeiError::CapacityReached)
        );
        assert_eq!(
            ledger.begin_record(LedgerEventKind::Proof, "  ", "two", vec![]),
            Err(SoeiError::EmptyActor)
        );
        assert_eq!(
            ledger.begin_record(LedgerEventKind::Proof, "b", "two", vec![]),
            Err(SoeiError::CapacityReached)
        );
    }
}
