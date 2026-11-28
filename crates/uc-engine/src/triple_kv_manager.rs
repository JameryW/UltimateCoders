//! Three-tier KV-cache residency with suffix-preserving recomputation (ledger item #103).
//!
//! TripleKV removes redundant DRAM copies of GPU-resident blocks and refills
//! evicted victims from SSD in a single tiered hierarchy. Penelope-style local
//! recomputation only recomputes the missing suffix of a prefix whose earlier
//! blocks are still cached. This module models the deterministic placement and
//! suffix accounting; it does not talk to a real inference engine.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use thiserror::Error;

pub const DEFAULT_GPU_BLOCKS: usize = 64;
pub const DEFAULT_DRAM_BLOCKS: usize = 256;
pub const DEFAULT_SSD_BLOCKS: usize = 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum KvTier {
    Gpu,
    Dram,
    Ssd,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct KvBlock {
    pub id: String,
    pub token_count: usize,
    pub tier: KvTier,
    pub last_access: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct KvPlan {
    pub block_id: String,
    pub hit_tier: Option<KvTier>,
    pub reused_tokens: usize,
    pub recompute_tokens: usize,
    pub evicted: Vec<(String, KvTier)>,
    pub refilled_from_ssd: bool,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct KvStats {
    pub gpu_hits: usize,
    pub dram_hits: usize,
    pub ssd_hits: usize,
    pub misses: usize,
    pub evictions: usize,
    pub ssd_reads: usize,
    pub reused_suffix_tokens: usize,
    pub recomputed_tokens: usize,
}

#[derive(Debug, Clone, Error, PartialEq, Eq)]
pub enum TripleKvError {
    #[error("block id must not be empty")]
    EmptyBlockId,
    #[error("token count must be positive")]
    ZeroTokens,
    #[error("capacity must be positive")]
    ZeroCapacity,
}

#[derive(Debug, Clone)]
pub struct TripleKvManager {
    gpu_capacity: usize,
    dram_capacity: usize,
    ssd_capacity: usize,
    blocks: BTreeMap<String, KvBlock>,
    clock: u64,
    stats: KvStats,
}

impl TripleKvManager {
    pub fn new(
        gpu_capacity: usize,
        dram_capacity: usize,
        ssd_capacity: usize,
    ) -> Result<Self, TripleKvError> {
        if gpu_capacity == 0 || dram_capacity == 0 || ssd_capacity == 0 {
            return Err(TripleKvError::ZeroCapacity);
        }
        Ok(Self {
            gpu_capacity,
            dram_capacity,
            ssd_capacity,
            blocks: BTreeMap::new(),
            clock: 0,
            stats: KvStats::default(),
        })
    }

    pub fn default_manager() -> Result<Self, TripleKvError> {
        Self::new(DEFAULT_GPU_BLOCKS, DEFAULT_DRAM_BLOCKS, DEFAULT_SSD_BLOCKS)
    }

    pub fn access(
        &mut self,
        block_id: impl Into<String>,
        token_count: usize,
        cached_prefix_tokens: usize,
    ) -> Result<KvPlan, TripleKvError> {
        let block_id = block_id.into();
        if block_id.trim().is_empty() {
            return Err(TripleKvError::EmptyBlockId);
        }
        if token_count == 0 {
            return Err(TripleKvError::ZeroTokens);
        }
        self.clock += 1;
        let mut evicted = Vec::new();
        let mut refilled_from_ssd = false;
        let mut hit_tier = None;
        let mut reused_tokens = 0usize;

        if let Some(block) = self.blocks.get_mut(&block_id) {
            block.last_access = self.clock;
        }

        if let Some(block) = self.blocks.get(&block_id) {
            hit_tier = Some(block.tier);
            reused_tokens = cached_prefix_tokens.min(block.token_count);
            match block.tier {
                KvTier::Gpu => self.stats.gpu_hits += 1,
                KvTier::Dram => self.stats.dram_hits += 1,
                KvTier::Ssd => {
                    self.stats.ssd_hits += 1;
                    self.stats.ssd_reads += 1;
                    refilled_from_ssd = true;
                }
            }
            if block.tier != KvTier::Gpu {
                self.move_to_gpu(&block_id, &mut evicted);
            }
        } else {
            self.stats.misses += 1;
            self.blocks.insert(
                block_id.clone(),
                KvBlock {
                    id: block_id.clone(),
                    token_count,
                    tier: KvTier::Gpu,
                    last_access: self.clock,
                },
            );
            self.enforce_gpu(&mut evicted);
        }

        if let Some(block) = self.blocks.get_mut(&block_id) {
            block.last_access = self.clock;
        }
        let recompute_tokens = token_count.saturating_sub(reused_tokens);
        self.stats.reused_suffix_tokens += reused_tokens;
        self.stats.recomputed_tokens += recompute_tokens;
        Ok(KvPlan {
            block_id,
            hit_tier,
            reused_tokens,
            recompute_tokens,
            evicted,
            refilled_from_ssd,
        })
    }

    fn move_to_gpu(&mut self, block_id: &str, evicted: &mut Vec<(String, KvTier)>) {
        if let Some(block) = self.blocks.get_mut(block_id) {
            block.tier = KvTier::Gpu;
        }
        self.enforce_gpu(evicted);
    }

    fn enforce_gpu(&mut self, evicted: &mut Vec<(String, KvTier)>) {
        while self.tier_size(KvTier::Gpu) > self.gpu_capacity {
            if let Some(victim) = self.oldest_in_tier(KvTier::Gpu) {
                self.demote(&victim, KvTier::Dram, evicted);
            } else {
                break;
            }
        }
        while self.tier_size(KvTier::Dram) > self.dram_capacity {
            if let Some(victim) = self.oldest_in_tier(KvTier::Dram) {
                self.demote(&victim, KvTier::Ssd, evicted);
            } else {
                break;
            }
        }
        while self.tier_size(KvTier::Ssd) > self.ssd_capacity {
            if let Some(victim) = self.oldest_in_tier(KvTier::Ssd) {
                self.blocks.remove(&victim);
                self.stats.evictions += 1;
                evicted.push((victim, KvTier::Ssd));
            } else {
                break;
            }
        }
    }

    fn demote(&mut self, block_id: &str, tier: KvTier, evicted: &mut Vec<(String, KvTier)>) {
        if let Some(block) = self.blocks.get_mut(block_id) {
            let old = block.tier;
            block.tier = tier;
            self.stats.evictions += 1;
            evicted.push((block_id.to_string(), old));
        }
    }

    fn tier_size(&self, tier: KvTier) -> usize {
        self.blocks
            .values()
            .filter(|block| block.tier == tier)
            .count()
    }

    fn oldest_in_tier(&self, tier: KvTier) -> Option<String> {
        self.blocks
            .values()
            .filter(|block| block.tier == tier)
            .min_by_key(|block| (block.last_access, block.id.clone()))
            .map(|block| block.id.clone())
    }

    pub fn tier_of(&self, block_id: &str) -> Option<KvTier> {
        self.blocks.get(block_id).map(|block| block.tier)
    }

    pub fn block_count(&self) -> usize {
        self.blocks.len()
    }

    pub fn stats(&self) -> &KvStats {
        &self.stats
    }

    pub fn reset_stats(&mut self) {
        self.stats = KvStats::default();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn capacities_must_be_positive() {
        assert!(matches!(
            TripleKvManager::new(0, 1, 1),
            Err(TripleKvError::ZeroCapacity)
        ));
        assert_eq!(TripleKvManager::new(1, 1, 1).unwrap().block_count(), 0);
    }

    #[test]
    fn gpu_hit_reuses_suffix() {
        let mut manager = TripleKvManager::new(2, 2, 2).unwrap();
        let first = manager.access("a", 100, 0).unwrap();
        assert_eq!(first.hit_tier, None);
        assert_eq!(first.recompute_tokens, 100);
        let second = manager.access("a", 100, 40).unwrap();
        assert_eq!(second.hit_tier, Some(KvTier::Gpu));
        assert_eq!(second.reused_tokens, 40);
        assert_eq!(second.recompute_tokens, 60);
        assert_eq!(manager.stats().gpu_hits, 1);
        assert_eq!(manager.stats().reused_suffix_tokens, 40);
    }

    #[test]
    fn gpu_pressure_demotes_to_dram() {
        let mut manager = TripleKvManager::new(1, 2, 2).unwrap();
        manager.access("a", 10, 0).unwrap();
        let plan = manager.access("b", 10, 0).unwrap();
        assert_eq!(plan.evicted, vec![("a".to_string(), KvTier::Gpu)]);
        assert_eq!(manager.tier_of("a"), Some(KvTier::Dram));
        assert_eq!(manager.stats().evictions, 1);
    }

    #[test]
    fn dram_refill_promotes_and_hits() {
        let mut manager = TripleKvManager::new(1, 1, 2).unwrap();
        manager.access("a", 10, 0).unwrap();
        manager.access("b", 10, 0).unwrap();
        let plan = manager.access("a", 10, 5).unwrap();
        assert_eq!(plan.hit_tier, Some(KvTier::Dram));
        assert_eq!(plan.reused_tokens, 5);
        assert!(!plan.refilled_from_ssd);
        assert_eq!(manager.tier_of("a"), Some(KvTier::Gpu));
    }

    #[test]
    fn ssd_refill_marks_ssd_read() {
        let mut manager = TripleKvManager::new(1, 1, 2).unwrap();
        manager.access("a", 10, 0).unwrap();
        manager.access("b", 10, 0).unwrap();
        manager.access("c", 10, 0).unwrap();
        assert_eq!(manager.tier_of("a"), Some(KvTier::Ssd));
        let plan = manager.access("a", 10, 3).unwrap();
        assert_eq!(plan.hit_tier, Some(KvTier::Ssd));
        assert!(plan.refilled_from_ssd);
        assert_eq!(manager.stats().ssd_reads, 1);
    }

    #[test]
    fn miss_recomputes_full_block() {
        let mut manager = TripleKvManager::new(2, 2, 2).unwrap();
        let plan = manager.access("missing", 50, 40).unwrap();
        assert_eq!(plan.hit_tier, None);
        assert_eq!(plan.reused_tokens, 0);
        assert_eq!(plan.recompute_tokens, 50);
        assert_eq!(manager.stats().misses, 1);
    }

    #[test]
    fn ssd_overflow_drops_oldest() {
        let mut manager = TripleKvManager::new(1, 1, 1).unwrap();
        manager.access("a", 10, 0).unwrap();
        manager.access("b", 10, 0).unwrap();
        manager.access("c", 10, 0).unwrap();
        manager.access("d", 10, 0).unwrap();
        assert_eq!(manager.tier_of("a"), None);
        assert_eq!(manager.block_count(), 3);
    }

    #[test]
    fn invalid_inputs_are_rejected() {
        let mut manager = TripleKvManager::new(1, 1, 1).unwrap();
        assert_eq!(manager.access("", 1, 0), Err(TripleKvError::EmptyBlockId));
        assert_eq!(manager.access("a", 0, 0), Err(TripleKvError::ZeroTokens));
    }

    #[test]
    fn stats_are_resettable() {
        let mut manager = TripleKvManager::new(2, 2, 2).unwrap();
        manager.access("a", 10, 0).unwrap();
        manager.access("a", 10, 1).unwrap();
        assert_eq!(manager.stats().gpu_hits, 1);
        manager.reset_stats();
        assert_eq!(manager.stats(), &KvStats::default());
    }

    #[test]
    fn default_manager_has_published_capacities() {
        let manager = TripleKvManager::default_manager().unwrap();
        assert_eq!(manager.block_count(), 0);
    }
}
