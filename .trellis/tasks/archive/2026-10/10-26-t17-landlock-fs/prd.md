# T17: Landlock FS enforcement (agent child)

## Goal

Add **opt-in** Landlock filesystem restriction for the agent child behind
`UC_LANDLOCK_ENFORCE` (default off), mirroring the T15/T16 NetworkMode child-only
wire pattern. When enforce is on and Landlock cannot be applied, fail closed.
Must not regress NetworkMode; must not touch worker NATS/Gateway.

## Source

- Ticket: `docs/architecture/agent-trust-eval/tickets/17-landlock-fs-later.md`
- Spec: `docs/architecture/agent-trust-eval/spec-p2.md` (Landlock deferred from Seam H)
- Decision: D8 — NetworkMode first; Landlock FS later (this ticket)
- Prior: T15 NetworkMode enforce; T16 default-offline allowlist
- Research: [`research/grounding.md`](research/grounding.md)

## What I already know

* T15/T16 shipped `network_mode.py`: enforce default off; plan + argv wrap;
  injectable applicator; `_execute_subprocess` choke point; engine bypass closed
  when network enforce on; Restricted = policy preflight (`allowlist_preflight`).
* `SandboxConfig` has `project_path` / `working_dir` / `network` — no FS allowlist
  field. Production spawn is Python `_execute_subprocess` (Rust SubprocessSandbox
  still has no FS/network isolation).
* This VM: Linux 6.12, Landlock ABI 6 via ctypes syscall; `landlockctl` absent;
  `PR_SET_NO_NEW_PRIVS` required before `landlock_restrict_self`.
* Nested probe: outer `unshare -Urn` + in-child Landlock → FS deny outside RW
  root **and** net `ENETUNREACH`.
* `docs/dsec-sandbox.md` covers env allowlist posture; Landlock is orthogonal
  (same child choke point).

## Assumptions (temporary)

* MVP FS policy is derived from spawn cwd / `working_dir` (worktree) + RO `/`
  + RW `/tmp` — not a new SandboxConfig schema.
* Syscall numbers documented for Linux x86_64 / aarch64; non-Linux → unavailable.
* Injectable applicator is enough for non-Linux unit tests; live probes skip.

## Decisions locked

* **Approach A (Recommended):** small `landlock.py` + argv exec helper +
  injectable applicator; wire **after** NetworkMode argv wrap in
  `_execute_subprocess`; `UC_LANDLOCK_ENFORCE` default off; fail closed when
  enforce on but Landlock unavailable; FS = RO `/` + RW worktree + RW `/tmp`
  (optional `UC_LANDLOCK_EXTRA_RW` / `EXTRA_RO` escape hatches).
* Child-only; never `restrict_self` the worker parent.
* Close Rust `execute_in_sandbox` bypass when landlock enforce is on (same
  honesty as T15).
* Do not regress NetworkMode; do not change T16 allowlist semantics.

## Requirements (evolving)

* Add `UC_LANDLOCK_ENFORCE` (default off; same truthy parser as other UC flags).
* New module `python/ultimate_coders/agent/landlock.py`:
  `LandlockError`, `LandlockApplyPlan`, `landlock_enforce_enabled`,
  `platform_supports_landlock`, `plan_landlock`, `apply_landlock_argv`,
  `set_landlock_applicator` / reset helpers.
* Exec helper (module `__main__` or `landlock_exec`): no-new-privs → ruleset →
  restrict_self → exec remaining argv (ctypes Landlock ABI; no `landlockctl`).
* Wire in `SandboxManager._execute_subprocess` **after** network plan/argv;
  refuse spawn with clear stderr on `LandlockError`.
* When landlock enforce on, do not use `engine.execute_in_sandbox`.
* FS MVP: RO beneath `/`; RW beneath effective working_dir; RW beneath `/tmp`
  (and absolute `TMPDIR` if set). Missing RW root under enforce → fail closed.
* Tests: unit + injectable; live Linux probe with skip matrix; dual-flag
  NetworkMode regression probe when both enforces on.
* Spec: add `.trellis/spec/backend/landlock-fs-spec.md` (cross-link from
  network-mode-spec “Landlock is T17” note).

## Acceptance Criteria (evolving)

* [ ] `UC_LANDLOCK_ENFORCE` defaults off; flag off → spawn FS behaviour unchanged
* [ ] Enforce on + Landlock available → agent child cannot write outside RW roots
* [ ] Enforce on + Landlock unavailable → fail closed (no silent unrestricted FS)
* [ ] Does not regress NetworkMode (`none` still blocks net; Restricted preflight intact)
* [ ] Both enforces on → FS restrict **and** network mode still apply
* [ ] Non-Linux / no-Landlock CI: unit tests green; live probes skipped or documented
* [ ] Injectable applicator for tests; small `landlock.py` module (not bloating network_mode)
* [ ] Feature flag remains default off

## Definition of Done (team quality bar)

* Tests added/updated (unit + skip-gated live probe; NetworkMode suites still green)
* Lint / typecheck / CI green
* `.trellis/spec/backend/landlock-fs-spec.md` (or equivalent) documents contracts
* Rollout: flag default off; rollback = leave flag unset

## Research References

* [`research/grounding.md`](research/grounding.md) — wire map, probe evidence, FS policy, approaches, regression guards

## Research Notes

### Constraints from our repo

* Child-only cut (D8); T15 argv-wrap + injectable pattern is the template.
* Prefer no new runtime packages; `landlockctl` / bwrap absent on this image / stock GHA.
* asyncio spawn path has no reliable `preexec_fn` → helper-exec wrap required.
* Must compose with `unshare` netns wrap without breaking either.

### Feasible approaches

**Approach A: `landlock.py` + ctypes helper + injectable applicator** (Recommended)

* How: sibling module to `network_mode.py`; plan/apply argv; wire after network;
  RO `/` + RW worktree + RW `/tmp`; fail closed; skip matrix for live probes.
* Pros: Small; mirrors T15/T16; probed nesting works; CI-testable via inject;
  default off.
* Cons: Coarse RO `/` (reads still wide); arch syscall tables to maintain.

**Approach B: external `landlockctl` / bwrap**

* How: shell out to distro tools. Pros: less ctypes. Cons: tools missing;
  weaker CI; extra deps.

**Approach C: SandboxConfig FS allowlist schema + Landlock**

* How: new config fields / Rust mirror. Pros: flexible. Cons: over-scope vs
  ticket; delays MVP.

## Decision (ADR-lite)

**Context**: T17 must ship optional agent-child FS restriction after NetworkMode
without regressing T15/T16 or requiring missing distro tools.  
**Decision (proposed)**: Approach A — `landlock.py` + ctypes argv helper +
injectable applicator; apply after network wrap; FS = RO `/` + RW worktree +
RW `/tmp`; fail closed when enforce on but unavailable.  
**Consequences**: Operators opt in via `UC_LANDLOCK_ENFORCE`; non-Linux enforce
refuses start; finer path allowlists / Rust Landlock deferred.

## Out of Scope (explicit)

* Enabling Landlock by default
* seccomp-bpf
* Kernel selective egress / nft (T16 follow-up)
* Implementing Landlock inside Rust `SubprocessSandbox` (bypass closed instead)
* New `SandboxConfig` FS allowlist schema / per-agent path policy tables
* macOS sandbox-exec / Windows equivalents beyond fail-closed under enforce
* Changing NetworkMode defaults or Restricted allowlist semantics

## Technical Approach (recommended)

1. Add `landlock.py` (flag, plan, apply, probe, injectables, error type).
2. Add exec entry that applies Landlock then `os.execvp`.
3. Wire `_execute_subprocess` after `apply_network_argv`; extend engine-bypass
   guard for landlock enforce.
4. Tests: `tests/python/test_landlock_enforce.py` + keep network suites green.
5. Spec: `landlock-fs-spec.md`; one-line cross-link from `network-mode-spec.md`.

## Technical Notes

* Key files: `network_mode.py`, `sandbox.py`, new `landlock.py`,
  `tests/python/test_network_mode_enforce.py` (regression),
  ticket 17 / D8 / spec-p2
* Probe: ABI 6; NNP required; unshare+landlock compose
* Branch naming: `cursor/t17-landlock-fs-7c92`
