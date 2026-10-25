# 06: adapters and re-eval condition

**What to build:** Existing verify-command and step-condition shapes become condition types (or thin adapters) under `completion_conditions`, so they do not form a second completion authority. Add a condition type that is satisfied only when independent correctness re-eval (ticket 03) has passed, composing D2 into D3. Review-node approval signals may adapt into the same namespace but must not alone bypass non-LLM evaluate.

**Blocked by:** 05 (completion_conditions), 03 (independent correctness re-eval).

**Status:** implemented (`verify_command` / `step_condition` / `independent_reeval` / `review_approved` under `evaluate_completion_conditions`; reuse `UC_COMPLETION_CONDITIONS_ENFORCE`)

- [x] verify-command is expressible as a `completion_conditions` condition type or adapter
- [x] step-condition is expressible as a `completion_conditions` condition type or adapter
- [x] A condition type exists that passes only when independent correctness re-eval passed
- [x] No parallel completion registry or second “done” authority is introduced
- [x] Model or review self-approval alone cannot satisfy the re-eval condition
- [x] Tests cover adapters and the re-eval condition at the contract evaluate seam
- [x] Feature flag defaults off
