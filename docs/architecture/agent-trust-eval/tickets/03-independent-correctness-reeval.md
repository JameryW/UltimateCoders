# 03: independent correctness re-eval

**What to build:** Before trust decisions that depend on correctness, the platform can re-run correctness measurement outside any Agent-writable tree, using the protected `BenchmarkRunner` measure path. Agent harness self-report alone cannot mark correctness passed. Fingerprint / provenance attestation remains required on the protected path; this ticket delivers the independent re-eval capability (dual-gate wiring into accept is ticket 04).

**Blocked by:** None (can start immediately).

**Status:** ready-for-agent

- [ ] Independent correctness re-eval runs with inputs and execution outside Agent-writable trees
- [ ] Agent-writable harness self-report alone cannot produce a passing independent re-eval outcome
- [ ] Protected fingerprint / provenance attestation still fail-closes when unarmed or mismatched
- [ ] Re-eval outcome is observable to downstream accept / completion callers
- [ ] Tests cover protect + independent re-eval at the BenchmarkRunner seam
- [ ] Capability is behind a feature flag that defaults off
