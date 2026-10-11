# Bug Analysis: rejected GPU harness calls the wrong entrypoint

## 1. Root Cause Category

- **Category**: B (cross-layer contract), with E (implicit assumption).
- **Specific cause**: the generated harness treated the raw Triton JIT object's
  run method as the documented module-level run(x, y). Real GPU replay failed
  every case because the internal method requires grid/warmup. A separate
  review seam returned prose instead of the requested file; pinned upstream
  reads the file, so it could not feed that prose back into regeneration.

## 2. Why Previous Attempts Failed

1. A longer full trial exercised generation/review again without a runnable
   self-reference check; it exceeded 900s without an optimized candidate.
2. An input-contract description did not guarantee correct launch mechanics.
3. Model text described review issues, but the actual tools only listed/read
   files. Without the review file the next generation received empty feedback.

## 3. Prevention Mechanisms

| Priority | Mechanism | Specific Action | Status |
| --- | --- | --- | --- |
| P1 | Runtime diagnosis | Replay exact rejected artifacts against the reference in owned OCI | DONE |
| P1 | Positive/negative control | Change only the two calls in a labelled copy; require reference pass and broken-candidate rejection | DONE |
| P1 | Executable contract | Distinguish module callable, raw JIT launch and actual requested review file in inference-infra-spec section 11 | DONE |
| P1 | Provenance | Preserve original/copy/kernel hashes and separate stopped-scope receipts | DONE |
| P1 | Actual workflow | Obtain genuine model-written artifacts and stable protected GPU acceptance before closing R8 | OPEN |

## 4. Systematic Expansion

- **Similar issues**: performance harnesses have the same launch and file
  deliverable boundaries. An exit-zero coding CLI is insufficient evidence.
- **Design improvement**: preserve upstream's authoritative file checks and
  UC's independent protected Oracle; do not synthesize artifacts from prose.
- **Process improvement**: use a small reference replay before another costly
  full trial. Keep external GPU conditions and unavailable second-host work
  separate from diagnostic results.

## 5. Knowledge Capture

- [x] Update the inference contract's callable/file rules and diagnostic tests.
- [x] Record original RED, copy GREEN, broken candidate RED and thirteen stopped
  OCI scopes in gpu-verification.json; preserve earlier trial evidence.
- [x] Record the user's explicit cross-host deferral without waiving R6/R8.
- [x] Verify template applicability: this repository has no spec-template tree.
- [ ] Close real optimization/Oracle/rollback/delivery and cross-host criteria
  only after their own evidence exists.

The original model harness remains rejected. The corrected copy is an
operator-owned diagnostic, not a production fix, optimized candidate or proof
that the model completes the pinned workflow. No production source changed.
