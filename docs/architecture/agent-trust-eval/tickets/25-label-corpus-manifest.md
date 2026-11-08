# 25 — Label corpus manifest + artifact cross-check (T25)

**What to build:** `python/ultimate_coders/agent/review_labels.py` (`LabelManifest` + `load_label_manifest`)
and an optional `label_manifest=` argument on
`typed_review_thresholds.load_threshold_artifact` — per [spec-p5.md](../spec-p5.md).
The repository holds provenance, never label content.

**Blocked by:** D12

**Status:** implemented (`python/ultimate_coders/agent/review_labels.py`,
`python/ultimate_coders/agent/typed_review_thresholds.py`; tests in
`tests/python/test_review_labels.py`)

- [x] Manifest validation: schema, kind, human labeler, URI scheme, role subset
- [x] `report` source role refused; `search`/`select` accepted
- [x] Artifact cross-checks: kind, `n_labels`, structural floor `cv_folds * 2`
- [x] Manifest-less artifact loading unchanged
