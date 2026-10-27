# T17 grounding — Landlock FS enforcement (D8 step 3)

Medium thoroughness. Grounded on post-T15/T16 code in `/workspace`. Do **not**
regress NetworkMode. Do **not** enable Landlock by default.

## 1. Ticket / D8 / Seam H requirements

**Ticket 17** (`docs/architecture/agent-trust-eval/tickets/17-landlock-fs-later.md`):

- Optional Landlock (or equivalent) FS restriction for the **agent child**
- Flag `UC_LANDLOCK_ENFORCE` **default off**
- Enforce on + unavailable platform → **fail closed**
- Must not regress NetworkMode child enforcement
- Tests **or** documented skip matrix for non-Linux CI
- Blocked by 15; preferably after 16 (both landed)

**D8**: NetworkMode first; Landlock FS as a later same-map ticket. Child-only
cut (worker NATS/Gateway untouched).

**Seam H / spec-p2**: Landlock was explicitly deferred from NetworkMode tickets;
T17 is that deferred FS slice. No Seam H network AC changes required beyond
“does not regress.”

## 2. NetworkMode wire pattern to mirror

### Module (`python/ultimate_coders/agent/network_mode.py`)

| Piece | Role |
|-------|------|
| `UC_NETWORK_MODE_ENFORCE` | Default off; truthy `1\|true\|yes\|on` |
| `NetworkApplyPlan` | `mode`, `argv_prefix`, `backend` |
| `plan_network_mode` / `apply_network_argv` | Pure plan + argv wrap |
| `set_network_mode_applicator` | Injectable for tests |
| `NetworkModeError` | Fail-closed signal |
| `platform_supports_unshare_netns` | Probe + skip matrix |

### Sandbox choke point (`sandbox.py` `_execute_subprocess`)

1. T11 env allowlist via `build_child_env`
2. `plan_network_mode(self.config.network)` → catch `NetworkModeError` →
   `ExecResult` refuse
3. `apply_network_argv(command, args, net_plan)`
4. Spawn (`asyncio.create_subprocess_exec` or `spawn_command`)

Also: when network enforce is on, `execute()` **must not** take
`engine.execute_in_sandbox` (Rust `SubprocessSandbox` does not apply
NetworkMode → would silently run Full).

T16 registers Restricted applicator at `SandboxManager.__init__`; construction
helpers call `resolve_network_default()`.

### Implication for T17

Same cut: **child-only**, same spawn site, same fail-closed / injectable /
default-off flag style. Prefer a sibling `landlock.py` rather than growing
`network_mode.py`.

## 3. Local landlock probe (this VM)

| Check | Result |
|-------|--------|
| Kernel | Linux 6.12.94+ x86_64 |
| `landlock.h` | Present (`/usr/include/linux/landlock.h`) |
| `landlockctl` | **Absent** — do not depend on it |
| ABI via `landlock_create_ruleset(NULL,0,VERSION)` | **ABI 6** |
| `restrict_self` without `PR_SET_NO_NEW_PRIVS` | `EPERM` |
| `prctl(PR_SET_NO_NEW_PRIVS)` then restrict | **Works** |
| RO `/` + RW tmpdir | Outside write → `EACCES`; inside write OK; outside read OK |
| Nested: `unshare -Urn --` then landlock in child | FS deny + net `ENETUNREACH` **both** hold |

Conclusion: **ctypes/syscall Landlock ABI** is shippable here without new
packages or `landlockctl`. Helper must set no-new-privs before restrict.

## 4. Where to apply (child-only)

**Must not** call `landlock_restrict_self` in the worker parent — that would
brick NATS/Gateway and the rest of the process.

`asyncio.create_subprocess_exec` has no portable `preexec_fn` on the production
path (and cancel-file uses `spawn_command`). Mirror T15: **argv prefix helper**
that:

1. Sets `PR_SET_NO_NEW_PRIVS`
2. Builds ruleset / path-beneath rules
3. `landlock_restrict_self`
4. `os.execvp` the remaining command

Wire order in `_execute_subprocess` (recommended):

```
plan_network_mode → apply_network_argv
plan_landlock     → apply_landlock_argv   # additional wrap; outer helper
```

Resulting shapes:

| Network | Landlock | Final argv sketch |
|---------|----------|-------------------|
| off / full | on | `python -m …landlock_exec -- <agent…>` |
| none (unshare) | on | `python -m …landlock_exec -- unshare … -- <agent…>` |
| either | off | unchanged NetworkMode behaviour |

Landlock as **outer** wrap inherits into `unshare` and the agent; probe showed
inner landlock under unshare also works. Outer wrap is the smallest change to
the existing network applicator (no surgery inside `UNSHARE_NETNS_PREFIX`).

When `UC_LANDLOCK_ENFORCE` is on, also close the Rust `execute_in_sandbox`
bypass (same honesty as T15).

## 5. FS rules — recommendation

`SandboxConfig` today has `project_path` / `working_dir` only — **no** FS
allowlist field. Agents run in a git **worktree** (`working_dir`) under a
repo (`project_path`).

**Recommended MVP rules (Approach A):**

| Access | Paths |
|--------|-------|
| RO (read/exec/readdir) | `/` (covers interpreters, agent CLIs, libs, `/etc`) |
| RW (write/create/remove/truncate + RO bits) | spawn `working_dir` (worktree / cwd) |
| RW | `/tmp` (and `TMPDIR` if set and absolute) — agent scratch |

Rationale:

- Coding agents must **read** the tree and **write** the worktree; RO-on-`/`
  still allows reading paths outside the worktree (e.g. sibling checkout
  files) while blocking writes outside RW roots — matches the probe.
- Deriving from existing config avoids a new `SandboxConfig` schema /
  Rust mirror in MVP.
- Optional escape hatch env (not required for ticket AC): 
  `UC_LANDLOCK_EXTRA_RW` / `UC_LANDLOCK_EXTRA_RO` (path lists) for operators.

**Not recommended for MVP:** inventing `SandboxConfig.fs_allowlist` /
per-agent policy tables — larger surface, blocks “small module” cut, and
ticket AC only asks for restrict-when-enforce + fail-closed + no NetworkMode
regression.

Fail closed when enforce on and `working_dir`/`project_path` cannot yield a
usable RW root (empty / missing path).

## 6. Platform / CI matrix

| Platform | Enforce off | Enforce on |
|----------|-------------|------------|
| Linux + Landlock ABI ≥ 1 + no-new-privs | noop | apply; live probe optional |
| Linux without Landlock / userns quirks that break helper | noop | **`LandlockError` refuse** |
| macOS / Windows / non-Linux CI | noop | **fail closed** (or skip live probe tests; unit tests use injectable applicator) |

Tests:

- Unit: flag default off; plan noop when off; enforce on + injected applicator;
  fail closed when unavailable; argv wrap pure; NetworkMode plan still applied
  first (ordering assertion).
- Live Linux probe: mark `@pytest.mark.skipif(not platform_supports_landlock())`
  — assert child cannot write outside RW root; assert NetworkMode `none` still
  blocks net when both flags on (regression).
- Non-Linux: skip live probe; keep unit/injectable tests green.

## 7. Feasible approaches

**Approach A: `landlock.py` + argv helper + injectable applicator** (Recommended)

* How: New `python/ultimate_coders/agent/landlock.py` with
  `UC_LANDLOCK_ENFORCE`, `LandlockApplyPlan`, `plan_landlock`,
  `apply_landlock_argv`, `LandlockError`, `set_landlock_applicator`,
  `platform_supports_landlock`, plus a tiny `__main__` / `landlock_exec`
  entry that applies ctypes syscalls then execs. Wire after network wrap in
  `_execute_subprocess`; close engine bypass when landlock enforce on.
  FS policy: RO `/` + RW worktree + RW `/tmp` (optional EXTRA_* envs).
* Pros: Mirrors T15/T16 seams; child-only; no new deps; testable via inject;
  nesting with unshare verified; default off preserves deployments.
* Cons: RO-`/` is coarse (read still wide); ctypes syscall numbers need
  arch care (document x86_64/aarch64); not a full container FS jail.

**Approach B: `landlockctl` / external bwrap wrapper**

* How: Shell out to distro tools for rules + wrap.
* Pros: Less ctypes. Cons: `landlockctl` absent here and on stock GHA images;
  bwrap not installed; weaker CI story; extra binary dependency.

**Approach C: SandboxConfig FS allowlist schema + Landlock**

* How: New config fields + planner from explicit path lists; optionally
  mirror into Rust.
* Pros: Flexible policy. Cons: Over-scope vs ticket; schema/version churn;
  delays MVP; still needs same apply machinery as A.

## 8. Must not regress NetworkMode

| Guard | How |
|-------|-----|
| Flag independence | Landlock off → byte-identical network path when landlock plan is noop |
| Ordering | Network plan first; landlock wrap outermost; both may be active |
| Dual-enforce probe | `UC_NETWORK_MODE_ENFORCE` + `UC_LANDLOCK_ENFORCE` + `network=none` → net blocked **and** FS write outside worktree denied |
| Engine bypass | Either enforce flag closes Rust sandbox bypass |
| Worker plane | No landlock on parent; NATS/Gateway unchanged |
| T16 allowlist | Restricted preflight untouched; landlock does not parse hosts |

## 9. Out of scope (MVP)

- seccomp-bpf
- Kernel selective egress / nft (still deferred from T16 honesty note)
- Default-on Landlock
- Rust `SubprocessSandbox` Landlock implementation (bypass closed instead)
- New `SandboxConfig` FS schema
- macOS sandbox-exec / Windows Job Object equivalents (fail closed under enforce)

## 10. Key files

- `python/ultimate_coders/agent/network_mode.py` (pattern)
- `python/ultimate_coders/agent/sandbox.py` (`_execute_subprocess`, engine bypass)
- New: `python/ultimate_coders/agent/landlock.py` (+ exec entry)
- Tests: `tests/python/test_landlock_enforce.py` (new); keep
  `test_network_mode_enforce.py` / `test_network_allowlist.py` green
- Spec: new `.trellis/spec/backend/landlock-fs-spec.md` (or short section
  cross-linked from `network-mode-spec.md`)
- Ticket / D8 / `docs/dsec-sandbox.md` (security posture note once behaviour
  lands — dsec itself needs no special Landlock path)

## 11. Probe evidence summary

- Landlock ABI 6 available; `PR_SET_NO_NEW_PRIVS` required before restrict.
- RO `/` + RW workdir denies writes outside allowlist (`EACCES`).
- `unshare --user --map-root-user --net` + landlock in-process: FS + net
  isolation compose correctly.
