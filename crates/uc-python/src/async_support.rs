//! Async support — bridges tokio and Python asyncio.
//!
//! Provides a shared tokio runtime for sync wrappers and helpers
//! for pyo3-async-runtimes integration.

use once_cell::sync::Lazy;
use tokio::runtime::Runtime;

/// Global tokio runtime used by sync PyO3 wrappers.
///
/// This runtime is created once and reused across all sync method calls.
/// It allows us to call async EngineApi methods from synchronous Python
/// code by blocking on them with `py.allow_threads()`.
static TOKIO_RUNTIME: Lazy<Runtime> =
    Lazy::new(|| Runtime::new().expect("Failed to create tokio runtime for PyO3 sync wrappers"));

/// Run an async future to completion on the shared runtime.
///
/// Intended for use inside `py.allow_threads()` blocks where the GIL
/// is released and we need to block the thread until the future completes.
pub fn block_on<F>(future: F) -> F::Output
where
    F: std::future::Future + Send,
    F::Output: Send,
{
    TOKIO_RUNTIME.block_on(future)
}
