# 03: independent correctness re-eval

**What to build:** Before trust decisions that depend on correctness, the platform can re-run correctness measurement outside any Agent-writable tree, using the protected `BenchmarkRunner` measure path. Agent harness self-report alone cannot mark correctness passed. Fingerprint / provenance attestation remains required on the protected path; this ticket delivers the independent re-eval capability (dual-gate wiring into accept is ticket 04).

**Blocked by:** None (can start immediately).

**Status:** implemented (branch `cursor/t3-independent-reeval-7c92`; flag `UC_INDEPENDENT_REEVAL_ENFORCE` default off)

- [x] Independent correctness re-eval runs with inputs and execution outside Agent-writable trees
- [x] Agent-writable harness self-report alone cannot produce a passing independent re-eval outcome
- [x] Protected fingerprint / provenance attestation still fail-closes when unarmed or mismatched
- [x] Re-eval outcome is observable to downstream accept / completion callers
- [x] Tests cover protect + independent re-eval at the BenchmarkRunner seam
- [x] Capability is behind a feature flag that defaults off
