# 21: docs numeric manifest + pin CI

**What to build:** Machine-readable numeric manifest for docs-facing measured numbers (SSE latency family first) plus a hard-fail CI checker: document text and pin must match. Also refuse undocumented uplift percentages on README-facing surfaces unless the entry is on the manifest with report-role provenance (align D1 / T2). Must run without a live cluster.

**Blocked by:** None.

**Status:** **implemented** (`docs/numeric-pins.yaml` + `scripts/check-docs-numeric-pins.py` + `ci-docs-numeric-pins.yml`; SSE family first; uplift ban fail-closed; no live stack)

- [x] Manifest lists at least the SSE latency family pins used in README / verification docs
- [x] CI checker hard-fails on doc↔pin drift
- [x] Undocumented uplift `%` on README-facing surfaces fails closed
- [x] No live stack required for the pin checker to pass
- [x] Tests cover match, drift, and uplift-ban cases
- [x] Checker wired into CI (or documented equivalent gate)

## Landing notes

- Manifest: `docs/numeric-pins.yaml` (SSE max / p99 / 200ms ceiling)
- Checker: `scripts/check-docs-numeric-pins.py` (exit code = verdict)
- Tests: `tests/python/test_check_docs_numeric_pins.py`
- CI: `.github/workflows/ci-docs-numeric-pins.yml`
- Live remeasure remains ticket 22
