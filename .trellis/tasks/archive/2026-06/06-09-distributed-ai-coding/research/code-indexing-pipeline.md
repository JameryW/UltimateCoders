# Research: Multi-Repo Code Indexing Pipeline

- **Query**: Best practices for building a multi-repo code indexing pipeline that supports text search, semantic search (vector), and AST-level queries
- **Scope**: Mixed (internal project context + external tool/system architecture)
- **Date**: 2026-06-09

## Findings

---

### 1. How Sourcegraph, GitHub Code Search, and Elasticsearch Structure Their Indexing Pipelines

#### 1.1 Sourcegraph / Zoekt Architecture

Sourcegraph uses **Zoekt** (Google's open-source trigram-based code search engine) as its primary text search backend.

**Indexing Pipeline Structure:**
- **Zoekt-indexindexer** reads git repositories and builds an inverted index based on trigram tokenization
- Each repository is indexed independently into a **shard** (one `.zoekt` file per repo or per N files)
- Shards are immutable once written; updates create new shards, old ones are replaced atomically
- **Sourcegraph indexer** runs as a separate service that clones repos, runs zoekt-indexer, and uploads shards to search nodes
- Search nodes load shards into memory for fast serving

**Key Design Decisions:**
- Trigram indexing: every 3-character substring of a file becomes a token. This enables substring/regex search without full-text inverted index complexity
- Shards are repo-bounded: each shard contains files from a single repository, enabling per-repo reindexing without cross-repo coordination
- **Ranking**: Sourcegraph uses a separate ranking service that computes file importance (based on commit frequency, recency, documentation references) to boost relevant results
- **Symbol search**: Zoekt has limited symbol awareness (extracts from ctags/universal-ctags). Sourcegraph augments this with precise language intelligence via LSP (Sourcegraph's "code intelligence" layer)

**Relevance to our system:**
- The shard-per-repo pattern maps well to our multi-repo use case
- Trigram approach is simple and effective for exact text/regex search
- Sourcegraph's decoupled architecture (indexer separate from searcher) is a good pattern for our Rust indexing engine

#### 1.2 GitHub Code Search (Blackbird)

GitHub's Blackbird engine powers GitHub's code search across 200M+ repositories.

**Indexing Pipeline Structure:**
- **Blackbird** is a custom-built search engine written in Rust (publicly disclosed at GitHub Universe 2022-2023)
- Uses a **ngram-based inverted index** (similar concept to Zoekt but at massive scale)
- Pipeline stages:
  1. **Git clone/fetch**: repositories are cloned and incrementally updated
  2. **Diff computation**: only changed files between commits are re-indexed (incremental)
  3. **Tokenization**: files are tokenized using language-aware analyzers (identifiers split on camelCase/snake_case boundaries)
  4. **Index building**: inverted index segments built per shard, then merged
  5. **Distribution**: index segments replicated across search nodes

**Key Design Decisions:**
- **Language-aware tokenization**: identifiers like `getUserById` are split into `get`, `user`, `by`, `id` for better substring matching. This is more sophisticated than raw trigram indexing
- **Incremental indexing via git diffs**: only re-index files that changed between commits, not the entire repo
- **Segment merging** (similar to Lucene): small index segments from frequent updates are periodically merged into larger segments for query efficiency
- **Separate path/content indexes**: file paths are indexed separately from file content for efficient path-based filtering
- GitHub uses a custom columnar format for storage, not off-the-shelf Lucene/Elasticsearch

**Relevance to our system:**
- Language-aware tokenization is a clear improvement over raw trigram indexing and should be adopted
- The incremental-via-diff pattern is essential for multi-repo at scale
- The segment-merge approach balances write throughput with query performance

#### 1.3 Elasticsearch Code Search

Elasticsearch provides code search through custom analyzers and the `code` field type (available in newer versions and via plugins).

**Indexing Pipeline Structure:**
- Uses standard Elasticsearch indexing pipeline with **custom analyzers**
- Pipeline stages:
  1. **File ingestion**: files extracted from git repos and pushed as documents
  2. **Custom analyzer chain**:
     - `path_hierarchy` tokenizer for file paths (enables searching `src/main.rs` by matching `src` or `main`)
     - `pattern` or `simple_pattern_split` tokenizer for code identifiers (splits on punctuation)
     - `word_delimiter` filter to split camelCase and snake_case
     - `ngram` or `edge_ngram` token filter for substring matching
     - `lowercase` normalization
  3. **Multi-field mapping**: same content indexed multiple ways (exact, ngram, path-analyzed) for different query types
  4. **Per-shard inverted index** (standard Lucene)

**Key Design Decisions:**
- **Multi-field indexing**: same source text indexed under different analyzers for different query modes. E.g., `content.exact` (keyword), `content.text` (analyzed), `content.ngram` (substring)
- **Routing by repository**: documents routed to the same shard by repo ID, so single-repo queries only hit one shard
- **Index lifecycle management**: ILM policies to roll over indices when they get too large
- **Percolator queries**: can pre-register query patterns and match against new documents (useful for alerting on code patterns)

**Relevance to our system:**
- Multi-field indexing pattern is directly applicable to our Rust indexing engine
- Repository-based routing/sharding is essential for multi-repo isolation
- We would NOT use Elasticsearch itself (since we're building a Rust-native engine), but the analyzer patterns are instructive

#### 1.4 Architectural Comparison

| Aspect | Sourcegraph/Zoekt | GitHub Blackbird | Elasticsearch Code |
|--------|-------------------|------------------|-------------------|
| Index method | Trigram inverted index | Ngram + language-aware tokenizer | Custom analyzers + ngram |
| Incremental | Shard replacement (full repo) | Diff-based incremental | Document-level upsert |
| Scale | ~1M repos | ~200M repos | Variable (shard-based) |
| Symbol/Awareness | ctags + LSP overlay | Language-aware tokenizer | None built-in |
| Storage format | Custom (.zoekt) | Custom columnar | Lucene segments |
| Written in | Go | Rust | Java (Lucene) |

---

### 2. Incremental vs Full Reindexing of Git Repositories

#### 2.1 Full Reindex

Full reindex clones/reads the entire repository and rebuilds the index from scratch.

**When to use:**
- Initial indexing of a new repository
- Index corruption or schema migration (analyzer/tokenizer changes)
- Periodic consistency checks (e.g., weekly full reindex to catch drift)
- When the index format changes (upgrades)

**Approach:**
1. Clone repository (shallow clone with `--depth=1` for latest, or full history)
2. Walk all files in the working tree
3. Tokenize and index each file
4. Build complete shard/index
5. Atomically swap old index with new index (prevent serving stale data)

**Cost:** O(repository size). For large monorepos this can take hours.

#### 2.2 Incremental Reindex

Incremental reindex only processes files that have changed since the last index.

**Git-diff based approach (recommended):**
1. Store the last-indexed commit SHA per repository (in PostgreSQL for our system)
2. On reindex trigger: `git fetch origin`, then `git diff <last-indexed-sha>..<latest-sha> --name-status`
3. Process only added/modified/removed files:
   - **Added/Modified**: tokenize and upsert into index
   - **Removed**: delete from index
4. Update the last-indexed commit SHA
5. Handle force-push: if `git merge-base` fails between old and new SHA, fall back to full reindex

**Tree-walk diff approach (alternative):**
1. Store a content hash (e.g., BLAKE3) per file path per repo
2. Walk current working tree, compare hashes against stored state
3. Only process files whose hash changed

**Key challenges:**
- **Force pushes / history rewrites**: old SHA may no longer be an ancestor of new SHA. Detection: `git merge-base --is-ancestor <old> <new>` returns non-zero. Solution: fall back to full reindex for that repo
- **File renames**: `git diff` with `--diff-filter=R` captures renames. Must delete old path and index new path
- **Large monorepos**: even a diff can be large. Consider path-prefix sharding to parallelize
- **Index consistency**: if indexing crashes mid-way, the last-indexed SHA should NOT be updated. Use transactional semantics: index first, then commit SHA

#### 2.3 Recommended Strategy for Our System

**Hybrid approach:**
- **Default: incremental** via git diff against last-indexed commit
- **Fallback: full reindex** when:
  - New repo added
  - Force push detected
  - Index schema migration
  - Consistency check fails (periodic audit)
- **Consistency audit**: periodically (configurable, e.g., daily) select random repos and verify file count + total hash matches index. If mismatch, trigger full reindex

**Implementation in our Rust engine:**
```
struct IndexState {
    repo_id: RepoId,
    last_indexed_sha: String,       // stored in PostgreSQL
    last_full_reindex: DateTime,    // for scheduling periodic full reindex
    index_version: u32,             // schema version for migration detection
}
```

**Storage of index state:**
- `last_indexed_sha` per repo in PostgreSQL (durable, queryable)
- Index files on local disk (or object storage), one shard per repo
- Qdrant collections for vector embeddings (point IDs include repo+path hash for upsert)

---

### 3. Multi-Language AST Parsing at Scale

#### 3.1 Tree-Sitter (Primary Approach)

Tree-sitter is the de facto standard for incremental, multi-language AST parsing. It is used by Neovim, Helix, Zed, GitHub, and many other tools.

**Key properties:**
- **Incremental parsing**: can re-parse only the changed portion of a file when edits occur, in O(edit size) time
- **Error recovery**: produces partial ASTs even for syntactically invalid code (essential for in-progress code)
- **Wide language support**: 60+ grammars available (TypeScript, Python, Rust, Go, Java, C/C++, Ruby, etc.)
- **C library with bindings**: Rust bindings via `tree-sitter` crate, Python bindings via `py-tree-sitter`
- **Fast**: typically parses files in microseconds to milliseconds

**Architecture for our system:**
1. **Grammar registry**: maintain a mapping from file extension to tree-sitter grammar. E.g., `.rs` -> `tree-sitter-rust`, `.py` -> `tree-sitter-python`
2. **Parse pipeline**:
   - Read file content
   - Select grammar based on file extension
   - Parse into AST (tree-sitter `Tree` object)
   - Walk AST to extract structural information:
     - **Symbol definitions**: function names, class names, type definitions, variable declarations
     - **Symbol references**: call sites, imports, type usages
     - **Structure**: nesting hierarchy, scope boundaries
   - Store extracted symbols in PostgreSQL (structured metadata)
   - Store AST-derived chunks in Qdrant (for structural semantic search)
3. **Incremental parsing**: when a file changes, tree-sitter can re-parse using the old tree, only recomputing changed nodes. However, for indexing pipeline, re-parsing the whole file is usually fast enough and simpler.

**Rust ecosystem:**
- `tree-sitter` crate: core parsing library
- `tree-sitter-languages` or individual grammar crates: `tree-sitter-rust`, `tree-sitter-python`, etc.
- Each grammar must be compiled at build time (tree-sitter generates C code from grammar JS files)

**Scaling considerations:**
- Parse files in parallel using rayon or tokio task spawning
- Batch parsing: parse all files of the same language together (avoid grammar loading overhead)
- Cache parsed ASTs on disk for files that haven't changed (BLAKE3 hash as cache key)
- For very large files (>1MB), consider truncating or using a streaming approach

#### 3.2 LSP (Language Server Protocol) Integration

LSP provides precise, type-aware code intelligence but has higher operational cost.

**When to use LSP over tree-sitter:**
- Need type information (e.g., "find all implementations of interface X")
- Need resolved imports (e.g., what does `foo` in `from foo import bar` resolve to?)
- Need cross-file references (e.g., "find all callers of function Y")

**Challenges with LSP at scale:**
- Each language server is a long-running process (memory overhead)
- Language servers need project context (package.json, Cargo.toml, etc.) to resolve imports
- Initial indexing is slow (language servers must build their own internal index)
- Not all languages have good language servers

**Recommended approach for our system:**
- **Phase 1 (MVP)**: tree-sitter only for AST parsing. Extract symbol definitions and syntactic references. Store in PostgreSQL for structural queries.
- **Phase 2**: add LSP integration for precise references. Run language servers per-repo on demand (not permanently). Use LSP's `textDocument/references`, `textDocument/definition`, `textDocument/hover` for enriched data.
- **Architecture**: LSP manager service that spawns language servers as Docker containers per repo, collects data, and shuts them down. This isolates language server crashes and manages resources.

**LSP server inventory for common languages:**
| Language | LSP Server | Notes |
|----------|-----------|-------|
| Rust | rust-analyzer | Excellent, type-aware |
| Python | pyright / pylsp | pyright is faster, pylsp is more extensible |
| TypeScript | typescript-language-server | Good, requires node_modules |
| Go | gopls | Good, built-in by Go team |
| Java | jdtls (Eclipse) | Heavy, JVM required |
| C/C++ | clangd | Good, requires compile_commands.json |

#### 3.3 AST-Based Query Patterns

With tree-sitter-extracted data stored in PostgreSQL, we can support:

1. **Symbol search**: `SELECT * FROM symbols WHERE name = 'my_function' AND repo_id = X`
2. **Call chain queries**: `SELECT * FROM references WHERE target_symbol = 'my_function'` (who calls this?)
3. **Definition queries**: `SELECT * FROM symbols WHERE kind = 'function' AND name ILIKE '%handler%'`
4. **Import graph**: `SELECT * FROM imports WHERE source_repo = X` (what does this repo depend on?)
5. **Structural search**: "find all try-catch blocks that catch Exception" -- this requires walking the AST and matching patterns, stored as serialized node paths

**PostgreSQL schema sketch:**
```sql
CREATE TABLE symbols (
    id BIGSERIAL PRIMARY KEY,
    repo_id UUID NOT NULL REFERENCES repos(id),
    file_path TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,  -- 'function', 'class', 'method', 'variable', 'type', 'constant', 'import'
    start_line INT NOT NULL,
    start_col INT NOT NULL,
    end_line INT NOT NULL,
    end_col INT NOT NULL,
    parent_symbol_id BIGINT REFERENCES symbols(id),  -- nesting
    language TEXT NOT NULL,
    content_hash TEXT NOT NULL,  -- BLAKE3 of the file content at index time
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE references (
    id BIGSERIAL PRIMARY KEY,
    repo_id UUID NOT NULL REFERENCES repos(id),
    file_path TEXT NOT NULL,
    source_symbol_id BIGINT REFERENCES symbols(id),  -- the symbol containing the reference
    target_name TEXT NOT NULL,  -- name of the referenced symbol (may need LSP to resolve fully)
    reference_kind TEXT NOT NULL,  -- 'call', 'import', 'type_usage', 'inheritance'
    start_line INT NOT NULL,
    start_col INT NOT NULL,
    language TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

---

### 4. Embedding Strategies for Code

#### 4.1 Embedding Models Comparison

| Model | Developer | Dimensions | Context Window | Training Data | Strengths | Weaknesses |
|-------|-----------|-----------|----------------|---------------|-----------|------------|
| **Voyage Code 3** | Voyage AI | 1024 | 32K tokens | Code-specific | Best-in-class for code retrieval (MTEB Code), long context, handles identifiers well | Proprietary, API-dependent |
| **CodeBERT** | Microsoft | 768 | 512 tokens | Code + NL (6 languages) | Open-source, well-studied baseline | Short context, older model, limited languages |
| **UniXcoder** | Microsoft | 768 | 1024 tokens | Code + NL (multiple) | Better than CodeBERT, supports generation too | Still limited context |
| **StarCoder2 (embeddings)** | BigCode | 2560+ | 16K tokens | The Stack v2 (649 languages) | Massive multilingual coverage, open weights | Large model, slower inference, no official embedding mode (requires mean pooling or [CLS] extraction) |
| **Nomic Embed Text** | Nomic | 768 | 8K tokens | Text + code | Open weights, good quality per dimension, efficient | Not code-specialized |
| **Jina Code v2** | Jina AI | 1024 | 8K tokens | Code-specific | Good code search, open weights | Newer, less battle-tested |
| **GTE-Code** | Alibaba | 1024 | 8K tokens | Code + NL | Strong benchmark scores, open weights | Primarily Chinese + English documentation |

**Recommendation for our system:**
- **Phase 1 (MVP)**: Voyage Code 3 via API (best quality, fast to integrate, no GPU needed)
- **Phase 2**: Self-hosted model for cost reduction. Options:
  - UniXcoder (lightweight, good quality, runs on CPU with ONNX)
  - StarCoder2 with mean pooling (highest multilingual coverage, needs GPU)
  - Fine-tuned model on project-specific code (if enough data)

#### 4.2 Chunking Strategies for Code

Code chunking is fundamentally different from document chunking because code has structural semantics.

**Strategy 1: Naive fixed-size chunking (AVOID for code)**
- Split by character/token count with overlap
- Breaks function definitions, class structures, logical units
- Results in poor retrieval quality

**Strategy 2: AST-aware chunking (RECOMMENDED)**
- Use tree-sitter to parse the file into an AST
- Chunk at AST node boundaries:
  - Each top-level function/method is one chunk
  - Each class definition (without method bodies) is one chunk
  - Each nested class/trait/impl block is one chunk
  - Imports/ module-level statements grouped as one chunk
- Include parent context in metadata (class name for methods, module path)
- **Maximum chunk size**: if a single function exceeds ~512 tokens, split at nested block boundaries (if/for/while blocks). Include function signature in each sub-chunk.
- **Minimum chunk size**: merge very small adjacent items (e.g., single-line functions) to avoid overly granular chunks

**Strategy 3: Hierarchical chunking (BEST for multi-granularity retrieval)**
- Index at multiple granularities:
  - **File level**: full file embedding (for broad relevance matching)
  - **Function/class level**: individual symbol embeddings (for precise matching)
  - **Block level**: code blocks within functions (for very specific queries)
- Store hierarchy in metadata: `file_id -> symbol_id -> block_id`
- At query time, first retrieve at file level, then drill down to symbol level

**Strategy 4: Sliding window with AST-aware boundaries**
- Use a sliding window of N tokens, but snap window boundaries to the nearest AST node boundary
- Overlap by ~25% for context continuity
- Good balance between simplicity and quality

**Implementation recommendations for our Rust engine:**
```rust
struct CodeChunk {
    id: ChunkId,              // BLAKE3(repo_id + file_path + start_line)
    repo_id: RepoId,
    file_path: String,
    start_line: u32,
    end_line: u32,
    content: String,          // the actual code text
    language: String,
    symbol_name: Option<String>,  // name of the containing function/class
    symbol_kind: Option<String>,  // "function", "class", "method", etc.
    parent_symbol: Option<String>,// enclosing class for methods
    chunk_type: ChunkType,    // File, Symbol, Block
}
```

**Chunking pipeline:**
1. Parse file with tree-sitter
2. Walk AST, extract symbol boundaries
3. For each symbol: create a chunk (type=Symbol)
4. For oversized symbols: split into sub-chunks (type=Block), each inheriting parent metadata
5. Create file-level chunk (type=File) with full content (or summary for very large files)
6. Compute embedding for each chunk
7. Upsert into Qdrant with metadata payload

#### 4.3 Storing Embeddings in Qdrant

**Qdrant collection design:**

```rust
// Collection: "code_embeddings"
// Vector size: 1024 (Voyage Code 3) or 768 (UniXcoder)
// Distance: Cosine (standard for semantic search)

struct QdrantPoint {
    id: String,           // BLAKE3 hash of (repo_id + file_path + start_line)
    vector: Vec<f32>,     // embedding vector
    payload: {
        repo_id: String,
        file_path: String,
        start_line: u32,
        end_line: u32,
        language: String,
        symbol_name: Option<String>,
        symbol_kind: Option<String>,
        parent_symbol: Option<String>,
        chunk_type: String,    // "file", "symbol", "block"
        content_hash: String,  // for incremental update detection
        indexed_at: String,    // ISO 8601 timestamp
    }
}
```

**Qdrant features to leverage:**
- **Payload indexing**: create payload indexes on `repo_id`, `language`, `chunk_type`, `symbol_kind` for filtered search
- **Batch upsert**: use Qdrant's batch API for bulk indexing (faster than point-by-point)
- **Optimizers config**: tune `indexing_threshold` (number of points before creating HNSW index), `memmap_threshold` for large collections
- **Scalar quantization**: enable `int8` quantization to reduce memory usage by 4x with minimal quality loss
- **Sharding**: use Qdrant's auto-sharding or custom sharding by repo_id for multi-tenant isolation
- **Snapshot**: use Qdrant snapshots for backup and disaster recovery

**Upsert strategy for incremental updates:**
1. When a file changes, compute new chunks + embeddings
2. Query Qdrant by `repo_id + file_path` to get existing point IDs
3. Delete old points for that file
4. Insert new points
5. This is an "upsert-by-delete-then-insert" pattern (Qdrant supports native upsert by point ID, but our IDs include line numbers which change)

**Alternative: use deterministic point IDs**
- Point ID = BLAKE3(repo_id + file_path + chunk_index_within_file)
- This enables native Qdrant upsert without delete-then-insert
- But chunk indices can shift when lines are added/removed
- Better: Point ID = BLAKE3(repo_id + file_path + symbol_name_or_line_range)
- This is stable across edits that don't affect the specific chunk

**Query patterns:**
1. **Semantic search**: vector similarity search with optional filters (`language`, `repo_id`, `chunk_type`)
2. **Hybrid search**: combine vector search with keyword filter (Qdrant supports `prefetch` + `must` filter)
3. **Multi-granularity**: search `chunk_type=Symbol` for precise results, fall back to `chunk_type=File` for broader context

---

### 5. Keeping the Index Consistent When Code Changes

#### 5.1 Webhook-Based Updates (Primary for Remote Repos)

**GitHub/GitLab webhooks:**
- Register a webhook on each monitored repository for `push` events
- On push: receive payload with `before` and `after` commit SHAs, plus list of modified files
- Trigger incremental reindex for the affected repository

**Implementation:**
1. **Webhook receiver service** (Rust, part of the indexing engine):
   - Expose HTTP endpoint for GitHub/GitLab webhook payloads
   - Validate webhook signature (HMAC-SHA256)
   - Extract repo ID and commit range
   - Push indexing job to NATS JetStream queue
2. **Indexing worker**:
   - Consumes jobs from NATS
   - Fetches the repo (git fetch)
   - Computes diff between old and new SHAs
   - Updates text index, AST index, and vector index incrementally
   - Updates `last_indexed_sha` in PostgreSQL

**Pros:** Real-time, minimal latency, efficient (only processes actual changes)
**Cons:** Requires public endpoint, webhook management overhead, can miss events if receiver is down

**Resilience measures:**
- **Dead letter queue**: failed indexing jobs go to DLQ for manual retry
- **Reconciliation loop**: periodically compare webhook-triggered updates against PostgreSQL state to find missed updates
- **Idempotent indexing**: same commit can be indexed multiple times safely (upsert semantics)

#### 5.2 Polling-Based Updates (Fallback / For Repos Without Webhooks)

**Implementation:**
1. **Scheduler service** (Rust):
   - Maintain a list of monitored repositories with their `last_indexed_sha` and `poll_interval`
   - For each repo, periodically: `git ls-remote origin HEAD` to get latest SHA
   - If SHA differs from `last_indexed_sha`, trigger incremental reindex
2. **Poll intervals:**
   - Active repos: every 1-5 minutes
   - Inactive repos: every 15-60 minutes
   - Adaptive: increase poll interval for repos that rarely change, decrease for frequently updated ones

**Pros:** Simple, works without public endpoint, works with any git remote
**Cons:** Higher latency (minutes instead of seconds), wasted API calls for unchanged repos

**Optimization:**
- Use GitHub's `ETag` / `If-Modified-Since` headers for conditional requests
- Batch remote checks: `git ls-remote` for multiple repos in parallel

#### 5.3 Git Hook-Based Updates (For Local/Developer Repos)

**Server-side hooks (for self-hosted git servers):**
- `post-receive` hook on the git server triggers indexing after every push
- Directly calls the indexing API with the old and new SHAs

**Client-side hooks (for developer workflow):**
- `post-commit` hook triggers local indexing after each commit
- Less reliable (developers can skip hooks with `--no-verify`)
- Useful for local-first indexing before pushing to remote

**Implementation:**
```bash
#!/bin/bash
# .git/hooks/post-receive
while read oldrev newrev refname; do
    curl -X POST http://indexer:8080/api/v1/index/repo \
        -H "Content-Type: application/json" \
        -d "{\"repo_id\": \"$(config repo.id)\", \"old_sha\": \"$oldrev\", \"new_sha\": \"$newrev\"}"
done
```

#### 5.4 Recommended Strategy for Our System

**Multi-layered approach:**

| Layer | Trigger | Latency | Use Case |
|-------|---------|---------|----------|
| 1. Webhook | Push event | Seconds | Primary for GitHub/GitLab repos |
| 2. Polling | Scheduled | Minutes | Fallback, repos without webhooks |
| 3. Git hook | post-receive | Seconds | Self-hosted git servers |
| 4. Manual | API call | On-demand | Force reindex, schema migration |
| 5. Reconciliation | Periodic (hourly) | Best-effort | Catch missed events |

**Consistency model:**
- **Eventual consistency**: the index may be slightly behind the actual code state, but will converge
- **Consistency check**: weekly full-reindex audit for randomly sampled repos
- **Content-hash verification**: each indexed chunk stores a `content_hash`. On read, optionally verify against actual file content. If mismatch, flag for reindex.

**Conflict handling for concurrent indexing:**
- Use PostgreSQL advisory locks or `SELECT FOR UPDATE` on the repo row to prevent two workers from indexing the same repo simultaneously
- NATS JetStream with `max_deliver=1` and `ack_wait` ensures each job is processed exactly once
- If a worker crashes mid-index, the job times out and is redelivered to another worker (NATS redelivery)

---

### 6. Integration Architecture for Our System

Based on the project's PRD (Rust core + Python agent layer, TiKV + Qdrant + PostgreSQL):

```
                    +-----------------+
                    |  Git Repos      |
                    | (GitHub/GitLab) |
                    +--------+--------+
                             |
                    webhook / poll / hook
                             |
                    +--------v--------+
                    | Index Scheduler  |  (Rust service)
                    | - Job queue mgmt |
                    | - Deduplication  |
                    | - Priority       |
                    +--------+--------+
                             |
                    NATS JetStream
                             |
              +--------------+--------------+
              |                             |
    +---------v----------+       +----------v---------+
    | Text Indexer       |       | AST Indexer        |
    | - Trigram/ngram    |       | - tree-sitter      |
    | - Language-aware   |       | - Symbol extraction |
    |   tokenization     |       | - Reference graph   |
    +--------+-----------+       +----------+---------+
             |                              |
    +--------v-----------+       +----------v---------+
    | Text Index Store   |       | PostgreSQL         |
    | (Custom Rust       |       | - symbols table    |
    |  shard format or   |       | - references table |
    |  TiKV)             |       | - index metadata   |
    +--------------------+       +--------------------+

              +--------------+--------------+
              |                             |
    +---------v----------+       +----------v---------+
    | Embedding Indexer  |       | LSP Indexer        |
    | - Chunk (AST-aware)|       | (Phase 2)          |
    | - Embed (API/local)|       | - Type resolution  |
    | - Upsert to Qdrant |       | - Cross-file refs  |
    +--------+-----------+       +----------+---------+
             |                              |
    +--------v-----------+       +----------v---------+
    | Qdrant             |       | PostgreSQL         |
    | - code_embeddings  |       | - resolved_refs    |
    | - Payload indexes  |       |                    |
    +--------------------+       +--------------------+
```

**Query serving (Rust gRPC service):**
```
SearchRequest {
    query: String,           // natural language or code pattern
    repo_ids: Vec<RepoId>,   // scope filter
    languages: Vec<String>,  // language filter
    search_type: SearchType, // Text, Semantic, AST, Hybrid
    max_results: u32,
}

SearchResponse {
    results: Vec<SearchResult>,  // ranked by relevance
    each: {
        repo_id, file_path, start_line, end_line,
        content_snippet, match_type, score,
        symbol_name, symbol_kind  // if AST match
    }
}
```

---

### External References

- [Sourcegraph Zoekt](https://github.com/sourcegraph/zoekt) -- trigram-based code search engine, Go
- [GitHub Blackbird announcement](https://github.blog/2023-02-06-github-code-search-is-generally-available/) -- GitHub's Rust-based code search architecture overview
- [Tree-sitter](https://tree-sitter.github.io/tree-sitter/) -- incremental parsing system with 60+ language grammars
- [Qdrant documentation](https://qdrant.tech/documentation/) -- vector database, Rust-native
- [Voyage Code 3](https://docs.voyageai.com/docs/embeddings) -- code embedding model, 1024 dims, 32K context
- [UniXcoder](https://arxiv.org/abs/2203.03850) -- unified cross-modal pre-training for code
- [CodeBERT](https://arxiv.org/abs/2002.08155) -- pre-trained model for programming and natural languages
- [StarCoder2](https://arxiv.org/abs/2402.19173) -- open code LLM with 649 language support
- [Elasticsearch Code Search analyzers](https://www.elastic.co/guide/en/elasticsearch/reference/current/analysis-code-search.html) -- custom analyzer patterns for code
- [Git diff-based indexing pattern](https://sourcegraph.com/blog/code-search-intelligence) -- Sourcegraph blog on incremental indexing

### Related Specs

- `.trellis/spec/backend/database-guidelines.md` -- to be filled with PostgreSQL schema conventions
- `.trellis/spec/backend/directory-structure.md` -- to be filled with module organization

## Caveats / Not Found

- GitHub Blackbird's internal architecture details are partially proprietary; the research draws from public blog posts and conference talks, not source code
- Specific benchmark comparisons between embedding models on code retrieval tasks were not directly fetched; rankings are based on published MTEB/CoSQA benchmarks as of early 2026
- LSP-at-scale operational patterns are less well-documented; the Phase 2 recommendations are based on general LSP knowledge rather than a specific production system's approach
- TiKV-specific index storage patterns (storing trigram/ngram inverted indexes in TiKV) need further research; the current document assumes either custom file-based shards or TiKV KV pairs, but the optimal encoding requires prototyping
