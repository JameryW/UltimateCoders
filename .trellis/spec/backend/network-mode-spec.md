# NetworkMode Enforcement Spec (Agent Child)

> Real `none` / `restricted` / `full` at agent-subprocess spawn (D8 / T15).
> Enforce flag `UC_NETWORK_MODE_ENFORCE` defaults off. Restricted host
> allowlist product policy is T16. Landlock FS is T17.

---

## 1. Scope / Trigger

Modules: `python/ultimate_coders/agent/network_mode.py`,
`python/ultimate_coders/agent/sandbox.py` (`SandboxManager._execute_subprocess`
and the `execute_in_sandbox` bypass guard).

Trigger: any change to the NetworkMode enforce flag, planner, argv wrap,
Restricted applicator seam, or spawn wiring. Do **not** cut NATS/Gateway on
the worker process. Do **not** invent Landlock or default-offline product
defaults here. Do **not** enable the flag by default.

Tests: `tests/python/test_network_mode_enforce.py`.

---

## 2. Signatures

```python
NETWORK_MODE_ENFORCE_ENV = "UC_NETWORK_MODE_ENFORCE"

class NetworkModeError(Exception): ...

@dataclass(frozen=True)
class NetworkApplyPlan:
    mode: str                  # "none" | "restricted" | "full"
    argv_prefix: tuple[str, ...] = ()
    backend: str = "noop"      # "noop" | "unshare_netns" | "injected" | …

def network_mode_enforce_enabled() -> bool: ...
def normalize_network_mode(mode: str) -> str: ...

def plan_network_mode(
    mode: str,
    *,
    enforce: bool | None = None,
    restricted_applicator: NetworkModeApplicator | None = None,
) -> NetworkApplyPlan: ...

def apply_network_argv(
    command: str,
    args: list[str] | tuple[str, ...],
    plan: NetworkApplyPlan,
) -> tuple[str, list[str]]: ...

def set_restricted_applicator(applicator: NetworkModeApplicator | None) -> None: ...
def set_network_mode_applicator(applicator: NetworkModeApplicator | None) -> None: ...
def clear_network_mode_applicator() -> None: ...
def platform_supports_unshare_netns() -> bool: ...
```

Truthy parser matches other UC flags: ``1`` / ``true`` / ``yes`` / ``on``
(case-insensitive). Unset / empty / other values → disabled.

---

## 3. Contracts

1. **Flag default off.** `UC_NETWORK_MODE_ENFORCE` is off unless truthy.
2. **Flag off → noop.** `plan_network_mode` returns a noop plan for any valid
   mode; `_execute_subprocess` spawn argv matches pre-T15 behaviour.
3. **Child-only.** Enforcement rewrites the agent child argv only. Worker
   control-plane sockets (NATS subscribe, Gateway gRPC heartbeat/register)
   are never wrapped by these helpers.
4. **Full + enforce on → noop.** No argv prefix; child networking unchanged.
5. **None + enforce on.** Rootless Linux backend prefixes
   `unshare --user --map-root-user --net --` when
   `platform_supports_unshare_netns()` is true. Otherwise raise
   `NetworkModeError` (fail closed — never silent Full).
6. **Restricted + enforce on.** Fail closed unless a Restricted applicator is
   registered (`set_restricted_applicator` or the
   `restricted_applicator=` argument). T16 owns allowlist host content; T15
   must not pretend empty-netns is Restricted.
7. **Unknown mode.** `normalize_network_mode` / planner raise `ValueError`;
   spawn refuses with stderr (no silent Full).
8. **Wire point.** `_execute_subprocess` plans after `build_child_env`,
   before either `spawn_command` or `asyncio.create_subprocess_exec`. On
   `NetworkModeError`, return `ExecResult(exit_code=-1, stderr=…)` without
   spawning.
9. **Engine bypass closed.** When enforce is on, `SandboxManager.execute`
   must **not** call `engine.execute_in_sandbox` (Rust path has no NetworkMode
   isolation today). Route through `_execute_subprocess` so the planner
   cannot be skipped.
10. **Injectable seam.** `set_network_mode_applicator` overrides the built-in
    planner for unit tests on any platform without root.

---

## 4. Platform / CI skip matrix

| Probe | When it runs | When skipped |
|-------|--------------|--------------|
| Unit flag / plan / Restricted fail-closed / injectable wrap | Any OS | Never |
| Live None vs Full child egress (`1.1.1.1:53`) | Linux + `platform_supports_unshare_netns()` + parent can egress | Windows; missing `unshare`; userns disabled; no parent egress |

Documented in `tests/python/test_network_mode_enforce.py` module docstring.

---

## 5. Out of scope

- Default-toward-offline / shipping LLM host allowlist product policy (T16).
- Landlock FS / `UC_LANDLOCK_ENFORCE` (T17).
- Cutting network on the entire worker container.
- Changing `SandboxConfig.network` default away from Full.
- Reviving `DockerSandbox`.

---

## 6. Related

- Ticket: `docs/architecture/agent-trust-eval/tickets/15-networkmode-real-enforcement.md`
- Spec P2 Seam H: `docs/architecture/agent-trust-eval/spec-p2.md`
- Decision D8: `docs/architecture/agent-trust-eval/issues/D8-sandbox-isolation-target.md`
