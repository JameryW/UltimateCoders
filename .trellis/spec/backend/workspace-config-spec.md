# Workspace Config Spec (`uc.repos.yaml`)

> Executable contract for the unified "working directory" config — a collection of
> repos grouped under a `workspace_id`, loaded by **both** the Rust gateway and the
> Python worker on startup. Two implementations must stay semantically aligned:
> `crates/uc-engine/src/repos_config.rs` (Rust) and `python/ultimate_coders/repo_config.py` (Python).

---

## 1. Scope / Trigger

- Trigger: A process boots that owns an `Engine` (Rust `uc-grpc-server` binary, or Python `nats_worker`) and must populate it with the configured workspace's repos before serving.
- Cross-layer: `uc.repos.yaml` (disk) → Rust `LocalEngine::index_repo` / Python `Engine.index_repo` → metadata store (`repos` table, `workspace_id` column) → `list_repos` / `SearchQuery.in_workspace`.
- Requires code-spec depth because the loading responsibility is **split across two implementations** and a prior bug (PR #208/#210) shipped with only the Python side wired — the Rust gateway never loaded the config, so `list_repos` returned 0 repos.

---

## 2. Signatures

### Config file shape (`uc.repos.yaml`)

```yaml
workspace_id: my-workspace        # default: "default"
repos:                            # optional explicit entries
  - repo_id: my-backend
    local_path: /abs/path/to/repo # local checkout (indexed directly)
    remote_url: https://...git    # optional
    default_branch: main          # optional, default "main"
    tags: [backend, python]       # optional
  - repo_id: my-frontend          # remote-only (no local_path)
    remote_url: https://...git    #  → Python worker clones to cache; Rust SKIPS
scan_dirs:                        # optional auto-discovery roots
  - /abs/path
scan_depth: 2                     # optional, default 2
```

### Rust (`crates/uc-engine/src/repos_config.rs`)

```rust
pub struct RepoConfig { pub workspace_id: String, pub repos: Vec<RepoEntry>,
                        pub scan_dirs: Vec<PathBuf>, pub scan_depth: i32 }
pub struct RepoEntry { pub repo_id: String, pub local_path: String,
                       pub remote_url: String, pub default_branch: String, pub tags: Vec<String> }

pub fn resolve_config_path(path: Option<&Path>) -> Option<PathBuf>;
pub fn load_repos_config(path: Option<&Path>) -> Option<RepoConfig>;   // None = skip, no error
pub fn discover_scan_dirs(cfg: &RepoConfig) -> Vec<RepoEntry>;
pub fn build_index_requests(cfg: &RepoConfig) -> Vec<IndexRequest>;    // each carries cfg.workspace_id, force_full=false
```

### Python (`python/ultimate_coders/repo_config.py`)

```python
@dataclass
class RepoEntry: repo_id: str; local_path: str; remote_url: str = ""; ...
@dataclass
class RepoConfig: repos: list[RepoEntry]; workspace_id: str = "default";
                  scan_dirs: list[str] = ...; scan_depth: int = 2
class RepoScanner:
    def clone_remote_entry(entry, workspace_id) -> str | None   # remote-only clone to ~/.uc-cache/repos/<ws>/<repo_id>
```

### Startup call sites

```rust
// crates/uc-grpc-server/src/main.rs — after LocalEngine created, before move into GrpcServer
index_workspace_repos(&engine).await;   // loads config, indexes each local repo + scan_dirs discovery
```

```python
# python/ultimate_coders/nats_worker.py — after _register_with_gateway, before subtask subscribe
cfg = self._engine.load_repos_config(config_path)   # config_path = os.environ.get("UC_REPOS_CONFIG")
```

---

## 3. Contracts

### Resolution order (both implementations, identical)

| Priority | Source | Behavior |
|----------|--------|----------|
| 1 | `UC_REPOS_CONFIG` env var | Load that exact path (error if unreadable → skip with warn) |
| 2 | `./uc.repos.yaml` | Load if exists |
| 3 | `./uc.repos.yml` | Load if exists |
| 4 | none | Skip silently (no error, no abort) |

### scan_dirs discovery semantics (both implementations, identical)

- Walk each `scan_dirs` entry recursively up to `scan_depth` levels.
- **Skip-list** (dir names, exact match, do not descend): `node_modules`, `__pycache__`,
  `.tox`, `.venv`, `venv`, `env`, `.mypy_cache`, `.pytest_cache`, `dist`, `build`, `target`.
- **Hidden dirs** (leading `.`): skip, do not descend.
- A subdir containing `.git` is a discovered repo: `repo_id = dir name`,
  `local_path = absolute path`. **Do not recurse into a found git repo** (return immediately).
- `scan_depth` semantics: `depth < 0` → return (no check, no descend);
  `depth == 0` → check current dir for `.git` but do **not** descend;
  `depth == N` → check current, then descend into non-skipped children with `N-1`.
- Default `scan_depth = 2` (catches `<root>/<repo>`).

### workspace_id threading

- Every indexed repo carries the config's `workspace_id` (whatever the yaml says, NOT hardcoded "default").
- `force_full = false` on all startup index requests (incremental index on repeat boots).
- Discovered repos are **deduped against explicit `repos:` entries** by `repo_id` (explicit wins).

### Environment keys

| Key | Required | Purpose |
|-----|----------|---------|
| `UC_REPOS_CONFIG` | no | Override config file path (skips CWD auto-discovery) |

### MVP scope split (intentional divergence, documented)

| Capability | Rust gateway | Python worker |
|------------|:-----------:|:-------------:|
| local_path repos | ✅ index | ✅ index |
| scan_dirs discovery | ✅ | ✅ |
| remote-only clone | ❌ skip with info log | ✅ `clone_remote_entry` → `~/.uc-cache/repos/<ws>/<repo_id>` |
| hot-reload | ❌ startup-only | ✅ `RepoConfigWatcher` (watchdog) |

---

## 4. Validation & Error Matrix

| Condition | Behavior |
|-----------|----------|
| No config file found, no env | Skip silently, log "No uc.repos.yaml found; skipping" |
| Config file unreadable / yaml parse error | Skip with `warn!`, **do not abort startup** |
| A repo's `local_path` does not exist | Skip that repo with `warn!`, continue others |
| `index_repo` fails for one repo | Log `warn!` with error, continue to next repo; startup proceeds |
| Remote-only entry (no `local_path`) on Rust | Skip with `info!` log, do NOT attempt clone |
| `repo_id` missing on an entry | Entry filtered out at parse/validation (both impls) |
| Duplicate `repo_id` (explicit + scan discovery) | Explicit entry wins, discovery deduped |
| Empty `repos:` + no `scan_dirs` | Log "loaded but no indexable repos", startup proceeds |

**Critical invariant**: no config or per-repo failure may abort process startup. The engine
must come up serving (possibly with a partial workspace) so the gateway stays reachable.

---

## 5. Good / Base / Bad Cases

- **Good**: `uc.repos.yaml` with `workspace_id: aiworks`, 2 explicit local repos + `scan_dirs: [~/aiworks]` → gateway indexes all `~/aiworks` git repos under `workspace_id=aiworks`; `list_repos()` returns them with correct `workspace_id`; `list_repos(workspace_id='aiworks')` filters correctly.
- **Base**: No `uc.repos.yaml`, no env → gateway boots with empty engine, `list_repos()` returns `[]`, no error. (Legacy single-repo mode via `UC_REPO_URL` is a separate path, not this config.)
- **Bad**: Config present but only loaded by Python worker while the Rust gateway boots standalone → **gateway engine empty, OMP sees 0 repos.** This was the PR #208/#210 root-cause bug. Both call sites in §2 MUST be wired.

---

## 6. Tests Required

### Rust (`crates/uc-engine/src/repos_config.rs::tests`)

- `parse` empty yaml → `workspace_id="default"`, `repos=[]`
- `parse` workspace_id + repos fields populated correctly
- remote-only entry (no local_path) is dropped from index requests
- invalid yaml → `parse_repos_yaml` returns None
- `build_index_requests` threads `workspace_id` into every `IndexRequest`
- dedup: explicit repo_id wins over scan_dirs discovery of same id
- `discover_scan_dirs`: finds git repos, respects skip-list, skips hidden dirs
- `discover_scan_dirs` depth: `depth=1` finds shallow, `depth=3` finds nested; `depth<0` returns empty
- combine explicit + scan repos, all carry workspace_id
- assertion point: every `IndexRequest.workspace_id == cfg.workspace_id` AND `force_full == false`

### Python (`tests/python/test_repo_config.py`, `tests/python/test_workspace.py`)

- mirror the Rust cases for the shared semantics (resolution order, scan skip-list, depth)
- `clone_remote_entry` produces a usable local path for remote-only entries (Rust has no equivalent)

### E2E (manual / integration)

- Boot `uc-grpc-server` with a `uc.repos.yaml` present → server log shows
  `Workspace repo indexing complete workspace_id=<id> indexed=N total=N`;
  `Engine(mode='grpc').list_repos()` returns N repos all with the configured `workspace_id`.

---

## 7. Wrong vs Correct

### Wrong — only one side loads the config

```rust
// crates/uc-grpc-server/src/main.rs — NEVER do this
let engine = LocalEngine::new(config).await?;
// ... directly into GrpcServer, no workspace loading ...
```
The gateway boots with an empty engine. OMP's `listRepos()` returns 0 repos regardless of
what `uc.repos.yaml` says, because only the Python worker (which may not be running) loads it.
**This is the exact bug shipped in PR #208 and not caught until PR #211.**

### Correct — both call sites load the config

```rust
// crates/uc-grpc-server/src/main.rs
let engine = LocalEngine::new(config).await?;   // or new_fallback()
index_workspace_repos(&engine).await;            // ← loads uc.repos.yaml, indexes workspace
let grpc_server = GrpcServer::with_backends(engine, task_backend, event_store);
```
```python
# python/ultimate_coders/nats_worker.py
cfg = self._engine.load_repos_config(config_path)   # ← worker mode loads the same config
```

---

## Common Mistake: "I added the config field to proto/Rust, so it works"

**Symptom**: `workspace_id` added to `engine.proto` + Rust gRPC server/client, OMP proto
binding regenerated, `listRepos(workspaceId?)` threads the filter — yet OMP still shows 0 repos
or the wrong workspace.

**Cause**: The query path (proto + gRPC + frontend) is necessary but not sufficient. If no
process actually **loads** `uc.repos.yaml` and indexes repos into the engine under that
`workspace_id`, the gateway's metadata store is empty and every query returns nothing.

**Fix**: Verify the **loader** runs at startup for whatever process owns the engine the OMP
front-end talks to. For `run-omp.sh` (default = Rust gRPC server), that's
`index_workspace_repos` in `main.rs`. For worker mode, that's `nats_worker.py`'s
`load_repos_config` call. Both must be wired.

**Prevention**: When adding a new config-driven feature, trace the full path from *disk file*
→ *loader call site* → *engine population* → *query* — not just proto → query. A gap at the
loader step is invisible to the type system and to `tsc`/`cargo check`.
