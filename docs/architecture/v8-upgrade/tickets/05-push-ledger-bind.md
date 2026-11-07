# 05 — Bind RuntimeState for push-on-release receipts

Status: implemented
Blocked by: none

## What to build

Ensure `_push_subtask_branch` can use EffectLedger when `push_on_release` is set even if the workspace was acquired without `require_worktree` (coding path). Lazy-create / bind the same RuntimeState path used by exclusive worktree acquire.

## Acceptance criteria

- With `push_on_release=True` and a remote, a coding-path release that pushes writes a `push-branch` EffectLedger record when RuntimeState dir is available
- Without remote / without push_on_release, behavior unchanged
- Extend `tests/python/test_workspace_push_effects.py` (or sibling) for the lazy-bind case
