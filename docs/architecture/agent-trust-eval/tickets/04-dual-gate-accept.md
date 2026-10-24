# 04: dual-gate accept

**What to build:** Oracle accept requires both existing harness fingerprint / provenance attestation and a passing independent correctness re-eval (ticket 03). Either gate failing rejects accept. Agent-generated harness output may still appear as candidate input but cannot alone open the gate. Closing this ticket is the product closure of the F8 remainder described in D2.

**Blocked by:** 03 (independent correctness re-eval).

**Status:** ready-for-agent

- [ ] Accept requires attestation AND independent re-eval both passing when the dual-gate flag is on
- [ ] Attestation failure rejects even if re-eval would pass
- [ ] Re-eval failure or skip rejects even if attestation passes
- [ ] Agent harness self-report alone never yields accept under the flag
- [ ] Accept / reject events expose which gate failed for diagnosis
- [ ] Tests cover dual-gate matrix at the Oracle accept seam
- [ ] Feature flag defaults off and does not weaken existing fingerprint fail-closed behaviour when off
