# T16 grounding — default-toward-offline + LLM allowlist

Medium thoroughness. Grounded on post-T15 code in `/workspace` (NetworkMode enforce landed). Do **not** invent Landlock (T17). Do **not** silently change `SandboxConfig.network` / `DSEC_DEFAULTS` away from Full unless an opt-in flag is on.

## 1. What T15 left for T16

| Seam | State after T15 |
|------|-----------------|
| `UC_NETWORK_MODE_ENFORCE` | Default **off**; truthy `1\|true\|yes\|on` |
| `none` | Rootless `unshare --user --map-root-user --net --` when probe passes; else fail closed |
| `full` | Noop plan |
| `restricted` | **Fail closed** unless `set_restricted_applicator` (or `restricted_applicator=`) is registered — no silent Full |
| Wire | `SandboxManager._execute_subprocess` after `build_child_env`; engine bypass closed when enforce on |
| Defaults | `SandboxConfig.network = Full`; `DSEC_DEFAULTS["network"] = Full`; `nats_worker` builds `SandboxConfig(...)` with no network override |

Key API already present for T16 (`python/ultimate_coders/agent/network_mode.py`):

- `set_restricted_applicator(applicator)` — process-global Restricted planner seam
- `NetworkApplyPlan(mode, argv_prefix, backend)`
- `NetworkModeError` — fail-closed signal
- Injectable `set_network_mode_applicator` for unit tests (any mode)

T15 intentionally did **not** register a Restricted applicator and did **not** change product defaults.

## 2. Ticket / D8 / Seam H requirements

**Ticket 16** (`docs/architecture/agent-trust-eval/tickets/16-default-offline-allowlist.md`):

- Opt-in default Restricted/None behind a flag (default off)
- Restricted consults an **explicit** LLM/API host allowlist
- Worker NATS/Gateway **not** on the child allowlist requirement
- Flag off preserves prior defaults
- Tests: allowlist allow/deny; flags default off

**D8**: NetworkMode real first; default toward offline (None or Restricted) behind a switch; LLM/API via explicit allowlist; NATS/Gateway are worker-plane; Landlock later.

**Seam H (spec-p2)**: Flag off → compatible Full spawn; flag on + None → child blocked (probe); Restricted → allowlist hosts reachable, others blocked; worker heartbeat unaffected.

## 3. Platform honesty (GitHub runners / this VM)

Re-probed here (uid 1000, Linux 6.12):

| Tool | Available? |
|------|------------|
| `unshare --user --map-root-user --net` | **Yes** — child TCP to `1.1.1.1:53` blocked (`OSError`); parent egress OK |
| `nft` / `iptables` / `bwrap` / `slirp4netns` / `pasta` | **Not** on PATH / image |
| Root / CAP_NET_ADMIN for classic netns nft | Not assumed on GHA `ubuntu-latest` |

Implications:

1. **True selective egress** (allow host A, deny host B at the kernel) needs nft/iptables in a prepared ns, CNI egress, bwrap+slirp, or a forced userspace proxy the child **cannot** bypass. None of that ships on stock GitHub runners without image/ops work.
2. **Empty netns (`none`)** is shippable and testable without root — but it allows **zero** hosts (not an allowlist).
3. **HTTP_PROXY-style** without OS force is **advisory** (CLIs can ignore proxy / use raw sockets). Putting the child in empty netns also cuts reachability to a parent-side proxy unless you add veth/loopback plumbing (not MVP).
4. Therefore a **CI-green Restricted MVP** must either (a) be **policy fail-closed** with an honest backend label, or (b) collapse to `none` and document that LLM egress needs a later real applicator / image.

Pretending empty-netns is Restricted, or shipping Full networking while claiming “others blocked”, would recreate the fake-safety problem T15 killed.

## 4. Flag name recommendation

Keep T15’s enforce flag separate from product-default policy.

| Env | Default | Role |
|-----|---------|------|
| `UC_NETWORK_MODE_ENFORCE` | off (exists) | Apply `SandboxConfig.network` at child spawn |
| **`UC_NETWORK_DEFAULT_RESTRICTED`** | **off** | When truthy, **resolve** sandbox/dsec/worker default `network` to `restricted` instead of Full |
| **`UC_NETWORK_ALLOWLIST`** | empty | Comma/space-separated hostnames for Restricted (e.g. `api.deepseek.com,api.anthropic.com`) |
| Optional same-PR: `UC_NETWORK_DEFAULT_MODE` | unset → Full | Values `full` \| `restricted` \| `none`; if set, wins over the bool (covers D8 “None or Restricted”) |

**Recommended primary names (Approach A):**

1. `UC_NETWORK_DEFAULT_RESTRICTED` — matches ticket example; truthy parser identical to other UC flags.
2. `UC_NETWORK_ALLOWLIST` — explicit host list; empty under Restricted+enforce → **fail closed** (never “Restricted with open world”).

**Do not** mutate the dataclass literal `network: str = NetworkMode.FULL` into Restricted unconditionally. Use a resolver helper consulted only by construction sites (`nats_worker` SandboxConfig, `dsec_sandbox_config` / `DSEC_DEFAULTS` consumers, any Worker factory). Explicit `SandboxConfig(network=...)` always wins. Flag off → today’s Full defaults everywhere.

Optional alias: document that `UC_NETWORK_DEFAULT_MODE=none` is the hard-offline default when operators want None instead of Restricted; keep bool as the common “toward offline but LLM-capable” path.

## 5. Restricted applicator — feasible designs

### Approach A — Allowlist preflight + labeled soft OS (Recommended MVP)

**How:**

1. Implement `plan_restricted_allowlist(mode) -> NetworkApplyPlan` in a small module (extend `network_mode.py` or `network_allowlist.py`).
2. Parse `UC_NETWORK_ALLOWLIST` → frozenset of normalized hostnames (strip scheme/path; optional port ignored for match).
3. **Empty allowlist + Restricted + enforce → `NetworkModeError`** (same honesty as T15).
4. **Preflight (fail closed):** collect *intended* LLM/API destinations from known child credential/URL env (`ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`, `DEEPSEEK_BASE_URL`, `MIMO_BASE_URL`, `MEMORY_LLM_BASE_URL`, … — derive host via `urllib.parse`). Every present intended host must be in the allowlist. Extra allowlist entries OK. Missing intended host when URL env unset → OK (agent may use compiled-in default — then either document that operators must list those defaults, or resolve defaults the same way adapters do).
5. **OS plan for MVP:** return `NetworkApplyPlan(mode="restricted", argv_prefix=(), backend="allowlist_preflight")` — **noop argv**. Log clearly that kernel egress filter is **not** active; policy gate is.
6. **Wire:** `SandboxManager.__init__` calls `set_restricted_applicator(...)` once (idempotent). Do **not** put NATS/Gateway hostnames into the default allowlist or into preflight required set — worker control-plane stays on the parent process (T15 child-only cut).
7. **Tests (no root):** allow/deny preflight matrix; empty allowlist fail closed; flag-off defaults unchanged; applicator registration; never require nft.

**Pros:** Testable on GHA; plugs T15 seam; explicit allowlist; fail closed; no new packages; truthful backend name.  
**Cons:** Does **not** meet Seam H “others blocked” at the **kernel**; a compromised/curious child CLI can still dial arbitrary hosts after preflight. Must document as **policy Restricted**, with OS selective egress as follow-up (image+nft/proxy ticket), not as Landlock (T17).

**Honesty note for PRD/spec:** Ticket AC “allowlist allow/deny” is satisfied by preflight. Full Seam H OS deny is **deferred** and called out — better than silent Full or fake Restricted-as-empty-netns.

### Approach B — Restricted ≡ None + documented exception

**How:** Restricted applicator returns the same unshare plan as `none` (`backend="unshare_netns"`), and allowlist is only used by docs / default-offline messaging (“these hosts would be needed when a real Restricted backend exists”). Or: non-empty allowlist → fail closed with “selective egress unavailable”.

**Pros:** Real offline; reuses T15 probe.  
**Cons:** LLM agents cannot call APIs under Restricted; weak match to ticket “allowlist hosts reachable”; forces Full for any LLM work (operators churn flags).

### Approach C — Forced HTTP CONNECT / allowlist proxy

**How:** Parent runs a tiny allowlist proxy; child gets `HTTPS_PROXY` and (ideally) empty netns + veth to proxy only.

**Pros:** Closest to Seam H.  
**Cons:** New service, net plumbing, image deps; not practical for stock GHA MVP; large scope vs T16 vertical slice.

## 6. Wiring map (Approach A)

```text
UC_NETWORK_DEFAULT_RESTRICTED=1  ──► resolve_network_default() → "restricted"
UC_NETWORK_ALLOWLIST=api…        ──► parse_allowlist()
UC_NETWORK_MODE_ENFORCE=1        ──► plan_network_mode(config.network)
                                      └─ restricted → registered applicator
                                           └─ preflight + backend=allowlist_preflight

SandboxManager.__init__:
  set_restricted_applicator(plan_restricted_allowlist)   # always register; only used when enforce+restricted

Construction sites (flag on only):
  nats_worker SandboxConfig(network=resolve_network_default(), ...)
  dsec_sandbox_config / helpers reading DSEC_DEFAULTS via resolve
  Worker(...) if it builds default SandboxConfig
```

**NATS/Gateway:** never added to `UC_NETWORK_ALLOWLIST` defaults; preflight does not require them; child argv wrap still does not touch worker sockets.

**Never silently change Full:** dataclass field default stays Full; `DSEC_DEFAULTS` static dict may keep Full and resolve at `dsec_sandbox_config()` time when flag on — or document that static dict remains Full and only the helper applies the flag (preferred: helper, so importing `DSEC_DEFAULTS` alone does not mutate semantics).

## 7. Default LLM host examples (documentation only)

Do **not** auto-enable these. Example allowlist strings for operators:

- `api.anthropic.com`
- `api.openai.com`
- `api.deepseek.com`
- `api.x.ai` / xAI endpoints used by grok-build
- `api.xiaomimimo.com`

Gateway (`UC_GATEWAY_ADDR`) and NATS URLs stay off this list by design.

## 8. Test matrix (Approach A)

| Case | Platform |
|------|----------|
| `UC_NETWORK_DEFAULT_RESTRICTED` default off → Full defaults | Any |
| Flag on → `resolve_network_default() == restricted` | Any |
| Enforce on + Restricted + empty allowlist → fail closed | Any |
| Enforce on + Restricted + allowlist allows intended BASE_URL host → plan backend `allowlist_preflight` | Any |
| Enforce on + Restricted + intended host not in allowlist → `NetworkModeError` | Any |
| Allowlist must not require NATS/Gateway hosts for spawn success | Any (unit) |
| Flag off → spawn argv unchanged (T15 regression) | Any |
| Live None still works when `network=none` (T15) | Linux + userns else skip |
| Optional live: Restricted preflight path still has parent NATS-shaped connectivity on worker | Doc + unit |

## 9. Out of invent

- Landlock / `UC_LANDLOCK_ENFORCE` (T17)
- Changing Full default with flag off
- Putting NATS/Gateway on child network allowlist
- Reviving DockerSandbox
- Claiming kernel “others blocked” without nft/proxy follow-up
- Silent Full when Restricted cannot be applied

## 10. Related paths

- `python/ultimate_coders/agent/network_mode.py`
- `python/ultimate_coders/agent/sandbox.py` (`SandboxConfig`, `SandboxManager.__init__`, `_execute_subprocess`)
- `python/ultimate_coders/agent/sandbox_dsec.py` (`DSEC_DEFAULTS`, `dsec_sandbox_config`)
- `python/ultimate_coders/nats_worker.py` (SandboxConfig construction)
- `tests/python/test_network_mode_enforce.py`
- `.trellis/spec/backend/network-mode-spec.md` (extend or add allowlist section)
- Ticket 16 / D8 / spec-p2 Seam H
- T15 archive research: `.trellis/tasks/archive/2026-10/10-26-t15-networkmode-real-enforcement/research/grounding.md`
