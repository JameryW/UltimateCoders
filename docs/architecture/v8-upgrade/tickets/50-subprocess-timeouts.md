# 50 — Subprocess calls must be bounded (T50)

**What to build:** Make "no unbounded `subprocess` call" an invariant.

- Fix the three hot-path sites that could block a worker forever: the inference
  workflow's `_git()` and its `git diff --no-index` patch capture (120s bound, with a
  named timeout error), and the runner's `git rev-parse` when writing the report manifest
  (30s bound).
- `scripts/check-subprocess-timeouts.py` — AST guard that reports every
  `subprocess.run/call/check_call/check_output/Popen` without `timeout=`, with exactly two
  documented exemptions: the two **child runners** (`inference/process.py` module level,
  `metainfer_service/execution.py::main`) whose parent owns the bound and where a timeout
  would cut a legitimate long workload.
- Tests with negative controls (unbounded `run` / `check_output` / `Popen` / module-level
  call are all reported), an exemption-table audit (a claim must name a real file and a
  real reason), the real-repo assertion and non-vacuity check.
- New `subprocess-timeouts` job in Scripts CI (3.9 + 3.12); README trigger table updated.

**Why:** a hung `git` (index.lock, credentials prompt, network fetch) or `nvidia-smi`
(driver wedge) occupies a worker slot while every scheduler still sees it as working.
Bounded calls turn that into a loud failure.

**Status:** implemented (`python/ultimate_coders/inference/workflow.py`,
`python/ultimate_coders/inference/runner.py`,
`scripts/check-subprocess-timeouts.py`,
`tests/python/test_check_subprocess_timeouts.py`, `.github/workflows/ci-scripts.yml`,
`README.md`)

**Boundary:** the bound is generous (120s / 30s) and not configurable yet — a workload
that legitimately needs longer is a different decision; the guard's exemption list is the
only escape hatch and carries a reason.
