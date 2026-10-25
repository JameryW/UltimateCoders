# 10: reviewer CV thresholds

**What to build:** Offline / checked-in cross-validation artifacts that arm per-kind thresholds for ticket 09 reviewers. Thresholds load only from those artifacts; kinds without an artifact stay closed. Document how to regenerate artifacts when labels change. Do not invent a labeling UI in this ticket — ownership of labels stays a fog item, but the load path must fail closed if data is absent.

**Blocked by:** 09 (typed reviewer interface).

**Status:** ready-for-agent

- [ ] CV artifact schema and loader for per-kind thresholds
- [ ] Loader refuses missing/corrupt artifacts (kind stays disabled)
- [ ] Regeneration steps documented for future label owners
- [ ] Typed reviewers read thresholds only from the loader (no magic defaults)
- [ ] Tests cover load success, missing artifact, and corrupt artifact
- [ ] Feature flag remains default off; artifacts alone do not enable without enforce
