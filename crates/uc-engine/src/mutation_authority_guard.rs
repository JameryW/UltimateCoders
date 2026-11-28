//! Cross-substrate mutation authority guard (#40).
//!
//! The ledger's *Beyond Agent Harnesses* guard is deliberately model-free: a
//! mutation may only cross a publication boundary when it carries a trusted
//! receipt for the exact mutation, commit, fencing token and verification
//! digest, and when its Git lineage extends the guard's current commit.
//!
//! This module is the pure decision core. It does not talk to Git, NATS or a
//! sandbox; callers present an already-observed request and receipt.

use std::collections::{BTreeSet, VecDeque};

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Default bound on retained accepted decisions.
pub const DEFAULT_MAX_DECISIONS: usize = 4096;

/// A proposed mutation at the publication boundary.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MutationRequest {
    pub mutation_id: String,
    pub base_commit: String,
    pub target_commit: String,
    pub fencing_token: u64,
    pub receipt: Option<AuthorityReceipt>,
}

impl MutationRequest {
    /// Create a request. Receipt validation happens in the guard.
    pub fn new(
        mutation_id: impl Into<String>,
        base_commit: impl Into<String>,
        target_commit: impl Into<String>,
        fencing_token: u64,
        receipt: Option<AuthorityReceipt>,
    ) -> Self {
        Self {
            mutation_id: mutation_id.into(),
            base_commit: base_commit.into(),
            target_commit: target_commit.into(),
            fencing_token,
            receipt,
        }
    }
}

/// A verification receipt issued by a trusted authority.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AuthorityReceipt {
    pub mutation_id: String,
    pub commit_hash: String,
    pub verification_digest: String,
    pub signer: String,
    pub signature: String,
}

impl AuthorityReceipt {
    /// Seal a receipt with the deterministic digest used by the guard.
    ///
    /// This is a content signature for the pure authority protocol, not a
    /// replacement for a deployment's asymmetric signing key.
    pub fn seal(
        mutation_id: impl Into<String>,
        commit_hash: impl Into<String>,
        verification_digest: impl Into<String>,
        signer: impl Into<String>,
        fencing_token: u64,
    ) -> Self {
        let mutation_id = mutation_id.into();
        let commit_hash = commit_hash.into();
        let verification_digest = verification_digest.into();
        let signer = signer.into();
        let signature = receipt_signature(
            &mutation_id,
            &commit_hash,
            &verification_digest,
            &signer,
            fencing_token,
        );
        Self {
            mutation_id,
            commit_hash,
            verification_digest,
            signer,
            signature,
        }
    }

    /// Recompute the expected signature.
    pub fn expected_signature(&self, fencing_token: u64) -> String {
        receipt_signature(
            &self.mutation_id,
            &self.commit_hash,
            &self.verification_digest,
            &self.signer,
            fencing_token,
        )
    }
}

/// Deterministic content signature over the receipt fields.
pub fn receipt_signature(
    mutation_id: &str,
    commit_hash: &str,
    verification_digest: &str,
    signer: &str,
    fencing_token: u64,
) -> String {
    let payload = format!(
        "{mutation_id}\u{1f}{commit_hash}\u{1f}{verification_digest}\u{1f}{signer}\u{1f}{fencing_token}"
    );
    blake3::hash(payload.as_bytes()).to_hex().to_string()
}

/// Accepted-decision audit record.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AuthorityDecision {
    pub accepted: bool,
    pub mutation_id: String,
    pub fencing_token: u64,
    pub verification_digest: String,
    pub guard_digest: String,
}

/// Errors from the pure authority guard.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum MutationAuthorityError {
    #[error("authority field `{field}` must not be empty")]
    EmptyField { field: &'static str },
    #[error("mutation request is missing its verification receipt")]
    MissingReceipt,
    #[error("receipt signer is not trusted: {0}")]
    UntrustedSigner(String),
    #[error("receipt mutation id does not match the request")]
    MutationMismatch,
    #[error("receipt commit hash does not match the target commit")]
    CommitMismatch,
    #[error("receipt verification digest must not be empty")]
    EmptyVerification,
    #[error("receipt signature is invalid")]
    InvalidSignature,
    #[error("fencing token must be positive")]
    InvalidFencingToken,
    #[error("stale fencing token {presented}; current token is {current}")]
    StaleFence { presented: u64, current: u64 },
    #[error("base commit {actual} does not extend current commit {expected}")]
    LineageMismatch { expected: String, actual: String },
}

/// Pure mutation-boundary authority state machine.
#[derive(Debug, Clone)]
pub struct MutationAuthorityGuard {
    trusted_signers: BTreeSet<String>,
    current_commit: String,
    last_fencing_token: u64,
    decisions: VecDeque<AuthorityDecision>,
    max_decisions: usize,
}

impl MutationAuthorityGuard {
    /// Create a guard anchored at a genesis commit.
    pub fn new(
        trusted_signers: impl IntoIterator<Item = impl Into<String>>,
        genesis_commit: impl Into<String>,
    ) -> Result<Self, MutationAuthorityError> {
        let genesis_commit = genesis_commit.into();
        if genesis_commit.trim().is_empty() {
            return Err(MutationAuthorityError::EmptyField {
                field: "genesis_commit",
            });
        }
        let mut signers = BTreeSet::new();
        for signer in trusted_signers {
            let signer = signer.into();
            if signer.trim().is_empty() {
                return Err(MutationAuthorityError::EmptyField { field: "signer" });
            }
            signers.insert(signer);
        }
        if signers.is_empty() {
            return Err(MutationAuthorityError::EmptyField {
                field: "trusted_signers",
            });
        }
        Ok(Self {
            trusted_signers: signers,
            current_commit: genesis_commit,
            last_fencing_token: 0,
            decisions: VecDeque::new(),
            max_decisions: DEFAULT_MAX_DECISIONS,
        })
    }

    /// Bound the retained accepted-decision audit log.
    #[must_use]
    pub fn with_max_decisions(mut self, max_decisions: usize) -> Self {
        self.max_decisions = max_decisions.max(1);
        while self.decisions.len() > self.max_decisions {
            self.decisions.pop_front();
        }
        self
    }

    /// Commit currently anchored by the guard.
    pub fn current_commit(&self) -> &str {
        &self.current_commit
    }

    /// Last accepted fencing token.
    pub fn last_fencing_token(&self) -> u64 {
        self.last_fencing_token
    }

    /// Whether a signer is in the trusted set.
    pub fn is_trusted_signer(&self, signer: &str) -> bool {
        self.trusted_signers.contains(signer)
    }

    /// Accepted decision log, oldest first.
    pub fn decisions(&self) -> impl Iterator<Item = &AuthorityDecision> {
        self.decisions.iter()
    }

    /// Validate a publication request and, when accepted, advance the guard.
    pub fn authorize(
        &mut self,
        request: &MutationRequest,
    ) -> Result<AuthorityDecision, MutationAuthorityError> {
        require_non_empty("mutation_id", &request.mutation_id)?;
        require_non_empty("base_commit", &request.base_commit)?;
        require_non_empty("target_commit", &request.target_commit)?;
        if request.fencing_token == 0 {
            return Err(MutationAuthorityError::InvalidFencingToken);
        }
        let receipt = request
            .receipt
            .as_ref()
            .ok_or(MutationAuthorityError::MissingReceipt)?;
        require_non_empty("receipt.signer", &receipt.signer)?;
        require_non_empty("receipt.verification_digest", &receipt.verification_digest)?;
        if !self.trusted_signers.contains(&receipt.signer) {
            return Err(MutationAuthorityError::UntrustedSigner(
                receipt.signer.clone(),
            ));
        }
        if receipt.mutation_id != request.mutation_id {
            return Err(MutationAuthorityError::MutationMismatch);
        }
        if receipt.commit_hash != request.target_commit {
            return Err(MutationAuthorityError::CommitMismatch);
        }
        if receipt.signature != receipt.expected_signature(request.fencing_token) {
            return Err(MutationAuthorityError::InvalidSignature);
        }
        if request.base_commit != self.current_commit {
            return Err(MutationAuthorityError::LineageMismatch {
                expected: self.current_commit.clone(),
                actual: request.base_commit.clone(),
            });
        }
        if request.fencing_token <= self.last_fencing_token {
            return Err(MutationAuthorityError::StaleFence {
                presented: request.fencing_token,
                current: self.last_fencing_token,
            });
        }

        self.current_commit = request.target_commit.clone();
        self.last_fencing_token = request.fencing_token;
        let decision = AuthorityDecision {
            accepted: true,
            mutation_id: request.mutation_id.clone(),
            fencing_token: request.fencing_token,
            verification_digest: receipt.verification_digest.clone(),
            guard_digest: self.guard_digest(),
        };
        self.decisions.push_back(decision.clone());
        while self.decisions.len() > self.max_decisions {
            self.decisions.pop_front();
        }
        Ok(decision)
    }

    /// Stable digest of the guard's current authority position.
    pub fn guard_digest(&self) -> String {
        let payload = format!(
            "{}\u{1f}{}\u{1f}{}",
            self.current_commit,
            self.last_fencing_token,
            self.decisions.len()
        );
        blake3::hash(payload.as_bytes()).to_hex().to_string()
    }
}

fn require_non_empty(field: &'static str, value: &str) -> Result<(), MutationAuthorityError> {
    if value.trim().is_empty() {
        Err(MutationAuthorityError::EmptyField { field })
    } else {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn guard() -> MutationAuthorityGuard {
        MutationAuthorityGuard::new(["ci-verifier"], "genesis").expect("guard")
    }

    fn receipt(token: u64) -> AuthorityReceipt {
        AuthorityReceipt::seal("m-1", "commit-1", "blake3:verified", "ci-verifier", token)
    }

    fn request(token: u64) -> MutationRequest {
        MutationRequest::new("m-1", "genesis", "commit-1", token, Some(receipt(token)))
    }

    #[test]
    fn accepts_a_well_formed_mutation() {
        let mut guard = guard();
        let decision = guard.authorize(&request(1)).expect("accept");
        assert!(decision.accepted);
        assert_eq!(guard.current_commit(), "commit-1");
        assert_eq!(guard.last_fencing_token(), 1);
        assert_eq!(guard.decisions().count(), 1);
    }

    #[test]
    fn rejects_a_missing_receipt() {
        let mut guard = guard();
        let mut req = request(1);
        req.receipt = None;
        assert_eq!(
            guard.authorize(&req).unwrap_err(),
            MutationAuthorityError::MissingReceipt
        );
    }

    #[test]
    fn rejects_an_untrusted_signer() {
        let mut guard = guard();
        let mut req = request(1);
        let receipt = req.receipt.as_mut().expect("receipt");
        receipt.signer = "outsider".into();
        assert_eq!(
            guard.authorize(&req).unwrap_err(),
            MutationAuthorityError::UntrustedSigner("outsider".into())
        );
    }

    #[test]
    fn rejects_a_mutation_id_mismatch() {
        let mut guard = guard();
        let mut req = request(1);
        req.receipt.as_mut().expect("receipt").mutation_id = "other".into();
        assert_eq!(
            guard.authorize(&req).unwrap_err(),
            MutationAuthorityError::MutationMismatch
        );
    }

    #[test]
    fn rejects_a_commit_mismatch() {
        let mut guard = guard();
        let mut req = request(1);
        req.receipt.as_mut().expect("receipt").commit_hash = "other".into();
        assert_eq!(
            guard.authorize(&req).unwrap_err(),
            MutationAuthorityError::CommitMismatch
        );
    }

    #[test]
    fn rejects_an_empty_verification_digest() {
        let mut guard = guard();
        let mut req = request(1);
        req.receipt.as_mut().expect("receipt").verification_digest = " ".into();
        assert!(matches!(
            guard.authorize(&req).unwrap_err(),
            MutationAuthorityError::EmptyField { .. }
        ));
    }

    #[test]
    fn rejects_a_stale_fence() {
        let mut guard = guard();
        guard.authorize(&request(2)).expect("first");
        let mut second = MutationRequest::new(
            "m-2",
            "commit-1",
            "commit-2",
            2,
            Some(AuthorityReceipt::seal(
                "m-2",
                "commit-2",
                "blake3:next",
                "ci-verifier",
                2,
            )),
        );
        assert_eq!(
            guard.authorize(&second).unwrap_err(),
            MutationAuthorityError::StaleFence {
                presented: 2,
                current: 2
            }
        );
        second.fencing_token = 3;
        second.receipt = Some(AuthorityReceipt::seal(
            "m-2",
            "commit-2",
            "blake3:next",
            "ci-verifier",
            3,
        ));
        assert!(guard.authorize(&second).is_ok());
    }

    #[test]
    fn rejects_a_commit_that_does_not_extend_the_guard() {
        let mut guard = guard();
        let req = MutationRequest::new("m-1", "other-base", "commit-1", 1, Some(receipt(1)));
        assert_eq!(
            guard.authorize(&req).unwrap_err(),
            MutationAuthorityError::LineageMismatch {
                expected: "genesis".into(),
                actual: "other-base".into()
            }
        );
    }

    #[test]
    fn rejects_a_tampered_signature() {
        let mut guard = guard();
        let mut req = request(1);
        req.receipt.as_mut().expect("receipt").signature = "0".repeat(64);
        assert_eq!(
            guard.authorize(&req).unwrap_err(),
            MutationAuthorityError::InvalidSignature
        );
    }

    #[test]
    fn rejects_zero_fencing_tokens() {
        let mut guard = guard();
        let mut req = request(1);
        req.fencing_token = 0;
        assert_eq!(
            guard.authorize(&req).unwrap_err(),
            MutationAuthorityError::InvalidFencingToken
        );
    }

    #[test]
    fn decision_log_is_bounded() {
        let mut guard = guard().with_max_decisions(1);
        guard.authorize(&request(1)).expect("first");
        let second = MutationRequest::new(
            "m-2",
            "commit-1",
            "commit-2",
            2,
            Some(AuthorityReceipt::seal(
                "m-2",
                "commit-2",
                "blake3:next",
                "ci-verifier",
                2,
            )),
        );
        guard.authorize(&second).expect("second");
        assert_eq!(guard.decisions().count(), 1);
        assert_eq!(
            guard.decisions().next().expect("decision").mutation_id,
            "m-2"
        );
    }

    #[test]
    fn requires_a_non_empty_genesis_and_signer() {
        assert!(MutationAuthorityGuard::new(["signer"], " ").is_err());
        assert!(MutationAuthorityGuard::new([" "], "genesis").is_err());
        assert!(MutationAuthorityGuard::new(std::iter::empty::<&str>(), "genesis").is_err());
    }
}
