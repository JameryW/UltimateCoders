# 02: report-only public uplift

**What to build:** Accept may still use select-role evidence, but every public uplift path (release artifacts, README-facing figures, published improvement percentages) must come only from a protected remeasure on the hold-out report set with `eval_role=report`. MetaInfer or search/select ranking must not write those public fields. Mis-tagged inputs fail closed rather than silently publishing select scores as uplift.

**Blocked by:** 01 (eval_role on results).

**Status:** ready-for-agent

- [ ] Public uplift helpers accept only `eval_role=report` results from a protected remeasure
- [ ] Select- or search-tagged scores cannot populate public uplift fields
- [ ] Oracle accept remains allowed to use select-role evidence
- [ ] A failing or missing report remeasure does not fall back to select scores for publication
- [ ] Tests demonstrate accept-vs-publish separation at the Oracle / result seam
- [ ] Behaviour is gated by a feature flag that defaults off
