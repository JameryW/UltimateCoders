# 21: docs numeric manifest + pin CI

**What to build:** Machine-readable numeric manifest for docs-facing measured numbers (SSE latency family first) plus a hard-fail CI checker: document text and pin must match. Also refuse undocumented uplift percentages on README-facing surfaces unless the entry is on the manifest with report-role provenance (align D1 / T2). Must run without a live cluster.

**Blocked by:** None.

**Status:** ready-for-agent

- [ ] Manifest lists at least the SSE latency family pins used in README / verification docs
- [ ] CI checker hard-fails on doc↔pin drift
- [ ] Undocumented uplift `%` on README-facing surfaces fails closed
- [ ] No live stack required for the pin checker to pass
- [ ] Tests cover match, drift, and uplift-ban cases
- [ ] Checker wired into CI (or documented equivalent gate)
