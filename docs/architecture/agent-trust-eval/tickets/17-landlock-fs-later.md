# 17: Landlock FS (later)

**What to build:** Optional Landlock (or equivalent) filesystem restriction for the agent child, behind `UC_LANDLOCK_ENFORCE` (default off). Must not block NetworkMode tickets. Document platform support matrix; fail closed when enforce is on but Landlock is unavailable.

**Blocked by:** 15 (network enforcement first per D8); preferably 16 as well but not a hard product blocker if sequenced after 15.

**Status:** implemented (branch `cursor/t17-landlock-fs-7c92`; planner/helper in `python/ultimate_coders/agent/landlock.py`; wired in `python/ultimate_coders/agent/sandbox.py` `_execute_subprocess` as Landlock inner + NetworkMode outer; engine `execute_in_sandbox` bypass closed when landlock enforce on; FS MVP RO `/` + RW worktree + RW `/tmp`; flag default off; tests in `tests/python/test_landlock_fs.py`)

- [x] Landlock (or documented equivalent) can restrict agent-child FS when enforce is on
- [x] Unavailable platform + enforce on → fail closed
- [x] Does not regress NetworkMode child enforcement
- [x] Flag defaults off
- [x] Tests or documented skip matrix for non-Linux CI
- [x] Feature flag defaults off (`UC_LANDLOCK_ENFORCE`)
