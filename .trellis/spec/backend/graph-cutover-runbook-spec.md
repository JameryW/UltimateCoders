# Graph Cutover Runbook Spec (V8 P4)

> D6 / D10: ops runbook + dry-run readiness only. Defaults stay off.
> Never flips `UC_GRAPH_SHADOW` in Compose/images; never auto graph-only from DB URL.

---

## 1. Scope / Trigger

Modules: `docs/architecture/graph-cutover-runbook.md`,
`scripts/check-graph-cutover-readiness.py`,
`tests/python/test_graph_cutover_readiness.py`,
gate donor `crates/uc-grpc-server/src/main.rs` (`resolve_graph_shadow_choice`).

Trigger: changes to the exact `on` gate contract, readiness hard blockers,
runbook enable/rollback steps, or docs that claim default-on. Do **not** flip
Compose defaults. Do **not** implement true replan execute (T18).

---

## 2. Signatures

```python
SHADOW_ENV = "UC_GRAPH_SHADOW"
DATABASE_ENV = "UC_DATABASE_URL"
RUNBOOK_REL = "docs/architecture/graph-cutover-runbook.md"

def resolve_graph_shadow_choice(shadow: str | None) -> bool: ...  # exact "on"
def database_url_is_set(value: str | None) -> bool: ...
def hard_blockers(env: Mapping[str, str | None]) -> list[str]: ...
def assemble_checklist(env, *, runbook_rel=..., repo=...) -> list[str]: ...
```

Rust: `resolve_graph_shadow_choice(Option<&str>) -> bool` — `matches!(shadow, Some("on"))`.

---

## 3. Contracts

1. Gate on iff value is exact `"on"` (case-sensitive); default off.
2. Dry-run never assigns `os.environ` / never writes Compose.
3. Hard fail when shadow is on and `UC_DATABASE_URL` is unset/empty.
4. Checklist reports shadow on/off, DB URL set/unset, and runbook path.
5. Runbook states Compose/image defaults remain off; cites durable-runtime
   upgrade notes for gateway/worker lockstep.

---

## 4. Validation & Error Matrix

| Condition | Behaviour |
|---|---|
| Shadow unset / non-`on` | Checklist off; exit 0 (unless other blockers) |
| Shadow `on` + DB URL set | Checklist on; exit 0 |
| Shadow `on` + DB URL empty | Hard blocker; exit 1 |
| Script invoked | No env mutation; no Compose rewrite |

---

## 5. Good / Base / Bad

- **Good:** ops enables exact `on` after dry-run green + authorization
- **Base:** defaults off; dry-run reports off without changing anything
- **Bad:** flipping Compose defaults; inferring graph-only from DB URL; enabling shadow from the check script

---

## 6. Tests

| Contract | Test |
|---|---|
| Exact `on` gate | `tests/python/test_graph_cutover_readiness.py` |
| Hard-fail / non-mutation | same |
| Rust gate matrix | `uc-grpc-server` `graph_shadow_only_enables_on_exact_value` |

---

## 7. Wrong vs Correct

| Wrong | Correct |
|---|---|
| Treat `true` / `1` / `On` as enabled | Exact `"on"` only |
| Check script sets `UC_GRAPH_SHADOW=on` | Report only; human sets env |
| Graph-only because DB URL present | Shadow flag + storage feature + ops auth |
