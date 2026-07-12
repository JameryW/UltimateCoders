# Remove Dead CODE_EMBEDDINGS_COLLECTION Const + Fix Doc

## Background

Auditing remaining `#[allow(dead_code)]` annotations found
`CODE_EMBEDDINGS_COLLECTION = "code_embeddings"` in
`crates/uc-engine/src/indexer/semantic.rs:18` — a const with zero
references. Code embeddings are NOT stored in a separate `code_embeddings`
Qdrant collection; the `SemanticIndexer` upserts them into the shared
`memory_embeddings` collection (managed by `LongTermMemory`,
`COLLECTION_NAME = "memory_embeddings"` in `memory/long_term.rs:29`) using
the key prefix `code_embedding:` to distinguish from memory embeddings.

The const is a dead planning residue from when a separate collection was
envisioned. The `search/semantic.rs:17` doc comment also references the
nonexistent `code_embeddings` collection — a documentation error.

## What I already know (verified this session)

- `indexer/semantic.rs:18` — `const CODE_EMBEDDINGS_COLLECTION: &str = "code_embeddings";`
  with `#[allow(dead_code)]` on the line above (line 17).
- `grep -rn "code_embeddings" crates/uc-engine/src/` → exactly 2 hits:
  the const def (indexer/semantic.rs:18) + the doc comment
  (search/semantic.rs:17). Zero code references to the const.
- Code embeddings actually flow: `SemanticIndexer::upsert` → `LongTermMemory`
  (key prefix `code_embedding:src/main.rs:10:20` etc., see
  indexer/semantic.rs:447,456,698,701) → Qdrant `memory_embeddings`
  collection.
- `CODE_EMBEDDINGS_COLLECTION` is not `pub` — no external API surface.

## The gap

A dead const + a doc comment describing a collection that doesn't exist.
Misleads readers into thinking code embeddings live in a separate
`code_embeddings` collection.

## Decisions (locked)

- **D1**: Delete `const CODE_EMBEDDINGS_COLLECTION: &str = "code_embeddings";`
  + its `#[allow(dead_code)]` line + the preceding doc comment
  `/// Qdrant collection name for code embeddings.` (lines 16-18 of
  `indexer/semantic.rs`).
- **D2**: Fix `search/semantic.rs:17` doc comment — replace
  `/// the \`code_embeddings\` collection in Qdrant (or the fallback store).`
  with an accurate description: code embeddings live in the shared
  `memory_embeddings` collection (key prefix `code_embedding:`) or the
  fallback store.
- **Out of scope**: `FallbackCodeEmbedding.chunk_type`/`content_hash` fields
  (also `#[allow(dead_code)]`, but they are stored fields on a fallback
  struct that may serve future search filters — judgment call, leave for
  separate evaluation); `rand_jitter_ms` (live, feature-gated); the
  `memory_embeddings` collection machinery (live).

## Acceptance Criteria

- [ ] `grep -rn "code_embeddings\|CODE_EMBEDDINGS_COLLECTION" crates/` → 0 hits.
- [ ] `search/semantic.rs:17` doc comment accurately describes the shared
      collection + key-prefix scheme (no mention of a `code_embeddings`
      collection).
- [ ] `cargo check -p uc-engine` green; `cargo test -p uc-engine` green.

## Technical Approach

1. `indexer/semantic.rs`: delete the 3 lines (doc comment +
   `#[allow(dead_code)]` + const def) at lines 16-18.
2. `search/semantic.rs`: rewrite line 17 doc comment to describe the actual
   storage (shared `memory_embeddings` collection via `code_embedding:` key
   prefix, or in-memory fallback).
3. Verify: `cargo check -p uc-engine`, `cargo test -p uc-engine`,
   `grep -rn "code_embeddings" crates/` → 0.

## Risk

- **None**: const has zero references, is private, no `Drop`/side-effect.
  Doc comment change is cosmetic. No behavior change.
