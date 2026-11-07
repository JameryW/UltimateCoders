# Decision Record Spec

> V8 Decision / StateOS P0: one attempt-scoped audit join for route + context
> identity + terminal verdict. No prompt content; persistence is best-effort.

---

## 1. Scope / Trigger

Modules: `python/ultimate_coders/decision_record.py`, Worker hooks in
`python/ultimate_coders/agent/worker.py`, Dashboard
`GET /dashboard/api/decisions` in `python/ultimate_coders/dashboard/app.py`.

Trigger: any change to Decision Record keying, start/terminal merge semantics,
digest algorithm, dashboard query contract, or worker call sites. Do **not**
store prompt / tool stdout content. Do **not** invent a second scheduler.
Governance ProactiveAudit remains its own store.

---

## 2. Signatures

```python
NAMESPACE = "decision_record"

def decision_key(task_id: str, subtask_id: str, attempt: int) -> str: ...
def context_digest(summary: dict) -> str: ...  # sha256(canonical JSON)

class DecisionRecordOutbox:
    def record_start(
        self, key: str, *, worker_id: str,
        route: dict | None = None,
        context_digest: str | None = None,
        context_key: str | None = None,
        governance: dict | None = None,
    ) -> dict: ...

    def record_terminal(
        self, key: str, *, success: bool,
        summary: str | None = None,
        error_class: str | None = None,
        oracle: dict | None = None,
        review: dict | None = None,
    ) -> dict: ...

    def history(self, task_id: str, subtask_id: str, *, limit: int = 100) -> list[dict]: ...
    def task_history(self, task_id: str, *, limit: int = 100) -> list[dict]: ...
    def total(self, task_id: str, *, subtask_id: str | None = None) -> int: ...
```

Dashboard: `GET /dashboard/api/decisions?task_id=&subtask_id=&limit=` →
`{decisions, count, total}`.

---

## 3. Contracts

1. **Key identity.** `task:subtask:attempt` where `attempt` is
   `dispatch_retry_count` (same as context manifest / checkpoint).
2. **Start facts.** Always `worker_id`. Omit `route` when identity. When a
   context manifest was compiled, store `context_digest` + `context_key`
   (pointer into `context_manifest`), never the prompt.
3. **Terminal facts.** Nested under `terminal` with `terminal_at`. Merge into
   the same key; always re-stamp `terminal_at`.
4. **Identical start.** No-op that keeps `recorded_at`. Different start
   replaces start fields, re-stamps `recorded_at`, **preserves** existing
   terminal.
5. **Best-effort.** Worker persistence failures log at debug and never fail
   the subtask.
6. **Digest.** `sha256(json.dumps(summary, sort_keys=True, separators=(",", ":")))`.
7. **Dashboard.** Auth required; `task_id` required (400 otherwise); task-wide
   scan sorts attempts numerically; unavailable → same `_unavailable` helper
   as context manifests.

---

## 4. Validation & Error Matrix

| Condition | Required behaviour |
|---|---|
| Re-dispatch attempt N+1 | New key; prior attempt untouched |
| Identity route | No `route` field on start record |
| No context compile | Omit digest/key |
| Persistence raises | Subtask result unchanged |
| Missing `task_id` on API | 400 |
| Runtime state down | 503-style unavailable JSON |

---

## 5. Good / Base / Bad Cases

- **Good:** start then terminal on one key; re-dispatch sibling key; digest
  stable across dict insertion order; dashboard lists three attempts sorted.
- **Base:** no `UC_ROUTE_TABLE` → events omit route and Decision Record omits
  route; default coding path unchanged aside from best-effort writes.
- **Bad:** storing prompt text; overwriting attempt 0 on re-dispatch; failing
  the subtask when SQLite is locked; terminal erase on start rewrite.

---

## 6. Tests Required

| Contract | Test |
|---|---|
| Key / digest / merge | `tests/python/test_decision_record.py` |
| Worker best-effort + early failure terminals | same |
| Dashboard auth / sort / unavailable | `tests/python/test_dashboard_decisions.py` |

Assertion points: no content keys beyond the allowed start/terminal schema;
`recorded_at` preserved on identical start; `terminal` survives start replace.

---

## 7. Wrong vs Correct

| Wrong | Correct |
|---|---|
| Key `task:subtask` only (like outcome outbox) | Include attempt — history per dispatch |
| Embed full context summary in Decision Record | Store digest + `context_key` pointer |
| Raise into the attempt on write failure | Catch, debug-log, continue |
| Flip `UC_GRAPH_SHADOW` in this change | Graph cutover is a separate human/ops ticket |
