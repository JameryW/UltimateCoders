//! PyO3 Python binding for UltimateCoders engine.
//!
//! Exposes a single `Engine` class that switches between
//! LocalEngine (PyO3 FFI) and GrpcEngineClient (tonic) at construction time.

mod engine;
mod scheduler;
mod types;
mod async_support;

use pyo3::prelude::*;

/// Python module: `ultimate_coders._uc_core`
#[pymodule]
fn _uc_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<engine::PyEngine>()?;
    // Health types
    m.add_class::<types::PyHealthStatus>()?;
    m.add_class::<types::PyComponentHealth>()?;
    // Search types
    m.add_class::<types::PySearchQuery>()?;
    m.add_class::<types::PySearchResult>()?;
    m.add_class::<types::PySearchResultItem>()?;
    m.add_class::<types::PyAstQuery>()?;
    // Memory types
    m.add_class::<types::PyMemoryEntry>()?;
    m.add_class::<types::PyMemorySearchResult>()?;
    // Index types
    m.add_class::<types::PyIndexResponse>()?;
    m.add_class::<types::PyRepoIndexState>()?;
    m.add_class::<types::PyIndexState>()?;
    // Scheduler types
    m.add_class::<scheduler::PySchedulerService>()?;
    m.add_class::<scheduler::PyScheduledTask>()?;
    m.add_class::<scheduler::PyExecutionHistory>()?;
    Ok(())
}
