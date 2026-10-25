# 10: reviewer CV thresholds

**What to build:** Offline / checked-in cross-validation artifacts that arm per-kind thresholds for ticket 09 reviewers. Thresholds load only from those artifacts; kinds without an artifact stay closed. Document how to regenerate artifacts when labels change. Do not invent a labeling UI in this ticket — ownership of labels stays a fog item, but the load path must fail closed if data is absent.

**Blocked by:** 09 (typed reviewer interface).

**Status:** implemented (branch `cursor/t10-reviewer-cv-thresholds-7c92`; `python/ultimate_coders/agent/typed_review_thresholds.py` + integration in `typed_review.py`; enablement requires labels **and** CV artifact; pass on `typed_review_score >= threshold`; refuse `calibrated_default`; no magic 0.5 default; `UC_TYPED_REVIEW_ENFORCE` still default off)

- [x] CV artifact schema and loader for per-kind thresholds
- [x] Loader refuses missing/corrupt artifacts (kind stays disabled)
- [x] Regeneration steps documented for future label owners
- [x] Typed reviewers read thresholds only from the loader (no magic defaults)
- [x] Tests cover load success, missing artifact, and corrupt artifact
- [x] Feature flag remains default off; artifacts alone do not enable without enforce
