# NetworkMode Enforcement Spec (Agent Child)

> Real `none` / `restricted` / `full` at agent-subprocess spawn (D8 / T15–T16).
> Enforce flag `UC_NETWORK_MODE_ENFORCE` defaults off. T16 adds opt-in
> default-toward-Restricted (`UC_NETWORK_DEFAULT_RESTRICTED`) and an explicit
> LLM/API host allowlist (`UC_NETWORK_ALLOWLIST`) with policy preflight
> Restricted applicator. Landlock FS is T17 — see [landlock-fs-spec.md](./landlock-fs-spec.md).

---

## 1. Scope / Trigger

Modules: `python/ultimate_coders/agent/network_mode.py`,
`python/ultimate_coders/agent/sandbox.py` (`SandboxManager.__init__`,
`SandboxManager._execute_subprocess`, and the `execute_in_sandbox` bypass
guard), construction helpers in `sandbox_dsec.py` / `nats_worker.py` /
`worker.py` that call `resolve_network_default()`.

Trigger: any change to NetworkMode enforce / default-restricted / allowlist
flags, planner, Restricted applicator, argv wrap, or spawn wiring. Do **not**
cut NATS/Gateway on the worker process. Do **not** invent Landlock. Do **not**
enable any of these flags by default. Do **not** silently change Full defaults
when flags are off.

Tests: `tests/python/test_network_mode_enforce.py`,
`tests/python/test_network_allowlist.py`.

---

## 2. Signatures

```python
NETWORK_MODE_ENFORCE_ENV = "UC_NETWORK_MODE_ENFORCE"
NETWORK_DEFAULT_RESTRICTED_ENV = "UC_NETWORK_DEFAULT_RESTRICTED"
NETWORK_ALLOWLIST_ENV = "UC_NETWORK_ALLOWLIST"
NETWORK_DEFAULT_MODE_ENV = "UC_NETWORK_DEFAULT_MODE"  # optional supersede
ALLOWLIST_PREFLIGHT_BACKEND = "allowlist_preflight"

class NetworkModeError(Exception): ...

@dataclass(frozen=True)
class NetworkApplyPlan:
    mode: str                  # "none" | "restricted" | "full"
    argv_prefix: tuple[str, ...] = ()
    backend: str = "noop"      # "noop" | "unshare_netns" | "allowlist_preflight" | "injected" | …

def network_mode_enforce_enabled() -> bool: ...
def network_default_restricted_enabled() -> bool: ...
def normalize_network_mode(mode: str) -> str: ...
def resolve_network_default() -> str: ...
def parse_network_allowlist(raw: str | None = None) -> frozenset[str]: ...
def normalize_allowlist_host(value: str) -> str: ...
def intended_llm_hosts(environ: dict[str, str] | None = None) -> frozenset[str]: ...
def plan_restricted_allowlist(mode: str = "restricted", ...) -> NetworkApplyPlan: ...

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

## 3. Contracts (T15 enforce)

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
6. **Restricted + enforce on.** Requires a Restricted applicator
   (`set_restricted_applicator` or `restricted_applicator=`). Product path
   registers `plan_restricted_allowlist` at `SandboxManager.__init__`.
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

## 4. Contracts (T16 default-offline + allowlist)

1. **`UC_NETWORK_DEFAULT_RESTRICTED` default off.** When off,
   `resolve_network_default()` returns `full`. Dataclass
   `SandboxConfig.network` and static `DSEC_DEFAULTS["network"]` remain Full
   literals — never mutate them unconditionally.
2. **Flag on → Restricted at construction helpers only.** `nats_worker`,
   `dsec_sandbox_config` (when caller omits `network=`), and `Worker()` with
   no explicit `sandbox_config` call `resolve_network_default()`. Explicit
   `SandboxConfig(network=…)` / `network=` overrides always win.
3. **`UC_NETWORK_DEFAULT_MODE`** (optional). When set to
   `full` / `restricted` / `none`, supersedes the bool flag.
4. **`UC_NETWORK_ALLOWLIST`.** Comma / semicolon / whitespace-separated
   hostnames. Normalize via `normalize_allowlist_host` (strip scheme/path;
   ignore port; lowercase).
5. **Empty allowlist + Restricted + enforce → fail closed.** Never
   “Restricted with open world”.
6. **Preflight.** Every present intended LLM/API host from
   `LLM_BASE_URL_ENVS` (`ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`,
   `OPENAI_API_BASE`, `DEEPSEEK_BASE_URL`, `MIMO_BASE_URL`,
   `MEMORY_LLM_BASE_URL`, `MEMORY_EMBEDDER_BASE_URL`) must be ⊆ allowlist.
   Unset URL envs contribute nothing. Extra allowlist entries are OK.
7. **Backend honesty.** Successful Restricted plan uses
   `backend=allowlist_preflight` with **noop argv**. This is a **policy**
   gate only — kernel selective egress (nft / iptables / bwrap+slirp /
   forced proxy) is **deferred**. Do not market empty-netns as Restricted,
   and do not claim OS “others blocked” for this MVP.
8. **NATS/Gateway off child allowlist.** Do not require Gateway /
   NATS hostnames on `UC_NETWORK_ALLOWLIST` for spawn success. Control-plane
   stays on the worker process (child-only cut).
9. **Register applicator at sandbox init.** `SandboxManager.__init__` calls
   `set_restricted_applicator(plan_restricted_allowlist)`. Tests clear via
   `reset_network_mode_state_for_tests()`.

---

## 5. Platform / CI skip matrix

| Probe | When it runs | When skipped |
|-------|--------------|--------------|
| Unit flag / plan / Restricted fail-closed / injectable wrap | Any OS | Never |
| Unit allowlist allow/deny / default-restricted resolver | Any OS | Never |
| Live None vs Full child egress (`1.1.1.1:53`) | Linux + `platform_supports_unshare_netns()` + parent can egress | Windows; missing `unshare`; userns disabled; no parent egress |

Documented in `tests/python/test_network_mode_enforce.py` and
`tests/python/test_network_allowlist.py` module docstrings.

---

## 6. Out of scope

- Kernel nft/iptables/bwrap/slirp / forced allowlist proxy selective egress
  (follow-up; not claimed by `allowlist_preflight`).
- Landlock FS / `UC_LANDLOCK_ENFORCE` (T17 — implemented separately; see
  [landlock-fs-spec.md](./landlock-fs-spec.md)).
- Cutting network on the entire worker container.
- Changing `SandboxConfig.network` / `DSEC_DEFAULTS` Full literals when flags
  are off.
- Hardcoding a non-empty production allowlist.
- Reviving `DockerSandbox`.

---

## 7. Related

- Ticket: `docs/architecture/agent-trust-eval/tickets/15-networkmode-real-enforcement.md`
- Ticket: `docs/architecture/agent-trust-eval/tickets/16-default-offline-allowlist.md`
- Spec P2 Seam H: `docs/architecture/agent-trust-eval/spec-p2.md`
- Decision D8: `docs/architecture/agent-trust-eval/issues/D8-sandbox-isolation-target.md`
