# 67 — Cleanup reported removals it never performed (T67)

**The defect, in two halves.**

```python
if not is_active:
    try:
        await self._git(["worktree", "remove", path, "--force"], cwd=self._project_path)
        cleaned += 1
    except Exception:
        shutil.rmtree(path, ignore_errors=True)
        cleaned += 1
```

1. `_git` **returns** a result dict; it never raises. So the `except Exception` fallback was
   dead code, and `cleaned += 1` fired even when git refused.
2. Nothing ever ran `git worktree prune`, so metadata for a worktree whose directory was gone
   survived forever.

**Measured on a real repository** (`.scratch/probe_cleanup_prune_b.py`, then the test):
add a worktree, wipe its directory and recreate it empty (the shape left by a crash between
`rmtree` and git's bookkeeping), then call `cleanup()`:

| | before | after |
|---|---|---|
| `cleanup()` return | `1` | `1` |
| directory still present | **Yes** | No |
| `git worktree list` entries | **2** | 1 |
| `git branch -D uc/subtask/ws1` | **`error: cannot delete branch … used by worktree at …`** | succeeds |

So cleanup reported success while the directory stayed, git kept the worktree registered, and
**the subtask branch could never be deleted** — it stayed pinned to a worktree that no longer
existed. Every subsequent cleanup repeated the same false success.

The other direction matters too: if the directory is gone entirely, the loop skips it (`not
os.path.isdir`), so without a prune the stale registration survives indefinitely.

**Fix.**

1. Check the removal's exit code; on failure delete the directory ourselves and only count it
   when it is really gone (warning with git's stderr otherwise);
2. run `git worktree prune` once per cleanup — bounded by T66's `GIT_TIMEOUT_SECONDS` — which
   drops metadata for every worktree whose directory is missing, including leaks left by
   earlier crashes or by older versions of this code.

**Tests** (`tests/python/test_workspace_cleanup.py`, real git — the defect *is* the git /
filesystem reconciliation, so a mock would only re-assert the code under test):

- `cleanup_really_removes_a_worktree_whose_git_file_vanished` — directory gone, metadata
  present: cleanup removes the directory, prunes the entry, and the branch deletes;
- `cleanup_prunes_metadata_when_the_directory_is_gone` — directory absent entirely: the stale
  entry is pruned and the branch deletes (this one needs the unconditional prune);
- `cleanup_leaves_an_active_worktree_alone` — an active handle keeps its worktree and its
  metadata (regression guard).

The first two fail on the unfixed file; the third passes either way, which is the point.

**Status:** implemented (`python/ultimate_coders/agent/workspace.py`,
`tests/python/test_workspace_cleanup.py`)

**Boundary:** the "stale" decision is unchanged (a directory that is not referenced by an
active handle or a live lease is reclaimable, whatever its age). This ticket makes the
reclamation *true* and its bookkeeping consistent; it does not change which worktrees
cleanup considers stale.
