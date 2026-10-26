# 17: Landlock FS (later)

**What to build:** Optional Landlock (or equivalent) filesystem restriction for the agent child, behind `UC_LANDLOCK_ENFORCE` (default off). Must not block NetworkMode tickets. Document platform support matrix; fail closed when enforce is on but Landlock is unavailable.

**Blocked by:** 15 (network enforcement first per D8); preferably 16 as well but not a hard product blocker if sequenced after 15.

**Status:** ready-for-agent

- [ ] Landlock (or documented equivalent) can restrict agent-child FS when enforce is on
- [ ] Unavailable platform + enforce on → fail closed
- [ ] Does not regress NetworkMode child enforcement
- [ ] Flag defaults off
- [ ] Tests or documented skip matrix for non-Linux CI
- [ ] Feature flag defaults off (`UC_LANDLOCK_ENFORCE`)
