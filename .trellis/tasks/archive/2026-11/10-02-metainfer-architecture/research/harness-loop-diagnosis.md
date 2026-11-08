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

## Continuation: actual native files and provider budgets (2026-10-12)

The earlier no-source statement applies to PR #711, merged at c12f2e49998f9e76f567aa5c4b50e0172a470fac.
This continuation repairs production seams found while pursuing the still-open
single-host acceptance. The upstream checkout remains pinned and unchanged.

### Root cause and why retries failed

- Cross-layer contract: the native client exposed only a shell tool. A model
  could complete with review prose while the pinned pipeline required a file.
  The new write_file tool publishes only actual structured provider calls.
- Implicit configuration: the client forced context 16384 and output 4096;
  local alias names did not establish the actual model size or template.
  An alias named gemma was measured as 11.9B. Installed 4B/9B profiles were
  tested separately, without downloading or changing original aliases.
- Serving/parser seam: an unconditional thinking prefix polluted the strict
  nonce, and the Go rendering path could return empty content/tool_calls.
  A separate pinned alias and the installed server's native GGUF rendering
  produced real write_file calls. No text/nonce stripping manufactured a pass.
- Budget seam: output 4096 and a later context 8192 exhausted real requests.
  Effective context/output fields now reach HTTP and bind cache, launch and
  sessions. The final measured profile uses context 16384 and output 8192.
- Artifact quality: a genuine 289-line harness selected the wrong Triton JIT
  object; a later 537-line harness used torch.randn for an excluded integer
  dtype. Both originals were preserved and rejected by reference execution.
  A written file or exit-zero native CLI is not correctness acceptance.
- Windows publication: an actual CreateFileW reader without FILE_SHARE_DELETE
  reproduced WinError 5. Same-directory replace now retries only Windows
  errors 5/32, five attempts and 150ms total backoff, with no delete fallback.

### Prevention and systematic expansion

| Boundary | Prevention | Evidence |
| --- | --- | --- |
| Implicit default followed by override | Compare the complete effective profile before workspace claim/create | Both context and output override regressions first failed, then passed |
| Resumed conversation changes budgets | Bind both fields, including legacy defaults | Real no-follow session files and HTTP identity |
| Output limit reports done=true | Reject done_reason=length before returning accumulated tools/text | Real HTTP client and CLI text/file-call tests first failed, then passed with no write/success event |
| Actual requested file | Bounded no-follow atomic write_file, return actual hash | HTTP-driven CLI, genuine OCI model artifacts, escaped/special path tests |
| File cleanup failure | Close the directory FD in an inner finally | Actual fstat of captured descriptors first failed, then passed |
| Transient Windows reader | Bounded atomic replace retries, preserve old bytes on failure | Two actual Windows-handle tests first failed, then passed |
| Full acceptance | Retain upstream file checks, self-reference and wrong-candidate controls, protected Oracle | Remains OPEN until its own passing evidence exists |

Performance harnesses share these callable, file and budget seams. Model
availability and a hardware nonce remain separate from generation quality.
Tests redirect only their session directory, so they do not touch live OCI
sessions. Linux FIFO/mode checks require a native filesystem; using NTFS caused
environment failures and was corrected in the test command, without skips.

### Primary sources and knowledge capture

- [Ollama template context](https://github.com/ollama/ollama/blob/main/template/template.go)
  exposes Think/IsThinkSet; the observed nonce was preserved through alias fixes.
- [Ollama serving configuration](https://github.com/ollama/ollama/blob/main/envconfig/config.go)
  exposes OLLAMA_GO_TEMPLATE; the installed 0.40.3 server was verified through
  real native requests after an explicitly owned restart.
- [Ollama issue #17274](https://github.com/ollama/ollama/issues/17274) describes
  the same empty content/no tool_calls symptom. The local observation, rather
  than that report alone, established the tested configuration result.
- Executable contracts and good/bad matrices are recorded in
  inference-infra-spec sections 11 and 12. Raw logs, model digests, artifact
  hashes and scope receipts are retained under .uc; portable outcomes are
  appended to gpu-verification.json. Earlier boot receipts remain historical.

No model artifact has been hand-corrected for acceptance. Full optimization,
protected whole-workflow acceptance/rollback/delivery and cross-host R8 remain
open; the user explicitly has no second host.
