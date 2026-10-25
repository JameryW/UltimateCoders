# 09: typed reviewers fail-closed

**What to build:** Per-failure-kind reviewer entrypoints aligned to routing `FailureKind` (`transient` / `auth` / `schema` / `unknown`). Without labeled data for a kind, that reviewer stays disabled and the existing JSON format gate / human path remains. Do not commit a default 0.5 as a calibrated threshold. Optionally map a successful typed-review outcome into completion evidence `review_verified` (LLM `approved` alone still fails completion).

**Blocked by:** None.

**Status:** ready-for-agent

- [ ] Shared taxonomy with routing FailureKind (no parallel enum names)
- [ ] One reviewer interface per kind; missing labels ⇒ kind disabled
- [ ] Existing review JSON parse gate still works when typed path is off
- [ ] No hardcoded “calibrated” 0.5 threshold shipped as product default
- [ ] Tests cover disable-without-labels and taxonomy alignment
- [ ] Feature flag defaults off (`UC_TYPED_REVIEW_ENFORCE`)
