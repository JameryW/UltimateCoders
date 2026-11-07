# 07 — Coding challenger policy module

Status: implemented
Blocked by: none (D8 decided)

## What to build

`python/ultimate_coders/agent/coding_challenger.py` per [spec-p2.md](../spec-p2.md): enforce flag, episode, decide/apply/maybe_apply, `eval_role=report` stamp, champion result stash for compare.

## Acceptance criteria

- Pure unit tests cover flag off noop, success→challenge_once, quota, no alternate, digest preserve
- Does not import orchestrator
