# Codegraph Integration Spec

> How the coding agent Worker integrates with the codegraph knowledge graph for structured code understanding.

---

## Overview

Codegraph provides a SQLite knowledge graph of every symbol, edge, and file in the workspace. The Worker integrates codegraph at two layers:

1. **Pre-processing layer**: Before subtask execution, auto-query codegraph to build structured context, injected into sandbox prompt or LLM messages
2. **Tool layer**: In LLM Worker's tool-calling loop, 5 codegraph tools let the LLM explore code structure on demand

Both layers share the same `CodegraphClient` implementation, which reads `.codegraph/codegraph.db` directly via `sqlite3`.

---

## Signatures

### CodegraphClient

```python
class CodegraphClient:
    def __init__(self, project_path: str) -> None:
        """project_path: absolute/relative path to project root.
        Empty string disables codegraph integration."""

    def is_available(self) -> bool:
        """Whether .codegraph/codegraph.db exists and is accessible."""

    def search(self, query: str, kind: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        """FTS5 search across symbol names, qualified names, docstrings, signatures.
        Returns list of dicts: id, name, qualified_name, kind, file_path, language,
        start_line, end_line, signature, docstring."""

    def callers(self, symbol: str, limit: int = 20) -> list[dict[str, Any]]:
        """Find all callers of a symbol (who calls this function/method).
        Returns list of dicts: name, kind, file_path, start_line."""

    def callees(self, symbol: str, limit: int = 20) -> list[dict[str, Any]]:
        """Find what a symbol calls.
        Returns list of dicts: name, kind, file_path, start_line."""

    def impact(self, symbol: str, depth: int = 2) -> list[dict[str, Any]]:
        """BFS traversal for all symbols affected by changing a symbol.
        Traverses reverse dependency edges (calls, references, imports, instantiates).
        Returns list of dicts: name, kind, file_path, start_line."""

    def explore(self, query: str, max_nodes: int = 15) -> str:
        """Build structured Markdown context from a natural language query.
        Combines search + callers/callees/impact. Truncated to ~2000 chars.
        Returns empty string if unavailable or no results."""
```

### Worker Codegraph Tools

| Tool Name | Method | CodegraphClient Method | Returns |
|-----------|--------|----------------------|---------|
| `symbol_search` | `_tool_symbol_search` | `search()` | JSON array of symbols |
| `find_callers` | `_tool_find_callers` | `callers()` | JSON array of callers |
| `find_callees` | `_tool_find_callees` | `callees()` | JSON array of callees |
| `impact_analysis` | `_tool_impact_analysis` | `impact()` | JSON `{symbol, affected_count, affected}` |
| `explore_code` | `_tool_explore_code` | `explore()` | Markdown string |

---

## Contracts

### Graceful Degradation Contract

| Condition | Behavior |
|-----------|----------|
| DB file does not exist | `is_available()` returns `False`; all methods return empty results |
| DB file corrupted | `_get_connection()` sets `_available = False`; queries return empty |
| DB table missing | `OperationalError` caught; returns empty list |
| Query execution fails | `OperationalError` / `DatabaseError` caught; returns empty list |
| `explore()` returns empty string | Treated as "no codegraph context" — no section added to prompt |

### Pre-processing Context Contract

**LLM mode** (`_gather_prior_context`):
- Calls `codegraph.explore(subtask.description, max_nodes=10)`
- Appends as `## Code Knowledge Graph\n{context}` section in prior_context
- Exceptions are caught and logged at DEBUG level; no codegraph section added

**Sandbox mode** (`_execute_in_sandbox`):
- Calls `codegraph.explore(subtask.description, max_nodes=10)` before building prompt
- Injects result as `prior_context` in `_SUBTASK_USER_TEMPLATE`
- Falls back to `"(sandbox mode: prior context not gathered)"` when unavailable

### Explore Output Format

```
## Relevant Symbols
- **symbol_name** (kind) @ file_path:start_line `signature`

## Dependencies
- **symbol_name** called by: caller1 (file:line), caller2 (file:line)
- **symbol_name** calls: callee1 (file:line), callee2 (file:line)

## Impact Analysis
Changing **symbol_name** would affect N symbol(s):
- affected_name (kind) @ file_path:start_line

## Affected Test Files
- tests/path/test_file.py (matched by: test_ prefix / _test suffix / /tests/ dir)
```

Total length capped at ~2000 chars; truncated with `... (truncated)` suffix.

---

## Validation & Error Matrix

| Condition | Error | Recovery |
|-----------|-------|----------|
| `project_path` is empty string | None | `is_available()` returns `False` |
| `.codegraph/codegraph.db` missing | None | All methods return empty results |
| SQLite connection fails | `OperationalError` / `DatabaseError` | `_available` set to `False`; logged at DEBUG |
| FTS5 query syntax error | `OperationalError` | Returns empty list; logged at DEBUG |
| `_sanitize_fts5` strips all chars | None | Returns `""`; search returns empty list |
| Codegraph explore exception in Worker | Any `Exception` | Caught in `_gather_prior_context` / `_execute_in_sandbox`; logged at DEBUG |
| Tool call when codegraph unavailable | None | Returns `{"error": "Codegraph not available"}` or `"Codegraph not available"` |

---

## Design Decisions

### ADR-1: Mixed Architecture (Pre-processing + Tool Layer)

**Context**: Balance flexibility with efficiency. LLM mode needs runtime tools; sandbox mode needs prompt-injected context.
**Decision**: Two-layer integration:
1. Pre-processing: Auto-build context before execution
2. Tool layer: 5 codegraph tools in tool-calling loop
**Consequences**: Sandbox gets direction from the start; LLM can deep-dive on demand; shared `CodegraphClient` implementation.

### ADR-2: Direct SQLite over CLI subprocess

**Context**: CLI subprocess overhead ~100-200ms per call; MCP needs stdio; direct SQLite is <1ms.
**Decision**: Python `CodegraphClient` reads `.codegraph/codegraph.db` via `sqlite3` module.
**Consequences**: Sub-ms queries; no subprocess overhead; must handle WAL locking and schema compatibility; must self-implement `explore` logic (no `buildContext` from codegraph CLI).

### ADR-3: FTS5 OR-query strategy

**Context**: Natural language queries need to match across multiple tokens.
**Decision**: `_sanitize_fts5` strips special characters, joins tokens with `OR` for broader matching.
**Consequences**: "process data" matches either "process" or "data"; may return more results but ensures recall.

---

## Good/Base/Bad Cases

### Good: Codegraph available, relevant symbols found
```python
client = CodegraphClient("/project")  # .codegraph/codegraph.db exists
results = client.search("execute_subtask")
# Returns: [{"name": "execute_subtask", "kind": "method", "file_path": "worker.py", ...}]
```

### Base: Codegraph unavailable, empty results
```python
client = CodegraphClient("/nonexistent")  # No .codegraph/ directory
client.is_available()  # False
client.search("anything")  # []
client.explore("query")  # ""
```

### Bad: Corrupted DB, graceful fallback
```python
# DB file exists but contains garbage
client = CodegraphClient("/project")
client.is_available()  # True (file exists)
client.search("test")  # [] (OperationalError caught internally)
```

---

## Tests Required

| Test | Class | Assertion Points |
|------|-------|------------------|
| `test_is_available_with_db` | `TestCodegraphClient` | `client.is_available() == True` |
| `test_is_available_without_db` | `TestCodegraphClient` | `client.is_available() == False` |
| `test_search_finds_symbols` | `TestCodegraphClient` | `len(results) >= 1`, `results[0]["name"]` matches |
| `test_search_with_kind_filter` | `TestCodegraphClient` | Results filtered by kind |
| `test_search_returns_empty_when_unavailable` | `TestCodegraphClient` | `results == []` |
| `test_callers_finds_call_sites` | `TestCodegraphClient` | Caller names include expected |
| `test_callees_finds_called_functions` | `TestCodegraphClient` | Callee names include expected |
| `test_impact_traverses_dependencies` | `TestCodegraphClient` | Affected names include expected |
| `test_explore_returns_markdown` | `TestCodegraphClient` | Contains `## Relevant Symbols`, `## Dependencies` |
| `test_search_on_corrupted_db` | `TestCodegraphClientDegradation` | `results == []` |
| `test_callers_on_missing_table` | `TestCodegraphClientDegradation` | `results == []` |
| `test_close_is_idempotent` | `TestCodegraphClientDegradation` | No exception on double close |
| `test_worker_has_codegraph_tools` | `TestWorkerCodegraphIntegration` | 5 tool names in `worker.tools` |
| `test_worker_has_codegraph_tool_definitions` | `TestWorkerCodegraphIntegration` | 5 tool names in definitions |
| `test_symbol_search_tool_when_unavailable` | `TestWorkerCodegraphIntegration` | JSON with `error` key |
| `test_prior_context_includes_codegraph` | `TestWorkerCodegraphIntegration` | "Code Knowledge Graph" in context |
| `test_prior_context_without_codegraph` | `TestWorkerCodegraphIntegration` | No codegraph section |
| `test_prior_context_codegraph_error_graceful` | `TestWorkerCodegraphIntegration` | No exception raised |
| `test_sandbox_prompt_includes_codegraph` | `TestWorkerCodegraphIntegration` | "Code Knowledge Graph" in prompt |
| `test_sandbox_prompt_without_codegraph` | `TestWorkerCodegraphIntegration` | Fallback message in prompt |

---

## Wrong vs Correct

### Wrong: Letting SQLite errors propagate to the Worker

```python
def search(self, query: str) -> list[dict]:
    conn = self._get_connection()
    rows = conn.execute(sql, params).fetchall()  # May raise OperationalError
    return [dict(row) for row in rows]
```

### Correct: Catch and degrade gracefully

```python
def search(self, query: str) -> list[dict]:
    conn = self._get_connection()
    if conn is None:
        return []
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        logger.debug("Codegraph search query failed: %s", query, exc_info=True)
        return []
```

**Why**: Worker must continue operating when codegraph is unavailable. Crashing on a query error would break the entire subtask execution pipeline.

---

## Common Mistakes

1. **Catching only `OperationalError`**: Corrupted DB files can raise other `DatabaseError` subtypes. Always catch `(sqlite3.OperationalError, sqlite3.DatabaseError)`.

2. **Returning `json.dumps({"error": ...})` from `explore_code` tool**: The `explore_code` tool declares it returns Markdown text. Returning JSON on error breaks the return-type contract for the LLM consumer. Return plain text error messages instead: `f"Codegraph explore error: {e}"`.

3. **Forgetting to sanitize FTS5 queries**: FTS5 has special operators (`AND`, `OR`, `NOT`, `*`, quotes) that cause syntax errors if passed raw from user input. Always use `_sanitize_fts5()`.

4. **Not checking `is_available()` before queries**: While all methods handle unavailable DB gracefully, checking `is_available()` first avoids unnecessary connection attempts and makes intent clearer.

5. **Excessive context length in `explore()`**: The 2000-char limit exists because this context gets injected into LLM prompts. Exceeding it wastes tokens and may truncate important information unevenly.
