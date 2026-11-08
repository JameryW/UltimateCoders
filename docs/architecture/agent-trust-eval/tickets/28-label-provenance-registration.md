# 28 — Label provenance at registration (T28)

**What to build:** Stop a CV artifact from opening a reviewer kind on an unbacked
`n_labels` claim. `register_threshold(artifact, *, label_manifest=None)` cross-checks the
manifest when given, records how the kind was earned, and — under
`UC_REVIEWER_LABEL_PROVENANCE` (default off) — refuses a registration with no manifest at
all. `threshold_provenance(kind)` exposes `manifest` / `unverified`.

**Blocked by:** D12 (same decision; this closes its enablement edge)

**Status:** implemented (`python/ultimate_coders/agent/typed_review_thresholds.py`,
`python/ultimate_coders/agent/typed_review.py`; tests in
`tests/python/test_review_labels.py`)

- [x] Flag off: registration behaves exactly as before, recorded as `unverified`
- [x] Flag on: registration without a manifest is refused (kind stays closed)
- [x] Flag on + matching manifest: registered, recorded as `manifest`
- [x] A manifest that does not back the artifact is refused at registration too
