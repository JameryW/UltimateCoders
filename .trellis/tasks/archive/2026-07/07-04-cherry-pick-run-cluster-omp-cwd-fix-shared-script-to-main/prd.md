# Cherry-pick run-cluster OMP cwd fix + shared script to main

## Goal

PR #215 merged only the first commit (inline OMP workspace setup in run-omp.sh). The second commit e5ff79c9 (run-cluster.sh cwd fix + extract scripts/omp-workspace.sh shared helper) was on the feature branch but not merged — so run-cluster.sh still launches OMP with cwd=vendor/oh-my-pi, displaying the wrong workspace. Cherry-pick e5ff79c9 onto main as a new PR.

## What I already know

- e5ff79c9 = "fix(omp): also fix run-cluster.sh OMP cwd; extract shared setup"
  - run-cluster.sh: cd vendor/oh-my-pi → setup_omp_workspace + cd $OMP_WORKSPACE + exec bun $OMP_ENTRY --extension $UC_EXT
  - run-omp.sh: inline functions → source scripts/omp-workspace.sh (dedup)
  - scripts/omp-workspace.sh (new): OMP_WORKSPACE/OMP_VENDOR/OMP_ENTRY/UC_EXT vars + link_config + setup_omp_workspace
- main (b034734c) has #215's inline version in run-omp.sh but NOT the shared script, NOT the run-cluster fix
- Commit was already reviewed (trellis-check on PR #215 verified idempotent symlinks + clobber-safe)
- Conflicts likely: run-omp.sh — main has inline version, e5ff79c9 refactored to source shared script. Cherry-pick may conflict on run-omp.sh (e5ff79c9 removed inline + added source; main has inline). Resolve by taking e5ff79c9's version (source shared script).

## Requirements

- Cherry-pick e5ff79c9 onto new branch from main
- Resolve any conflict (run-omp.sh: prefer the sourced-shared-script version)
- New PR, base=main
- run-cluster.sh OMP launch uses setup_omp_workspace + $OMP_WORKSPACE cwd
- scripts/omp-workspace.sh exists and is sourced by both run-omp.sh + run-cluster.sh

## Acceptance Criteria

- [ ] run-cluster.sh OMP launch: setup_omp_workspace + cd $OMP_WORKSPACE + exec bun $OMP_ENTRY --extension $UC_EXT
- [ ] scripts/omp-workspace.sh exists, sourced by run-omp.sh + run-cluster.sh
- [ ] bash -n both scripts pass
- [ ] PR open, base=main, CI (if any shell gate) green

## Out of Scope

- 4 old task archive commits (separate concern — trellis metadata, not code)
- stash README + grpc-bridge debug log

## Technical Notes

- Commit: e5ff79c9 on origin/fix/omp-workspace-cwd-aiworks
- main HEAD: b034734c
- Conflict expected on run-omp.sh (inline → sourced refactor)
