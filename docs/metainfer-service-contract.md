# MetaInfer service contract `uc-metainfer/1`

The specification a MetaInfer deployment must satisfy before UC will run a
**mutating** job against it. Everything here is extracted from the code that
consumes it -- `python/ultimate_coders/inference/service_contract.py`,
`python/ultimate_coders/inference/adapter.py`,
`python/ultimate_coders/inference/reconcile.py` and
`scripts/verify-metainfer-release.py` -- not from prose. If this document and
those files disagree, the files win and this document is wrong.

## 1. Scope / Trigger

Code-spec depth is required here because this is an infra contract across a
process boundary: a third-party service must implement it, and the cost of a
vague field is a job that half-succeeds against someone's GPU.

**Status.** Stock upstream `HuangPuStar/MetaInfer` at the pinned revision
`b3f6505a11ab704ee1cfb68e9c1b2c13c95ac890` does **not** implement it. Verified
by two independent source extractions (a fresh checkout of that revision, and
the 2026-10-02 review's own dumps): zero occurrences of `/api/uc`, `quiescence`,
`workspace_probe`, `contract_version` or `uc-metainfer`. So implementing this is
the single remaining input to closing R8 -- it is not optional polish.

**What UC does without it.** Schema discovery is allowed; every **mutating**
POST fails closed first. That split is deliberate: read-only probes need no
safety guarantees, writes do.

**Why these four cannot be softened.** They are the only things standing between
a lost POST response and a rollback that deletes a live remote writer's work --
the R2 finding this whole task exists to fix. A service that "usually" stops its
writers has not satisfied this contract.

## 2. Signatures

Four endpoints, all under `/api/uc`, all JSON, all on the MetaInfer service.

| Method | Path | Required for |
|--------|------|--------------|
| `GET` | `/api/uc/contract` | every mutating job |
| `POST` | `/api/uc/workspaces/verify` | every mutating job |
| `GET` | `/api/uc/jobs/{remote_id}/quiescence` | every mutating completion / cancel |
| `GET` | `/api/uc/hardware` | GPU jobs (and the release gate) |

`{remote_id}` is the id the service returned when it accepted the job. UC
restricts it to `[A-Za-z0-9_-]+`.

## 3. Contracts

### `GET /api/uc/contract`

200 response fields:

| Field | Type | Constraint |
|-------|------|------------|
| `contract_version` | string | must equal `uc-metainfer/1` |
| `backend_id` | string | non-empty, **stable** across restarts and URL aliases |
| `revision` | string | non-empty; must equal `UC_METAINFER_REVISION` when that is set |
| `capabilities` | list of string | must contain `workspace_probe` **and** `quiescence` |

404 means "no UC contract", which UC reports as
`Mutating MetaInfer jobs require the UC workspace/stop contract`.
Any other non-200 is a transport/HTTP error.

`backend_id` is the resource identity: UC budgets concurrency and allocates
reservations against it, not against the URL. Two URL aliases with the same
`backend_id` are one budget; two `backend_id`s are two.

### `POST /api/uc/workspaces/verify`

Proves the service can see and write the same filesystem UC does, *before* a job
is created. Request fields:

| Field | Type | Meaning |
|-------|------|---------|
| `workspace` | string | absolute root the job will run in (already resolved by UC) |
| `read_path` | string | absolute path to a probe file UC has already written |
| `write_path` | string | absolute path the service must write |
| `write_token` | string | the exact content the service must write to `write_path` |

Response fields:

| Field | Type | Constraint |
|-------|------|------------|
| `read_sha256` | string | lowercase hex sha256 of the bytes at `read_path` |
| `writer_identity` | string | non-empty; identifies the writing process |

Required side effect: the service writes `write_token` to `write_path`. UC then
asserts the file exists and its content equals `write_token` exactly.

Any mismatch raises `DeploymentContractError: Worker and MetaInfer do not share
a writable workspace`. This check runs before launch, so a misconfigured shared
volume is caught before a remote job exists.

### `GET /api/uc/jobs/{remote_id}/quiescence`

Polled by UC until `writers_stopped` is `true`, with an overall 5s budget. That
budget is short on purpose: if the service cannot prove writers are gone
promptly, UC keeps the quarantine rather than guessing.

Response fields:

| Field | Type | Constraint |
|-------|------|------------|
| `contract_version` | string | must equal `uc-metainfer/1` |
| `task_id` | string | must equal the `{remote_id}` in the path |
| `backend_id` | string | must equal the contract's `backend_id` |
| `writers_stopped` | bool | must be `true` |
| `proof_kind` | string | one of `cgroup_empty`, `workspace_fenced` |
| `execution_scope` | string | non-empty; the scope the proof covers |
| `evidence_id` | string | non-empty; an id the operator can look up |

Failure raises `DeploymentContractError: Service has not proved all workspace
writers quiescent`.

> **Warning** These fields are all mandatory and all are load-bearing. In
> particular `proof_kind`, `execution_scope` and `evidence_id` are what make the
> proof *audit-able* rather than a boolean someone can set true. A service that
> answers `{"writers_stopped": true}` alone is rejected.

### `GET /api/uc/hardware`

Required for GPU work and by the release gate. Response fields:

| Field | Type | Constraint |
|-------|------|------------|
| `contract_version` | string | must equal `uc-metainfer/1` |
| `backend_id` | string | must equal the contract's `backend_id` |
| `revision` | string | must equal the expected revision when one is set |
| `devices` | list of object | non-empty; each has `model` |
| `runtime` | any | non-empty |
| `model_identity` | any | non-empty |

When the caller passes an expected model, at least one device's `model` must
contain it case-insensitively. 404 is reported as
`GPU tasks require the UC hardware identity contract`.

### Environment the UC side reads

| Key | Meaning |
|-----|---------|
| `UC_METAINFER_URL` | enables routing to the service |
| `UC_METAINFER_REVISION` | the required pin; when set, `revision` must match |
| `UC_METAINFER_BACKEND_ID` | operator's stable alias for the backend |
| `UC_METAINFER_MAX_CONCURRENCY` | shared backend budget |
| `UC_METAINFER_TASK_TYPES` | shared backend policy |

## 4. Validation & Error Matrix

| Condition | Result |
|-----------|--------|
| `/api/uc/contract` 404 | `REJECTED`, exit 2 -- no UC contract |
| `contract_version` != `uc-metainfer/1` | `DeploymentContractError`, contract incompatible |
| `revision` != `UC_METAINFER_REVISION` | `DeploymentContractError`, revision is not the configured pin |
| `capabilities` missing `workspace_probe` or `quiescence` | `DeploymentContractError`, contract incompatible |
| workspace probe byte/token mismatch | `DeploymentContractError`, workspace not shared |
| `writers_stopped` not `true` within 5s | `DeploymentContractError`, quiescence not proved |
| quiescence `task_id` / `backend_id` mismatch | `DeploymentContractError`, quiescence not proved |
| GPU `devices` empty or `model_identity` missing | `DeploymentContractError`, GPU identity incomplete |
| expected model absent from `devices` | `DeploymentContractError`, GPU model does not match |
| service unreachable / non-200 transport | `REJECTED`, exit 3 -- distinct from a contract verdict |

The release gate maps contract verdicts to exit 2 and transport failures to
exit 3 so a run log distinguishes "your service is missing the contract" from
"the service is down". Neither writes an evidence file.

## 5. Good / Base / Bad Cases

- **Good**: all four endpoints answer as specified, the probe round-trips with
  matching sha256 and written token, quiescence returns a full auditable proof
  within 5s, and the gate writes evidence.
- **Base**: contract and workspace prove out, but the service has no GPU. The
  gate without `--require-gpu` still passes; GPU claims remain unmade.
- **Bad**: a service answers `{"writers_stopped": true}` with no
  `proof_kind`/`execution_scope`/`evidence_id`. It is rejected -- correctly --
  and that is the behaviour to preserve, not to relax.

## 6. Tests Required

Pinned in `tests/python/test_inference_architecture.py`:

- `test_stop_acknowledgement_and_finished_metadata_are_not_stop_proofs` -- an
  `ok`/`finished`/`signal_sent` reply is not a quiescence proof.
- `test_release_gate_rejects_a_contractless_service_with_a_reason_not_a_traceback`
  -- a service shaped like stock upstream is a **named** refusal with no
  traceback and no evidence file.
- `test_release_gate_names_an_unreachable_service_separately` -- transport
  failure is reported apart from a contract verdict.

A new contract field must arrive with (a) a validator in `service_contract.py`
and (b) a test that proves a service omitting it is rejected.

## 7. Wrong vs Correct

### Wrong

Treat the contract as "nice to have" and degrade a mutating job to best effort
when `/api/uc/contract` is missing, on the grounds that the dispatch half of the
adapter already works against stock upstream.

### Correct

Fail closed. The dispatch routes (`/api/sys-shell/...`) are compatible with
stock upstream, and that is exactly why the safety contract must not be assumed:
an adapter that *appears* to work end to end will POST into a workspace whose
writers UC cannot prove stopped, and the rollback path will then delete live
work. The contract is the thing that makes cancellation and rollback safe, so
its absence is the answer "not yet", never "proceed carefully".
