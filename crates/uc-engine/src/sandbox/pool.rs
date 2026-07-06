//! Sandbox pool -- manages warm sandbox instances for fast allocation.
//!
//! Pre-creates sandbox instances so that workers can acquire them
//! without waiting for container startup. Supports configurable pool
//! sizes and automatic warming.

use super::{EngineError, Sandbox, SandboxConfig, SandboxHandle, SandboxStatus};
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::Mutex;

/// Factory trait for creating sandbox instances.
///
/// This allows the pool to create new sandbox instances on demand.
#[async_trait::async_trait]
pub trait SandboxFactory: Send + Sync {
    /// Create a new sandbox with the given configuration.
    async fn create(&self, config: &SandboxConfig) -> Result<SandboxHandle, EngineError>;
}

/// Default factory that delegates to a `Sandbox` implementation.
struct DefaultSandboxFactory {
    sandbox: Arc<dyn Sandbox>,
}

#[async_trait::async_trait]
impl SandboxFactory for DefaultSandboxFactory {
    async fn create(&self, config: &SandboxConfig) -> Result<SandboxHandle, EngineError> {
        self.sandbox.create(config).await
    }
}

/// Pool of sandbox instances for fast allocation.
///
/// Maintains a set of pre-warmed idle sandboxes and tracks active ones.
/// When a worker needs a sandbox, it can acquire one from the pool
/// without waiting for creation.
pub struct SandboxPool {
    /// Factory for creating new sandbox instances.
    factory: Arc<dyn SandboxFactory>,
    /// Configuration used to create sandbox instances.
    config: SandboxConfig,
    /// Idle sandbox instances ready for use.
    idle: Mutex<Vec<SandboxHandle>>,
    /// Currently active (in-use) sandbox instances.
    active: Mutex<HashMap<String, SandboxHandle>>,
    /// Maximum total number of sandboxes (idle + active).
    max_pool_size: usize,
    /// Number of sandboxes to keep pre-warmed.
    warm_pool_size: usize,
}

impl SandboxPool {
    /// Create a new sandbox pool with the given sandbox implementation.
    pub fn new(
        sandbox: Arc<dyn Sandbox>,
        config: SandboxConfig,
        max_pool_size: usize,
        warm_pool_size: usize,
    ) -> Self {
        let factory = Arc::new(DefaultSandboxFactory { sandbox });
        Self {
            factory,
            config,
            idle: Mutex::new(Vec::new()),
            active: Mutex::new(HashMap::new()),
            max_pool_size,
            warm_pool_size,
        }
    }

    /// Create a new sandbox pool with a custom factory.
    pub fn with_factory(
        factory: Arc<dyn SandboxFactory>,
        config: SandboxConfig,
        max_pool_size: usize,
        warm_pool_size: usize,
    ) -> Self {
        Self {
            factory,
            config,
            idle: Mutex::new(Vec::new()),
            active: Mutex::new(HashMap::new()),
            max_pool_size,
            warm_pool_size,
        }
    }

    /// Acquire a sandbox from the pool.
    ///
    /// Returns an existing idle sandbox if available, or creates a new one
    /// if under the max pool size. Blocks if the pool is at capacity
    /// (returns an error in this implementation -- future versions may wait).
    pub async fn acquire(&self) -> Result<SandboxHandle, EngineError> {
        // 1. Try to take from idle pool
        let maybe_handle = {
            let mut idle = self.idle.lock().await;
            idle.pop()
        };
        // idle lock is released here before acquiring active lock

        if let Some(handle) = maybe_handle {
            let mut active = self.active.lock().await;
            let mut acquired = handle;
            acquired.status = SandboxStatus::Busy;
            active.insert(acquired.id.clone(), acquired.clone());
            return Ok(acquired);
        }

        // 2. Create new if under max pool size. Hold the active lock across
        // the count-check + create + insert so two concurrent acquirers
        // can't both pass the capacity check and exceed max_pool_size.
        // (Creation under the lock serializes spawns; acceptable for the
        // subprocess/docker factories which spawn quickly. Upgrade to a
        // counting semaphore if creation latency becomes a bottleneck.)
        let mut active = self.active.lock().await;
        let idle_count = self.idle.lock().await.len();
        if active.len() + idle_count < self.max_pool_size {
            let handle = self.factory.create(&self.config).await?;
            let mut acquired = handle;
            acquired.status = SandboxStatus::Busy;
            active.insert(acquired.id.clone(), acquired.clone());
            return Ok(acquired);
        }

        // 3. Pool is at capacity
        Err(EngineError::SandboxError(format!(
            "Sandbox pool at capacity ({}/{}). Wait for a sandbox to be released.",
            active.len() + idle_count,
            self.max_pool_size,
        )))
    }

    /// Release a sandbox back to the pool.
    ///
    /// If the idle pool is below the warm pool size, the sandbox
    /// is added back to the idle pool. Otherwise, it is stopped
    /// and discarded.
    pub async fn release(&self, handle: SandboxHandle) -> Result<(), EngineError> {
        // Remove from active
        {
            let mut active = self.active.lock().await;
            active.remove(&handle.id);
        }

        // If idle pool is below warm size, keep it; otherwise discard.
        // Hold the idle lock across the count-check + push so two concurrent
        // releases can't both pass the warm-size check and overflow idle.
        {
            let mut idle = self.idle.lock().await;
            if idle.len() < self.warm_pool_size {
                let mut released = handle;
                released.status = SandboxStatus::Ready;
                idle.push(released);
            }
        }
        // If not keeping it, just drop the handle.
        // For Docker sandboxes, the container was --rm so it's already gone.

        Ok(())
    }

    /// Pre-warm the pool by creating sandbox instances.
    ///
    /// Should be called at startup to ensure sandboxes are ready.
    pub async fn warm_up(&self) -> Result<(), EngineError> {
        let mut idle = self.idle.lock().await;
        let current_count = idle.len();

        for _ in current_count..self.warm_pool_size {
            let handle = self.factory.create(&self.config).await?;
            idle.push(handle);
        }

        tracing::info!(
            warm_count = idle.len(),
            warm_target = self.warm_pool_size,
            "Warmed up sandbox pool"
        );

        Ok(())
    }

    /// Get the number of idle sandboxes in the pool.
    pub async fn idle_count(&self) -> usize {
        self.idle.lock().await.len()
    }

    /// Get the number of active sandboxes.
    pub async fn active_count(&self) -> usize {
        self.active.lock().await.len()
    }

    /// Get the total number of sandboxes (idle + active).
    pub async fn total_count(&self) -> usize {
        self.idle.lock().await.len() + self.active.lock().await.len()
    }

    /// Stop all idle sandboxes and clear the pool.
    pub async fn drain(&self) -> Result<(), EngineError> {
        let mut idle = self.idle.lock().await;
        idle.clear();
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sandbox::subprocess::SubprocessSandbox;
    use crate::sandbox::NetworkMode;
    use std::collections::HashMap;

    fn test_config() -> SandboxConfig {
        SandboxConfig {
            project_path: "/tmp/test-project".to_string(),
            working_dir: "/tmp/test-project".to_string(),
            ..Default::default()
        }
    }

    #[tokio::test]
    async fn pool_acquire_and_release() {
        let sandbox = Arc::new(SubprocessSandbox::new());
        let config = test_config();
        let pool = SandboxPool::new(sandbox, config, 5, 2);

        // Acquire a sandbox
        let handle = pool.acquire().await.unwrap();
        assert_eq!(handle.status, SandboxStatus::Busy);
        assert_eq!(pool.active_count().await, 1);
        assert_eq!(pool.idle_count().await, 0);

        // Release it back
        pool.release(handle).await.unwrap();
        assert_eq!(pool.active_count().await, 0);
        assert_eq!(pool.idle_count().await, 1); // Kept because warm_pool_size=2
    }

    #[tokio::test]
    async fn pool_warm_up() {
        let sandbox = Arc::new(SubprocessSandbox::new());
        let config = test_config();
        let pool = SandboxPool::new(sandbox, config, 5, 3);

        pool.warm_up().await.unwrap();
        assert_eq!(pool.idle_count().await, 3);
    }

    #[tokio::test]
    async fn pool_max_capacity() {
        let sandbox = Arc::new(SubprocessSandbox::new());
        let config = test_config();
        let pool = SandboxPool::new(sandbox, config, 2, 0);

        // Acquire both slots
        let h1 = pool.acquire().await.unwrap();
        let h2 = pool.acquire().await.unwrap();

        // Third should fail
        let result = pool.acquire().await;
        assert!(result.is_err());
        match result.unwrap_err() {
            EngineError::SandboxError(msg) => assert!(msg.contains("capacity")),
            other => panic!("Expected SandboxError, got {:?}", other),
        }

        // Release one and try again
        pool.release(h1).await.unwrap();
        let h3 = pool.acquire().await.unwrap();
        assert!(!h3.id.is_empty());

        pool.release(h2).await.unwrap();
        pool.release(h3).await.unwrap();
    }

    #[tokio::test]
    async fn pool_drain() {
        let sandbox = Arc::new(SubprocessSandbox::new());
        let config = test_config();
        let pool = SandboxPool::new(sandbox, config, 5, 3);

        pool.warm_up().await.unwrap();
        assert_eq!(pool.idle_count().await, 3);

        pool.drain().await.unwrap();
        assert_eq!(pool.idle_count().await, 0);
    }

    #[tokio::test]
    async fn pool_release_respects_warm_size() {
        let sandbox = Arc::new(SubprocessSandbox::new());
        let config = test_config();
        let pool = SandboxPool::new(sandbox, config, 10, 2);

        // Acquire 4 sandboxes
        let handles: Vec<_> = futures::future::join_all((0..4).map(|_| pool.acquire()))
            .await
            .into_iter()
            .map(|r| r.unwrap())
            .collect();

        assert_eq!(pool.active_count().await, 4);

        // Release all 4
        for h in handles {
            pool.release(h).await.unwrap();
        }

        // Only warm_pool_size (2) should remain idle
        assert_eq!(pool.idle_count().await, 2);
        assert_eq!(pool.active_count().await, 0);
    }

    #[tokio::test]
    async fn pool_total_count() {
        let sandbox = Arc::new(SubprocessSandbox::new());
        let config = test_config();
        let pool = SandboxPool::new(sandbox, config, 5, 2);

        pool.warm_up().await.unwrap();
        assert_eq!(pool.total_count().await, 2);

        let h1 = pool.acquire().await.unwrap();
        assert_eq!(pool.total_count().await, 2);

        pool.release(h1).await.unwrap();
        assert_eq!(pool.total_count().await, 2);
    }
}
