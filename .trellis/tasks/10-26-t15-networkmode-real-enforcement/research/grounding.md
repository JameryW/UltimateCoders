# T15 grounding — NetworkMode real enforcement (D8 step 1)

Medium thoroughness. Code + local probe on this VM (Ubuntu 24.04, uid 1000, kernel 6.12). Do **not** invent Landlock (T17) or default-offline / product allowlist policy (T16).

## 1. NetworkMode enum / defaults

### Python — `python/ultimate_coders/agent/sandbox.py`

```python
class NetworkMode:
    NONE = "none"
    RESTRICTED = "restricted"
    FULL = "full"

@dataclass
class SandboxConfig:
    ...
    network: str = NetworkMode.FULL  # default Full
```

- Serialized into engine dict via `SandboxConfig.to_engine_config()` → `"network": self.network`.
- `sandbox_dsec.DSEC_DEFAULTS` also sets `"network": NetworkMode.FULL`.
- Tests only assert enum string values / config round-trip (`tests/python/test_sandbox.py::TestNetworkMode`). **No spawn-time behaviour tests.**

### Rust — `crates/uc-engine/src/sandbox/mod.rs`

```rust
pub enum NetworkMode {
    None,
    Restricted,  // doc: "Only specific hosts allowed (API endpoints)."
    #[default]
    Full,
}
```

- `SandboxConfig::default().network == NetworkMode::Full`.
- Agent adapter unit fixtures sometimes use `Restricted` (claude/codex/grok tests) — config theatre only.
- `DockerSandbox` (old bridge/host mapping) was **deleted** (2026-07 dead-code cleanup). Production backend is `SubprocessSandbox` only.

## 2. Where the agent subprocess is spawned

### Python choke point (production worker path)

| Path | Role |
|------|------|
| `SandboxManager.execute()` | Coding agents; may use Rust `engine.execute_in_sandbox` **or** Python `_execute_subprocess` |
| `SandboxManager.execute_decompose()` | Always `_execute_subprocess` |
| `SandboxManager._execute_subprocess()` | **Single choke point** for env allowlist + spawn |

Spawn mechanics inside `_execute_subprocess`:

1. `env = self.config.build_child_env(os.environ, env_vars, agent=agent_name)` (T11).
2. If `cancel_file`: `ultimate_coders.inference.process.spawn_command([...])` (wrapper child).
3. Else: `asyncio.create_subprocess_exec(command, *args, ..., env=env, start_new_session=(posix))`.

**`self.config.network` is never read** on this path.

When `engine.execute_in_sandbox` is used (no cancel_file + engine present), network mode is also unused on the Rust execute path (see below).

### Rust — `crates/uc-engine/src/sandbox/subprocess.rs`

- Module docs: “MVP … **no filesystem/network isolation**.”
- `execute()` builds `tokio::process::Command`, overlays `request.env_vars`, calls empty `apply_unix_limits`, spawns.
- `SandboxConfig.network` is accepted at `create()` but **not stored on the handle** and **not consulted in `execute()`**.
- `apply_unix_limits` is intentionally a stub (`Ok(())`).

## 3. Isolation that exists today

| Layer | Status |
|-------|--------|
| Env allowlist (T11 #653) | **Real** — deny-by-default; `BASE` + `SHARED` (`UC_*`, proxies) + per-adapter credentials; `UC_SANDBOX_ENV_EXTRA` escape hatch |
| `NetworkMode` | **Config theatre** — stored/serialized/tested as strings; no OS enforcement |
| FS isolation / Landlock | Absent (T17 later) |
| seccomp | Absent |
| Docker network modes | Removed with `DockerSandbox` |
| Resource limits | Soft: asyncio/tokio timeout only; ulimit not applied |

Worker process (NATS subscribe, Gateway gRPC heartbeat/register) is **out of band** of `_execute_subprocess` — good for child-only cut.

## 4. Practical Linux enforcement options (CI / no root)

Probed on this environment:

| Mechanism | Result here |
|-----------|-------------|
| `unshare --net` alone | `Operation not permitted` (needs CAP_NET_ADMIN / root) |
| `unshare --user --map-root-user --net -- <cmd>` | **Works as uid 1000** — child gets `ENETUNREACH` (errno 101) to `8.8.8.8:53` and even `127.0.0.1` |
| Same wrap via `asyncio.create_subprocess_exec(..., start_new_session=True)` | Works |
| `bwrap` / bubblewrap | Not installed |
| `nft` / `iptables` / `ip` / `slirp4netns` / `pasta` | Not present on this VM |

Implications:

1. **`None`**: argv-prefix with `unshare -Urn` (user+net ns) is a shippable, rootless Linux backend for “no egress”. Child-only; parent worker keeps NATS/Gateway.
2. **`Full`**: identity / no wrap.
3. **`Restricted` (host allowlist)**: raw empty netns is total cut — **not** allowlist. Real Restricted needs extra plumbing (userspace proxy, CNI egress, nft in a prepared ns, bwrap+slirp, etc.). That product allowlist is **T16**. T15 must not invent default-offline allowlist policy; it should either (a) leave a Restricted hook that fails closed until T16 registers a backend, or (b) treat Restricted as “enforcement attempted + fail closed if no Restricted applicator”.
4. **seccomp-bpf** blocking `connect`/`sendto`: can approximate `None` without netns, but Restricted-by-host is awkward in pure seccomp; heavier Rust deps; harder to mock.
5. **Mockable `apply_network_mode` hook**: required for unit tests on any platform; integration probe can skip when `unshare` userns unavailable (document CI matrix).

GitHub `ubuntu-latest` generally allows unprivileged user namespaces; still gate live probes with a capability check + `pytest.mark.skip` so runners with userns disabled stay green.

`asyncio.create_subprocess_exec` does **not** take `preexec_fn` reliably the way `subprocess.Popen` does — prefer **argv rewrite** (`unshare … -- cmd args`) over `preexec_fn`. Works for both direct spawn and (with care) `spawn_command` cancel wrapper: wrap the *inner* agent command, or wrap the outer argv consistently and document interaction with process-tree cancel.

## 5. Minimal vertical slice (recommended)

- Flag: `UC_NETWORK_MODE_ENFORCE` default **off** (same truthy set as other P2 flags: `1|true|yes|on`).
- When **off**: today’s spawn unchanged (compatible Full behaviour).
- When **on**: before spawn, apply mode from `SandboxConfig.network`:
  - `full` → no wrap
  - `none` → Linux unshare userns+netns wrap (or injected applicator)
  - `restricted` → **fail closed unless a Restricted applicator is registered** (T16 owns allowlist hosts); do not silently run Full
- Unsupported platform / missing `unshare` / wrap probe fail + enforce on → **refuse start** (raise / return failed ExecResult with clear stderr) — never silent Full.
- Scope: **agent child only**. Do not touch worker NATS/Gateway sockets.
- Landlock / default-offline product defaults: **out of scope**.

### Recommended API shape (small module)

New module e.g. `python/ultimate_coders/agent/network_mode.py` (mirror `typed_review.py` / `stall_redispatch.py`):

```text
NETWORK_MODE_ENFORCE_ENV = "UC_NETWORK_MODE_ENFORCE"
network_mode_enforce_enabled() -> bool

normalize_network_mode(mode: str) -> "none"|"restricted"|"full"  # ValueError on unknown

class NetworkModeError(Exception): ...  # fail-closed signal

@dataclass(frozen=True)
class NetworkApplyPlan:
    mode: str
    argv_prefix: tuple[str, ...]   # e.g. ("unshare","--user","--map-root-user","--net","--")
    backend: str                   # "noop" | "unshare_netns" | "injected" | ...

def plan_network_mode(mode: str, *, enforce: bool | None = None) -> NetworkApplyPlan
    # enforce off → noop plan for any mode
    # enforce on + full → noop
    # enforce on + none → unshare plan or raise NetworkModeError
    # enforce on + restricted → raise unless restricted applicator registered

def apply_network_argv(command: str, args: list[str], plan: NetworkApplyPlan) -> tuple[str, list[str]]
    # prefix argv; pure; unit-testable

# Test seam
set_network_mode_applicator(callable | None)  # inject fake plan / fail
clear_network_mode_applicator()
platform_supports_unshare_netns() -> bool     # cheap probe / cache
```

**Wire point:** `_execute_subprocess` immediately after `build_child_env`, before either spawn path — rewrite `command`/`args` from `plan_network_mode(self.config.network)`. Optionally refuse early in `execute()` when enforce on and plan would fail, so adapters see a clear error.

**Rust (optional same PR / thin follow-up):** store `network` on handle or pass into `execute`; call equivalent wrap only when a Rust-side enforce flag is set. Python worker path is the vertical slice; Rust docs already admit no isolation — prefer Python-first so CI pytest covers the real choke point. If engine `execute_in_sandbox` remains used without cancel_file, either (1) force Python spawn when enforce on, or (2) pass network into Rust execute — pick one in impl so enforce cannot be bypassed via engine path.

### Test strategy

| Test | Platform |
|------|----------|
| Flag default off; `plan` is noop for none/restricted/full | Any |
| Flag on + full → noop argv | Any |
| Flag on + none + injected applicator → argv_prefix applied | Any |
| Flag on + unsupported / applicator raises → fail closed (no spawn) | Any |
| Flag on + restricted without T16 applicator → fail closed | Any |
| Live: flag on + none + `python -c` connect probe → blocked; parent can still open TCP | Linux + `platform_supports_unshare_netns()` else skip |
| Worker heartbeat / NATS not wrapped | Doc + unit (helpers only touch child argv) |

Document skip matrix in test module docstring (and later backend spec).

## 6. Feasible approaches

**A — Mockable planner + unshare argv wrap (Recommended)**  
Small `network_mode.py`; wire in `_execute_subprocess`; Restricted fail-closed until applicator; live None probe when userns works.  
Pros: matches flag/fail-closed culture; CI-friendly; child-only; no new deps.  
Cons: Restricted semantics incomplete until T16; Windows → fail closed under enforce.

**B — seccomp-bpf None only**  
Pros: no userns dependency.  
Cons: Restricted still unsolved; Rust/CFFI weight; harder mock; overkill for T15.

**C — Require bubblewrap in image**  
Pros: richer sandbox later.  
Cons: not on runners today; ops dependency; Restricted still needs slirp/nft story (T16).

## 7. Do not invent

- Landlock FS / `UC_LANDLOCK_ENFORCE` (T17)
- Default toward offline / shipping LLM host allowlist product policy (T16)
- Cutting network on the whole worker container
- Reviving `DockerSandbox` as the T15 vehicle
- Silent fallback to Full when enforce is on

## Key file index

- `python/ultimate_coders/agent/sandbox.py` — `NetworkMode`, `SandboxConfig`, `build_child_env`, `_execute_subprocess`
- `python/ultimate_coders/agent/sandbox_dsec.py` — dsec defaults Full
- `python/ultimate_coders/inference/process.py` — `spawn_command` cancel wrapper
- `crates/uc-engine/src/sandbox/mod.rs` — Rust `NetworkMode`
- `crates/uc-engine/src/sandbox/subprocess.rs` — no network isolation
- `docs/architecture/agent-trust-eval/tickets/15-networkmode-real-enforcement.md`
- `docs/architecture/agent-trust-eval/issues/D8-sandbox-isolation-target.md`
- `docs/architecture/agent-trust-eval/spec-p2.md` — Seam H
