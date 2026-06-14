# Research: Codegraph Integration for Coding Agent

- **Query**: Research codegraph project and integration with coding agent Worker
- **Scope**: Mixed (internal codebase + external codegraph project)
- **Date**: 2026-06-14

## Findings

### 1. What Codegraph Is

Codegraph (https://github.com/colbymchenry/codegraph) is a local code intelligence tool that builds a SQLite knowledge graph of every symbol, edge, and file in a workspace. It provides sub-millisecond reads via MCP tools and a CLI.

**Core capabilities:**
- Symbol extraction via tree-sitter (functions, classes, methods, structs, traits, enums, imports, variables)
- Edge extraction (calls, references, imports, extends, implements, contains, instantiates)
- FTS5 full-text search across symbol names, qualified names, docstrings, and signatures
- Call graph traversal (callers, callees)
- Impact/blast-radius analysis (what would break if you change X)
- Affected test detection (which tests cover changed source files)
- Auto-sync via native file watcher (FSEvents on macOS)
- Framework-aware route detection (FastAPI, Flask, Express, etc.)

**Key stats for this project:**
- 133 indexed files
- 3,158 nodes (1,082 methods, 761 functions, 140 classes, 127 structs, 617 imports, etc.)
- 7,551 edges (3,489 contains, 2,704 calls, 525 references, 495 imports, 316 instantiates, 11 extends, 11 implements)
- 7,662 unresolved references
- Languages: 1,587 Rust nodes, 1,570 Python nodes, 1 JavaScript node
- DB size: 8.05 MB

### 2. How Codegraph Indexes Code

**Storage:** SQLite database at `.codegraph/codegraph.db`

**Table schemas (exact from this project):**

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `nodes` | All symbols | id, kind, name, qualified_name, file_path, language, start_line, end_line, start_column, end_column, docstring, signature, visibility, is_exported, is_async, is_static, is_abstract, decorators (JSON), type_parameters (JSON), updated_at |
| `edges` | All relationships | id, source (FK->nodes), target (FK->nodes), kind, metadata (JSON), line, col, provenance |
| `files` | Indexed files | path, content_hash, language, size, modified_at, indexed_at, node_count, errors (JSON) |
| `unresolved_refs` | Unresolved symbols | from_node_id (FK->nodes), reference_name, reference_kind, line, col, candidates (JSON), file_path, language |
| `nodes_fts` | Full-text search index | FTS5 virtual table on id, name, qualified_name, docstring, signature |

**Node kinds observed:** method, function, import, class, enum_member, file, struct, variable, enum, trait

**Edge kinds observed:** contains, calls, references, imports, instantiates, extends, implements

**Resolution process:**
1. tree-sitter parses source into ASTs
2. Language-specific queries extract nodes and edges
3. References are resolved: calls -> definitions, imports -> source files, class inheritance
4. Everything goes into SQLite with FTS5
5. File watcher auto-syncs on changes (debounced 2s)

### 3. APIs/Interfaces Exposed

#### MCP Tools (primary interface for AI agents)

8 tools registered in this project's settings. 4 are "primary" (listed by default in v1.0); 4 are "extended" (hidden by default but fully functional).

| Tool | Purpose | Current Project Availability |
|------|---------|------|
| `codegraph_explore` | PRIMARY: Answer architecture/flow questions in one call. Returns verbatim source of relevant symbols grouped by file, plus relationship map and blast radius. Surfaces dynamic-dispatch hops. | Available (v1.0 MCP; CLI `codegraph explore` not in v0.9.9) |
| `codegraph_node` | One symbol's full source + caller/callee trail, or read a whole file (like Read tool with line numbers, offset/limit) | Available |
| `codegraph_search` | Find symbols by name across codebase | Available |
| `codegraph_callers` | Every call site of a function (including callback registrations) | Available |
| `codegraph_callees` | What a function calls | Available (extended, needs `CODEGRAPH_MCP_TOOLS` env var to list) |
| `codegraph_impact` | Blast radius analysis - what code is affected by changing a symbol | Available (extended) |
| `codegraph_files` | Project file structure from the index | Available (extended) |
| `codegraph_status` | Index status and statistics | Available (extended) |

#### CLI Commands

```bash
codegraph init [path]            # Initialize + build full index
codegraph index [path]           # Full index (force re-index)
codegraph sync [path]            # Incremental update
codegraph status [path]          # Show statistics
codegraph query <search>         # Search symbols (--kind, --limit, --json)
codegraph callers <symbol>       # Find callers (--limit, --json)
codegraph callees <symbol>       # Find callees (--limit, --json)
codegraph impact <symbol>        # Impact analysis (--depth, --json)
codegraph affected [files...]    # Affected test files (--stdin, --depth, --filter, --json)
codegraph explore <query>        # (v1.0 only) Architecture question in one shot
codegraph node <symbol|file>     # (v1.0 only) One symbol's source + callers
codegraph serve --mcp            # Start as MCP server (stdio transport)
```

All query commands support `-j` / `--json` for structured output.

#### Programmatic API (Node.js/TypeScript)

```typescript
import CodeGraph from '@colbymchenry/codegraph';

const cg = await CodeGraph.init('/path/to/project');
await cg.indexAll({ onProgress: (p) => ... });
const results = cg.searchNodes('UserService');
const callers = cg.getCallers(results[0].node.id);
const context = await cg.buildContext('fix login bug', { maxNodes: 20, includeCode: true, format: 'markdown' });
const impact = cg.getImpactRadius(results[0].node.id, 2);
cg.watch();   // auto-sync
cg.close();
```

Requires Node 22.5+ (for built-in `node:sqlite`). Not relevant for this project's Python stack directly.

#### Direct SQLite Access

The `.codegraph/codegraph.db` is a standard SQLite database with WAL journal mode. It can be queried directly from Python via `sqlite3` module. The FTS5 index enables `MATCH` queries.

### 4. Current Integration in This Project

**MCP Server Configuration** (in `~/.claude.json`):
```json
{
  "codegraph": {
    "type": "stdio",
    "command": "codegraph",
    "args": ["serve", "--mcp"]
  }
}
```

**Permissions** (in `~/.claude/settings.json`):
All 8 codegraph MCP tools are in the allow list.

**Index State:**
- `.codegraph/` directory exists with `codegraph.db` (8 MB), WAL, SHM, daemon socket/pid
- Daemon running (pid 96178, v0.9.9) with file watcher active
- Auto-sync operational (individual file syncs ~100-230ms)
- Index is up to date as of last check

**Installed Version:** v0.9.9 (CLI at `/usr/local/bin/codegraph`)
- Note: v1.0 is released upstream but this project has 0.9.9
- v1.0 adds `codegraph explore` and `codegraph node` CLI commands
- The MCP server may expose these tools even at 0.9.9 (server instructions mention them)

**Usage in Claude Code sessions:** Codegraph MCP tools are available to Claude Code during interactive sessions. The MCP server instructions guide Claude Code to use `codegraph_explore` as the primary tool for any architecture/flow question, avoiding redundant grep/read loops.

### 5. Capabilities Benefiting a Coding Agent (Worker)

#### Symbol Search / Exploration
- **codegraph_search**: Find symbols by name (FTS5, sub-ms). The Worker's current `_tool_search` relies on the engine's text search, which searches content, not symbol structure. Codegraph adds symbol-level granularity.
- **codegraph_explore**: Natural language queries return verbatim source of relevant symbols grouped by file. A Worker could ask "how does the Orchestrator dispatch subtasks?" and get a structured answer with code, instead of multiple grep/read iterations.

#### Call Graph / Dependency Analysis
- **codegraph_callers**: Find all call sites of a symbol, including callback registrations. Example: `codegraph callers execute_subtask` returns `auto_execute_loop` in `run_dashboard.py:43`, `run_dashboard.py:67`, `tui/app.py:334`.
- **codegraph_callees**: What a function calls. Example: `execute_subtask` calls `emit`, `_execute_in_sandbox`, `_execute_with_llm`, and instantiates `SubtaskResult`.
- These enable a Worker to understand the impact and reach of code before modifying it.

#### Impact Analysis for Refactoring
- **codegraph_impact**: Traverse the dependency graph to find all symbols affected by changing a given symbol. Example: `codegraph impact LocalEngine` returns 76 affected symbols across the codebase.
- **codegraph affected**: Given changed source files, find which test files are affected. This is directly useful for a Worker running tests after code changes.
- These are critical for a coding agent that modifies code: it can predict blast radius before editing and verify test coverage after.

#### Code Navigation (Go to Definition, Find References)
- **codegraph_node**: Get a symbol's full source + caller/callee trail. Can also read a file with line numbers (like the Read tool).
- The `nodes` table contains `start_line`, `end_line`, `start_column`, `end_column` for precise location.
- The `edges` table with `references` and `calls` kinds provides "find references" capability.
- **Unresolved refs** (`unresolved_refs` table): 7,662 entries where references couldn't be resolved to definitions. This can flag potential issues or cross-boundary calls.

### 6. Integration Approaches for Worker Tool-Calling

#### Approach A: Codegraph as Worker Tools (LLM mode)

Add codegraph-backed tools to the Worker's `_build_tools()` and `_build_tool_definitions()` methods. The LLM would call these tools during its tool-calling loop.

**New tools to add:**

| Tool Name | Codegraph Operation | Description |
|-----------|-------------------|-------------|
| `symbol_search` | `codegraph query` CLI | Search for symbols by name, kind, or pattern |
| `symbol_explore` | `codegraph explore` (if v1.0) or compose from `query` + `node` | Natural language code exploration |
| `find_callers` | `codegraph callers` CLI | Find all call sites of a symbol |
| `find_callees` | `codegraph callees` CLI | Find what a symbol calls |
| `impact_analysis` | `codegraph impact` CLI | Analyze blast radius of changing a symbol |
| `affected_tests` | `codegraph affected` CLI | Find test files affected by source changes |

**Implementation pattern:** Each tool wraps a `codegraph` CLI invocation with `--json` output, parses the JSON, and returns it to the LLM.

```python
async def _tool_symbol_search(self, query: str, kind: str = None, limit: int = 10) -> str:
    """Search for symbols in the codebase knowledge graph."""
    import asyncio, json
    args = ["query", query, "--json", "--limit", str(limit)]
    if kind:
        args.extend(["--kind", kind])
    proc = await asyncio.create_subprocess_exec(
        "codegraph", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode("utf-8", errors="replace")
```

**Pros:** LLM decides when to use codegraph; flexible exploration; follows existing tool pattern.
**Cons:** Each CLI call is a subprocess spawn (~100-200ms overhead per call); LLM must learn to use the tools effectively.

#### Approach B: Pre-processing Step Before Invoking Claude Code (Sandbox mode)

Before launching Claude Code in a sandbox, run codegraph queries to build a context summary that gets injected into the prompt. This gives the sandboxed Claude Code instance structural knowledge without requiring it to discover it via grep/read.

**Implementation pattern:**

```python
async def _build_codegraph_context(self, subtask: Subtask) -> str:
    """Pre-gather codegraph context for a subtask."""
    import asyncio, json
    context_parts = []

    # 1. Search for relevant symbols based on subtask description
    proc = await asyncio.create_subprocess_exec(
        "codegraph", "query", subtask.description[:50], "--json", "--limit", "5",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    results = json.loads(stdout.decode())
    context_parts.append(f"Relevant symbols: {json.dumps(results, indent=2)}")

    # 2. For each relevant symbol, get impact analysis
    for r in results[:3]:
        symbol = r["node"]["name"]
        proc = await asyncio.create_subprocess_exec(
            "codegraph", "impact", symbol, "--json", "--depth", "1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        impact = json.loads(stdout.decode())
        context_parts.append(f"Impact of {symbol}: {json.dumps(impact, indent=2)}")

    return "\n\n".join(context_parts)
```

**Pros:** Reduces Claude Code's exploration cost (fewer grep/read turns); structural context available from the start.
**Cons:** Static pre-selection may miss symbols the agent needs; adds latency before sandbox launch.

#### Approach C: Hybrid -- Codegraph Context + Worker Tools

Combine both approaches:
1. Pre-processing: Build a lightweight codegraph context summary and inject it into the prompt.
2. Worker tools: Also expose codegraph tools so the LLM can query for more details during execution.

**Pros:** Best of both worlds; the pre-context gives orientation, and the tools enable deep exploration.
**Cons:** More complex implementation; prompt may get large.

#### Approach D: Direct SQLite Access (Python)

Instead of spawning CLI subprocesses, read the `.codegraph/codegraph.db` SQLite database directly from Python using the `sqlite3` module.

```python
import sqlite3

class CodegraphClient:
    def __init__(self, db_path: str = ".codegraph/codegraph.db"):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row

    def search(self, query: str, kind: str = None, limit: int = 10) -> list[dict]:
        """FTS5 search across symbol names, qualified names, docstrings."""
        sql = """
            SELECT n.* FROM nodes n
            JOIN nodes_fts fts ON n.id = fts.id
            WHERE nodes_fts MATCH ?
        """
        params = [query]
        if kind:
            sql += " AND n.kind = ?"
            params.append(kind)
        sql += " LIMIT ?"
        params.append(limit)
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def callers(self, symbol_name: str) -> list[dict]:
        """Find all callers of a symbol."""
        sql = """
            SELECT n_src.name, n_src.kind, n_src.file_path, n_src.start_line, e.line as call_line
            FROM edges e
            JOIN nodes n_src ON e.source = n_src.id
            JOIN nodes n_tgt ON e.target = n_tgt.id
            WHERE n_tgt.name = ? AND e.kind = 'calls'
        """
        return [dict(row) for row in self.conn.execute(sql, [symbol_name]).fetchall()]

    def impact(self, symbol_name: str, depth: int = 2) -> list[dict]:
        """BFS traversal from symbol to find all affected symbols."""
        # Start: find node by name
        target = self.conn.execute(
            "SELECT id FROM nodes WHERE name = ? LIMIT 1", [symbol_name]
        ).fetchone()
        if not target:
            return []
        # BFS over reverse edges
        visited = set()
        queue = [target[0]]
        affected = []
        for _ in range(depth):
            next_queue = []
            for node_id in queue:
                if node_id in visited:
                    continue
                visited.add(node_id)
                # Find nodes that reference/call/instantiate this node
                dependents = self.conn.execute("""
                    SELECT DISTINCT n.id, n.name, n.kind, n.file_path, n.start_line
                    FROM edges e JOIN nodes n ON e.source = n.id
                    WHERE e.target = ? AND e.kind IN ('calls', 'references', 'imports', 'instantiates')
                """, [node_id]).fetchall()
                for d in dependents:
                    if d[0] not in visited:
                        affected.append(dict(zip(['id','name','kind','file_path','start_line'], d)))
                        next_queue.append(d[0])
            queue = next_queue
        return affected
```

**Pros:** No subprocess overhead; direct SQL access is the fastest possible; full control over queries.
**Cons:** Must handle SQLite locking (WAL mode helps); schema changes between codegraph versions could break queries; doesn't get the smart `explore`/`buildContext` logic that the CLI/MCP tools provide.

### Files Found

| File Path | Description |
|---|---|
| `.codegraph/codegraph.db` | SQLite knowledge graph database (8 MB, 3158 nodes, 7551 edges) |
| `.codegraph/daemon.sock` | Unix socket for codegraph daemon |
| `.codegraph/daemon.pid` | Daemon process ID |
| `.codegraph/daemon.log` | Daemon activity log |
| `python/ultimate_coders/agent/worker.py` | Worker class with LLM tool-calling loop and sandbox execution (860 lines) |
| `python/ultimate_coders/agent/sandbox.py` | SandboxManager, ClaudeCodeAdapter, CodexAdapter (652 lines) |
| `python/ultimate_coders/agent/llm.py` | LLMClient with tool calling support |
| `python/ultimate_coders/agent/orchestrator.py` | Orchestrator class (1043 lines) |
| `python/ultimate_coders/agent/types.py` | Subtask, SubtaskResult, WorkerInfo types |
| `~/.claude.json` | Global MCP server config (codegraph configured) |
| `~/.claude/settings.json` | Permissions including all 8 codegraph MCP tools |

### Code Patterns

**Worker tool registration** (worker.py:576-584):
```python
def _build_tools(self) -> dict[str, Callable]:
    return {
        "search": self._tool_search,
        "read_memory": self._tool_read_memory,
        "write_memory": self._tool_write_memory,
        "read_file": self._tool_read_file,
        "list_files": self._tool_list_files,
    }
```

**Worker tool definition** (worker.py:586-669):
Each tool is defined via `make_tool_definition(name, description, parameters)` and passed to `self.llm_client.complete_with_tools()`.

**Sandbox mode execution** (worker.py:334-371):
When `execution_mode == "sandbox"`, the Worker delegates to `SandboxManager.execute(prompt)` which runs Claude Code as a subprocess (`claude -p <prompt> --output-format json --max-turns 20`).

**LLM mode execution** (worker.py:249-332):
When `execution_mode != "sandbox"`, the Worker runs its own LLM tool-calling loop using `llm_client.complete_with_tools()` with the registered tools.

**Codegraph CLI JSON output format** (tested):
- `codegraph query <term> -j`: Returns `[{"node": {id, kind, name, qualifiedName, filePath, language, startLine, endLine, signature, docstring, ...}, "score": float}]`
- `codegraph callers <symbol> -j`: Returns `{"symbol": "...", "callers": [{name, kind, filePath, startLine}]}`
- `codegraph callees <symbol> -j`: Returns `{"symbol": "...", "callees": [{name, kind, filePath, startLine}]}`
- `codegraph impact <symbol> -j`: Returns `{"symbol": "...", "depth": N, "nodeCount": N, "edgeCount": N, "affected": [{name, kind, filePath, startLine}]}`
- `codegraph affected <files> -j`: Returns `{"changedFiles": [...], "affectedTests": [...], "totalDependentsTraversed": N}`

### External References

- [Codegraph GitHub](https://github.com/colbymchenry/codegraph) -- Main repository, MIT license
- [Codegraph Documentation](https://colbymchenry.github.io/codegraph/) -- Guides and API reference
- npm package: `@colbymchenry/codegraph` (v1.0+)
- Benchmark claims: ~16% cheaper, ~58% fewer tool calls, ~47% fewer tokens vs raw grep/read exploration
- Supports 20+ languages including Python and Rust (both used in this project)
- Framework-aware routes: FastAPI, Flask, Express (FastAPI is used in this project's dashboard)

### Related Specs

- `.trellis/spec/backend/index.md` -- Backend spec index
- `.trellis/spec/backend/dashboard-spec.md` -- Dashboard spec (related but not directly)

## Caveats / Not Found

1. **CLI version mismatch**: This project has codegraph v0.9.9 installed, but v1.0 is the latest release. The `codegraph explore` and `codegraph node` CLI commands are v1.0 only. The MCP server may still expose these tools at v0.9.9, but this was not verified (the MCP server instructions text mentions them, suggesting they are available through MCP even at 0.9.9).

2. **Codegraph explore not tested via MCP**: The `codegraph explore` CLI command is not available at v0.9.9, so its MCP equivalent (`codegraph_explore`) was not tested in this research. It may work through the MCP server since the server instructions explicitly mention it as the primary tool.

3. **Unresolved references are high**: 7,662 unresolved references in the index. This could limit the accuracy of call graph and impact analysis for cross-file references that couldn't be resolved. The high number may be due to this project's mixed Rust+Python codebase where cross-language references (Python calling Rust via PyO3) cannot be resolved by tree-sitter alone.

4. **No test file detection**: `codegraph affected` returns empty affected tests for all tested files. This project's test files may not follow codegraph's default test file detection patterns (test files are in `tests/python/`, not co-located with source).

5. **Subprocess overhead**: CLI-based integration requires spawning a new process per codegraph query. For the Worker's tool-calling loop, this adds ~100-200ms per tool call. Direct SQLite access (Approach D) avoids this overhead but loses the smart context-building logic of `codegraph_explore`.

6. **Sandbox mode isolation**: When Claude Code runs in sandbox mode, it has its own MCP server access (if configured). The codegraph index would need to be accessible from within the sandbox, and the sandbox Claude Code instance would need the codegraph MCP server configured. This was not investigated in this research.

7. **No programmatic Python API**: Codegraph's programmatic API is Node.js/TypeScript only. For Python integration, the options are: (a) CLI subprocess calls, (b) direct SQLite access, or (c) running a local HTTP server and calling it from Python.
