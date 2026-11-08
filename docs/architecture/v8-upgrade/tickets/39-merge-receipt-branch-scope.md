# 39 — Merge receipt branch scope (T39)

**What to build:** Record `merged_branches` on the `push-main` receipt and match it in
`confirmed_effect_ids` — per [spec-p12.md](../spec-p12.md).

**Blocked by:** D22

**Status:** implemented (`python/ultimate_coders/agent/merge_arbiter.py`,
`python/ultimate_coders/effect_ledger.py`; tests in
`tests/python/test_merge_arbiter_effects.py` and `tests/python/test_effect_ledger.py`)

- [x] Receipt `request.merged_branches` is the sorted merge list
- [x] A carried branch matches; an unrelated branch does not
- [x] Effect identity unchanged; `UNKNOWN`/branch-less receipts stay out
