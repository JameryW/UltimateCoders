# Fix Agent Trust/Eval PR CI failures

## Goal

Green the four failing checks on PR #775 (spec reference guard ×2, line ending guard ×2).

## Root cause

1. **Spec refs:** New `docs/architecture/agent-trust-eval/**` docs added path-shaped mentions. D2 backticked `correctness_harness.py` (not a tracked file) → 2 untriaged dangling mentions. Mention census pin still expects 347.
2. **Line endings:** Guard itself is green; size pin stale after +10 text files. Need allowed pair `(2083, 2074)`.

## MVP

- Triage D2 dangling mentions (rephrase or exempt).
- Update mention census pin and line-ending size pin with comments.
- Locally re-run the two guards / focused pytest.

## Out of scope

- Implementing Agent Trust/Eval product work.
- Changing guard tool behavior beyond triaging this corpus.
