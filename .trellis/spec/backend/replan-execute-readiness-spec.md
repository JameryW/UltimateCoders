# Replan Execute Readiness Spec (V8 T18d)

> D14: ops runbook + dry-run readiness only. True execute stays deferred.
> Never invents/flips an execute env gate; never bumps digest; never calls
> `admit_execution`; never mutates Compose.

---

## 1. Scope / Trigger

Modules: `docs/architecture/replan-execute-readiness-runbook.md`,
`scripts/check-replan-execute-readiness.py`,
`tests/python/test_replan_execute_readiness.py`.

Trigger: changes to precursor-flag truthy reporting, readiness hard blockers,
runbook deferral/rollback wording, or docs that claim execute is enabled. Do
**not** add `UC_REPLAN_EXECUTE_ENFORCE`. Do **not** call `admit_execution`.

---

## 2. Signatures

```python
PROPOSAL_ENV = "UC_REPLAN_PROPOSAL_ENFORCE"
CHECKLIST_ENV = "UC_REPLAN_EXECUTE_CHECKLIST_ENFORCE"
ACCOUNTING_ENV = "UC_REPLAN_ACCOUNTING_ENFORCE"
COMPLETION_ENV = "UC_COMPLETION_CONDITIONS_ENFORCE"
RUNBOOK_REL = "docs/architecture/replan-execute-readiness-runbook.md"
TRUTHY_TOKENS = frozenset({"1", "true", "yes", "on"})

def enforce_flag_enabled(raw: str | None) -> bool: ...
def hard_blockers(env, *, runbook_rel=..., repo=...) -> list[str]: ...
def assemble_checklist(env, *, runbook_rel=..., repo=...) -> list[str]: ...
```

Truthy contract matches Python `_TRUTHY_ENV` helpers and Rust
`enforce_flag_enabled` (trim + ascii-lowercase).

---

## 3. Contracts

1. Flag reporting uses shared truthy tokens; default off when unset/empty/other.
2. Dry-run never assigns `os.environ` / never writes Compose.
3. Hard fail when the readiness runbook file is missing.
4. Checklist reports T18a–c + completion_conditions flag states and runbook path.
5. Runbook states execute remains deferred; cites durable-runtime lockstep and
   trust-eval ticket 18; never enables execute / digest bump / `admit_execution`.

---

## 4. Validation & Error Matrix

| Condition | Behaviour |
|---|---|
| Runbook present | Checklist present; exit 0 (flags advisory) |
| Runbook missing | Hard blocker; exit 1 |
| Precursor flags on/off | Reported only; never a hard blocker |
| Script invoked | No env mutation; no Compose rewrite |

---

## 5. Good / Base / Bad

- **Good:** ops records dry-run evidence before a later explicit execute decision
- **Base:** defaults off; dry-run reports off without changing anything
- **Bad:** inventing execute env gate; enabling execute from the check script; silent DAG rewrite

---

## 6. Tests

| Contract | Test |
|---|---|
| Truthy gate | `tests/python/test_replan_execute_readiness.py` |
| Hard-fail / non-mutation | same |
| Runbook deferral pins | same (`test_runbook_exists_for_pointer`) |

---

## 7. Wrong vs Correct

| Wrong | Correct |
|---|---|
| Treat readiness as execute authorization | Readiness ≠ execute (ticket 18) |
| Check script sets precursor / execute env | Report only; human sets env |
| Hard-fail because precursors are off | Precursors advisory; missing runbook hard-fails |
