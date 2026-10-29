# T21 grounding — docs numeric pin CI

Surveyed 2026-10-29 against `origin/main` (T19 landed; T20 PR open, independent of T21).

## SSE pin inventory (docs-facing)

| Value | Role | Surfaces |
|------:|------|----------|
| `6.394ms` | Steady max sample | `README.md`, `README.zh-CN.md`, `docs/live-feedback-and-metainfer-verification.md` |
| `55.712ms` | Burst max sample | same three |
| `2.260ms` | Steady highest-client p99 | verification doc table only |
| `55.270ms` | Burst highest-client p99 | verification doc table only |
| `200ms` | “every sample below” ceiling | verification doc prose |
| `300` events / `3` clients | measurement shape | README prose + verification table |

MVP pins the **latency family** numbers (ms values above). Event/client counts are optional context; include the five ms pins as the closed set so drift of the public latency story fails closed.

## Uplift ban

- `README.md` and `README.zh-CN.md` contain **zero** `%` today.
- Fail-closed: any `\d+(\.\d+)?\s*%` on those surfaces fails unless listed under `uplift_allowed` with `eval_role: report`.
- Manifest starts with `uplift_allowed: []`.

## Peer CI shape (`check-readme-ci-table`)

- Script exit code IS the verdict.
- Own workflow with closed `paths` (READMEs + workflows dir + script + tests).
- Sandbox mutation tests: copy inputs, mutate one axis, assert judgment substring, restore.
- Adding a workflow: bump prose count Eleven→Twelve / 十一→十二 and add a table row; otherwise `ci-readme-ci-table` fails.

## Approach A sketch

```yaml
# docs/numeric-pins.yaml
version: 1
pins:
  - id: sse-steady-max-ms
    family: sse-latency
    value: "6.394"
    unit: ms
    source: fixed-pin  # live refresh = T22
    surfaces:
      - path: README.md
        must_contain: "6.394ms"
      # ...
uplift_ban_surfaces:
  - README.md
  - README.zh-CN.md
uplift_allowed: []
```

Checker judgments (separate messages for ablation):

1. `manifest-load` — file exists and parses; non-empty pins
2. `surface-missing` — listed path absent
3. `pin-drift` — `must_contain` not in surface text
4. `uplift-undocumented` — `%` on ban surface not allowlisted
5. `uplift-provenance` — allowlist entry missing `eval_role: report`

## Test plan

* Repo green: checker exit 0 on real tree.
* Drift: mutate README `6.394ms` → `6.395ms` → fail `pin-drift`.
* Uplift ban: inject `40% uplift` into README → fail `uplift-undocumented`.
* Allowlist positive (optional): with a sandboxed manifest allowing a token + report role, matching `%` passes that judgment.
* No live NATS/Dashboard required.

## Out of scope

* Invoking `verify-sse-latency.py` against a stack (T22).
* GPU / BenchmarkRunner doc recompute.
* Pinning non-SSE numbers outside the manifest.
