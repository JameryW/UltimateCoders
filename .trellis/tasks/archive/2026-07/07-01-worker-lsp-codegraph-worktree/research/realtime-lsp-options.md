# Research: Real-Time LSP Backend Options for Worker

- **Query**: Best way to give a Python worker a REAL-TIME LSP backend for code being edited in git worktrees, replacing/augmenting codegraph (precomputed SQLite graph, ~1s lag)
- **Scope**: Mixed (internal codebase + external package/ecosystem research)
- **Date**: 2026-07-01

## Findings

### The Problem (Internal Context)

The worker spawns a coding agent (Claude Code / Codex) inside a git worktree per subtask. The agent currently uses codegraph MCP tools (`codegraph_explore`/`callers`/`node`/`goToDefinition`/`findReferences`/`hover`/`documentSymbol`) for symbol navigation. Codegraph is a precomputed SQLite knowledge graph that:

- Lags file writes by ~1s (file watcher debounce)
- May not index the active worktree at all (the worktree is a separate directory `.uc/worktrees/<ws_id>`, not the project root where `.codegraph/` lives)
- Cannot reflect edits the agent just made within the same session

**Goal**: Real-time LSP semantics (go-to-def, find-refs, hover, document symbols) that reflect the CURRENT state of files in the worktree.

### Internal Files Found

| File Path | Description |
|---|---|
| `python/ultimate_coders/agent/engine_mcp.py` | uc-engine MCP server (stdio): search_code, read_memory, write_memory. Pattern for new MCP servers. |
| `python/ultimate_coders/agent/fs_mcp.py` | uc-fs MCP server (stdio): read_file, write_file, edit_file. Workspace-scoped, sandboxed paths. **This is the template for a new uc-lsp MCP server.** |
| `python/ultimate_coders/agent/codegraph.py` | CodegraphClient: direct SQLite reader for `.codegraph/codegraph.db`. FTS5 search, callers/callees, impact BFS, explore. Graceful degradation. |
| `python/ultimate_coders/agent/sandbox.py` | SandboxConfig.mcp_configs (list of file paths or inline dicts), ClaudeCodeAdapter builds `--mcp-config` CLI flags. |
| `python/ultimate_coders/agent/workspace.py` | WorkspaceManager: git worktree per subtask at `.uc/worktrees/<ws_id>`. Worktree path is the agent's working directory. |
| `.mcp.json` | Project MCP config: codegraph stdio server (`codegraph serve --mcp`). |
| `.codegraph/codegraph.db` | 8 MB SQLite, 3158 nodes, 7551 edges. WAL mode. |
| `docker/Dockerfile` | Worker container: `python:3.11-slim`, no Node.js, no rust-analyzer, no gopls. Only git + Python. |
| `.trellis/spec/backend/codegraph-integration.md` | Spec: CodegraphClient API, graceful degradation contract, ADR-2 (direct SQLite over CLI). |
| `.trellis/tasks/archive/2026-06/06-14-coding-agent-codegraph/research/codegraph-integration.md` | Prior research: codegraph uses tree-sitter for parsing, file watcher auto-syncs ~2s debounce, 7662 unresolved refs. |

#### MCP Server Pattern (from fs_mcp.py)

The existing pattern for a new in-process MCP server:

```python
# python/ultimate_coders/agent/<name>_mcp.py
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

server = Server("uc-<name>")

@server.list_tools()
async def list_tools() -> list[Tool]: ...

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]: ...

def main():
    # argparse for --workspace, etc.
    asyncio.run(stdio_server(server).serve())
```

Registered via `SandboxConfig.mcp_configs` as inline dict:
```json
{"uc-lsp": {"command": "python", "args": ["-m", "ultimate_coders.agent.lsp_mcp", "--workspace", "/workspace"]}}
```

---

### 1. Real-Time LSP Backends a Python Process Can Drive

#### A. multilspy (Microsoft) — Multi-Language LSP Client Library

**What**: A Python library that wraps multiple LSP servers behind a unified API. Originally from Microsoft's NeurIPS 2023 Monitor-Guided Decoding research. Automatically downloads, installs, and manages language server binaries.

| Attribute | Value |
|---|---|
| PyPI | `multilspy` v0.0.15 (latest, 2026) |
| License | MIT (Microsoft) |
| Wheel size | 134 KB (pure Python) |
| Requires Python | >=3.8, <4.0 |
| Status | Development Status: 2 - Pre-Alpha |
| Dependencies | `jedi-language-server==0.41.3` (pinned), `requests`, `typing-extensions`, `psutil` |

**Supported languages** (10 total):

| Language | LSP Server | Binary Source |
|---|---|---|
| Python | jedi-language-server | pip install (bundled as dep) |
| Rust | rust-analyzer | Auto-downloaded binary (GitHub releases) |
| Go | gopls | Expects in PATH (requires Go installed) |
| TypeScript/JS | typescript-language-server | Auto-installed via `npm install` |
| Java | Eclipse JDTLS | Auto-downloaded (vscode-java VSIX) |
| C# | OmniSharp | Auto-downloaded |
| Ruby | Solargraph | Auto-installed via `gem install` |
| Dart | Dart SDK | Auto-downloaded |
| Kotlin | Kotlin Language Server | Auto-downloaded |

**API surface** (both sync and async):
- `request_definition(file_path, line, column)` → List[Location]
- `request_references(file_path, line, column)` → List[Location]
- `request_completions(file_path, line, column)` → completions
- `request_document_symbols(file_path)` → (List[UnifiedSymbolInformation], TreeRepr)
- `request_hover(file_path, line, column)` → Hover
- `request_workspace_symbol(query)` → List[UnifiedSymbolInformation]

**Usage pattern**:
```python
from multilspy import SyncLanguageServer
from multilspy.multilspy_config import MultilspyConfig
from multilspy.multilspy_logger import MultilspyLogger

config = MultilspyConfig.from_dict({"code_language": "python"})
lsp = SyncLanguageServer.create(config, MultilspyLogger(), "/abs/path/to/worktree")
with lsp.start_server():
    defs = lsp.request_definition("src/main.py", 42, 10)
    refs = lsp.request_references("src/main.py", 42, 10)
    symbols = lsp.request_document_symbols("src/main.py")
    hover = lsp.request_hover("src/main.py", 42, 10)
```

**Key properties**:
- Handles JSON-RPC communication with LSP servers
- Manages server lifecycle (start/stop/shutdown)
- Downloads platform-specific binaries to `~/.multilspy/lsp/`
- Opens/closes files via `textDocument/didOpen` / `textDocument/didChange` — **real-time**: the LSP server sees file content as it is right now, not a stale index
- Async API available (`LanguageServer` with `async def request_*`)

**Pros**:
- One Python dep, one API, 10 languages
- MIT licensed, Microsoft-backed
- Async API fits MCP server's asyncio event loop
- Handles the messy parts: binary download, JSON-RPC, server-specific init params
- Already designed for AI4Code scenarios (Monitor-Guided Decoding)

**Cons**:
- Pre-Alpha status (v0.0.15) — API may change
- Pinned `jedi-language-server==0.41.3` (latest is 0.47.0) — version drift
- Binary downloads at runtime (network needed on first use; container image bloats over time as binaries cache in `~/.multilspy/`)
- Some servers need their own runtimes in PATH (gopls needs Go, typescript-language-server needs npm/Node.js)
- No built-in file watching — caller must send `didChange` notifications after edits
- Server startup latency: 1-10s per language server (jedi is fast ~1s; rust-analyzer can take 5-10s on first index)

#### B. System LSP Servers as Subprocesses (DIY LSP Client)

**What**: Spawn LSP servers directly (pylsp, gopls, rust-analyzer, typescript-language-server, clangd) and speak LSP/JSON-RPC from Python using `lsprotocol` + `pygls` or raw JSON-RPC.

| Package | Version | Purpose | Deps |
|---|---|---|---|
| `lsprotocol` | 2025.0.0 | Python types for LSP (auto-generated from spec) | attrs, cattrs |
| `pygls` | 2.1.1 | Generic LSP server framework (for WRITING servers, but includes client primitives) | attrs, cattrs, lsprotocol |
| `python-lsp-server` (pylsp) | 1.14.0 | Python LSP server (pylsp) | 38 deps (jedi, pluggy, ujson, etc.) |

**Feasibility**: Fully feasible. The LSP protocol is JSON-RPC 2.0 over stdio. You can speak it with any JSON-RPC client. `lsprotocol` gives you typed request/response objects. However, you're reimplementing what multilspy already does: binary management, init handshake, `didOpen`/`didChange` tracking, server-specific quirks.

**Pros**:
- Full control over server lifecycle and configuration
- Can pin exact server versions
- No dependency on multilspy's pre-alpha API
- Can use any LSP server (not limited to multilspy's 10)

**Cons**:
- Significant implementation effort (LSP init handshake, capability negotiation, file sync tracking)
- Per-language server binary must be installed in the container
- `pygls` is primarily for WRITING LSP servers, not consuming — its client primitives are low-level
- Each server has its own init quirks (rust-analyzer needs `initializeOptions`, gopls needs Go env, etc.)
- Reimplements multilspy's value proposition

#### C. pygls / tower-lsp — LSP Server Frameworks (NOT Consumers)

**Confirmed**: `pygls` (Python) and `tower-lsp` (Rust) are frameworks for **writing** LSP servers, not consuming them. They are NOT relevant for this use case (we need to be an LSP **client**, not a server).

`pygls` does include some low-level client communication primitives (JSON-RPC over stdio), but using it as a client is like using a web framework to make HTTP requests — technically possible but not what it's designed for.

#### D. Existing LSP-as-MCP Bridges

Several projects already wrap LSP servers behind an MCP interface. This is directly relevant — we could use one of these instead of building our own `uc-lsp` MCP server.

| Project | Language | License | Stars | Description |
|---|---|---|---|---|
| **jonrad/lsp-mcp** | TypeScript | MIT | 190 | MCP server that provides LLMs with LSP capabilities. Multi-LSP at once. Docker image available. POC state. |
| **bug-ops/mcpls** | Rust | MIT/Apache-2.0 | 44 | Universal MCP-to-LSP bridge. Single static binary. Zero-config for Rust. Graceful degradation. |
| **Tritlo/lsp-mcp** | TypeScript | MIT | 123 | MCP server for LSP hover/completions/code actions. `npx tritlo/lsp-mcp`. |
| **agentic-labs/lsproxy** | Docker (Go) | Open source | 107 | Multi-language code navigation via REST API in a container. Runs LSP servers + ast-grep. Python SDK (`lsproxy-sdk` v0.3.1). |
| **SteelMorgan/mcp-bsl-lsp-bridge** | — | — | 57 | BSL Language Server MCP bridge (niche). |

**Details on the top candidates**:

**jonrad/lsp-mcp** (TypeScript, MIT, 190 stars, POC):
- Runs as Docker container (`docker run -i --rm docker.io/jonrad/lsp-mcp:0.3.1`)
- Multi-LSP support via config file
- Dynamically generates LSP methods from JSON schema
- Requires Docker or Node.js (npx) — **UC's worker container has neither**

**bug-ops/mcpls** (Rust, MIT/Apache-2.0, 44 stars):
- `cargo install mcpls` — single binary
- Zero-config for Rust projects (auto-detects rust-analyzer)
- Graceful degradation (continues if one LSP fails)
- Pre-built binaries for Linux/macOS/Windows
- Supports: rust-analyzer, gopls, typescript-language-server, jedi-language-server, clangd, and more
- **Best fit for UC's ethos** (single binary, Rust, minimal) but it's an external process, not in-process Python

**agentic-labs/lsproxy** (Docker, 107 stars):
- Runs LSP servers in a Docker container, exposes REST API
- Python SDK: `lsproxy-sdk` (v0.3.1, deps: httpx, pydantic)
- Languages: C/C++ (clangd), Go (gopls), Java (jdtls), JS (typescript-language-server), PHP (phpactor), Python (jedi-language-server), Rust (rust-analyzer), TypeScript (typescript-language-server)
- Auto-configures language servers based on project files
- REST API → could be called from a Python MCP server
- **Heavy**: requires a separate Docker container running alongside the worker

---

### 2. How Similar AI Coding Tools Get Real-Time Code Intelligence

| Tool | Approach | Details |
|---|---|---|
| **Aider** (46.9k stars, Python) | **Tree-sitter repo map + ctags** | Aider builds a "repo map" using tree-sitter for symbol extraction and ctags as fallback. No LSP. The repo map is a ranked tree of symbols injected into context. Aider does NOT use LSP for real-time navigation — it relies on its repo map + grep/read for code understanding. |
| **OpenHands** (79k stars, Python) | **No LSP** | OpenHands (formerly OpenDevin) does not embed an LSP client. The agent uses file read/grep/search tools. No real-time code intelligence — relies on the LLM's reasoning over file contents. |
| **Continue** (34.6k stars, TypeScript) | **LSP via VS Code extension API** | Continue is a VS Code extension, so it uses the editor's built-in LSP integration. It does NOT run its own LSP servers — it accesses LSP through VS Code's `vscode.languag` API. Not applicable to a standalone worker. |
| **Cursor** (closed source) | **Proprietary code intelligence** | Cursor uses a combination of its own indexing (codebase embeddings) and VS Code's LSP. Not open-source; approach not replicable. |
| **Roo Code** (24.3k stars, TypeScript) | **VS Code LSP + tree-sitter** | Roo Code is a VS Code extension like Continue. Uses editor LSP. Also uses tree-sitter for some features. Not standalone-worker applicable. |

**Key insight**: Most standalone AI coding tools (Aider, OpenHands) do NOT use LSP at all. They rely on tree-sitter/ctags for structure and grep/read for navigation. The LSP-based approaches (Continue, Cursor, Roo Code) are all IDE extensions that leverage the host editor's LSP — they don't run LSP servers themselves.

**No major standalone AI coding tool runs LSP servers as subprocesses for real-time code intelligence.** This is a gap that the LSP-as-MCP bridge projects (jonrad/lsp-mcp, mcpls, lsproxy) are trying to fill.

---

### 3. Minimal Viable Real-Time LSP Story for Multi-Language

**Can one library cover multiple languages?**
- Yes: `multilspy` covers 10 languages with one API, but each language needs its own LSP server binary.
- `lsproxy` covers 8 languages via REST API, but needs a Docker container.
- `mcpls` covers multiple languages as a single binary, but it's an external process (Rust binary, not Python).

**Is there a language-agnostic option without per-language LSP servers?**
- Yes: **tree-sitter** based symbol navigation. `tree-sitter` (v0.26.0, Python bindings) + `tree-sitter-languages` (v1.10.2, pre-built grammars for 20+ languages) can parse any supported language and extract symbols (functions, classes, methods) from the AST.
- However, tree-sitter gives you **syntax-only** navigation: it knows where symbols are defined, but cannot do **cross-file find-references** or **semantic hover** (type info, docstrings) without a full language model.
- For cross-file references, you'd need to either:
  - Build your own reference index from tree-sitter parse results (essentially reimplementing codegraph)
  - Use an LSP server (which does this properly via semantic analysis)

**The honest tradeoff**: There is no free lunch. For true real-time, cross-file, semantic code intelligence (find-refs, hover with type info, go-to-def across files), you need a language server. Tree-sitter alone gives you single-file symbol listing and intra-file navigation, but not cross-file references or type information.

---

### 4. Integration Approaches for UltimateCoders

#### Approach A: `uc-lsp` MCP Server Wrapping `multilspy`

**Shape**: New `python/ultimate_coders/agent/lsp_mcp.py` (parallel to `engine_mcp.py` and `fs_mcp.py`). Wraps multilspy's async `LanguageServer` API. Detects file language by extension, creates the appropriate `LanguageServer.create()` per language, opens files via `didOpen`, sends `didChange` after edits.

**MCP tools exposed**: `lsp_definition`, `lsp_references`, `lsp_document_symbols`, `lsp_hover`, `lsp_workspace_symbol`.

**How it handles real-time**: When the agent calls `lsp_definition` for a file it just edited, the MCP server sends `textDocument/didChange` with the current file content before making the request. The LSP server processes the change and returns up-to-date results.

| Criterion | Assessment |
|---|---|
| Multi-language | Yes — 10 languages via multilspy |
| Real-time | Yes — LSP sees current file state via `didChange` |
| In-process Python | Yes — pure Python MCP server + multilspy |
| Minimal new deps | Moderate — `multilspy` (134 KB) + `jedi-language-server` (for Python). Other language servers auto-downloaded on first use. |
| Container image impact | Low for Python-only; High for multi-lang (needs Node.js for TS, Go for gopls, etc.) |
| Ponytail ethos | Good — one dep, one file, follows existing MCP pattern |
| Risk | multilspy is pre-alpha; pinned jedi v0.41.3 is old; binary downloads at runtime need network |

**Container image additions needed**:
- Python-only: just `pip install multilspy` (jedi-language-server comes as dep)
- TS/JS: Node.js + npm (for typescript-language-server auto-install)
- Rust: rust-analyzer binary (auto-downloaded by multilspy)
- Go: Go toolchain + gopls

**MVP scope**: Start with Python-only (jedi-language-server). Add languages incrementally by installing their LSP servers in the Dockerfile.

#### Approach B: `uc-lsp` MCP Server Spawning Per-Language LSP Servers Directly

**Shape**: Same MCP server file, but instead of multilspy, use `lsprotocol` for LSP types and manage JSON-RPC communication directly. Spawn `jedi-language-server`, `rust-analyzer`, `gopls` as subprocesses.

| Criterion | Assessment |
|---|---|
| Multi-language | Yes — any LSP server |
| Real-time | Yes — same `didChange` mechanism |
| In-process Python | Yes |
| Minimal new deps | Low — just `lsprotocol` (types) + LSP server binaries |
| Container image impact | Same as A (needs LSP server binaries per language) |
| Ponytail ethos | Poor — reimplements multilspy's binary management, init handshake, capability negotiation |
| Risk | High implementation effort; server-specific quirks; fragile |

**Verdict**: This is strictly worse than Approach A unless multilspy's pre-alpha status is a dealbreaker. multilspy already does the hard work; reimplementing it is not ponytail.

#### Approach C: Tree-Sitter-Based Symbol Navigation (No LSP Server)

**Shape**: `uc-treesitter_mcp.py` MCP server using `tree-sitter` + `tree-sitter-languages`. Parses files on demand, extracts symbols from AST, provides `ts_symbols`, `ts_go_to_def` (intra-file), `ts_find_refs` (intra-file by name search in AST).

| Criterion | Assessment |
|---|---|
| Multi-language | Yes — tree-sitter-languages bundles 20+ grammars |
| Real-time | Yes — parses current file content on each call |
| In-process Python | Yes |
| Minimal new deps | Very low — `tree-sitter` + `tree-sitter-languages` (pre-built wheels, no runtime binary downloads) |
| Container image impact | Minimal — pure Python wheels, no external runtimes needed |
| Ponytail ethos | Excellent — two deps, no external processes, no network |
| **Critical limitation** | **No cross-file find-references. No semantic hover (type info, docstrings). No go-to-def across files.** Only intra-file symbol listing and navigation. |

**What it can do**:
- `document_symbols(file)` → list of functions, classes, methods with line ranges
- `go_to_definition(file, symbol)` → definition location **within the same file**
- `find_references(file, symbol)` → references **within the same file** (AST text match, not semantic)

**What it CANNOT do**:
- Find references to `foo()` across other files in the project
- Provide hover type information (`x: List[int]` → "List[int]")
- Go to definition in another file (e.g., clicking an import)

**Verdict**: Good as a lightweight supplement, but does not replace codegraph's cross-file call graph / find-references capability. The agent would still need codegraph for cross-file queries.

#### Approach D: Refresh Codegraph Index On-Demand Before Each Query

**Shape**: No new MCP server. Instead, modify the existing codegraph integration to trigger a sync of the worktree directory before answering queries. Either:
- Run `codegraph sync <worktree_path>` before each codegraph tool call, or
- Point CodegraphClient at the worktree's own `.codegraph/codegraph.db` (if the worktree is indexed separately)

| Criterion | Assessment |
|---|---|
| Multi-language | Yes — codegraph already supports 20+ languages |
| Real-time | **Semi** — sync takes ~100-230ms per file (from prior research); full index rebuild takes longer. Still not truly real-time during rapid edits. |
| In-process Python | Yes — CodegraphClient is already in-process |
| Minimal new deps | **Zero** — reuses existing codegraph infrastructure |
| Container image impact | None |
| Ponytail ethos | Good — no new deps, minimal code change |
| Risk | codegraph daemon may not be running in worker container; worktree may not have `.codegraph/` dir; sync latency still ~100ms+ per file |

**Key problem**: The codegraph daemon + file watcher runs on the HOST (developer machine), not in the worker container. The worker container (`docker/Dockerfile`) has no codegraph installed. To use this approach, codegraph would need to be installed in the worker container and configured to index the worktree directory. This is a non-trivial container change.

Also, codegraph uses tree-sitter for parsing (same as Approach C), so it has the same semantic limitations — it's not a true LSP. It resolves references via tree-sitter AST analysis, not semantic type analysis. The 7,662 unresolved references in the current index (from prior research) show this limitation.

**Verdict**: Cheapest to implement but does not achieve the "real-time LSP semantics" goal. It's still a precomputed index, just refreshed more often. And it requires installing codegraph (Node.js + SQLite) in the worker container.

---

### 5. Comparison Matrix

| Criterion | A: multilspy | B: DIY LSP | C: tree-sitter | D: codegraph refresh |
|---|---|---|---|---|
| Real-time (reflects current edits) | Yes | Yes | Yes | Semi (~100ms lag) |
| Cross-file find-refs | Yes | Yes | **No** | Yes (but tree-sitter-based, not semantic) |
| Semantic hover (types) | Yes | Yes | **No** | Partial (signatures, not types) |
| Go-to-def across files | Yes | Yes | **No** | Yes |
| Multi-language | 10 langs | Any | 20+ langs (syntax only) | 20+ langs (syntax only) |
| In-process Python MCP | Yes | Yes | Yes | Yes (existing) |
| New Python deps | multilspy + jedi-ls | lsprotocol | tree-sitter + ts-languages | None |
| External runtime deps | Per-lang LSP binaries | Per-lang LSP binaries | None | codegraph (Node.js) |
| Container image bloat | Low (Py) → High (all langs) | Same as A | Minimal | Moderate (Node.js) |
| Implementation effort | Moderate | High | Moderate | Low |
| Ponytail fit | Good | Poor | Excellent | Good |
| Maturity of dependency | Pre-alpha | N/A (DIY) | Stable (v0.26) | Stable (codegraph v1.0) |

---

### 6. External References

| Resource | URL | Relevance |
|---|---|---|
| multilspy (PyPI) | https://pypi.org/project/multilspy/ | Python LSP client library, MIT, v0.0.15 |
| multilspy (GitHub) | https://github.com/microsoft/multilspy | Source, issue tracker |
| multilspy paper (NeurIPS 2023) | https://arxiv.org/abs/2306.10763 | Monitor-Guided Decoding — original use case |
| jonrad/lsp-mcp | https://github.com/jonrad/lsp-mcp | LSP-as-MCP bridge (TypeScript, MIT, 190 stars, POC) |
| bug-ops/mcpls | https://github.com/bug-ops/mcpls | Universal MCP-to-LSP bridge (Rust, MIT/Apache-2.0, 44 stars) |
| Tritlo/lsp-mcp | https://github.com/Tritlo/lsp-mcp | LSP MCP server for hover/completions (TypeScript, MIT, 123 stars) |
| agentic-labs/lsproxy | https://github.com/agentic-labs/lsproxy | Multi-lang code navigation via REST API (Docker, 107 stars) |
| lsproxy-sdk (PyPI) | https://pypi.org/project/lsproxy-sdk/ | Python SDK for lsproxy (v0.3.1) |
| lsproxy supported langs | https://docs.lsproxy.dev | C/C++, Go, Java, JS, PHP, Python, Rust, TS |
| DeusData/codebase-memory-mcp | https://github.com/DeusData/codebase-memory-mcp | High-perf code intelligence MCP (C, MIT, 23.4k stars, 158 langs, tree-sitter + hybrid LSP) |
| tree-sitter (PyPI) | https://pypi.org/project/tree-sitter/ | Python bindings to tree-sitter (v0.26.0) |
| tree-sitter-languages (PyPI) | https://pypi.org/project/tree-sitter-languages/ | Pre-built grammars for 20+ languages (v1.10.2, Apache 2.0) |
| lsprotocol (PyPI) | https://pypi.org/project/lsprotocol/ | Python types for LSP (v2025.0.0) |
| pygls (PyPI) | https://pypi.org/project/pygls/ | Generic LSP server framework (v2.1.1, for writing servers NOT consuming) |
| python-lsp-server (PyPI) | https://pypi.org/project/python-lsp-server/ | Python LSP server (pylsp, v1.14.0) |
| jedi-language-server (PyPI) | https://pypi.org/project/jedi-language-server/ | Python LSP server via jedi (v0.47.0) |
| LSP Specification | https://microsoft.github.io/language-server-protocol/ | Official LSP spec |
| LSP Server Implementors | https://microsoft.github.io/language-server-protocol/implementors/servers/ | List of all LSP servers |

---

## Recommendation

**Primary: Approach A (multilspy) with a phased language rollout. Start with Python-only MVP.**

### Reasoning

1. **Fits the constraint model best**: in-process Python MCP server (matches `engine_mcp.py`/`fs_mcp.py` pattern), one dependency, async API fits the MCP event loop, MIT licensed.

2. **Truly real-time**: LSP `didChange` notifications mean the LSP server sees file content as it is right now — no index lag. This directly solves the codegraph staleness problem.

3. **Ponytail ethos**: one new Python dep (`multilspy`, 134 KB wheel), one new file (`lsp_mcp.py`), follows the existing `_create_server` / `@server.list_tools()` / `@server.call_tool()` pattern exactly. No external processes to manage (multilspy handles binary lifecycle).

4. **Phased rollout controls container bloat**: Start with Python-only (just `pip install multilspy` — jedi-language-server comes as a dependency, no Node.js/Go/Rust needed). Add `rust-analyzer`, `gopls`, `typescript-language-server` to the Dockerfile only when the worker needs to handle those languages. The worker already knows what language a subtask targets (from the repo), so it can lazily start only the needed LSP server.

5. **Better than C (tree-sitter)**: The agent needs cross-file find-references and semantic hover — tree-sitter alone cannot provide these. Codegraph already does tree-sitter-based cross-file refs (with 7,662 unresolved), but LSP servers do proper semantic resolution.

6. **Better than D (codegraph refresh)**: Doesn't require installing Node.js/codegraph in the worker container. Truly real-time (no sync debounce). Gives semantic hover (types, docstrings) that codegraph's tree-sitter approach cannot.

7. **Better than B (DIY LSP)**: multilspy already handles the hard parts (binary download, init handshake, capability negotiation, server-specific config). Reimplementing this is not ponytail.

### Mitigations for key risks

- **multilspy pre-alpha**: Pin version (`multilspy==0.0.15`). The API surface we use (`request_definition`, `request_references`, `request_hover`, `request_document_symbols`) is stable across versions. Wrap in a try/except with graceful degradation (return empty results on failure, same as CodegraphClient).

- **Pinned jedi v0.41.3**: Acceptable for MVP. If it causes issues, we can vendor a newer jedi-language-server and monkey-patch multilspy's config, or contribute a PR upstream.

- **Binary downloads in container**: Run `multilspy` once during Docker build (for Python/rust-analyzer) to pre-cache binaries into `~/.multilspy/lsp/`. For TS/Go, install the language server in the Dockerfile directly.

- **Server startup latency**: Start the LSP server when the MCP server initializes (first tool call), not on every request. Keep the server alive for the MCP server's lifetime. jedi-language-server starts in ~1s; acceptable for a subtask that runs for minutes.

### Fallback: Approach C (tree-sitter) as a complement

If multilspy proves too unstable or a language's LSP server is unavailable in the container, fall back to tree-sitter for `document_symbols` (which tree-sitter does well) and let the agent use `uc-fs`'s `read_file` + grep for the rest. This is already what Aider/OpenHands do successfully.

---

## Key Risks

| Risk | Severity | Mitigation |
|---|---|---|
| multilspy pre-alpha API changes | Medium | Pin version; wrap in adapter with graceful degradation; the 5 methods we use are unlikely to change (they map directly to LSP spec) |
| Container image size growth | Medium | Phased language rollout; only install LSP servers for languages the worker actually handles; pre-cache during build |
| LSP server startup latency | Low-Medium | Start once per MCP server lifetime, not per request; jedi ~1s, rust-analyzer ~5s (acceptable for minute-scale subtasks) |
| Language coverage gaps | Low | Start with Python (jedi). Rust/Go/TS are well-supported by multilspy. Add languages as needed. |
| Network dependency for binary downloads | Medium | Pre-download during Docker build; cache in image layer |
| multilspy pinned jedi v0.41.3 vs latest v0.47.0 | Low | Acceptable for MVP; jedi's core API (completion, goto, refs) is stable |
| `didChange` tracking complexity | Low | MCP server reads file fresh on each tool call and sends full `didChange` (not incremental). Simple and correct, if slightly less efficient. |
| Worktree path mismatch | Low | MCP server takes `--workspace` arg (same as fs_mcp.py), points at the worktree path. multilspy's `repository_root_path` = worktree path. |
| Python 3.11 in container vs multilspy requires >=3.8 | None | Compatible. Worker Dockerfile uses python:3.11-slim. |
| No file watching in multilspy | Low | Not needed — the MCP server sends `didChange` on demand before each query. The agent's edits go through `uc-fs` MCP (write_file/edit_file), so we know when files change. Could also intercept writes to send proactive `didChange`. |

## Caveats / Not Found

1. **multilspy runtime behavior under concurrent access**: Not tested. The async API should be fine in an asyncio event loop, but the sync API (`SyncLanguageServer`) runs its own event loop in a thread, which could conflict with the MCP server's asyncio loop. **Recommendation: use the async `LanguageServer` class, not `SyncLanguageServer`.**

2. **multilspy binary download reliability**: The runtime dependency JSON files point to specific GitHub release URLs (e.g., rust-analyzer 2023-10-09 release). These URLs could break if releases are deleted. Pre-downloading during Docker build mitigates this.

3. **codegraph in worker container**: The current worker Dockerfile (`docker/Dockerfile`) does not install codegraph (Node.js-based). Approach D would require adding Node.js + codegraph to the container — a significant image size increase (~200MB for Node.js runtime alone).

4. **How Aider builds its repo map**: Confirmed via documentation that Aider uses tree-sitter + ctags, NOT LSP. The exact implementation (in `aider/repomap.py`) was not examined in detail since it confirms the "no LSP" approach.

5. **Continue's LSP usage**: Continue accesses LSP through VS Code's extension API (`vscode.languages`), not by running LSP servers directly. This approach is not applicable to a standalone containerized worker.

6. **mcpls (bug-ops/mcpls) as an alternative to multilspy**: mcpls is a single Rust binary that bridges LSP to MCP. It could replace the entire `uc-lsp` MCP server — just run `mcpls` as an MCP server in the container. However, it's an external process (not in-process Python), which breaks the UC pattern of Python in-process MCP servers. Worth considering as a "zero-code" alternative if the ponytail pattern is flexible enough to allow one external binary.

7. **codebase-memory-mcp (DeusData)**: 23.4k stars, 158 languages, single static C binary, MIT. This is a tree-sitter-based code intelligence MCP server (not LSP-based, but has "hybrid LSP" for 9 languages). Could be another zero-code alternative to a custom `uc-lsp` MCP server. However, it indexes into a knowledge graph (like codegraph), so it may have the same staleness issue unless it re-indexes on demand.
