//! HerAgent (#43): semantic setup blocks, checkpoints, and failure relocation.
//!
//! Environment setup is modelled as an ordered sequence of seven semantic blocks
//! (system packages, language runtime, project dependencies, extra runtimes,
//! native build configuration, test toolchain, and a final verification block).
//! A block becomes a checkpoint only after its stage verifier succeeds. When a
//! later block fails, the controller can relocate the root cause to an earlier
//! block, patch that block back, and replay the tail in a clean-room container.
//!
//! The module is pure: it stores metadata, checkpoints, and replay plans. It
//! does not run Docker, execute a shell, or mutate a real workspace.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Default number of semantic setup blocks for one environment.
pub const DEFAULT_MAX_BLOCKS: usize = 7;
/// Default bound on retained checkpoints.
pub const DEFAULT_MAX_CHECKPOINTS: usize = 64;

/// The seven semantic setup block classes from the ledger.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SetupBlockKind {
    SystemPackages,
    LanguageRuntime,
    ProjectDependencies,
    ExtraRuntime,
    NativeBuild,
    TestToolchain,
    Verification,
}

/// One declarative environment-setup block.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SetupBlock {
    pub id: String,
    pub name: String,
    pub kind: SetupBlockKind,
    pub purpose: String,
    pub prelude: Vec<String>,
    pub script: String,
    pub verify_command: String,
}

impl SetupBlock {
    /// Build a block with the required shell fields and no extra prelude.
    pub fn new(
        id: impl Into<String>,
        name: impl Into<String>,
        kind: SetupBlockKind,
        purpose: impl Into<String>,
        script: impl Into<String>,
        verify_command: impl Into<String>,
    ) -> Self {
        Self {
            id: id.into(),
            name: name.into(),
            kind,
            purpose: purpose.into(),
            prelude: Vec::new(),
            script: script.into(),
            verify_command: verify_command.into(),
        }
    }

    /// Attach persistent environment variables/commands (the `Prelude` field).
    pub fn with_prelude(mut self, prelude: impl IntoIterator<Item = impl Into<String>>) -> Self {
        self.prelude = prelude.into_iter().map(Into::into).collect();
        self
    }

    fn validate(&self) -> Result<(), HerError> {
        require_non_empty("id", &self.id)?;
        require_non_empty("name", &self.name)?;
        require_non_empty("purpose", &self.purpose)?;
        require_non_empty("script", &self.script)?;
        require_non_empty("verify_command", &self.verify_command)?;
        Ok(())
    }
}

/// A verified checkpoint for one block.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BlockCheckpoint {
    pub block_id: String,
    pub seq: u32,
    pub digest: String,
    pub variables: BTreeMap<String, String>,
}

/// A completed checkpoint, returned by [`HerBlockController::checkpoint`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CheckpointOutcome {
    pub block_id: String,
    pub seq: u32,
    pub digest: String,
    pub next_block: Option<String>,
}

/// The result of relocating a failure to an earlier semantic block.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FailureRelocation {
    pub failed_block: String,
    pub suspected_root: String,
    pub rollback_to: Option<String>,
    pub patch_back_required: bool,
    pub replay_required: bool,
    pub reason: String,
}

/// A patch-back record for one block.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PatchBack {
    pub block_id: String,
    pub previous_digest: String,
    pub new_digest: String,
    pub truncated_checkpoints: usize,
}

/// A clean-room replay plan.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReplayPlan {
    pub from_checkpoint: Option<String>,
    pub replay_blocks: Vec<String>,
    pub clean_room: bool,
}

/// Errors from the setup-block state machine.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum HerError {
    #[error("setup block field `{field}` must not be empty")]
    EmptyField { field: &'static str },
    #[error("duplicate setup block `{0}`")]
    DuplicateBlock(String),
    #[error("unknown setup block `{0}`")]
    UnknownBlock(String),
    #[error("setup block capacity reached")]
    CapacityReached,
    #[error("checkpoint must follow block order; expected `{expected}`")]
    OutOfOrder { expected: String },
    #[error("block `{0}` is already checkpointed")]
    DuplicateCheckpoint(String),
    #[error("checkpoint digest must not be empty")]
    EmptyDigest,
    #[error("checkpoint capacity reached")]
    CheckpointCapacityReached,
    #[error("root cause `{root}` must precede the failed block `{failed}`")]
    RootNotBeforeFailure { root: String, failed: String },
}

fn require_non_empty(field: &'static str, value: &str) -> Result<(), HerError> {
    if value.trim().is_empty() {
        Err(HerError::EmptyField { field })
    } else {
        Ok(())
    }
}

/// Bounded controller for an ordered environment-setup sequence.
#[derive(Debug, Clone)]
pub struct HerBlockController {
    blocks: Vec<SetupBlock>,
    checkpoints: Vec<BlockCheckpoint>,
    patch_backs: Vec<PatchBack>,
    max_blocks: usize,
    max_checkpoints: usize,
    replay_required: bool,
}

impl Default for HerBlockController {
    fn default() -> Self {
        Self::with_capacity(DEFAULT_MAX_BLOCKS, DEFAULT_MAX_CHECKPOINTS)
    }
}

impl HerBlockController {
    /// Build a controller with the default bounds.
    pub fn new() -> Self {
        Self::default()
    }

    /// Build a controller with explicit block and checkpoint bounds.
    pub fn with_capacity(max_blocks: usize, max_checkpoints: usize) -> Self {
        Self {
            blocks: Vec::new(),
            checkpoints: Vec::new(),
            patch_backs: Vec::new(),
            max_blocks,
            max_checkpoints,
            replay_required: false,
        }
    }

    /// Register one setup block in execution order.
    pub fn add_block(&mut self, block: SetupBlock) -> Result<(), HerError> {
        block.validate()?;
        if self.blocks.len() >= self.max_blocks {
            return Err(HerError::CapacityReached);
        }
        if self.blocks.iter().any(|existing| existing.id == block.id) {
            return Err(HerError::DuplicateBlock(block.id));
        }
        self.blocks.push(block);
        Ok(())
    }

    /// Registered blocks in execution order.
    pub fn blocks(&self) -> &[SetupBlock] {
        &self.blocks
    }

    /// Retained checkpoints in execution order.
    pub fn checkpoints(&self) -> &[BlockCheckpoint] {
        &self.checkpoints
    }

    /// Patch-back records.
    pub fn patch_backs(&self) -> &[PatchBack] {
        &self.patch_backs
    }

    /// Whether a patch-back invalidated the tail of the checkpoint chain.
    pub fn replay_required(&self) -> bool {
        self.replay_required
    }

    fn block_index(&self, block_id: &str) -> Option<usize> {
        self.blocks.iter().position(|block| block.id == block_id)
    }

    /// Record a successful stage verification as a checkpoint.
    pub fn checkpoint(
        &mut self,
        block_id: &str,
        digest: impl Into<String>,
        variables: BTreeMap<String, String>,
    ) -> Result<CheckpointOutcome, HerError> {
        let digest = digest.into();
        if digest.trim().is_empty() {
            return Err(HerError::EmptyDigest);
        }
        let index = self
            .block_index(block_id)
            .ok_or_else(|| HerError::UnknownBlock(block_id.to_string()))?;
        if self
            .checkpoints
            .iter()
            .any(|checkpoint| checkpoint.block_id == block_id)
        {
            return Err(HerError::DuplicateCheckpoint(block_id.to_string()));
        }
        let expected = self
            .blocks
            .get(self.checkpoints.len())
            .map(|block| block.id.clone())
            .unwrap_or_default();
        if index != self.checkpoints.len() {
            return Err(HerError::OutOfOrder { expected });
        }
        if self.checkpoints.len() >= self.max_checkpoints {
            return Err(HerError::CheckpointCapacityReached);
        }
        let seq = self.checkpoints.len() as u32;
        self.checkpoints.push(BlockCheckpoint {
            block_id: block_id.to_string(),
            seq,
            digest: digest.clone(),
            variables,
        });
        self.replay_required = false;
        Ok(CheckpointOutcome {
            block_id: block_id.to_string(),
            seq,
            digest,
            next_block: self.blocks.get(index + 1).map(|block| block.id.clone()),
        })
    }

    /// The most recent verified checkpoint.
    pub fn latest_checkpoint(&self) -> Option<&BlockCheckpoint> {
        self.checkpoints.last()
    }

    /// Whether every registered block has a checkpoint in order.
    pub fn verify_sequence(&self) -> bool {
        self.blocks.len() == self.checkpoints.len()
            && self
                .blocks
                .iter()
                .zip(self.checkpoints.iter())
                .all(|(block, checkpoint)| block.id == checkpoint.block_id)
    }

    /// Relocate a later failure to an earlier block and roll back before it.
    ///
    /// The caller supplies the suspected root cause. The controller validates
    /// that it precedes the failed block and returns the last checkpoint before
    /// the root, which is the first state that must be rebuilt.
    pub fn relocate_failure(
        &mut self,
        failed_block: &str,
        suspected_root: &str,
        reason: impl Into<String>,
    ) -> Result<FailureRelocation, HerError> {
        let failed_index = self
            .block_index(failed_block)
            .ok_or_else(|| HerError::UnknownBlock(failed_block.to_string()))?;
        let root_index = self
            .block_index(suspected_root)
            .ok_or_else(|| HerError::UnknownBlock(suspected_root.to_string()))?;
        if root_index >= failed_index {
            return Err(HerError::RootNotBeforeFailure {
                root: suspected_root.to_string(),
                failed: failed_block.to_string(),
            });
        }
        let rollback_to = self
            .checkpoints
            .iter()
            .rev()
            .find(|checkpoint| {
                self.block_index(&checkpoint.block_id)
                    .is_some_and(|index| index < root_index)
            })
            .map(|checkpoint| checkpoint.block_id.clone());
        self.replay_required = true;
        Ok(FailureRelocation {
            failed_block: failed_block.to_string(),
            suspected_root: suspected_root.to_string(),
            rollback_to,
            patch_back_required: true,
            replay_required: true,
            reason: reason.into(),
        })
    }

    /// Rewrite one block after a relocated failure and invalidate its tail.
    pub fn patch_back(
        &mut self,
        block_id: &str,
        new_script: impl Into<String>,
        new_digest: impl Into<String>,
    ) -> Result<PatchBack, HerError> {
        let new_script = new_script.into();
        let new_digest = new_digest.into();
        require_non_empty("script", &new_script)?;
        if new_digest.trim().is_empty() {
            return Err(HerError::EmptyDigest);
        }
        let index = self
            .block_index(block_id)
            .ok_or_else(|| HerError::UnknownBlock(block_id.to_string()))?;
        let previous_digest = self
            .checkpoints
            .iter()
            .find(|checkpoint| checkpoint.block_id == block_id)
            .map(|checkpoint| checkpoint.digest.clone())
            .unwrap_or_default();
        let before = self.checkpoints.len();
        let keep_ids: Vec<String> = self.blocks[..index]
            .iter()
            .map(|block| block.id.clone())
            .collect();
        self.checkpoints
            .retain(|checkpoint| keep_ids.iter().any(|id| id == &checkpoint.block_id));
        let truncated = before - self.checkpoints.len();
        self.blocks[index].script = new_script;
        self.replay_required = true;
        let record = PatchBack {
            block_id: block_id.to_string(),
            previous_digest,
            new_digest,
            truncated_checkpoints: truncated,
        };
        self.patch_backs.push(record.clone());
        Ok(record)
    }

    /// The clean-room replay required after a relocation or patch-back.
    pub fn replay_plan(&self) -> ReplayPlan {
        let from_checkpoint = self
            .checkpoints
            .last()
            .map(|checkpoint| checkpoint.block_id.clone());
        let next_index = from_checkpoint
            .as_deref()
            .and_then(|block_id| self.block_index(block_id))
            .map_or(0, |index| index + 1);
        ReplayPlan {
            from_checkpoint,
            replay_blocks: self.blocks[next_index..]
                .iter()
                .map(|block| block.id.clone())
                .collect(),
            clean_room: true,
        }
    }

    /// A bounded diagnostic snapshot.
    pub fn snapshot(&self) -> HerSnapshot {
        HerSnapshot {
            blocks: self
                .blocks
                .iter()
                .map(|block| block.id.clone())
                .collect::<BTreeSet<_>>()
                .into_iter()
                .collect(),
            checkpointed: self
                .checkpoints
                .iter()
                .map(|checkpoint| checkpoint.block_id.clone())
                .collect(),
            patch_backs: self.patch_backs.len(),
            replay_required: self.replay_required,
            verified: self.verify_sequence(),
        }
    }
}

/// A bounded, serialisable diagnostic snapshot.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HerSnapshot {
    pub blocks: Vec<String>,
    pub checkpointed: Vec<String>,
    pub patch_backs: usize,
    pub replay_required: bool,
    pub verified: bool,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn block(id: &str, kind: SetupBlockKind) -> SetupBlock {
        SetupBlock::new(
            id,
            format!("{id} block"),
            kind,
            format!("setup {id}"),
            format!("echo {id}"),
            format!("verify-{id}"),
        )
    }

    fn controller() -> HerBlockController {
        let mut controller = HerBlockController::new();
        for (id, kind) in [
            ("sys", SetupBlockKind::SystemPackages),
            ("runtime", SetupBlockKind::LanguageRuntime),
            ("deps", SetupBlockKind::ProjectDependencies),
        ] {
            controller.add_block(block(id, kind)).expect("block");
        }
        controller
    }

    fn vars() -> BTreeMap<String, String> {
        BTreeMap::from([("PATH".to_string(), "/usr/bin".to_string())])
    }

    #[test]
    fn checkpoints_follow_block_order() {
        let mut controller = controller();
        let err = controller
            .checkpoint("deps", "sha256:deps", vars())
            .unwrap_err();
        assert_eq!(
            err,
            HerError::OutOfOrder {
                expected: "sys".to_string()
            }
        );
        controller
            .checkpoint("sys", "sha256:sys", vars())
            .expect("first checkpoint");
        assert!(!controller.verify_sequence());
        controller
            .checkpoint("runtime", "sha256:runtime", vars())
            .expect("second checkpoint");
        controller
            .checkpoint("deps", "sha256:deps", vars())
            .expect("third checkpoint");
        assert!(controller.verify_sequence());
        assert_eq!(controller.latest_checkpoint().expect("latest").seq, 2);
    }

    #[test]
    fn duplicate_and_invalid_blocks_are_rejected() {
        let mut controller = controller();
        assert_eq!(
            controller
                .add_block(block("sys", SetupBlockKind::Verification))
                .unwrap_err(),
            HerError::DuplicateBlock("sys".to_string())
        );
        let mut empty = block("empty", SetupBlockKind::Verification);
        empty.script = " ".to_string();
        assert_eq!(
            controller.add_block(empty).unwrap_err(),
            HerError::EmptyField { field: "script" }
        );
    }

    #[test]
    fn failure_relocation_rolls_back_before_the_root() {
        let mut controller = controller();
        controller
            .checkpoint("sys", "sha256:sys", vars())
            .expect("sys");
        controller
            .checkpoint("runtime", "sha256:runtime", vars())
            .expect("runtime");
        let relocation = controller
            .relocate_failure("deps", "runtime", "dependency version conflict")
            .expect("relocation");
        assert_eq!(relocation.failed_block, "deps");
        assert_eq!(relocation.suspected_root, "runtime");
        assert_eq!(relocation.rollback_to.as_deref(), Some("sys"));
        assert!(relocation.patch_back_required);
        assert!(controller.replay_required());
    }

    #[test]
    fn root_must_precede_the_failure() {
        let mut controller = controller();
        assert_eq!(
            controller
                .relocate_failure("sys", "deps", "impossible")
                .unwrap_err(),
            HerError::RootNotBeforeFailure {
                root: "deps".to_string(),
                failed: "sys".to_string(),
            }
        );
    }

    #[test]
    fn patch_back_truncates_the_tail_and_plans_replay() {
        let mut controller = controller();
        for (id, digest) in [("sys", "sha256:sys"), ("runtime", "sha256:runtime")] {
            controller
                .checkpoint(id, digest, vars())
                .expect("checkpoint");
        }
        let patch = controller
            .patch_back("runtime", "echo fixed", "sha256:runtime-fixed")
            .expect("patch back");
        assert_eq!(patch.truncated_checkpoints, 1);
        assert_eq!(controller.checkpoints().len(), 1);
        let plan = controller.replay_plan();
        assert_eq!(plan.from_checkpoint.as_deref(), Some("sys"));
        assert_eq!(plan.replay_blocks, vec!["runtime", "deps"]);
        assert!(plan.clean_room);
    }

    #[test]
    fn patch_back_at_the_first_block_has_no_checkpoint() {
        let mut controller = controller();
        controller
            .checkpoint("sys", "sha256:sys", vars())
            .expect("sys");
        let patch = controller
            .patch_back("sys", "echo fixed", "sha256:sys-fixed")
            .expect("patch back");
        assert_eq!(patch.truncated_checkpoints, 1);
        assert_eq!(controller.replay_plan().from_checkpoint, None);
        assert_eq!(
            controller.replay_plan().replay_blocks,
            vec!["sys", "runtime", "deps"]
        );
    }

    #[test]
    fn checkpoint_validation_rejects_unknown_blocks_and_empty_digests() {
        let mut controller = controller();
        assert!(matches!(
            controller.checkpoint("missing", "sha256:x", vars()),
            Err(HerError::UnknownBlock(_))
        ));
        assert_eq!(
            controller.checkpoint("sys", " ", vars()).unwrap_err(),
            HerError::EmptyDigest
        );
    }

    #[test]
    fn capacity_is_enforced() {
        let mut controller = HerBlockController::with_capacity(1, 1);
        controller
            .add_block(block("sys", SetupBlockKind::SystemPackages))
            .expect("first");
        assert_eq!(
            controller
                .add_block(block("runtime", SetupBlockKind::LanguageRuntime))
                .unwrap_err(),
            HerError::CapacityReached
        );
        controller
            .checkpoint("sys", "sha256:sys", vars())
            .expect("checkpoint");
        let mut two = HerBlockController::with_capacity(2, 1);
        two.add_block(block("a", SetupBlockKind::SystemPackages))
            .expect("a");
        two.add_block(block("b", SetupBlockKind::LanguageRuntime))
            .expect("b");
        two.checkpoint("a", "sha256:a", vars()).expect("a");
        assert_eq!(
            two.checkpoint("b", "sha256:b", vars()).unwrap_err(),
            HerError::CheckpointCapacityReached
        );
    }

    #[test]
    fn snapshot_is_bounded_and_deterministic() {
        let mut controller = controller();
        controller
            .checkpoint("sys", "sha256:sys", vars())
            .expect("sys");
        let snapshot = controller.snapshot();
        assert_eq!(snapshot.blocks, vec!["deps", "runtime", "sys"]);
        assert_eq!(snapshot.checkpointed, vec!["sys"]);
        assert!(!snapshot.verified);
    }
}
