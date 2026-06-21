# Research: Python Testing Patterns for Rust/PyO3 Agent Systems

- **Query**: Testing patterns for Python agent/orchestration systems that wrap Rust via PyO3
- **Scope**: internal / external (mixed)
- **Date**: 2026-06-18

## Findings

### 1. Testing Python Code When the Rust PyO3 Extension May Not Be Available

#### Current Pattern in Codebase

The `Engine` class at `python/ultimate_coders/engine.py:15-19` uses a guarded import:

```python
try:
    from ultimate_coders._uc_core import PyEngine, PySearchQuery
except ImportError:
    PyEngine = None  # Rust extension not built yet
    PySearchQuery = None
```

However, the `Engine.__init__` at line 71 **raises ImportError** if `PyEngine is None`, meaning the Engine class is unusable without the Rust extension. This is a hard dependency at construction time.

The `test_async_engine.py` fixture at line 16-17 creates `Engine(mode="local")` directly, which means **all async engine tests require the Rust extension to be built** (`maturin develop`). These tests will fail with ImportError if the extension is missing.

#### Standard Patterns for PyO3 Extension Availability

**Pattern A: Duck-typed protocol / Protocol class**
Define a `typing.Protocol` that describes the Engine API surface. The real `Engine` wraps PyO3; a `StubEngine` or `InMemoryEngine` implements the same protocol in pure Python for testing.

```python
from typing import Protocol

class EngineProtocol(Protocol):
    def health(self) -> object: ...
    def read_memory(self, key_scope: str, key: str, ...) -> object | None: ...
    def write_memory(self, key_scope: str, key: str, content: str, ...) -> object: ...
    def search(self, query: Any) -> object: ...
```

**Pattern B: `pytest.importorskip` for conditional test collection**
Skip entire test modules or classes when the Rust extension is unavailable:

```python
pytest.importorskip("ultimate_coders._uc_core")
```

This is used in the broader Python/PyO3 ecosystem (e.g., polars, pydantic-core). Tests that genuinely require the Rust extension are skipped rather than failing.

**Pattern C: Separate test tiers**
- `tests/python/unit/` -- pure Python tests, no Rust extension needed (mock Engine)
- `tests/python/integration/` -- requires `maturin develop`, tests real PyO3 bridge
- Mark integration tests with `@pytest.mark.integration` and use `pytest -m "not integration"` for fast CI.

**Pattern D: InMemoryEngine stub**
The Rust `LocalEngine` already has an in-memory fallback (`LocalEngine::new_fallback()` used in `crates/uc-grpc/tests/grpc_integration.rs:13`). A Python-side equivalent could be a simple dict-backed stub:

```python
class InMemoryEngine:
    """Pure Python stub for testing without Rust extension."""
    def __init__(self):
        self._memory: dict[str, dict] = {}

    def write_memory(self, key_scope, key, content, **kwargs):
        mem_key = f"{key_scope}:{key}"
        self._memory[mem_key] = {"content": content, **kwargs}
        return self._memory[mem_key]

    def read_memory(self, key_scope, key, **kwargs):
        return self._memory.get(f"{key_scope}:{key}")
```

#### External References

- [PyO3 testing guide](https://pyo3.rs/v0.22/testing) -- recommends `maturin develop` + pytest, and separating unit from integration tests
- [Maturin testing docs](https://www.maturin.rs/testing) -- `maturin develop` then `pytest`
- [Polars test structure](https://github.com/pola-rs/polars/tree/main/py-polars/tests) -- uses `pytest.importorskip` and separate unit/SLD test directories

---

### 2. Mock Patterns for Engine (Duck-typed Python Wrapper Around PyEngine)

#### Current Mocking Approach in Codebase

The existing tests use `unittest.mock.MagicMock` for the Engine in several places:

- `test_agent.py:257-259` -- `TestShortTermMemory._make_engine()` creates a MagicMock with `read_memory.return_value = None` and `write_memory.return_value = {"id": "mem-1", "content": "test"}`
- `test_agent.py:299-302` -- `TestLongTermMemory._make_engine()` similarly
- `test_nats_worker.py:201-209` -- `mock_nc` fixture creates MagicMock for NATS client with `AsyncMock()` for publish

The Orchestrator and Worker accept `engine: Any = None`, which means:
- When `engine is None`, operations that need the engine are skipped (with `try/except` and logging)
- When `engine` is a MagicMock, calls are recorded but return mock values

#### Recommended Mock Patterns

**Pattern A: MagicMock with configured return values (current approach, works well)**

```python
def make_mock_engine():
    engine = MagicMock()
    engine.read_memory.return_value = None
    engine.write_memory.return_value = MagicMock(content="test", id="mem-1")
    engine.search_memory.return_value = []
    engine.search.return_value = MagicMock(items=[])
    engine.health.return_value = MagicMock(status="ok", version="0.1.0")
    return engine
```

This is already used in `test_agent.py` and works well for unit tests where Engine behavior is not the system under test.

**Pattern B: AsyncMock for async Engine methods**

The Engine has `_async` variants (e.g., `read_memory_async`, `write_memory_async`). When mocking these:

```python
engine = MagicMock()
engine.read_memory_async = AsyncMock(return_value=None)
engine.write_memory_async = AsyncMock(return_value=MagicMock(content="test"))
```

**Pattern C: `spec=` or `spec_set=` for stricter mocks**

Using `MagicMock(spec=Engine)` would catch attribute typos but requires the real Engine class (and thus the Rust extension). An alternative is to define a Protocol and use `MagicMock(spec=EngineProtocol)`.

**Pattern D: `monkeypatch` for method-level patching (used in test_orchestrator_sandbox.py)**

```python
async def fake_execute_subprocess(request):
    return fake_result

monkeypatch.setattr(sm, "_execute_subprocess", fake_execute_subprocess)
```

This is used at `test_orchestrator_sandbox.py:129-131` and is the cleanest way to replace a single method for a test.

---

### 3. Testing Async Python Code (Orchestrator, Worker) with pytest-asyncio

#### Current Configuration

From `pyproject.toml:34-35`:
```toml
[tool.pytest.ini_options]
testpaths = ["tests/python"]
asyncio_mode = "auto"
```

The `asyncio_mode = "auto"` setting means pytest-asyncio automatically detects async test functions and async fixtures without requiring explicit `@pytest.mark.asyncio` decorators. However, the codebase **still uses explicit `@pytest.mark.asyncio`** decorators throughout (e.g., `test_agent.py:359`, `test_sandbox.py:371`, etc.).

#### Patterns in Use

**Async test functions** -- decorated with `@pytest.mark.asyncio`:
```python
@pytest.mark.asyncio
async def test_register_worker(self):
    orch = Orchestrator()
    wi = WorkerInfo(id="w1", capabilities=["code"])
    await orch.register_worker(wi)
```

**Sync fixtures with async test methods** -- fixtures are sync, test methods are async:
```python
@pytest.fixture
def engine():
    return Engine(mode="local")  # sync fixture

@pytest.mark.asyncio
async def test_health_async_returns_health_status(self, engine):
    result = await engine.health_async()
```

**Async fixtures** -- used in `test_nats_worker.py:200-209`:
```python
@pytest.fixture()
def mock_nc(self):
    nc = MagicMock()
    nc.publish = AsyncMock()
    return nc
```

Note: The `mock_nc` fixture is sync but returns an object with `AsyncMock` attributes. The actual test methods are async.

**Patching async methods** -- `test_nats_worker.py:580-584`:
```python
with patch("ultimate_coders.nats_worker.asyncio.sleep", side_effect=fake_sleep):
    await worker._heartbeat_loop()
```

#### Key Considerations

- `asyncio_mode = "auto"` in pyproject.toml makes `@pytest.mark.asyncio` technically redundant, but keeping it explicit is fine for clarity.
- For async fixtures that need setup/teardown, use `@pytest_asyncio.fixture` (from `pytest-asyncio`):
  ```python
  @pytest_asyncio.fixture
  async def orchestrator():
      orch = Orchestrator()
      yield orch
      # cleanup
  ```
- The `event_loop` fixture is deprecated in pytest-asyncio >= 0.21. Use `loop` or let pytest-asyncio manage the loop automatically.

#### External References

- [pytest-asyncio docs](https://pytest-asyncio.readthedocs.io/) -- `asyncio_mode = "auto"`, fixture patterns
- [pytest-asyncio migration guide](https://pytest-asyncio.readthedocs.io/en/latest/reference/migration.html) -- for upgrading from older versions

---

### 4. Integration Test Patterns: gRPC Server + Python Worker End-to-End

#### Current Rust-side Integration Test Pattern

From `crates/uc-grpc/tests/grpc_integration.rs:11-35`:
```rust
async fn start_server() -> String {
    let engine = LocalEngine::new_fallback();
    let grpc_server = GrpcServer::new(engine);
    let (engine_service, task_service) = grpc_server.into_services();

    let addr: std::net::SocketAddr = "127.0.0.1:0".parse().unwrap();
    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
    let actual_addr = listener.local_addr().unwrap();

    tokio::spawn(async move {
        Server::builder()
            .add_service(engine_service)
            .add_service(task_service)
            .serve_with_incoming(...)
            .await
            .unwrap();
    });

    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    format!("http://{}", actual_addr)
}
```

Key pattern: bind to port 0 (OS picks free port), spawn server in background, wait 100ms for startup, return endpoint URL.

#### Python-side Integration Test Pattern (Not Yet Implemented)

There are no Python integration tests that spin up a real gRPC server. The closest is `mock_worker.py` which implements a JSON-RPC mock worker via stdin/stdout.

**Recommended pattern for Python gRPC integration tests:**

```python
import subprocess
import time
import pytest

@pytest.fixture(scope="session")
def grpc_server():
    """Start the Rust gRPC server for integration tests."""
    # Build with maturin first
    proc = subprocess.Popen(
        ["cargo", "run", "-p", "uc-grpc-server", "--", "--port", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Wait for server to print its bound address
    # ... parse port from stderr/stdout
    time.sleep(2)  # wait for server ready
    yield f"http://localhost:{port}"
    proc.terminate()
    proc.wait()

@pytest.mark.integration
async def test_engine_grpc_mode(grpc_server):
    engine = Engine(mode="grpc", grpc_endpoint=grpc_server)
    health = engine.health()
    assert health.status == "degraded"  # fallback storage
```

**Alternative: Use `subprocess` + `Engine(mode="grpc")`**

The Python `Engine` class already supports gRPC mode. An integration test would:
1. Start `uc-grpc-server` as a subprocess
2. Create `Engine(mode="grpc", grpc_endpoint=...)` in Python
3. Exercise the full Python -> gRPC -> Rust -> LocalEngine path
4. Tear down the subprocess

**Alternative: Use the mock_worker.py bridge**

`tests/python/mock_worker.py` already implements a JSON-RPC mock worker that can be started as a subprocess. This could be extended for gRPC integration tests.

#### External References

- [tonic integration testing](https://github.com/hyperium/tonic/blob/master/examples/src/integration/tests/integration.rs) -- Rust gRPC test pattern with port 0
- [pytest-subprocesses](https://pytest-subprocesses.readthedocs.io/) -- managing subprocess fixtures in pytest

---

### 5. Test Coverage Tools with Maturin-built Packages

#### Current State

There is **no coverage configuration** in the codebase. No `pytest-cov`, `coverage.py`, or `.coveragerc` found.

#### Coverage Challenges with Maturin/PyO3

**Problem**: Standard Python coverage tools (coverage.py / pytest-cov) trace Python bytecode. PyO3 extension modules are native shared libraries -- coverage.py cannot instrument them. This means:
- Python code that delegates to Rust (like `Engine.__init__` calling `PyEngine()`) will show the Python function as covered, but the Rust internals are invisible.
- Branch coverage for Python code that conditionally uses the Rust extension (the `try/except ImportError` pattern) will show partial coverage depending on whether the extension is available.

**Solutions:**

**For Python-side coverage:**
```toml
[tool.pytest.ini_options]
addopts = "--cov=ultimate_coders --cov-report=term-missing --cov-report=html"
```

Use `pytest-cov` with `--cov=ultimate_coders`. This covers the Python ergonomic layer (orchestrator, worker, llm, sandbox, etc.) but not the Rust extension.

**For Rust-side coverage:**
Use `cargo tarpaulin` or `cargo llvm-cov` for Rust code coverage:
```bash
cargo llvm-cov --html  # generates HTML coverage for Rust crates
```

**Combined coverage approach:**
1. Run `pytest --cov=ultimate_coders` for Python coverage
2. Run `cargo llvm-cov` for Rust coverage
3. Combine in CI reports (Codecov/Coveralls can merge multiple reports)

**Important caveat**: When running `pytest --cov` with a maturin-built package, the `.so`/`.dylib` extension file will be reported as having 0% coverage by coverage.py. Use `--cov-config=.coveragerc` with:

```ini
[run]
omit =
    ultimate_coders/_uc_core*
```

to exclude the native extension from Python coverage reports.

#### External References

- [pytest-cov docs](https://pytest-cov.readthedocs.io/) -- Python coverage with pytest
- [cargo-llvm-cov](https://github.com/taiki-e/cargo-llvm-cov) -- Rust coverage via LLVM
- [Maturin testing section](https://www.maturin.rs/testing) -- notes on coverage limitations
- [PyO3 testing guide](https://pyo3.rs/v0.22/testing) -- mentions pytest + maturin develop workflow

---

### Files Found

| File Path | Description |
|---|---|
| `python/ultimate_coders/engine.py` | Engine class with PyO3 guarded import, gRPC fallback, async methods |
| `python/ultimate_coders/agent/orchestrator.py` | Orchestrator with async decompose/assign/monitor, night window, NATS |
| `python/ultimate_coders/agent/worker.py` | Worker with LLM tool-calling loop, sandbox mode, codegraph tools |
| `python/ultimate_coders/agent/llm.py` | LLMClient wrapping Anthropic AsyncAnthropic, retry logic |
| `python/ultimate_coders/agent/sandbox.py` | SandboxManager, AgentAdapter, subprocess execution |
| `pyproject.toml` | Maturin build config, pytest config (asyncio_mode=auto) |
| `tests/python/test_agent.py` | Unit tests: Task/Subtask types, LLMClient, Memory wrappers, Orchestrator, Worker |
| `tests/python/test_async_engine.py` | Integration tests requiring Rust extension: async Engine methods |
| `tests/python/test_sandbox.py` | Unit tests: SandboxConfig, adapters, SandboxManager pool |
| `tests/python/test_orchestrator_sandbox.py` | Tests for sandbox decomposition path with monkeypatch |
| `tests/python/test_nats_worker.py` | Tests for NATS publisher/worker with MagicMock/AsyncMock |
| `tests/python/test_codegraph.py` | Tests for CodegraphClient with temp SQLite DB |
| `tests/python/test_scheduler_integration.py` | Integration tests for Scheduler + Orchestrator night window |
| `tests/python/mock_worker.py` | JSON-RPC mock worker for integration testing |
| `crates/uc-grpc/tests/grpc_integration.rs` | Rust gRPC integration test: server on port 0, client exercises RPCs |
| `crates/uc-python/src/engine.rs` | PyO3 PyEngine implementation |
| `crates/uc-grpc/src/server.rs` | gRPC server implementation |

### Code Patterns

**Guarded PyO3 import** (`engine.py:15-19`):
```python
try:
    from ultimate_coders._uc_core import PyEngine, PySearchQuery
except ImportError:
    PyEngine = None
    PySearchQuery = None
```

**MagicMock Engine** (`test_agent.py:257-259`):
```python
engine = MagicMock()
engine.read_memory.return_value = None
engine.write_memory.return_value = {"id": "mem-1", "content": "test"}
```

**monkeypatch for sandbox subprocess** (`test_orchestrator_sandbox.py:129-131`):
```python
monkeypatch.setattr(sm, "_execute_subprocess", fake_execute_subprocess)
```

**AsyncMock for NATS** (`test_nats_worker.py:201-204`):
```python
nc = MagicMock()
nc.publish = AsyncMock()
```

**Rust gRPC test server on port 0** (`grpc_integration.rs:11-35`):
```rust
let addr: std::net::SocketAddr = "127.0.0.1:0".parse().unwrap();
let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
```

**Engine accepts `engine=None`** (`orchestrator.py:93`, `worker.py:95`):
Both Orchestrator and Worker accept `engine: Any = None`, with all engine calls guarded by `if self.engine is not None:`.

### Related Specs

- `.trellis/spec/backend/local-worker-bridge-spec.md` -- JSON-RPC bridge between Rust gRPC server and Python Worker
- `.trellis/spec/backend/nats-bridge-spec.md` -- NATS JetStream bridge for task events
- `.trellis/spec/backend/taskservice-grpc-spec.md` -- gRPC task service spec
- `.trellis/spec/backend/quality-guidelines.md` -- Quality/testing guidelines

## Caveats / Not Found

- **No conftest.py exists** -- there is no shared pytest fixture file. Fixtures are defined inline in each test module. A `conftest.py` with shared fixtures (mock_engine, mock_llm_client, etc.) would reduce duplication.
- **No coverage tooling is configured** -- no pytest-cov, no .coveragerc, no cargo-llvm-cov.
- **No `@pytest.mark.integration` marker** -- there is no separation between unit and integration tests. The `test_async_engine.py` tests require the Rust extension but are not marked differently.
- **No skip markers for missing Rust extension** -- tests that require `maturin develop` will fail with ImportError rather than being skipped.
- **`asyncio_mode = "auto"` but explicit `@pytest.mark.asyncio` used everywhere** -- redundant but harmless.
- **No proto files found** -- the `proto/` directory appears to be empty or the proto files are generated elsewhere (build.rs in uc-grpc).
