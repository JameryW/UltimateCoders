# 07: approx rank-agreement gate

**What to build:** A rank-agreement harness over a fixed report-role hold-out truth set (Spearman and/or Kendall — lock the metric in the PR). The harness records pass/fail and may write an arming latch. `UC_APPROX_EVAL_ENFORCE` alone must not prune until agreement has passed. No candidate prune wiring yet (ticket 08). Do not invent a second Oracle or feed approx scores into uplift.

**Blocked by:** None (uses P0 report-role identity / remasure; full dataset three-way split is out of scope).

**Status:** ready-for-agent

- [ ] Agreement API evaluates approx ranking vs full Oracle ranking on a report-role hold-out
- [ ] Metric choice (Spearman and/or Kendall) and threshold are explicit in code + tests
- [ ] Failed or missing agreement leaves approx unarmed
- [ ] Passing agreement can set an arming latch consulted by later prune wiring
- [ ] Tests cover the agreement seam without requiring MetaInfer GPU
- [ ] Feature flag(s) default off (`UC_APPROX_EVAL_ENFORCE`; optional latch env)
