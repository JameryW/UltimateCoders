# Research: Rust + Python Hybrid Architecture with PyO3 FFI + gRPC Dual-Mode Bridging

- **Query**: Best practices for Rust + Python hybrid architecture with PyO3 FFI + gRPC dual-mode bridging
- **Scope**: External (mature project patterns, architectural patterns, async bridging, monorepo layout)
- **Date**: 2026-06-09

## Findings

### 1. How Mature Projects Structure Rust Core + Python Binding

#### HuggingFace Tokenizers

**Repository**: `huggingface/tokenizers`

Structure pattern:
```
tokenizers/
├── bindings/          # All language bindings
│   └── python/       # Python-specific binding code
│       ├── src/      # Rust lib.rs that defines PyO3 classes
│       │   └── lib.rs
│       ├── tokenizers/  # Pure Python wrapper/ergonomic API
│       │   ├── __init__.py
│       │   └── ...
│       ├── Cargo.toml   # PyO3-specific crate config
│       └── pyproject.toml
├── tokenizers/        # Core Rust library (no Python dependency)
│   ├── src/
│   │   ├── lib.rs
│   │   ├── models/
│   │   ├── pre_tokenizers/
│   │   └── ...
│   └── Cargo.toml
└── Cargo.toml         # Workspace root
```

Key patterns:
- **Separation of concerns**: The core Rust crate (`tokenizers/`) has zero Python dependency. The Python binding (`bindings/python/src/lib.rs`) is a thin wrapper that only does type conversion and PyO3 class registration.
- **PyO3 class mapping**: Each Rust struct gets a `#[pyclass]` attribute, methods get `#[pymethods]`. The binding layer does NOT contain business logic.
- **Build system**: Uses `setuptools-rust` (now migrating to `maturin`) for building. The `Cargo.toml` in the Python binding directory declares `pyo3` as a dependency with `extension-module` feature.
- **Ergonomic Python layer**: The `tokenizers/` Python package wraps the raw PyO3 module with a more Pythonic API (context managers, property decorators, etc.).

#### Polars

**Repository**: `pola-rs/polars`

Structure pattern:
```
polars/
├── crates/
│   ├── polars/           # Top-level crate (re-exports)
│   ├── polars-core/      # Core DataFrame/Series logic
│   ├── polars-lazy/      # LazyFrame query engine
│   ├── polars-io/        # I/O (CSV, Parquet, JSON, etc.)
│   ├── polars-ops/       # Operations (aggregations, joins)
│   ├── polars-plan/      # Query plan
│   ├── polars-utils/     # Shared utilities
│   └── py-polars/        # Python binding crate
│       ├── src/          # Rust PyO3 binding code
│       │   ├── lib.rs
│       │   ├── dataframe/
│       │   ├── lazy/
│       │   └── series/
│       ├── polars/       # Pure Python ergonomic layer
│       │   ├── __init__.py
│       │   ├── dataframe/
│       │   ├── series/
│       │   └── ...
│       ├── Cargo.toml
│       └── pyproject.toml
└── Cargo.toml            # Workspace root
```

Key patterns:
- **Crate-per-feature**: Each major feature area is its own Rust crate. `py-polars` depends on the other crates but none depend on `py-polars`.
- **Feature flags**: Rust crates use Cargo feature flags extensively. The Python binding crate enables specific features when building for Python.
- **Arrow interop**: Uses Apache Arrow as the zero-copy data interchange format between Rust and Python. This is critical for performance -- no serialization overhead for DataFrame data.
- **Lazy evaluation**: The `polars-lazy` crate implements the query optimizer. Python `LazyFrame` calls map 1:1 to Rust `LazyFrame` methods. The actual execution happens entirely in Rust.

#### PyO3 Ecosystem Conventions

**Repository**: `PyO3/pyo3`

Key crates in the ecosystem:
- `pyo3` (v0.22+): Core FFI bindings, `#[pyclass]`, `#[pymethods]`, type conversions
- `pyo3-async-runtimes` (v0.22+): Async runtime integration (replaces deprecated `pyo3-asyncio`)
  - `pyo3-async-runtimes-tokio`: Tokio-specific backend
  - `pyo3-async-runtimes-async-std`: async-std backend
- `pyo3-build-config`: Build-time configuration
- `maturin` (v1.7+): Build and publish tool for Rust-based Python packages

### 2. Unified Interface Pattern: PyO3 Local + gRPC Remote Runtime Switching

This is the most architecturally significant pattern for this project. The goal is: **one Python API surface, two backends that switch at runtime based on deployment topology**.

#### Pattern A: Protocol-Trait Abstraction (Recommended)

Define a Rust trait that represents the core API, then implement it twice:

```rust
// In core crate (no Python/gRPC dependency)
pub trait EngineApi: Send + Sync {
    fn search(&self, query: SearchQuery) -> Result<SearchResult, EngineError>;
    fn index_repo(&self, repo: RepoSpec) -> Result<(), EngineError>;
    fn read_memory(&self, key: &str) -> Result<Option<MemoryEntry>, EngineError>;
    fn write_memory(&self, entry: MemoryEntry) -> Result<(), EngineError>;
}

// Local implementation (in-process)
pub struct LocalEngine {
    // Direct references to core components
    indexer: Arc<Indexer>,
    memory_store: Arc<MemoryStore>,
    searcher: Arc<Searcher>,
}

impl EngineApi for LocalEngine { /* direct calls */ }

// gRPC client implementation
pub struct GrpcEngineClient {
    channel: tonic::transport::Channel,
}

impl EngineApi for GrpcEngineClient { /* gRPC calls */ }
```

Then in the PyO3 binding layer:

```rust
// In py-bindings crate
#[pyclass]
struct Engine {
    inner: Box<dyn EngineApi>,
}

#[pymethods]
impl Engine {
    #[new]
    #[pyo3(signature = (mode="local", grpc_endpoint=None))]
    fn new(mode: &str, grpc_endpoint: Option<&str>) -> PyResult<Self> {
        let inner: Box<dyn EngineApi> = match mode {
            "local" => Box::new(LocalEngine::new()?),
            "grpc" => {
                let endpoint = grpc_endpoint.ok_or_else(|| {
                    PyErr::new::<pyo3::exceptions::PyValueError, _>(
                        "grpc_endpoint required for grpc mode"
                    )
                })?;
                Box::new(GrpcEngineClient::connect(endpoint)?)
            }
            _ => return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(
                "mode must be 'local' or 'grpc'"
            )),
        };
        Ok(Engine { inner })
    }

    fn search(&self, py: Python<'_>, query: &SearchQueryPy) -> PyResult<SearchResultPy> {
        // For local mode: release GIL during Rust computation
        py.allow_threads(|| {
            self.inner.search(query.into())
                .map_err(|e| PyErr::from(e))
        })
        .map(Into::into)
    }
}
```

#### Pattern B: Python-Side Abstract Base Class

Define the abstraction in Python, with two concrete implementations:

```python
# In Python ergonomic layer
from abc import ABC, abstractmethod
from typing import Optional
from . import _rust_core  # PyO3 native module

class EngineApi(ABC):
    @abstractmethod
    def search(self, query: SearchQuery) -> SearchResult: ...
    @abstractmethod
    def index_repo(self, repo: RepoSpec) -> None: ...
    @abstractmethod
    def read_memory(self, key: str) -> Optional[MemoryEntry]: ...

class LocalEngine(EngineApi):
    """Uses PyO3 FFI -- Rust runs in-process."""
    def __init__(self):
        self._engine = _rust_core.LocalEngine()

    def search(self, query: SearchQuery) -> SearchResult:
        return self._engine.search(query)

class GrpcEngine(EngineApi):
    """Uses gRPC -- Rust runs in a remote service."""
    def __init__(self, endpoint: str):
        self._channel = _rust_core.GrpcChannel(endpoint)
        self._client = _rust_core.GrpcEngineClient(self._channel)

    def search(self, query: SearchQuery) -> SearchResult:
        return self._client.search(query)

def create_engine(mode: str = "local", **kwargs) -> EngineApi:
    if mode == "local":
        return LocalEngine()
    elif mode == "grpc":
        return GrpcEngine(endpoint=kwargs["grpc_endpoint"])
    raise ValueError(f"Unknown mode: {mode}")
```

#### Pattern C: Hybrid (Recommended for This Project)

Combine both: Rust trait for type safety + Python ABC for ergonomics. The Rust trait ensures the gRPC client and local engine have identical method signatures at compile time. The Python ABC provides a clean interface for Python consumers and allows Python-level extensions (caching, retries, logging).

```python
# Python layer
class Engine:
    """Unified engine interface. Switches between local (PyO3) and remote (gRPC)."""
    def __init__(self, mode: str = "local", **kwargs):
        if mode == "local":
            self._impl = _rust_core.LocalEngine()
        elif mode == "grpc":
            self._impl = _rust_core.GrpcEngineClient(kwargs["endpoint"])
        else:
            raise ValueError(f"Unknown mode: {mode}")

    def search(self, query: SearchQuery) -> SearchResult:
        # Python-level cross-cutting concerns (logging, metrics, retries)
        logger.debug(f"search: mode={self._mode}, query={query}")
        result = self._impl.search(query)
        metrics.increment("engine.search", tags={"mode": self._mode})
        return result
```

**Key design decisions for the unified interface:**

1. **Error normalization**: Both PyO3 and gRPC paths must map to the same Python exception hierarchy. PyO3 naturally raises Python exceptions. gRPC errors (tonic Status codes) must be mapped to the same Python exception types in the binding layer.

2. **Type consistency**: The protobuf message types and the PyO3 `#[pyclass]` types should share the same Rust struct definitions. Use a shared `types` crate:
   ```
   crates/
   ├── types/        # Shared data types (no PyO3/gRPC dependency)
   ├── engine/       # Core logic + EngineApi trait
   ├── grpc-server/  # tonic service implementation
   ├── grpc-client/  # tonic client + EngineApi impl
   └── py-bindings/  # PyO3 classes + EngineApi impl
   ```

3. **GIL handling**: For local mode, `py.allow_threads()` is essential to release the GIL during long Rust computations. For gRPC mode, the GIL is naturally released during network I/O (the Python async event loop handles this).

4. **Configuration**: Use a single config format (TOML or YAML) that specifies mode, endpoints, timeouts, etc. Both Rust and Python can read this.

### 3. Async Rust Code Called from Python via PyO3 (Tokio Runtime Bridging)

This is one of the trickiest parts of the architecture. There are several patterns, each with tradeoffs.

#### The Core Problem

Python has its own async event loop (asyncio). Rust/tokio has its own runtime. You cannot simply call an async Rust function from Python and have it "just work" -- you need to bridge the two runtimes.

#### Pattern A: Dedicated Tokio Runtime (Most Common)

Spawn a dedicated tokio runtime on a background thread at module init time. Use `pyo3::async_runtime::run` or manual `tokio::runtime::Runtime` management.

```rust
use pyo3::prelude::*;
use once_cell::sync::Lazy;
use tokio::runtime::Runtime;

static TOKIO_RUNTIME: Lazy<Runtime> = Lazy::new(|| {
    Runtime::new().expect("Failed to create tokio runtime")
});

#[pyclass]
struct Engine {
    inner: Arc<LocalEngineInner>,
}

#[pymethods]
impl Engine {
    // Synchronous method -- blocks until async Rust completes
    // GIL is released during the wait
    fn search(&self, py: Python<'_>, query: SearchQueryPy) -> PyResult<SearchResultPy> {
        let inner = self.inner.clone();
        let query: SearchQuery = query.into();
        py.allow_threads(|| {
            TOKIO_RUNTIME.block_on(async {
                inner.search(query).await
            })
        })
        .map_err(|e| PyErr::from(e))
        .map(Into::into)
    }
}
```

**Pros**: Simple, reliable, works with any Python code (sync or async).
**Cons**: Blocks the calling Python thread (though GIL is released, the thread itself waits). Not ideal for high-concurrency Python async code.

#### Pattern B: pyo3-async-runtimes (Async-to-Async Bridge)

Using `pyo3-async-runtimes` (v0.22+, replaces deprecated `pyo3-asyncio`), you can expose Rust async functions as Python coroutines:

```rust
use pyo3::prelude::*;
use pyo3_async_runtimes::tokio::future_into_py;

#[pymethods]
impl Engine {
    // Returns a Python coroutine -- can be awaited in asyncio
    fn search_async<'py>(&self, py: Python<'py>, query: SearchQueryPy) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        let query: SearchQuery = query.into();
        future_into_py(py, async move {
            inner.search(query).await
                .map_err(|e| PyErr::from(e))
                .map(Into::into)
        })
    }
}
```

Python usage:
```python
import asyncio
from my_engine import Engine

async def main():
    engine = Engine(mode="local")
    result = await engine.search_async(query)  # Non-blocking!
    print(result)

asyncio.run(main())
```

**How it works internally**:
1. `future_into_py` takes a Rust `Future` and wraps it as a Python coroutine object.
2. When Python `await`s it, the Rust future is spawned on the tokio runtime.
3. When the Rust future completes, the result is sent back to the Python event loop via a callback.
4. The Python coroutine resolves with the result.

**Pros**: True non-blocking async from Python's perspective. Python's event loop stays responsive.
**Cons**: More complex. Requires careful lifetime management. The Rust future must be `'static` and `Send`.

#### Pattern C: Channel-Based (For Long-Running Operations)

For operations that take a long time (indexing a repo, bulk search), use a channel pattern:

```rust
use pyo3::prelude::*;
use tokio::sync::oneshot;
use std::sync::mpsc;

#[pyclass]
struct Engine {
    cmd_tx: mpsc::Sender<EngineCommand>,
}

enum EngineCommand {
    Search {
        query: SearchQuery,
        response_tx: oneshot::Sender<Result<SearchResult, EngineError>>,
    },
    IndexRepo {
        repo: RepoSpec,
        response_tx: oneshot::Sender<Result<(), EngineError>>,
    },
}

#[pymethods]
impl Engine {
    #[new]
    fn new() -> Self {
        let (cmd_tx, cmd_rx) = mpsc::channel();
        std::thread::spawn(move || {
            let rt = Runtime::new().unwrap();
            rt.block_on(async {
                let engine = LocalEngineInner::new().await;
                while let Ok(cmd) = cmd_rx.recv() {
                    match cmd {
                        EngineCommand::Search { query, response_tx } => {
                            let result = engine.search(query).await;
                            let _ = response_tx.send(result);
                        }
                        // ...
                    }
                }
            });
        });
        Engine { cmd_tx }
    }

    fn search(&self, py: Python<'_>, query: SearchQueryPy) -> PyResult<SearchResultPy> {
        let (response_tx, response_rx) = oneshot::channel();
        self.cmd_tx.send(EngineCommand::Search {
            query: query.into(),
            response_tx,
        }).map_err(|_| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>("Engine shut down"))?;

        py.allow_threads(|| {
            TOKIO_RUNTIME.block_on(async {
                response_rx.await
                    .map_err(|_| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>("Channel closed"))
            })
        })
        .and_then(|r| r.map_err(PyErr::from))
        .map(Into::into)
    }
}
```

**Pros**: Clean separation. The Rust runtime is fully isolated. Easy to add priority, cancellation, rate limiting.
**Cons**: More boilerplate. Overhead of channel communication (negligible for non-trivial operations).

#### Recommended Approach for This Project

Use **Pattern B (pyo3-async-runtimes)** for the primary async API surface, with **Pattern A (sync wrapper)** as a convenience for synchronous callers:

```rust
#[pymethods]
impl Engine {
    // Sync convenience -- blocks the calling thread (GIL released)
    fn search(&self, py: Python<'_>, query: SearchQueryPy) -> PyResult<SearchResultPy> {
        let fut = self.search_inner(query.into());
        py.allow_threads(|| {
            TOKIO_RUNTIME.block_on(fut)
        })
        .map_err(PyErr::from)
        .map(Into::into)
    }

    // Async -- returns Python coroutine
    fn search_async<'py>(&self, py: Python<'py>, query: SearchQueryPy)
        -> PyResult<Bound<'py, PyAny>>
    {
        let fut = self.search_inner(query.into());
        future_into_py(py, fut)
    }

    // Shared implementation
    fn search_inner(&self, query: SearchQuery)
        -> impl Future<Output = Result<SearchResult, EngineError>> + Send + 'static
    {
        let inner = self.inner.clone();
        async move { inner.search(query).await }
    }
}
```

**Critical version constraints**:
- `pyo3` >= 0.22.0 (for `Bound` API, `pyo3-async-runtimes` compatibility)
- `pyo3-async-runtimes` >= 0.22.0 (with `tokio-runtime` feature)
- `tokio` >= 1.35 (for `tokio::runtime::Runtime::block_on` safety guarantees)
- Python >= 3.9 (for asyncio features used by pyo3-async-runtimes)

**Important caveats**:
1. **Never call `tokio::runtime::Runtime::block_on` from within a tokio context** -- this will panic. If the Rust code is already running on a tokio runtime, use `tokio::task::spawn_blocking` or `tokio::runtime::Handle::current()`.
2. **The Rust future must be `Send + 'static`** to cross the FFI boundary. This means no borrowed Python references inside the future.
3. **GIL management**: In async mode, the GIL is released while the Rust future runs. In sync mode, `py.allow_threads()` explicitly releases it. Never hold the GIL during long Rust operations.
4. **Cancellation**: Python `asyncio` task cancellation does NOT automatically cancel the Rust future. You need to implement cancellation explicitly (e.g., using `CancellationToken` or `tokio::select!` with a cancellation channel).

### 4. Recommended Monorepo Layout for Rust Crate + Python Package

#### Layout (Adapted for This Project)

```
ultimate-coders/
├── Cargo.toml                    # Workspace root
├── Cargo.lock
├── pyproject.toml                # Root Python project config (maturin)
├── README.md
├── .github/
│   └── workflows/
│       ├── ci-rust.yml
│       ├── ci-python.yml
│       └── release.yml
│
├── crates/
│   ├── uc-types/                 # Shared types (no I/O, no PyO3, no gRPC)
│   │   ├── Cargo.toml
│   │   └── src/
│   │       ├── lib.rs
│   │       ├── search.rs         # SearchQuery, SearchResult
│   │       ├── memory.rs         # MemoryEntry, MemoryKey
│   │       ├── index.rs          # RepoSpec, IndexStatus
│   │       └── error.rs          # EngineError, unified error types
│   │
│   ├── uc-engine/                # Core engine (no Python/gRPC dependency)
│   │   ├── Cargo.toml
│   │   └── src/
│   │       ├── lib.rs
│   │       ├── api.rs            # EngineApi trait definition
│   │       ├── local.rs          # LocalEngine implementation
│   │       ├── indexer/
│   │       │   ├── mod.rs
│   │       │   ├── text.rs       # ripgrep-style text search
│   │       │   ├── semantic.rs   # vector search (Qdrant client)
│   │       │   └── ast.rs        # AST-based search
│   │       ├── memory/
│   │       │   ├── mod.rs
│   │       │   ├── short_term.rs # TiKV-backed short-term memory
│   │       │   └── long_term.rs  # Qdrant-backed long-term memory
│   │       ├── scheduler/
│   │       │   ├── mod.rs
│   │       │   └── orchestrator.rs
│   │       └── git/
│   │           ├── mod.rs
│   │           └── repo_manager.rs
│   │
│   ├── uc-grpc/                  # gRPC server + client + proto definitions
│   │   ├── Cargo.toml
│   │   ├── build.rs              # tonic-build prost compilation
│   │   ├── proto/
│   │   │   ├── engine.proto      # Mirrors EngineApi trait
│   │   │   ├── memory.proto
│   │   │   └── search.proto
│   │   └── src/
│   │       ├── lib.rs
│   │       ├── server.rs         # tonic Server wrapping EngineApi
│   │       ├── client.rs         # GrpcEngineClient implementing EngineApi
│   │       └── conversions.rs    # Proto <-> uc-types conversions
│   │
│   ├── uc-grpc-server/           # Binary crate: standalone gRPC server
│   │   ├── Cargo.toml
│   │   └── src/
│   │       └── main.rs           # Starts tonic server with LocalEngine
│   │
│   └── uc-python/                # PyO3 Python binding
│       ├── Cargo.toml            # cdylib, pyo3 + extension-module feature
│       └── src/
│           ├── lib.rs            # #[pymodule] definition
│           ├── engine.rs         # #[pyclass] Engine wrapper
│           ├── types.rs          # #[pyclass] for SearchQuery, etc.
│           ├── local_engine.rs   # PyO3 wrapper for LocalEngine
│           ├── grpc_engine.rs    # PyO3 wrapper for GrpcEngineClient
│           └── async_support.rs  # pyo3-async-runtimes integration
│
├── python/                       # Pure Python package (ergonomic layer)
│   └── ultimate_coders/
│       ├── __init__.py
│       ├── engine.py             # Engine factory (create_engine)
│       ├── search/
│       │   ├── __init__.py
│       │   ├── query.py          # Pythonic SearchQuery builder
│       │   └── result.py         # SearchResult with helpers
│       ├── memory/
│       │   ├── __init__.py
│       │   ├── short_term.py
│       │   └── long_term.py
│       ├── agent/
│       │   ├── __init__.py
│       │   ├── orchestrator.py   # Python Orchestrator (LLM interaction)
│       │   └── worker.py         # Python Worker agent
│       ├── config.py             # Configuration loading
│       └── exceptions.py         # Unified exception hierarchy
│
├── proto/                        # (Alternative: proto files at root)
│   └── ...                       # If proto is shared across crates
│
├── tests/
│   ├── rust/                     # Rust unit/integration tests
│   │   └── ...
│   └── python/                   # Python tests
│       ├── test_engine.py
│       ├── test_local_mode.py
│       ├── test_grpc_mode.py
│       └── test_async.py
│
└── docs/
    ├── architecture.md
    └── ...
```

#### Workspace Cargo.toml

```toml
[workspace]
resolver = "2"
members = [
    "crates/uc-types",
    "crates/uc-engine",
    "crates/uc-grpc",
    "crates/uc-grpc-server",
    "crates/uc-python",
]

[workspace.dependencies]
pyo3 = { version = "0.22", features = ["extension-module"] }
pyo3-async-runtimes = { version = "0.22", features = ["tokio-runtime"] }
tokio = { version = "1", features = ["full"] }
tonic = "0.12"
tonic-build = "0.12"
prost = "0.13"
serde = { version = "1", features = ["derive"] }
thiserror = "1"
```

#### uc-python Cargo.toml

```toml
[package]
name = "ultimate-coders-python"
version = "0.1.0"
edition = "2021"

[lib]
name = "_uc_core"
crate-type = ["cdylib"]

[dependencies]
pyo3 = { workspace = true }
pyo3-async-runtimes = { workspace = true }
uc-types = { path = "../uc-types" }
uc-engine = { path = "../uc-engine" }
uc-grpc = { path = "../uc-grpc" }
tokio = { workspace = true }
```

#### Root pyproject.toml (Maturin)

```toml
[build-system]
requires = ["maturin>=1.7,<2.0"]
build-backend = "maturin"

[project]
name = "ultimate-coders"
version = "0.1.0"
requires-python = ">=3.9"

[tool.maturin]
features = ["pyo3/extension-module"]
module-name = "ultimate_coders._uc_core"
python-source = "python"
```

**Key points about this layout**:

1. **`uc-types` is the foundation**: All other crates depend on it. It contains zero I/O, zero framework code -- just data types and the `EngineApi` trait. This ensures gRPC and PyO3 bindings always use the same types.

2. **`uc-engine` has no framework dependency**: It implements `EngineApi` using core Rust. It depends on `uc-types` and storage clients (TiKV, Qdrant, PostgreSQL) but NOT on PyO3 or tonic.

3. **`uc-grpc` bridges engine to gRPC**: It depends on `uc-engine` and `uc-types`. The `server.rs` wraps an `EngineApi` implementor as a tonic service. The `client.rs` implements `EngineApi` by calling tonic stubs.

4. **`uc-python` bridges engine to Python**: It depends on `uc-engine`, `uc-grpc` (for the client), and `uc-types`. It wraps `EngineApi` implementors as PyO3 classes.

5. **`python/` is the ergonomic layer**: Pure Python code that imports `_uc_core` (the compiled PyO3 module) and provides a clean, Pythonic API. This is what users actually import.

6. **Build with maturin**: Running `maturin develop` in the workspace root compiles the Rust code and installs the Python package in editable mode. `maturin build --release` produces a wheel.

### 5. gRPC + PyO3 Unified Error Handling

Both paths must produce the same Python exceptions. The pattern:

```rust
// In uc-types
#[derive(Debug, thiserror::Error)]
pub enum EngineError {
    #[error("Search failed: {0}")]
    SearchError(String),
    #[error("Index not found: {0}")]
    IndexError(String),
    #[error("Memory error: {0}")]
    MemoryError(String),
    #[error("Connection error: {0}")]
    ConnectionError(String),
    #[error("Timeout: {0}")]
    TimeoutError(String),
}

// In uc-python
impl From<EngineError> for PyErr {
    fn from(err: EngineError) -> PyErr {
        match err {
            EngineError::SearchError(msg) => {
                pyo3::exceptions::PyRuntimeError::new_err(msg)
            }
            EngineError::IndexError(msg) => {
                pyo3::exceptions::PyKeyError::new_err(msg)
            }
            EngineError::ConnectionError(msg) => {
                pyo3::exceptions::PyConnectionError::new_err(msg)
            }
            EngineError::TimeoutError(msg) => {
                pyo3::exceptions::PyTimeoutError::new_err(msg)
            }
            _ => pyo3::exceptions::PyRuntimeError::new_err(err.to_string()),
        }
    }
}

// In uc-grpc client.rs
impl EngineApi for GrpcEngineClient {
    fn search(&self, query: SearchQuery) -> Result<SearchResult, EngineError> {
        // Convert tonic::Status to EngineError
        self.blocking_client.search(query)
            .map_err(|status| match status.code() {
                tonic::Code::NotFound => EngineError::IndexError(status.message().to_string()),
                tonic::Code::DeadlineExceeded => EngineError::TimeoutError(status.message().to_string()),
                tonic::Code::Unavailable => EngineError::ConnectionError(status.message().to_string()),
                _ => EngineError::SearchError(status.message().to_string()),
            })
    }
}
```

### 6. Proto Definition Mirroring EngineApi Trait

The proto definitions should mirror the `EngineApi` trait exactly:

```protobuf
syntax = "proto3";
package ultimate_coders;

service EngineService {
    rpc Search(SearchRequest) returns (SearchResponse);
    rpc IndexRepo(IndexRepoRequest) returns (IndexRepoResponse);
    rpc ReadMemory(ReadMemoryRequest) returns (ReadMemoryResponse);
    rpc WriteMemory(WriteMemoryRequest) returns (WriteMemoryResponse);
    rpc WatchMemory(WatchMemoryRequest) returns (stream MemoryEvent);
}

message SearchRequest {
    SearchQuery query = 1;
}

message SearchResponse {
    SearchResult result = 1;
}
// ... etc
```

The `uc-grpc` crate's `conversions.rs` handles `uc-types <-> prost` conversions. This is boilerplate but necessary because prost generates its own types from `.proto` files.

**Alternative**: Use `prost` with `prost-wkt` or manual implementations to share types between proto and Rust, but this adds complexity. For most projects, explicit conversion functions are clearer and more maintainable.

### External References

- [PyO3 User Guide](https://pyo3.rs/v0.22.0/) -- Official documentation for PyO3 v0.22+
- [pyo3-async-runtimes crate](https://docs.rs/pyo3-async-runtimes/latest/pyo3_async_runtimes/) -- Async runtime bridging (replaces pyo3-asyncio)
- [Maturin Documentation](https://www.maturin.rs/) -- Build tool for Rust-based Python packages
- [HuggingFace Tokenizers](https://github.com/huggingface/tokenizers) -- Reference Rust+Python project structure
- [Polars](https://github.com/pola-rs/polars) -- Reference Rust+Python project with crate-per-feature
- [Tonic Documentation](https://docs.rs/tonic/latest/tonic/) -- Rust gRPC framework
- [PyO3 Async Example](https://github.com/PyO3/pyo3/tree/main/examples/async) -- Official async example using pyo3-async-runtimes

### Related Specs

- `.trellis/tasks/06-09-distributed-ai-coding/prd.md` -- Project requirements specifying Rust+Python hybrid with PyO3 FFI + gRPC dual-mode bridging

## Caveats / Not Found

1. **No existing codebase to reference**: This is a greenfield project. All patterns are from external projects and documentation.
2. **pyo3-async-runtimes is relatively new**: It replaced `pyo3-asyncio` in PyO3 v0.22. Some blog posts and Stack Overflow answers still reference the old API. Always use `pyo3-async-runtimes` for new projects.
3. **gRPC streaming from PyO3**: Calling gRPC server-streaming RPCs from Python via PyO3 is possible but requires careful handling. The Rust gRPC client returns a `Streaming<T>` which must be wrapped as a Python iterator or async generator. This is not covered in detail above but will be needed for `WatchMemory` and similar streaming operations.
4. **Windows support**: PyO3 cross-compilation for Windows from macOS/Linux requires additional toolchain setup. Consider CI-based wheel building with `cibuildwheel` for multi-platform releases.
5. **Performance of gRPC vs PyO3**: PyO3 local calls have ~microsecond overhead per call (type conversion + FFI). gRPC calls have ~millisecond overhead (serialization + network). For fine-grained operations (e.g., reading a single memory key), PyO3 is significantly faster. For coarse-grained operations (e.g., searching across repos), the overhead difference is negligible.
6. **Maturin vs setuptools-rust**: Maturin is the recommended build tool for new projects. setuptools-rust is still maintained but maturin provides a better developer experience (faster builds, better wheel support, editable installs).
