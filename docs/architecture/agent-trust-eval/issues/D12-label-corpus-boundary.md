# D12 — Label corpus boundary (who labels, where it lives, leakage into report)

Status: decided
Decided: 2026-11-09

## Question

D5 decided typed reviewers need labelled data and that "no labels ⇒ that kind stays
closed", but the corpus itself has always been "fog": who may label, where labels are
stored, and how a label derived from the report hold-out is kept out. What decides the
corpus boundary, and what can code enforce about it?

## Decision

**Labels are human-authored and stored outside the repository. The repository holds a
manifest, not the corpus, and the manifest is checked against the CV artifact.**

1. **Who labels.** `labeler` names a human principal. An agent/worker/service principal
   may *propose* labels but never certify them — the manifest refuses a non-human
   labeler (D5's "no calibrated claim without human labels", applied at the identity).
2. **Where it lives.** `corpus_uri` is an external URI with a scheme (`s3://`,
   `https://`, `file://…`); a bare repo-relative path is refused, so a corpus cannot be
   accidentally committed and drift into the report set. Label *content* never enters
   the repository or `RuntimeState`.
3. **Leakage.** `source_eval_roles` lists the roles the labelled attempts came from and
   may only contain `search` / `select`. A label derived from the **report** hold-out
   is refused: calibrating a reviewer on the hold-out is the same leakage D11 closed
   for workloads, one level up.
4. **Evidence behind the count.** A threshold artifact may be loaded *with* its
   manifest. Then: same `kind`, same `n_labels`, and a structural floor
   `n_labels >= cv_folds * 2` (each fold can hold at least two labels). The floor is
   structural, not a calibration number; without a manifest the historical artifact
   check is unchanged.
5. **No new runtime surface.** Nothing here is read by the reviewer at decision time;
   it is the provenance gate for the artifact that already exists.

## Why

The CV artifact already claims `n_labels` and `cv_folds`; nothing ties that claim to a
corpus that exists. A manifest makes the claim checkable and makes the two decided
boundaries (human-only labels, no report-role sources) enforceable with a validator
instead of prose.

## Boundary

- No label storage, no schema for label rows, no upload tooling.
- No change to how a reviewer scores at decision time.
- No calibration numbers: the floor is `cv_folds * 2`, not a quality claim.

## Implementation ticket

T25: `review_labels.load_label_manifest` + `load_threshold_artifact(...,
label_manifest=...)` cross-checks + focused tests.
