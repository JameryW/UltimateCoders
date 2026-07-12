# Remove Dead FallbackCodeEmbedding chunk_type/content_hash Fields

## Background

`FallbackCodeEmbedding` struct (indexer/semantic.rs:389) stores code
embeddings in-memory when Qdrant is unavailable. Two fields are marked
`#[allow(dead_code)]`:
- `chunk_type: String` (line 404)
- `content_hash: String` (line 407)

Both are written at construction (lines 492-493) but never read anywhere.
The fallback search path (search() at :503, fallback iteration at :602-636)
reads `repo_id`, `file_path`, `start_line`, `end_line`, `vector`, `content`,
`symbol_name`, `symbol_kind`, `parent_symbol` — never `chunk_type` or
`content_hash`. `remove_file`/`remove_repo`/tests read only `repo_id`/`file_path`.

The prior PR (#242) evaluated these as "design judgment — may serve future
search filters." On re-verification, the fallback search is fully
implemented (filters by repo_id + language, maps to SearchResultItem) and
does NOT use chunk_type/content_hash. They are genuinely dead — YAGNI applies.

## What I already know (verified this session)

- `indexer/semantic.rs:404-407` — field defs with `#[allow(dead_code)]`:
  ```rust
  #[allow(dead_code)]
  chunk_type: String,
  #[allow(dead_code)]
  content_hash: String,
  ```
- Write sites: `:492` `chunk_type: format_chunk_type(&chunk.chunk_type).to_string(),`
  and `:493` `content_hash: chunk.content_hash.clone(),` (in the FallbackCodeEmbedding
  push at :477).
- Read sites: grep `\.chunk_type\|\.content_hash` on `FallbackCodeEmbedding`
  instances → the only `.chunk_type` reads are on `CodeChunk` / `chunks[]`
  (different type, :458/:1168/:1174/:1177) and `format_chunk_type(&chunk.chunk_type)`
  reads the source `CodeChunk`, not the stored `FallbackCodeEmbedding`.
  Zero reads of `f.chunk_type` / `f.content_hash` / `entry.chunk_type` etc.
- The fallback search (`:602` read) + map (`:627-636`) + `remove_file` (`:660`)
  + `remove_repo` (`:667`) + tests (`:1029/:1076/:1112`) all read
  `repo_id`/`file_path` only.

## The gap

Two stored-but-unread fields on the fallback struct. Dead → delete.

## Decisions (locked)

- **D1**: Remove `chunk_type: String` field + its `#[allow(dead_code)]` line
  from `FallbackCodeEmbedding` (lines 404-405).
- **D2**: Remove `content_hash: String` field + its `#[allow(dead_code)]` line
  (lines 406-407).
- **D3**: Remove the two constructor writes at `:492-493`
  (`chunk_type: format_chunk_type(&chunk.chunk_type).to_string(),` and
  `content_hash: chunk.content_hash.clone(),`).
- **Out of scope**: other `FallbackCodeEmbedding` fields (all live, read by
  search); `format_chunk_type` fn (still used at :458 for the LTM key);
  the `CodeChunk.chunk_type`/`content_hash` source fields (live).

## Acceptance Criteria

- [ ] `FallbackCodeEmbedding` has no `chunk_type`/`content_hash` fields.
- [ ] `grep -n "chunk_type\|content_hash" crates/uc-engine/src/indexer/semantic.rs`
      → only `format_chunk_type` fn + `CodeChunk` source reads remain (no
      `FallbackCodeEmbedding` field).
- [ ] `cargo check -p uc-engine` green; `cargo test -p uc-engine` green.

## Technical Approach

1. `indexer/semantic.rs`: delete the 4 lines (2 fields + 2 `#[allow(dead_code)]`)
   from `FallbackCodeEmbedding` struct (~404-407).
2. Delete the 2 constructor lines at `:492-493`.
3. Verify: `cargo check -p uc-engine`, `cargo test -p uc-engine`.
4. Confirm `format_chunk_type` is still used (at :458 for LTM key) — keep it.

## Risk

- **Low**: fields never read, struct is private (not pub), no `Drop`. The
  `format_chunk_type(&chunk.chunk_type)` call at :458 stays (reads source
  CodeChunk for the LTM key, not the deleted FallbackCodeEmbedding field).
  Deletion cannot change behavior.
