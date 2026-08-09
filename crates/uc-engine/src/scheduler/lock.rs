//! Distributed lock provider for multi-instance scheduler coordination.
//!
//! When multiple gateway instances run (HA/scaling), each runs its own
//! `SchedulerService`. Without coordination, a cron job fires on ALL
//! instances simultaneously, causing duplicate task submissions.
//!
//! The `LockProvider` trait abstracts a distributed lock so only ONE
//! instance fires each cron tick. The default `NoOpLockProvider` always
//! acquires (single-instance behavior — no regression).
//!
//! The real `NatsKvLockProvider` (in `uc-grpc`, feature-gated behind
//! `messaging`) uses NATS KV with a TTL lease for cross-instance coordination.

use std::time::Duration;

/// Trait for acquiring a distributed lock before dispatching a scheduled task.
///
/// Used by `SchedulerService` in the cron callback to ensure only one gateway
/// instance fires each cron tick. The lock key is per-tick
/// (`scheduler:{job_id}:{tick_timestamp}`), and the TTL auto-releases the lock
/// if the holder crashes.
///
/// # Default
///
/// `NoOpLockProvider` always returns `true` (always acquires). This is the
/// single-instance fallback — no coordination needed, no regression.
pub trait LockProvider: Send + Sync {
    /// Try to acquire a lock for the given key.
    ///
    /// Returns `true` if the lock was acquired (this instance should fire),
    /// `false` if another instance holds the lock (this instance should skip).
    ///
    /// The lock auto-releases after `ttl` (auto-release on crash).
    fn try_acquire(&self, key: &str, ttl: Duration) -> bool;
}

/// No-op lock provider — always acquires (single-instance fallback).
///
/// This is the default. When no NATS / no messaging feature is available,
/// the scheduler runs as a single instance and the lock is a no-op (always
/// acquire). This preserves the existing single-gateway behavior with no
/// regression.
#[derive(Debug, Clone, Default)]
pub struct NoOpLockProvider;

impl LockProvider for NoOpLockProvider {
    fn try_acquire(&self, _key: &str, _ttl: Duration) -> bool {
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn noop_lock_provider_always_acquires() {
        let provider = NoOpLockProvider;
        assert!(provider.try_acquire("any-key", Duration::from_secs(30)));
        assert!(provider.try_acquire("another-key", Duration::from_secs(60)));
    }

    #[test]
    fn noop_lock_provider_default() {
        let provider = NoOpLockProvider::default();
        assert!(provider.try_acquire("key", Duration::from_secs(10)));
    }

    #[test]
    fn noop_lock_provider_clone() {
        let provider = NoOpLockProvider;
        let cloned = provider.clone();
        assert!(cloned.try_acquire("key", Duration::from_secs(10)));
    }
}
