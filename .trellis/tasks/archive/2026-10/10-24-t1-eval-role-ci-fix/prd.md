# Fix T1 eval_role CI failures

## Goal

Green PR #778: ruff on `test_eval_role.py` + task-context corpus pin `883 → 886`.

## Fixes

1. Ruff I001/E501 in `tests/python/test_eval_role.py`
2. Allow `886` in `test_check_tasks_refs.py` real-corpus pin (T1 archive +3 `.trellis` refs)
