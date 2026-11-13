# 63 — A pathless repo could be indexed once, then never again (T63)

**How it was found.** After the panic surveys (production `unwrap`/`expect`: `uc-grpc` 0,
`uc-grpc-server` 0, `uc-engine` 26 — the `task_store`/`graph_store` ones are sound because
the preceding branch proves the value exists), I surveyed `unwrap_or_default()` in production
code: 161 sites, mostly optional fields folding to empty. One of them **changes the meaning
of a stored fact**:

```rust
// full_index
let head_sha = self.get_head_sha(repo_spec).await.unwrap_or_default();
... update_index_state(&IndexState { last_indexed_sha: head_sha, health: Healthy, .. })
```

A failed HEAD read becomes a **valid-looking empty baseline** with `health: Healthy`.

**The consequence, which the type system happily allows.** `RepoSpec.local_path` is
optional, and `get_head_sha` requires it. So for a repo spec without a local git path:

1. First `index_repo` → full index → `get_head_sha` fails → `""` is stored → **Ok**.
2. Second `index_repo` → a state exists, so the dispatch picks **incremental** → whose first
   statement is `let head_sha = self.get_head_sha(repo_spec).await?;` → the same failure,
   now **propagated**: `IndexingError("local path required for HEAD SHA")`.

The same request succeeds once and fails forever after. For a git repo with a transient
HEAD failure it is milder but the same shape: the next call errors instead of re-indexing.

**Fix.** An empty baseline is not a baseline: `index_repo` only chooses the incremental
path when the stored `last_indexed_sha` is non-empty, and otherwise indexes in full — which
is exactly what the call that wrote that state did. This mirrors the guard the diff-empty
path already carries (`diffs.is_empty() && !existing_state.last_indexed_sha.is_empty()`).

**Test** (`crates/uc-engine/src/indexer/mod.rs`):
`a_repo_without_a_local_path_can_be_indexed_twice` — two consecutive `index_repo` calls on a
spec with `local_path: None` both return `Ok` with zero files. Before the fix the second
call fails with the HEAD error.

**Status:** implemented (`crates/uc-engine/src/indexer/mod.rs`)

**Boundary:** this does not make a pathless repo searchable (it has nothing to index) and
does not change what `full_index` stores — it stops a stored "unknown baseline" from being
read as "nothing changed". `cargo fmt` is clean locally; `check`/`clippy`/`test` are the
build evidence in Rust CI (this sandbox cannot reach crates.io).
