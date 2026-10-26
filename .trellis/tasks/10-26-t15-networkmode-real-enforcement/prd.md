# T15: NetworkMode real enforcement

## Goal

Make `NetworkMode` (`none` / `restricted` / `full`) real at **agent-subprocess** spawn when `UC_NETWORK_MODE_ENFORCE` is on, so operators cannot believe they are offline when they are not. Fail closed if the platform cannot apply the mode. Worker NATS/Gateway stay untouched. Landlock (T17) and default-offline / LLM allowlist product policy (T16) stay out of scope.

## Source

- Ticket: `docs/architecture/agent-trust-eval/tickets/15-networkmode-real-enforcement.md`
- Spec: `docs/architecture/agent-trust-eval/spec-p2.md` (Seam H)
- Decision: D8 — NetworkMode first; Landlock FS later
- Research: [`research/grounding.md`](research/grounding.md)

## What I already know

* Python `NetworkMode` + `SandboxConfig.network` default **Full**; Rust enum mirrors; both are config theatre today.
* Production spawn choke point: `SandboxManager._execute_subprocess` (also used by `execute_decompose`). Optional Rust `engine.execute_in_sandbox` bypasses that path when no cancel_file.
* Isolation today: **T11 env allowlist only**. `SubprocessSandbox` docs say no FS/network isolation; `apply_unix_limits` is empty; `DockerSandbox` removed.
* `config.network` is never read at spawn (Python or Rust execute).
* Rootless Linux probe: `unshare --user --map-root-user --net -- <cmd>` works here and blocks child egress (`ENETUNREACH`); bare `unshare --net` fails without privileges. No bwrap/nft/slirp on this VM.
* P2 flags use truthy `1|true|yes|on` and default **off** (`typed_review`, `stall_redispatch`, etc.).

## Assumptions (temporary)

* Python-first vertical slice is enough for T15 MVP; Rust parity is thin follow-up or same-PR only if needed to close the `execute_in_sandbox` bypass.
* Restricted host allowlist **content** is T16; T15 only needs Restricted to be enforceable or fail closed (no silent Full).
* Live egress probes skip when userns/unshare unavailable; unit tests use an injectable applicator.

## Decisions locked

* **Restricted under enforce (T15):** fail closed until an allowlist applicator is registered (T16). Never silent Full; never pretend Restricted works with empty allowlist.
* **Approach A:** mockable planner + `unshare -Urn` argv wrap for `none`; `full` noop; wire at `_execute_subprocess`.

## Requirements (evolving)

* Add `UC_NETWORK_MODE_ENFORCE` (default off) + helpers in a small module (e.g. `network_mode.py`).
* When enforce on, apply `SandboxConfig.network` at agent child spawn only (`none` / `restricted` / `full`).
* Fail closed if enforce on and mode cannot be applied (unsupported OS, missing unshare, Restricted without backend) — never silent Full.
* Flag off → today’s spawn behaviour unchanged.
* Worker process control-plane (NATS / Gateway) not wrapped / not cut.
* Do not invent Landlock or default-offline allowlist product policy.

## Acceptance Criteria (evolving)

* [x] Flag defaults off; spawn behaviour compatible with today
* [x] Flag on + `none` → child cannot reach external hosts (live probe or documented skip)
* [x] Flag on + `full` → child networking unchanged (noop plan)
* [x] Flag on + `restricted` → enforceable path or fail closed (per decision); never silent Full
* [x] Unsupported platform + enforce on → refuse start
* [x] Worker NATS/Gateway path unaffected by child wrap
* [x] Unit tests cover flag matrix + injectable applicator without root
* [x] No Landlock / no T16 default-offline policy in this change

## Definition of Done (team quality bar)

* Tests added/updated (unit + optional Linux live probe with skip)
* Lint / typecheck / CI green
* Backend spec note or new `network-mode-spec.md` if contracts are non-obvious
* Rollout: flag remains default off

## Research References

* [`research/grounding.md`](research/grounding.md) — enums, spawn paths, isolation today, Linux options, API shape, test matrix

## Research Notes

### Constraints from our repo

* Child-only cut (D8 / Seam H); env allowlist already at `_execute_subprocess`.
* Prefer argv wrap over `preexec_fn` (asyncio spawn).
* Avoid new runtime deps (bwrap not in CI image).

### Feasible approaches

**Approach A: mockable planner + unshare argv wrap** (Recommended)

* How: `network_mode.py` plans argv prefix; wire in `_execute_subprocess`; `none` → `unshare -Urn`; `full` → noop; `restricted` → fail closed or registered applicator; inject applicator in tests.
* Pros: rootless CI path for None; matches fail-closed flag culture; no new packages.
* Cons: Restricted incomplete until T16; Windows enforce → fail closed.

**Approach B: seccomp-bpf None**

* Heavier; Restricted still open; weaker fit for T15 vertical slice.

**Approach C: require bubblewrap**

* Ops/image dependency; not on current runners.

## Decision (ADR-lite)

**Context**: Need real NetworkMode without Landlock or T16 allowlist policy; must work in GitHub CI without root when possible.  
**Decision (proposed)**: Approach A — Python `network_mode` helpers + wire at `_execute_subprocess`; unshare userns+netns for None; Full noop; Restricted fail-closed-or-hook; flag default off.  
**Consequences**: T16 plugs Restricted allowlist into the same applicator seam; T17 Landlock stays separate; Rust engine path must not bypass enforce silently.

## Out of Scope (explicit)

* Landlock FS (`UC_LANDLOCK_ENFORCE`, T17)
* Default-toward-offline / shipping LLM host allowlist product policy (T16)
* Cutting network on the whole worker / container
* Reviving DockerSandbox
* Changing SandboxConfig default away from Full (that is T16 territory)

## Technical Approach (recommended)

1. Module `python/ultimate_coders/agent/network_mode.py`: flag helper, normalize, `plan_network_mode`, `apply_network_argv`, injectable applicator, `platform_supports_unshare_netns`.
2. Wire in `_execute_subprocess` after `build_child_env`; close `execute_in_sandbox` bypass when enforce on (force Python path or pass mode into Rust).
3. Tests: `tests/python/test_network_mode.py` — flag matrix + mock applicator + optional live None probe with skip.

## Technical Notes

* Key files: `sandbox.py` (`_execute_subprocess`, `NetworkMode`), `inference/process.py` (`spawn_command`), `crates/uc-engine/src/sandbox/{mod,subprocess}.rs`
* Ticket AC + Seam H testing decisions in spec-p2
* Probe evidence: `unshare --user --map-root-user --net` → child `ENETUNREACH` on this agent VM
