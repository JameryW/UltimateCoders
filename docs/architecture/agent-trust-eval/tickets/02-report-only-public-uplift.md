# 02: report-only public uplift

**What to build:** Accept may still use select-role evidence, but every public uplift path (release artifacts, README-facing figures, published improvement percentages) must come only from a protected remeasure on the hold-out report set with `eval_role=report`. MetaInfer or search/select ranking must not write those public fields. Mis-tagged inputs fail closed rather than silently publishing select scores as uplift.

**Blocked by:** 01 (eval_role on results).

**Status:** implemented (branch `cursor/t2-report-only-uplift-7c92`; flag `UC_REPORT_UPLIFT_ENFORCE` default off)

- [x] Public uplift helpers accept only `eval_role=report` results from a protected remeasure
- [x] Select- or search-tagged scores cannot populate public uplift fields
- [x] Oracle accept remains allowed to use select-role evidence
- [x] A failing or missing report remeasure does not fall back to select scores for publication
- [x] Tests demonstrate accept-vs-publish separation at the Oracle / result seam
- [x] Behaviour is gated by a feature flag that defaults off
