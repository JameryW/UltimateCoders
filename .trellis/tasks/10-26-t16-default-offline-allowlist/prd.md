# T16: default-toward-offline + LLM allowlist

## Goal

With T15 NetworkMode enforcement present, add **opt-in** product policy that defaults agent sandboxes toward offline/`restricted` and registers a Restricted applicator that consults an **explicit** LLM/API host allowlist. Worker NATS/Gateway stay off the child allowlist. Never silently change Full deployments when flags are off.

## Source

- Ticket: `docs/architecture/agent-trust-eval/tickets/16-default-offline-allowlist.md`
- Spec: `docs/architecture/agent-trust-eval/spec-p2.md` (Seam H)
- Decision: D8 — default toward offline + LLM allowlist; Landlock later (T17)
- Prior: T15 NetworkMode enforce (`UC_NETWORK_MODE_ENFORCE`, Restricted applicator seam)
- Research: [`research/grounding.md`](research/grounding.md)

## What I already know

* T15 shipped `network_mode.py`: enforce flag default off; `none` → rootless unshare netns; `full` → noop; `restricted` → fail closed until `set_restricted_applicator`; wire in `_execute_subprocess`; engine bypass closed when enforce on.
* `SandboxConfig.network` and `DSEC_DEFAULTS["network"]` remain **Full**. `nats_worker` builds `SandboxConfig` without a network override.
* Restricted selective egress (nft/iptables/bwrap/slirp) is **not** available on stock GitHub runners / this image; unshare empty-netns **is** available and blocks all child egress.
* Child env already inherits `UC_*` and proxy vars (T11); that is env allowlisting, not network host allowlisting. Control-plane sockets stay on the worker process.
* P2 flags use truthy `1|true|yes|on` and default **off**.

## Assumptions (temporary)

* MVP Restricted is **policy fail-closed** (`backend=allowlist_preflight`, noop argv) with an honest label — kernel “others blocked” deferred to a follow-up image/proxy ticket, not claimed as shipped.
* Default-offline product path targets **Restricted** (LLM-capable under allowlist); hard **None** remains available via explicit `network=none` or optional `UC_NETWORK_DEFAULT_MODE=none`.
* Registering the Restricted applicator at `SandboxManager` init is enough; no need to touch worker NATS subscribe paths.

## Decisions locked

* **Approach A (Recommended):** `UC_NETWORK_DEFAULT_RESTRICTED` + `UC_NETWORK_ALLOWLIST` + allowlist preflight applicator; wire `set_restricted_applicator` at sandbox init; dataclass/static Full defaults unchanged unless flag on.
* Never silently change `SandboxConfig.network` / dsec Full default when flags are off.
* Do not put NATS/Gateway hosts on the child network allowlist requirement.
* Do not invent Landlock (T17).

## Requirements (evolving)

* Add `UC_NETWORK_DEFAULT_RESTRICTED` (default off). When on, construction helpers resolve default `network` to `restricted`.
* Add `UC_NETWORK_ALLOWLIST` (empty default). Restricted applicator parses it; empty under enforce+Restricted → fail closed.
* Implement Restricted applicator: normalize hosts; preflight intended LLM/API BASE_URL hosts ⊆ allowlist; return `NetworkApplyPlan(mode=restricted, backend=allowlist_preflight)`.
* Call `set_restricted_applicator` from `SandboxManager.__init__` (clearable in tests via existing reset helpers).
* Flag off → prior Full defaults and T15 spawn behaviour preserved.
* Document honesty: OS selective egress not in this MVP; backend name must not imply nft/proxy.
* Optional same-PR: `UC_NETWORK_DEFAULT_MODE=none|restricted|full` as superseding resolver (document alias).

## Acceptance Criteria (evolving)

* [x] `UC_NETWORK_DEFAULT_RESTRICTED` defaults off; Full defaults unchanged
* [x] Flag on → resolved sandbox/dsec/worker default network is `restricted` (explicit overrides still win)
* [x] Enforce on + Restricted + empty allowlist → fail closed (no silent Full)
* [x] Enforce on + Restricted + allowlisted intended host → plan succeeds (`allowlist_preflight`)
* [x] Enforce on + Restricted + non-allowlisted intended host → fail closed
* [x] NATS/Gateway hosts are not required on the child allowlist for spawn success
* [x] `set_restricted_applicator` registered at sandbox init; tests cover allow/deny
* [x] Feature flags default off; no Landlock / no kernel fake-Restricted-as-empty-netns marketed as allowlist

## Definition of Done (team quality bar)

* Tests added/updated (unit allow/deny + default-flag matrix; no root required)
* Lint / typecheck / CI green
* Extend `.trellis/spec/backend/network-mode-spec.md` (or add allowlist section) with T16 contracts + honesty note
* Rollout: all new flags remain default off

## Research References

* [`research/grounding.md`](research/grounding.md) — flags, applicator options, runner honesty, wire map, test matrix

## Research Notes

### Constraints from our repo

* Child-only cut (D8 / Seam H); T15 applicator seam already exists.
* Prefer no new runtime packages; GHA has unshare but not nft/slirp/bwrap.
* Env allowlist (`UC_*`) ≠ network host allowlist.

### Feasible approaches

**Approach A: default-restricted flag + allowlist preflight applicator** (Recommended)

* How: `UC_NETWORK_DEFAULT_RESTRICTED` + `UC_NETWORK_ALLOWLIST`; resolver for construction sites; Restricted applicator preflight fail-closed; `backend=allowlist_preflight` noop argv; register at `SandboxManager.__init__`.
* Pros: CI-testable without root; plugs T15 seam; honest backend label; preserves Full when flags off; NATS/Gateway stay worker-side.
* Cons: Does not kernel-block non-allowlisted hosts; Seam H OS deny deferred (must be documented).

**Approach B: Restricted ≡ None (+ documented exception)**

* How: applicator returns unshare plan; allowlist informational or forces fail closed when non-empty.
* Pros: Real offline. Cons: LLM unreachable under Restricted; weak ticket fit.

**Approach C: forced allowlist proxy / nft**

* How: parent proxy or netns nft. Pros: true Seam H. Cons: ops/image scope; not stock GHA MVP.

## Decision (ADR-lite)

**Context**: T16 must ship default-offline + LLM allowlist behind flags without fake safety, on runners that lack selective-egress tools.  
**Decision (proposed)**: Approach A — opt-in `UC_NETWORK_DEFAULT_RESTRICTED` + `UC_NETWORK_ALLOWLIST` with policy preflight Restricted applicator; register at sandbox init; document OS selective egress as follow-up.  
**Consequences**: Operators who need kernel deny must wait for a proxy/nft follow-up or use `network=none`; product defaults stay Full until they opt in.

## Out of Scope (explicit)

* Landlock FS / `UC_LANDLOCK_ENFORCE` (T17)
* Kernel nft/iptables/bwrap/slirp selective egress (follow-up)
* Cutting NATS/Gateway on the worker process
* Silently changing Full defaults with flags off
* Reviving DockerSandbox
* Hardcoding a non-empty production allowlist

## Technical Approach (recommended)

1. Helpers: `network_default_restricted_enabled()`, `parse_network_allowlist()`, `resolve_network_default()`, `plan_restricted_allowlist()` (+ host extract from BASE_URL envs).
2. `SandboxManager.__init__`: `set_restricted_applicator(plan_restricted_allowlist)` (or thin wrapper).
3. Construction sites: `nats_worker`, `dsec_sandbox_config` (and any Worker default) use `resolve_network_default()` when caller does not pass `network=`.
4. Tests: new `tests/python/test_network_allowlist.py` (or extend enforce tests) — flag/default matrix + allow/deny preflight; reset applicator state in fixtures.
5. Spec: extend `network-mode-spec.md` with T16 flags, applicator contract, honesty note.

## Decisions locked (cont.)

* Document OS selective-egress deferral in `network-mode-spec.md` and a one-line note on ticket 16 when marking implemented.

## Technical Notes

* Key files: `network_mode.py`, `sandbox.py`, `sandbox_dsec.py`, `nats_worker.py`, `tests/python/test_network_mode_enforce.py`, `.trellis/spec/backend/network-mode-spec.md`
* Probe evidence: unshare netns blocks child egress here; nft/slirp/bwrap absent
* Related T15 archive PRD/research for seam continuity
