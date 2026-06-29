# Research: External Git Sync Model for Distributed Workers

- **Query**: How should a distributed fleet of worker containers sync code with an EXTERNAL GitHub/GitLab remote as the central "unified git repository management system"? Cover code-sync model, result-return model, cross-host conflict handling, credential management, WorkspaceManager changes, and compose/deployment changes.
- **Scope**: mixed (internal codebase grounding + external git/container best practices)
- **Date**: 2026-06-29

## Current State (grounded from code)

### WorkspaceManager — LOCAL-ONLY worktree isolation
`python/ultimate_coders/agent/workspace.py`

- `acquire(subtask_id)` (L72): runs `git worktree add -b uc/subtask/<id> .uc/worktrees/<ws_id> <base_branch>` against the **local repo at `self._project_path`** (default `os.getcwd()`). On failure falls back to `shutil.copytree` of the project (L267 `_copy_project`).
- `release(handle, merge=True)` (L142): `git log base..branch` → if commits exist, `git merge <branch> --no-edit` **into `self._base_branch` in the local repo** (L171). On conflict → `git merge --abort`, branch preserved (L177). On success → `git worktree remove` + `git branch -D`.
- **No `fetch`, `push`, `clone`, or `remote` invocation anywhere in `workspace.py`.** The worktrees branch off whatever HEAD the local clone happens to be at. base_branch defaults to `"main"` (L63) but is **never fetched from origin** — it is purely local.
- `__init__` takes `project_path`, `max_worktrees`, `base_branch`. No `remote_url`, no credential config.

### DistributedConflictDetector — assumes shared-local-FS coordination
`python/ultimate_coders/agent/distributed_conflict.py`

- `declare_intent` (L85): local ConflictDetector check → `_acquire_lock` (in-memory `dict[file_path, (owner, ts)]`, L210) → NATS broadcast of `edit_intent_declared` (L113).
- `_acquire_lock` (L210): **optimistic, in-process** — claims the lock in local memory; the NATS request-reply comment (L217) is aspirational, not implemented (no actual request-reply round-trip; it only sets `self._file_locks[fp] = (self._worker_id, now)`).
- `receive_remote_intent` (L162) / `receive_remote_release` (L196): fed by NATS `uc.file.changed` subscription (`nats_worker.py:_handle_file_changed` L1519), which calls `receive_remote_intent(...)`.
- The whole layer assumes workers see each others' edits via NATS intent broadcast **before** they hit git. With cross-host + per-worker clones, two workers editing the same file on different hosts will not actually conflict at the file-write level (separate worktrees), so the detector becomes advisory only — the real conflict surfaces at git merge/push time.

### File-change broadcast
`python/ultimate_coders/agent/worker.py:_emit_file_changed` (L860): for each `result.modified_files`, publishes a `FileChangeEvent` to NATS subject `uc.file.changed` with `file_path`, `change_type`, `diff_summary` (first 200 chars of diff). Consumed by `nats_worker._handle_file_changed` (L1519) → `DistributedConflictDetector.receive_remote_intent`.

### Repo config — remote_url exists but unused for sync
`python/ultimate_coders/repo_config.py:RepoEntry` (L24): has `remote_url` field, but `RepoScanner._get_remote_url` (L272) only reads it via `git remote get-url origin` for **repo_id derivation** (`_derive_repo_id` L261). `index_repo` (engine.py) receives it but does not drive worker clone/fetch.

### Worker construction wiring
`python/ultimate_coders/nats_worker.py` (L730-758):
- `WorkspaceManager(project_path=self._project_path or os.getcwd())` — no remote/credential args.
- `DistributedConflictDetector(nats_publisher=self._publisher, worker_id="")` — wired to `worker.conflict_detector` (the local detector inside it).
- `self._project_path = os.environ.get("UC_PROJECT_PATH", os.getcwd())` (L1959).

### Docker worker service — empty workspace, no git cred
`docker/docker-compose.yml` worker (L182-197):
- `UC_PROJECT_PATH: "/workspace"` but **no volume mounted** → `/workspace` is empty in the container.
- No `UC_REPO_URL`, `UC_REPO_BRANCH`, `GIT_*`, or credential env/secret.
- `docker/Dockerfile` runtime stage (L17): installs `libssl3 ca-certificates curl` — **does NOT install `git`** in the runtime image (only the builder stage L6 has `git`). **This is a blocker**: the worker container cannot run any git command today. (`git` would need to be added to the runtime stage.)

### ScaleWorkers (PR #192, MERGED 2026-06-29)
- Gateway shells out `docker compose -p <project> -f <file> up -d --no-deps --scale worker=<N> worker`.
- `--no-deps` is mandatory (worker `depends_on: gateway`; gateway issuing the command would deadlock).
- Scale-down → worker receives SIGTERM → `_deregister_from_gateway()` (nats_worker.py:1975 trap); 60s heartbeat timeout is backstop.
- **Implication for git sync**: every newly-scaled worker container is a fresh process with no local clone. If model (b)/(c) is used, the clone must either be (i) baked into the image, (ii) on a shared volume, or (iii) created on worker startup. Cross-host scaling (future k8s) means (iii) is the only portable option — each worker fetches its own clone at startup.

---

## Findings

### 1. Code Sync Model

#### (a) Clone-on-demand — fresh `git clone` per subtask
- Flow: `git clone <remote> <ws>` → `git worktree add` → work → `git push` → `rm -rf <ws>`.
- **Pros**: perfect isolation, no stale state, trivially stateless workers, ideal for cross-host/k8s.
- **Cons**: a full clone of even a moderate repo (100MB+) takes 5-30s+ over the network; repeated per subtask is wasteful and slow. Large monorepos (GB-scale) are prohibitive.
- **Fit**: only sensible for small repos or when subtasks are rare/long-running (clone cost amortized). Not the default for a coding-agent fleet doing many short subtasks.

#### (b) Persistent shared clone + fetch/pull per subtask — **RECOMMENDED**
- Flow (per worker container):
  1. **Startup**: `git clone <remote> /workspace/<repo>` (once, into a persistent path).
  2. **acquire**: `git fetch origin` → `git worktree add -b uc/subtask/<id> .uc/worktrees/<ws_id> origin/<base_branch>`.
  3. **work** in worktree.
  4. **release**: commit on branch → `git push origin uc/subtask/<id>` (result branch) → `git worktree remove`.
- **Pros**: clone cost paid once; `fetch` is incremental (seconds); worktree branch is based on the **fetched** `origin/main`, not a stale local HEAD; each subtask sees fresh upstream state. Naturally fits independent cross-host workers (each host/container has its own clone).
- **Cons**: needs persistent-volume or startup-clone logic; clone can drift if not fetched; must handle `fetch` network failures.
- **Per-host vs per-worker**: workers on the **same host** could share one clone via a host-mounted volume (multiple worktree branches off one clone is exactly what `git worktree` is designed for). But docker-compose `--scale worker=N` on one host gives each container its own filesystem unless a shared named volume is mounted. **Simplest correct model: each worker container owns its own clone** (stored on a per-container volume or cloned at startup to `/workspace`). Sharing is an optimization, not a requirement.

#### (c) Bare local mirror clone + worktrees
- Flow: `git clone --mirror <remote> /mirror.git` (bare) → each subtask `git --git-dir=/mirror.git worktree add ...`.
- **Pros**: smallest disk footprint; mirror is fetch-only (never checked out); worktrees share the object store.
- **Cons**: `git clone --mirror` sets `remote.origin.mirror=true`, which **pushes all refs** on push — wrong for subtask branch push. Worktrees off a bare repo require `git worktree add --git-dir` gymnastics and a separate checkout. More complex than (b) with marginal benefit at this scale.
- **Fit**: only worth it for very large repos where the working-tree checkout cost of (b) matters. Not recommended for MVP.

**Recommendation: model (b)** — persistent clone per worker container + `fetch` before each `acquire` + push of the subtask branch on `release`. Rationale: balances speed, isolation, and cross-host portability; aligns with how `git worktree` is meant to be used (multiple branches off one clone); each scaled worker is self-sufficient.

### 2. Result-Return Model

#### (a) Direct push to subtask branch, last-write-wins, merge centrally
- Worker pushes `uc/subtask/<id>` to remote; gateway/arbiter later merges subtask branches into `main`.
- **Pros**: simple, decouples worker from merge policy; worker never touches `main`.
- **Cons**: "last-write-wins" only applies to the branch namespace (each subtask gets its own branch, so no clobbering); the real merge happens later and may conflict.

#### (b) Worker pushes branch + opens a PR (GitHub/GitLab API)
- Worker calls `POST /repos/{owner}/{repo}/pulls` after push.
- **Pros**: human/gateway review gate; native conflict surface (GitHub shows merge conflicts on the PR); auditable.
- **Cons**: requires GitHub/GitLab API client + token in the worker; PR-per-subtask creates PR noise for a coding-agent fleet doing dozens of subtasks; merge orchestration now depends on the external API.

#### (c) Worker pushes branch only, gateway/arbiter merges into main — **RECOMMENDED for MVP**
- Worker's only remote action is `git push origin uc/subtask/<id>` (a subtask-scoped branch, never `main`).
- A **central merge arbiter** (the gateway, or a dedicated merge step in the Orchestrator/Aggregator) pulls the subtask branch and merges it into `main` using the **existing `WorkspaceManager.release` merge logic** (`conflict.py:ConflictResolver` 4-tier pipeline) but against `origin/main`.
- **Pros**: reuses the existing `ConflictDetector` + `ConflictResolver` (auto-merge → LLM-assisted → reassign → human); workers stay dumb (push-only); `main` is never written by workers, so no force-push races; the external remote is just durable storage + branch transport, not the merge authority.
- **Cons**: gateway must hold a clone too (or fetch subtask branches on demand) to perform merges.

**Recommendation: model (c)**. The external GitHub/GitLab remote is the **transport + durable store**, not the merge authority. Workers push subtask branches; the gateway/Orchestrator (which already has the merge logic in `conflict.py`) fetches + merges into `main` + pushes `main`. This keeps workers thin and reuses the existing resolution pipeline. PR-per-subtask (model b) is a future opt-in for human-review workflows, not the default.

**How the existing `DistributedConflictDetector` + `uc.file.changed` fits in**: today it assumes a shared local FS (in-memory `_file_locks`, optimistic no-real-roundtrip). With per-worker clones, the NATS intent broadcast becomes a **pre-write advisory hint** (scheduler can avoid assigning two workers overlapping files — see `worker.py` `_can_accept_more` + `file_constraints`), while the **authoritative** conflict resolution moves to merge time in the gateway (git 3-way merge + the existing `ConflictResolver`). The `uc.file.changed` broadcast remains useful for the gateway to know "worker X is editing file Y" for scheduling, but it is no longer the conflict-detection source of truth.

### 3. Conflict Handling Across Hosts

Current `DistributedConflictDetector` problems for cross-host:
- `_acquire_lock` (L210) is **in-process only** — no actual distributed lock (the NATS request-reply is aspirational, comment at L217). Two workers on different hosts each claim the lock in their own memory → no mutual exclusion.
- `receive_remote_intent` relies on `uc.file.changed` arriving before the second worker writes — best-effort timing, not guaranteed across hosts with NATS latency.

**Recommended two-layer approach**:

1. **Pre-write scheduling layer (advisory, keeps the existing design)**: the Orchestrator/Scheduler already uses `subtask.file_constraints` (`worker.py` L423-426 throttles when ≥3 files). Extend this so the scheduler **does not assign two concurrent subtasks with overlapping `file_constraints` to different workers** — this is a cheap, deterministic overlap check at dispatch time (the gateway already knows each subtask's `file_constraints`). This is far more reliable than the NATS intent race. The `uc.file.changed` broadcast becomes informational (dashboard, logging) rather than load-bearing for correctness.

2. **Authoritative merge-time layer (new, in gateway)**: when the gateway merges a subtask branch into `origin/main`, run `git merge` and on conflict invoke the existing `ConflictResolver` (`conflict.py` L300, 4-tier: auto-merge → LLM → reassign → human). This is the only place a real conflict is deterministically detected, because git sees the actual content divergence. The `MergeVerifier` (`distributed_conflict.py` L259, runs `cargo check`/`py_compile`/`tsc`) validates the merged result.

**The `DistributedConflictDetector` should be demoted from "conflict authority" to "intent broadcast for scheduling hints"**, with a comment noting it is not a distributed lock. True distributed locking (if ever needed) should use TiKV (already deployed) or NATS KV, not in-process dicts.

### 4. Credential Management

Options for giving worker containers non-interactive write access to GitHub/GitLab:

| Method | Setup | Security | Non-interactive? | Fit |
|---|---|---|---|---|
| **PAT in env var** (`UC_GIT_TOKEN`) | Trivial: generate fine-grained PAT, pass as env/secret | Medium — token in env (visible to `inspect`, child procs); fine-grained PAT limits scope | Yes (via credential helper or URL) | **Pragmatic default** |
| **SSH key mounted** (read-only secret) | Generate deploy key, mount `~/.ssh/id_ed25519` + known_hosts | High — key never in env; revocable per-repo deploy key | Yes (ssh agent or key file) | Good for SSH-remote orgs |
| **GitHub App** (installation token) | Create App, install to org, worker exchanges JWT for install token | Highest — short-lived tokens, per-repo perms, auditable | Yes (token refresh loop needed) | Best for production/multi-org; overkill for MVP |

**Non-interactive `git push` in a container** — the three mechanisms:

1. **Embedded token in remote URL**: `https://x-access-token:<TOKEN>@github.com/org/repo.git`. Simplest, but token is in `.git/config` and `git remote -v` output (visible to anyone reading the worktree). **Avoid** for any shared clone.
2. **`GIT_ASKPASS` script**: set `GIT_ASKPASS=/usr/local/bin/git-askpass.sh` (a script that `echo $UC_GIT_TOKEN`) + `GIT_TERMINAL_PROMPT=0`. Git invokes the script only when auth is needed; token stays in env, not in the repo. **Recommended** — clean separation, no token in `.git/config`.
3. **`git credential.helper`**: `git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=$UC_GIT_TOKEN"; }; f'`. Equivalent to (2), standard mechanism. Also recommended.

**Recommendation for docker-compose-deployed internal tool**: **fine-grained PAT via env var `UC_GIT_TOKEN` + `GIT_ASKPASS` script** (or credential helper). Rationale: trivial to wire into compose `environment:`/`secrets:`, no SSH key/known_hosts management, fine-grained PAT can be scoped to specific repos with `contents: write` only. For orgs standardizing on SSH, mount a deploy key as a read-only secret instead. GitHub App is the production end-state but not needed for MVP.

**Important**: the worker remote URL must be the **HTTPS** form (`https://github.com/org/repo.git`), not SSH, when using a PAT. `RepoEntry.remote_url` from `repo_config.py` may already be HTTPS (depends on how the repo was cloned on the host); the worker should normalize/override it from `UC_REPO_URL`.

### 5. WorkspaceManager Changes (concrete sketch)

Goal: go from local-only worktree to **remote-synced** — `fetch` before acquire, `push` after release, base_branch tracked against `origin/main`.

**New `__init__` params** (additive, defaults preserve local-only behavior):
```python
def __init__(
    self,
    project_path: str = "",
    max_worktrees: int = 8,
    base_branch: str = "main",
    remote_url: str = "",        # NEW: e.g. https://github.com/org/repo.git
    remote_name: str = "origin", # NEW
    fetch_on_acquire: bool = False,  # NEW: gate remote behavior
    push_on_release: bool = False,   # NEW
    credential_helper: str = "",     # NEW: e.g. "!f(){ echo password=$UC_GIT_TOKEN; }; f"
) -> None:
```

**New: ensure clone exists at startup** (idempotent):
```python
async def ensure_clone(self) -> None:
    """Clone the remote if project_path is empty/non-git, else add remote."""
    if not self._remote_url:
        return  # local-only mode, backward compatible
    git_dir = os.path.join(self._project_path, ".git")
    if not os.path.exists(git_dir):
        await self._git(["clone", self._remote_url, self._project_path])
    else:
        # ensure origin points at remote_url
        await self._git(["remote", "set-url", self._remote_name, self._remote_url])
```

**Modified `acquire`** (insert fetch before worktree add; base off `origin/<base_branch>`):
```python
# BEFORE creating the worktree:
if self._fetch_on_acquire:
    await self._git(["fetch", self._remote_name, self._base_branch],
                    cwd=self._project_path)
    base_ref = f"{self._remote_name}/{self._base_branch}"  # track remote
else:
    base_ref = self._base_branch  # local-only fallback (current behavior)

result = await self._git(
    ["worktree", "add", "-b", branch_name,
     f".uc/worktrees/{ws_id}", base_ref],
    cwd=self._project_path,
)
```

**Modified `release`** (after local merge success, push the subtask branch; do NOT push `main` — gateway owns `main`):
```python
# After successful local merge (existing logic L171-185):
if self._push_on_release and handle.branch_name:
    # Push the subtask branch to remote (gateway will merge into main later)
    push = await self._git(
        ["push", self._remote_name, f"{handle.branch_name}:refs/heads/{handle.branch_name}"],
        cwd=self._project_path,
    )
    if push["exit_code"] != 0:
        result_info["status"] = "push_failed"
        result_info["branch_preserved"] = handle.branch_name
        logger.warning("Push failed for %s: %s", handle.branch_name, push["stderr"][:200])
```

**Key behavioral notes**:
- `base_branch` is now tracked against `origin/main` (via `fetch`), not the stale local `main`. This fixes the current bug where worktrees branch off an arbitrarily-old HEAD.
- The **local merge in `release`** merges the subtask branch into the **local** `main` (for the worker's own sanity/verification). The **authoritative** merge into `origin/main` happens in the gateway. Alternatively, skip the local merge entirely and just push the branch — but keeping the local merge lets the worker run `MergeVerifier` before pushing.
- `ensure_clone()` must be called at worker startup (`nats_worker._init_orchestrator` ~L732) before any subtask arrives.
- Credential helper is configured once via `git config --global credential.helper "<helper>"` in `ensure_clone` (or in the Dockerfile entrypoint), so `fetch`/`push` are non-interactive.

### 6. Compose / Deployment Changes

**`docker/Dockerfile` runtime stage** — add `git` (BLOCKER today):
```dockerfile
RUN apt-get update && apt-get install -y \
    libssl3 ca-certificates curl git openssh-client \
    && rm -rf /var/lib/apt/lists/*
```

**`docker/docker-compose.yml` worker service** — add repo + credential env, persistent volume for the clone:
```yaml
worker:
  build:
    context: ..
    dockerfile: docker/Dockerfile
  profiles: ["worker", "app"]
  depends_on:
    nats: { condition: service_healthy }
    gateway: { condition: service_started }
  environment:
    UC_NATS_URL: "nats://nats:4222"
    UC_GRPC_ENDPOINT: "http://gateway:50051"
    UC_SANDBOX_MODE: "subprocess"
    UC_PROJECT_PATH: "/workspace/<repo>"   # points INTO the clone
    UC_REPO_URL: "https://github.com/<org>/<repo>.git"   # NEW
    UC_REPO_BRANCH: "main"                                  # NEW
    UC_GIT_TOKEN: "${UC_GIT_TOKEN}"                         # NEW (from .env / secret)
    GIT_TERMINAL_PROMPT: "0"                                # NEW: never prompt
    GIT_ASKPASS: "/usr/local/bin/git-askpass.sh"           # NEW (bake into image)
  volumes:
    - worker_workspace:/workspace    # NEW: persist clone across restarts
  command: ["python", "-m", "ultimate_coders.nats_worker", "--mode", "worker"]
```

**`git-askpass.sh`** (bake into Dockerfile, reads token from env, never echoes to repo):
```sh
#!/bin/sh
echo "$UC_GIT_TOKEN"
```

**`.env`** (gitignored, sourced by compose):
```
UC_GIT_TOKEN=github_pat_<fine-grained-PAT>
```
Or use docker compose `secrets:` for stricter handling.

**Persistent volume rationale**: model (b) keeps a long-lived clone. A named volume `worker_workspace` survives container restarts so the clone isn't re-done every time. **Cross-host caveat**: a named volume is per-host; when workers run on different hosts (future k8s/multi-host compose), each host gets its own volume and the worker clones on first start. This is correct and expected — each host needs at least one clone.

**ScaleWorkers (PR #192) interaction**:
- `docker compose up -d --scale worker=N` starts N containers. Each mounts the **same** named volume `worker_workspace` on a single host → they would share one clone (model b's per-host optimization). This works because `git worktree` is designed for multiple branches off one clone, and each worktree is a separate directory (`.uc/worktrees/<ws_id>`). **Caveat**: concurrent `git fetch` from N workers on the same clone can race; mitigate by serializing fetch (a per-host lock file) or having only one worker fetch and others read the updated refs (advanced). For MVP, allow each worker to fetch — git handles concurrent fetch reasonably (refs are updated atomically; worst case a redundant fetch).
- **Scaling up on a NEW host** (future): the new host has no `worker_workspace` volume → worker clones on startup via `ensure_clone()`. Each host is self-sufficient. This is the portable model.
- **Network egress**: worker containers need outbound HTTPS (443) to `github.com`/`gitlab.com`. In a locked-down network, add a `github.com` allowlist to the worker network. No inbound port needed (workers are NATS/gRPC clients only — already the case).

---

## Concrete Recommendation (summary)

| Decision | Choice | Why |
|---|---|---|
| **Code sync model** | (b) Persistent clone per worker + `fetch` before `acquire` + push subtask branch on `release` | Fast (clone once), isolation via worktree, cross-host portable (each worker self-sufficient), aligns with `git worktree` design |
| **Result-return model** | (c) Worker pushes `uc/subtask/<id>` branch only; gateway fetches + merges into `origin/main` using existing `ConflictResolver` | Workers stay thin; `main` never written by workers; reuses 4-tier resolution pipeline; external remote = transport, not authority |
| **Conflict handling** | Two layers: (1) pre-write `file_constraints` overlap scheduling at gateway (deterministic), (2) authoritative git merge + `ConflictResolver` + `MergeVerifier` at gateway merge time. Demote `DistributedConflictDetector` to advisory scheduling hint | In-process optimistic lock is not a real distributed lock; git merge is the only deterministic conflict point with per-worker clones |
| **Credentials** | Fine-grained PAT via `UC_GIT_TOKEN` env + `GIT_ASKPASS` script; HTTPS remote URL | Trivial compose wiring, no SSH key mgmt, fine-grained scope. SSH deploy key / GitHub App are future upgrades |
| **WorkspaceManager** | Add `remote_url`, `fetch_on_acquire`, `push_on_release` params; `ensure_clone()` at startup; `acquire` fetches + branches off `origin/main`; `release` pushes subtask branch (not main) | Additive — local-only mode preserved when `remote_url` empty |
| **Compose** | Add `git` to runtime Dockerfile; add `UC_REPO_URL`/`UC_REPO_BRANCH`/`UC_GIT_TOKEN`/`GIT_ASKPASS`/`GIT_TERMINAL_PROMPT` env + `worker_workspace` volume + `git-askpass.sh` | Unblocks container git; persists clone; non-interactive auth |

### MVP scope (suggested)
1. Add `git` to runtime Dockerfile + `git-askpass.sh`.
2. Extend `WorkspaceManager` with `remote_url`/`fetch_on_acquire`/`push_on_release`/`ensure_clone()`.
3. Wire `UC_REPO_URL`/`UC_REPO_BRANCH`/`UC_GIT_TOKEN` from env into `WorkspaceManager` in `nats_worker._init_orchestrator`.
4. Add `worker_workspace` volume + credential env to compose worker service.
5. Gateway-side merge arbiter: fetch subtask branch + `ConflictResolver` merge into `origin/main` + push (can reuse `WorkspaceManager.release` merge logic against `origin/main`).
6. Demote `DistributedConflictDetector` doc/comments to "advisory scheduling hint"; add gateway-side `file_constraints` overlap check in the Scheduler/dispatch path.

### Out of MVP (future)
- PR-per-subtask (GitHub API) for human-review workflows.
- GitHub App auth (short-lived tokens) for multi-org production.
- True distributed locking via TiKV/NATS-KV if advisory scheduling proves insufficient.
- Shared-clone fetch serialization for high N on one host.
- Kubernetes deployment (design is compatible — each pod clones on startup).

---

## Related Specs / Files

| File | Relevance |
|---|---|
| `python/ultimate_coders/agent/workspace.py` | WorkspaceManager — primary file to modify (acquire/release/ensure_clone) |
| `python/ultimate_coders/agent/distributed_conflict.py` | DistributedConflictDetector — demote to advisory; MergeVerifier reused by gateway |
| `python/ultimate_coders/agent/conflict.py` | ConflictDetector + ConflictResolver (4-tier) — reused by gateway merge arbiter |
| `python/ultimate_coders/agent/worker.py` | Worker._emit_file_changed (L860), declare_edit_intent (L898), file_constraints throttling (L423) |
| `python/ultimate_coders/nats_worker.py` | Worker construction (L730-758), _handle_file_changed (L1519), UC_PROJECT_PATH (L1959) — wire env into WorkspaceManager |
| `python/ultimate_coders/repo_config.py` | RepoEntry.remote_url (L29) — source of UC_REPO_URL, currently only for repo_id |
| `docker/docker-compose.yml` | worker service (L182) — add volume + cred env |
| `docker/Dockerfile` | runtime stage (L17) — add `git` (BLOCKER) |
| `.trellis/spec/backend/worker-service-spec.md` | WorkerService gRPC registration — unchanged, but gateway merge arbiter is a new role |
| PR #192 (MERGED) | ScaleWorkers — scaling model must tolerate per-host clone volumes |

## Caveats / Not Found

- **`git` is NOT in the runtime Docker image** (`docker/Dockerfile` L17-21 installs only `libssl3 ca-certificates curl`). This is a hard blocker for any remote git sync — must be fixed first.
- The `DistributedConflictDetector._acquire_lock` (L210) is **not a real distributed lock** despite the docstring; the NATS request-reply mentioned in the comment is not implemented. Do not rely on it for cross-host mutual exclusion.
- No existing code path performs `git clone`/`fetch`/`push` — this is entirely new functionality. The `RepoScanner._get_remote_url` only *reads* the remote URL for repo_id derivation.
- External web search tools (exa) were not available in this environment; the git/container best-practice recommendations (credential helpers, worktree mirror tradeoffs, GitHub App vs PAT) are based on established, well-documented git/GitHub behavior, not freshly-fetched citations. The git CLI semantics cited (`--mirror` pushes all refs, `GIT_ASKPASS` invocation, worktree off `origin/<branch>`) are stable and verifiable via `git --help` / GitHub docs.
- The recommendation assumes the external remote is GitHub/GitLab over HTTPS. If the org mandates SSH, swap PAT+`GIT_ASKPASS` for a mounted deploy key + `ssh-keyscan` known_hosts (the model (b)/(c) sync + result flow is unchanged).
