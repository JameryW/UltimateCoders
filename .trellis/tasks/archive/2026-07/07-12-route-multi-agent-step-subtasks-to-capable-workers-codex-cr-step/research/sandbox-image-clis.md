# Research: Sandbox Image CLIs (claude + codex)

- **Query**: Does `ultimate-coders/sandbox:latest` ship BOTH the `claude` and `codex` coding-agent CLIs? Where is the sandbox image Dockerfile? Does the worker probe for agent CLIs?
- **Scope**: internal
- **Date**: 2026-07-12

## Findings

### Q1: Sandbox image Dockerfile location — DOES NOT EXIST

There is **no Dockerfile that builds the `ultimate-coders/sandbox:latest` image**. The repo contains only two Dockerfiles:

| File Path | Builds | Description |
|---|---|---|
| `docker/Dockerfile` | Worker / orchestrator image (`python:3.11-slim` based) | Python worker runtime — installs nodejs, npm, typescript-language-server, git, rust-analyzer. Does NOT install `claude` or `codex` CLIs. |
| `docker/Dockerfile.grpc` | Gateway image (`rust:1.88-slim` → `debian:bookworm-slim`) | Rust gRPC server binary only. No agent CLIs. |

Key evidence:
- `find . -name "Dockerfile*"` (excluding vendor) returns only the two files above.
- `docker/docker-compose.yml` has three `build:` sections (gateway → `Dockerfile.grpc`, orchestrator/worker/nats-worker → `Dockerfile`). None reference a sandbox image.
- No build script (`run-cluster.sh`, `run-gateway.sh`, `run-omp.sh`) contains `docker build` for a sandbox image — `run-gateway.sh --build` only rebuilds the gateway image.
- The README has no instructions for building `ultimate-coders/sandbox:latest`.

The Rust constant `DEFAULT_SANDBOX_IMAGE` at `crates/uc-engine/src/sandbox/docker.rs:21` references `"ultimate-coders/sandbox:latest"`, but **no Dockerfile in the repo produces an image with that tag**.

### Q2: Does the image install claude + codex CLIs? — NEITHER is installed

Since there is no sandbox-image Dockerfile, neither `claude` nor `codex` is installed in any image built by this repo.

The closest image is `docker/Dockerfile` (the worker image). Its `npm install -g` commands (line 74) install only:
```
npm install -g typescript-language-server typescript
```
There is no `npm install -g @anthropic-ai/claude-code`, no `@openai/codex`, no binary download for either CLI. Grep for `claude`, `codex`, `@anthropic`, `@openai` across all Dockerfiles and docker-compose files returns zero hits.

### Q3: Worker host fallback — `_derive_capabilities` does NOT probe for agent CLIs

`python/ultimate_coders/agent/worker.py:316` — `_derive_capabilities()` derives capabilities **purely from `SandboxConfig` fields** (tools, mcp_configs, agent_name, agents_json) and env-var opt-in flags. It does **not** call `shutil.which` for `claude` or `codex`.

The only `shutil.which` call in the entire `python/` tree is in `python/ultimate_coders/dashboard/app.py:920` (checking for the `script` binary for terminal recording — unrelated to agent CLIs).

Capabilities a worker currently advertises (always, unconditionally):
```
["code", "search", "memory", "test", "decompose", "review"]
```
Plus conditionally:
- `"mcp"` + `"mcp:<server>"` tags (from `cfg.mcp_configs`)
- `"codegraph"` + `"lsp"` (if `mcp__codegraph` tools detected)
- semantic aliases via `_MCP_CAP_ALIASES` map
- `"browser"` / `"debug"` (if `UC_CAP_BROWSER` / `UC_CAP_DEBUG` env set)
- `"agent:<name>"` (from `cfg.agent_name` or parsed from `cfg.agents_json`)

The worker advertises `"code"` unconditionally regardless of whether `claude` or `codex` actually exists on the host PATH. There is **no capability like `"agent:claude-code"` or `"agent:codex"`** derived from CLI presence — only from config strings.

### Also checked

#### `crates/uc-engine/src/sandbox/docker.rs` — expects CLIs baked into image, no mount/inject

The `DockerSandbox::execute` method (line 143) builds a `docker run` command:
- Mounts the project directory: `-v=<host_path>:/workspace`
- Sets working dir: `-w=/workspace`
- Passes env vars via `-e`
- Sets memory/CPU limits and network mode
- Uses the image from `self.image` (default `ultimate-coders/sandbox:latest`)
- Runs `request.command` (e.g. `"claude"` or `"codex"`) + args directly

It does **not** mount or inject any CLIs into the container — it expects them **baked into the image**. Since no image is built, a `docker run` with `command: "claude"` would fail with `executable file not found`.

#### DockerSandbox is never instantiated in production code

`DockerSandbox` is defined and tested but **never wired in**:
- `crates/uc-engine/src/local.rs` — `LocalEngine` always uses `SubprocessSandbox::new()` (lines 157, 225, 286). The `set_sandbox()` method exists for runtime swap but is never called with a `DockerSandbox`.
- `DockerSandbox::new()` / `with_image()` appear only in `docker.rs` tests, not in any production code path.
- The Python worker always hardcodes `backend="subprocess"` (`python/ultimate_coders/nats_worker.py:834`).

#### `UC_SANDBOX_MODE` env var — set but never read

- `docker/docker-compose.yml:245` and `:290` set `UC_SANDBOX_MODE: "subprocess"` for worker and nats-worker services.
- README line 448 documents it: "Sandbox mode: `subprocess` or empty".
- **No Python or Rust code reads this env var.** Grep for `UC_SANDBOX_MODE` across all `.py` and `.rs` files returns zero hits.
- The Python `SandboxConfig.backend` field defaults to `"subprocess"` (`sandbox.py:56`) and is hardcoded to `"subprocess"` at the only call site (`nats_worker.py:834`).

#### `UC_SANDBOX_IMAGE` env var — does not exist

There is no `UC_SANDBOX_IMAGE` env var anywhere in the codebase. The sandbox image is a hardcoded Rust constant (`DEFAULT_SANDBOX_IMAGE`), overridable only via `DockerSandbox::with_image()` — which is never called in production.

#### Agent CLI command building (for reference)

Both adapters build commands assuming the CLI is on PATH:

**Rust `CodexAgent`** (`crates/uc-engine/src/sandbox/agents/codex.rs:56`):
```rust
ExecRequest {
    command: "codex".to_string(),
    args: vec![prompt, "--sandbox", "workspace-write"],
    ...
}
```

**Python `CodexAdapter`** (`python/ultimate_coders/agent/sandbox.py:1063`):
```python
return {
    "command": "codex",
    "args": [prompt, "--sandbox", "workspace-write"],
    ...
}
```

**Python `ClaudeCodeAdapter`** (`python/ultimate_coders/agent/sandbox.py:868`):
```python
return {
    "command": "claude",
    "args": ["-p", prompt, "--output-format", "stream-json", "--max-turns", "20", ...],
    ...
}
```

**Python `DecomposeAdapter`** (`sandbox.py:615`):
```python
return {
    "command": "claude",
    "args": ["-p", prompt, "--output-format", "json", "--max-turns", "1", ...],
    ...
}
```

All four adapters invoke the CLI by bare name (`"claude"` / `"codex"`) via `asyncio.create_subprocess_exec` (Python) or `docker run <image> <command>` (Rust). They assume the binary is already available — either on the host PATH (subprocess mode) or baked into the image (docker mode).

## Caveats / Not Found

- **The sandbox image does not exist as a buildable artifact in this repo.** The `ultimate-coders/sandbox:latest` tag is a dangling reference — a default constant in Rust code that points to an image nobody builds. If it was previously built manually and pushed to a local Docker registry, there's no record of it in the repo (no build script, no docs, no CI config for it).
- Whether `claude` and/or `codex` CLIs are installed on the **host machine** (for subprocess-mode workers) is environment-dependent and cannot be determined from the repo alone — the worker does not probe for them.
- The `UC_SANDBOX_MODE` env var appears to be a **dead config knob** — documented and set in compose, but never consumed by code.
