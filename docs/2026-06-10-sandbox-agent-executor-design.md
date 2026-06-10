# Design: Sandbox Agent Executor

**Date**: 2026-06-10
**Status**: Approved

## Overview

让 Worker 能够在隔离的 Sandbox 中运行 Claude Code 和 Codex 等外部编码 Agent，作为 LLM tool-calling 之外的另一种执行模式。通过 `Sandbox` trait 抽象，支持多种隔离实现（subprocess / Docker / namespace），MVP 先实现 subprocess 和 Docker 两种。

## Architecture

### Component Stack

```
Orchestrator
    │
    ├── Subtask 分配
    │
Worker
    │
    ├── execution_mode="llm"  →  LLM tool-calling loop (现有)
    │
    └── execution_mode="sandbox"  →  SandboxManager
                                          │
                                    ┌─────┴─────┐
                                    │  Sandbox   │  (trait)
                                    │  Pool      │
                                    └─────┬─────┘
                                          │
                              ┌───────────┼───────────┐
                              │           │           │
                        SubprocessSandbox DockerSandbox NsJailSandbox
                        (MVP, 跨平台)    (生产, 安全)  (Linux, 轻量)
```

### Sandbox Trait

```rust
#[async_trait]
pub trait Sandbox: Send + Sync {
    async fn create(&self, config: SandboxConfig) -> Result<SandboxHandle, EngineError>;
    async fn execute(&self, handle: &SandboxHandle, request: ExecRequest) -> Result<ExecResult, EngineError>;
    async fn stop(&self, handle: &SandboxHandle) -> Result<(), EngineError>;
    async fn health(&self, handle: &SandboxHandle) -> Result<SandboxHealth, EngineError>;
}
```

### Sandbox Pool

```rust
pub struct SandboxPool {
    sandbox_factory: Arc<dyn SandboxFactory>,
    idle: Vec<SandboxHandle>,      // 预热的空闲 sandbox
    active: HashMap<String, SandboxHandle>,  // 正在使用的
    max_pool_size: usize,
    warm_pool_size: usize,         // 预热数量
}
```

### Agent Adapter

```rust
pub trait AgentAdapter: Send + Sync {
    fn name(&self) -> &str;
    fn build_command(&self, prompt: &str, working_dir: &str) -> ExecRequest;
    fn parse_output(&self, result: &ExecResult) -> AgentOutput;
}

pub struct AgentOutput {
    pub summary: String,
    pub file_changes: Vec<FileChange>,
    pub token_usage: Option<TokenUsage>,
    pub success: bool,
}
```

### Agent Implementations

**Claude Code**:
- Command: `claude -p "{prompt}" --output-format json --max-turns 20`
- Output: JSON with conversation, tool uses, cost
- File changes: git diff inside sandbox
- API key: `ANTHROPIC_API_KEY` env var

**Codex**:
- Command: `codex "{prompt}" --full-auto`
- Output: stdout text + exit code
- File changes: git diff inside sandbox
- API key: `OPENAI_API_KEY` env var

### File Change Tracking

1. Before execution: `git add -A && git commit -m "baseline"` in sandbox
2. After execution: `git diff HEAD` → parse unified diff → `Vec<FileChange>`
3. Cleanup: `git reset --soft HEAD~1` (keep changes but remove commit)

### Resource Limits

```rust
pub struct ResourceLimits {
    pub max_cpu_seconds: u64,    // Default: 300 (5 min)
    pub max_memory_mb: u64,      // Default: 2048 (2 GB)
    pub max_output_bytes: u64,   // Default: 10 MB
    pub max_file_size_mb: u64,   // Default: 50 MB per file
}
```

### Network Modes

```rust
pub enum NetworkMode {
    None,        // No network access
    Restricted,  // Only specific hosts (API endpoints)
    Full,        // Unrestricted (development only)
}
```

## Implementation Backends

### 1. SubprocessSandbox (MVP, cross-platform)

- Runs `claude`/`codex` as subprocess with resource limits (ulimit on Unix, Job Objects on Windows)
- No filesystem isolation (uses host filesystem)
- No network isolation
- Startup: <50ms
- Security: low (trust-based)
- Good for: local development, trusted environments

### 2. DockerSandbox (production, secure)

- Runs agent in Docker container with volume mount for project code
- Full isolation: filesystem, network, PID namespace
- Resource limits via Docker cgroups
- Startup: ~2-5s (cold), <500ms (warm pool)
- Security: high
- Good for: production, multi-tenant, untrusted code

### 3. NsJailSandbox (future, Linux-only)

- Uses Linux namespaces for lightweight isolation
- No Docker daemon needed
- Startup: <100ms
- Security: medium-high
- Good for: Linux production, high-density scenarios

## Python Integration

### Worker Execution Mode

```python
class Worker:
    def __init__(self, ..., execution_mode: str = "llm"):
        self.execution_mode = execution_mode  # "llm" or "sandbox"

    async def execute_subtask(self, subtask):
        if self.execution_mode == "sandbox":
            return await self._execute_in_sandbox(subtask)
        else:
            return await self._execute_with_llm(subtask)  # existing
```

### SandboxConfig (Python)

```python
@dataclass
class SandboxConfig:
    agent: str = "claude-code"       # or "codex"
    backend: str = "subprocess"       # or "docker"
    project_path: str = ""
    api_key: Optional[str] = None
    max_cpu_seconds: int = 300
    max_memory_mb: int = 2048
    network: str = "restricted"       # or "none", "full"
    warm_pool_size: int = 2
```

## File Structure

### Rust (uc-engine)

```
crates/uc-engine/src/
├── sandbox/
│   ├── mod.rs              # Sandbox trait, SandboxConfig, types
│   ├── pool.rs             # SandboxPool (warm pool management)
│   ├── subprocess.rs       # SubprocessSandbox implementation
│   ├── docker.rs           # DockerSandbox implementation
│   └── agents/
│       ├── mod.rs           # AgentAdapter trait
│       ├── claude_code.rs   # Claude Code adapter
│       └── codex.rs         # Codex adapter
```

### Python

```
python/ultimate_coders/
├── agent/
│   ├── sandbox.py           # SandboxConfig, SandboxManager Python wrappers
│   └── worker.py            # Updated with sandbox execution mode
```

## Acceptance Criteria

- [ ] `Sandbox` trait defined with create/execute/stop/health methods
- [ ] `SubprocessSandbox` implementation (MVP, cross-platform)
- [ ] `DockerSandbox` implementation (production, secure)
- [ ] `SandboxPool` with warm pool management
- [ ] `ClaudeCodeAgent` adapter (build command, parse output)
- [ ] `CodexAgent` adapter (build command, parse output)
- [ ] File change tracking via git diff
- [ ] Worker supports `execution_mode="sandbox"` 
- [ ] Resource limits enforced (CPU, memory, time)
- [ ] Python SandboxConfig and SandboxManager wrappers
- [ ] Integration test: Worker executes subtask via sandbox
- [ ] `cargo test` and `pytest` pass
