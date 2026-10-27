# Landlock FS Enforcement Spec (Agent Child)

> Optional Landlock filesystem restriction at agent-subprocess spawn (D8 / T17).
> Enforce flag `UC_LANDLOCK_ENFORCE` defaults off. Mirrors NetworkMode
> child-only argv wrap; must not regress T15/T16. NetworkMode contracts live
> in [network-mode-spec.md](./network-mode-spec.md).

---

## 1. Scope / Trigger

Modules: `python/ultimate_coders/agent/landlock.py`,
`python/ultimate_coders/agent/sandbox.py` (`SandboxManager._execute_subprocess`
and the `execute_in_sandbox` bypass guard).

Trigger: any change to Landlock enforce flag, planner, argv wrap, ctypes
helper, FS path policy, or spawn wiring. Do **not** call
`landlock_restrict_self` on the worker parent. Do **not** enable the flag by
default. Do **not** change NetworkMode / allowlist semantics.

Tests: `tests/python/test_landlock_fs.py` (keep
`tests/python/test_network_mode_enforce.py` /
`tests/python/test_network_allowlist.py` green).

---

## 2. Signatures

```python
LANDLOCK_ENFORCE_ENV = "UC_LANDLOCK_ENFORCE"
LANDLOCK_EXTRA_RW_ENV = "UC_LANDLOCK_EXTRA_RW"
LANDLOCK_EXTRA_RO_ENV = "UC_LANDLOCK_EXTRA_RO"
LANDLOCK_ABI_BACKEND = "landlock_abi"

class LandlockError(Exception): ...

@dataclass(frozen=True)
class LandlockApplyPlan:
    argv_prefix: tuple[str, ...] = ()
    backend: str = "noop"      # "noop" | "landlock_abi" | "injected"
    ro_paths: tuple[str, ...] = ()
    rw_paths: tuple[str, ...] = ()

def landlock_enforce_enabled() -> bool: ...
def platform_supports_landlock() -> bool: ...
def landlock_abi_version() -> int: ...
def resolve_landlock_paths(working_dir: str, *, environ=None) -> tuple[tuple[str, ...], tuple[str, ...]]: ...
def plan_landlock(working_dir: str, *, enforce=None, environ=None) -> LandlockApplyPlan: ...
def apply_landlock_argv(command, args, plan) -> tuple[str, list[str]]: ...
def apply_landlock_self(ro_paths, rw_paths) -> None: ...
def set_landlock_applicator(applicator) -> None: ...
def clear_landlock_applicator() -> None: ...
def reset_landlock_state_for_tests() -> None: ...
```

Truthy parser matches other UC flags: ``1`` / ``true`` / ``yes`` / ``on``
(case-insensitive). Unset / empty / other values → disabled.

Syscall numbers (ctypes; no `landlockctl`): x86_64 and aarch64 use
`444` / `445` / `446` for create_ruleset / add_rule / restrict_self.

---

## 3. Contracts

1. **Flag default off.** `UC_LANDLOCK_ENFORCE` is off unless truthy.
2. **Flag off → noop.** `plan_landlock` returns a noop plan; spawn argv
   matches pre-T17 behaviour (NetworkMode path unchanged).
3. **Child-only.** Enforcement rewrites the agent child argv only. Worker
   control-plane sockets (NATS / Gateway) are never Landlock-restricted.
4. **Wire order.** In `_execute_subprocess`: plan NetworkMode and Landlock
   first (either may fail closed). Apply Landlock **inner**, then NetworkMode
   **outer** (`unshare … -- python landlock.py … -- <agent>`). Landlock sets
   `PR_SET_NO_NEW_PRIVS`, which would break a subsequent
   `unshare --map-root-user` uid_map write if Landlock were outermost.
5. **Fail closed.** Enforce on + Landlock unavailable (non-Linux, ABI below 1,
   missing syscall table) → `LandlockError` / `ExecResult` refuse — never
   silent unrestricted FS.
6. **Missing RW root.** Enforce on + empty / non-directory `working_dir` →
   fail closed.
7. **FS MVP policy.** RO beneath `/`; RW beneath absolute spawn
   `working_dir`; RW beneath `/tmp`; RW beneath absolute `TMPDIR` when set.
   Optional `UC_LANDLOCK_EXTRA_RW` / `UC_LANDLOCK_EXTRA_RO` path lists.
8. **Helper.** `python <landlock.py> --ro … --rw … -- cmd…` (file-path
   invoke, stdlib-only) sets `PR_SET_NO_NEW_PRIVS`, builds ruleset,
   `landlock_restrict_self`, then `os.execvp`.
9. **Engine bypass closed.** When landlock enforce is on,
   `SandboxManager.execute` must **not** call `engine.execute_in_sandbox`
   (same honesty as NetworkMode enforce).
10. **Injectable seam.** `set_landlock_applicator` overrides the built-in
    planner for unit tests on any platform.
11. **No NetworkMode regression.** Restricted allowlist preflight and
    `none` / `full` argv behaviour stay owned by `network_mode.py`.

---

## 4. Platform / CI skip matrix

| Probe | When it runs | When skipped |
|-------|--------------|--------------|
| Unit flag / plan / fail-closed / injectable wrap / ordering | Any OS | Never |
| Live FS write-deny outside RW root | Linux + `platform_supports_landlock()` | macOS / Windows / ABI unavailable |
| Live dual-flag (Landlock + NetworkMode `none`) | Linux + Landlock + `platform_supports_unshare_netns()` + parent egress | Otherwise |

Documented in `tests/python/test_landlock_fs.py` module docstring.

---

## 5. Out of scope

- Enabling Landlock by default
- seccomp-bpf
- Kernel selective egress / nft (T16 follow-up)
- Landlock inside Rust `SubprocessSandbox` (bypass closed instead)
- New `SandboxConfig` FS allowlist schema / per-agent path tables
- macOS sandbox-exec / Windows Job Object equivalents beyond fail-closed
- Changing NetworkMode defaults or Restricted allowlist semantics

---

## 6. Related

- Ticket: `docs/architecture/agent-trust-eval/tickets/17-landlock-fs-later.md`
- Spec P2: `docs/architecture/agent-trust-eval/spec-p2.md`
- Decision D8: `docs/architecture/agent-trust-eval/issues/D8-sandbox-isolation-target.md`
- NetworkMode: [network-mode-spec.md](./network-mode-spec.md)
