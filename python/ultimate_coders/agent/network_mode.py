"""Real NetworkMode enforcement at agent-subprocess spawn (P2 / D8).

``UC_NETWORK_MODE_ENFORCE`` defaults **off**. When off, spawn argv is unchanged
(compatible Full behaviour). When on, ``SandboxConfig.network`` is applied to
the **agent child only** via an argv prefix (or a registered Restricted
applicator); the worker process keeps NATS/Gateway connectivity.

Fail closed: if enforce is on and the platform cannot apply the requested
mode, refuse start — never silently run Full.

T16 product policy (also default **off**):

* ``UC_NETWORK_DEFAULT_RESTRICTED`` — construction helpers resolve default
  ``network`` to ``restricted`` instead of Full (dataclass literal stays Full).
* ``UC_NETWORK_ALLOWLIST`` — comma/space hostnames for Restricted preflight.
* ``UC_NETWORK_DEFAULT_MODE`` — optional superseding resolver
  (``full`` / ``restricted`` / ``none``).

Restricted MVP backend is ``allowlist_preflight`` (policy gate, **noop argv**).
Kernel selective egress (nft/proxy) is deferred; do not pretend empty-netns is
Restricted. NATS/Gateway hosts are **not** on the child allowlist requirement.
Landlock FS is T17.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

NETWORK_MODE_ENFORCE_ENV = "UC_NETWORK_MODE_ENFORCE"
NETWORK_DEFAULT_RESTRICTED_ENV = "UC_NETWORK_DEFAULT_RESTRICTED"
NETWORK_ALLOWLIST_ENV = "UC_NETWORK_ALLOWLIST"
NETWORK_DEFAULT_MODE_ENV = "UC_NETWORK_DEFAULT_MODE"

_TRUTHY_ENV = frozenset({"1", "true", "yes", "on"})

_VALID_MODES = frozenset({"none", "restricted", "full"})

#: Rootless Linux empty-netns wrap for ``none`` (user + net namespaces).
UNSHARE_NETNS_PREFIX: tuple[str, ...] = (
    "unshare",
    "--user",
    "--map-root-user",
    "--net",
    "--",
)

#: Honest Restricted MVP backend label — policy preflight only; no kernel
#: selective egress. Do not rename to imply nft/proxy.
ALLOWLIST_PREFLIGHT_BACKEND = "allowlist_preflight"

#: LLM/API base-URL env vars whose hosts (when set) must be ⊆ allowlist under
#: Restricted+enforce. Worker control-plane (NATS / Gateway) is intentionally
#: absent — those sockets stay on the parent process.
LLM_BASE_URL_ENVS: tuple[str, ...] = (
    "ANTHROPIC_BASE_URL",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "DEEPSEEK_BASE_URL",
    "MIMO_BASE_URL",
    "MEMORY_LLM_BASE_URL",
    "MEMORY_EMBEDDER_BASE_URL",
)

#: Optional Restricted-mode planner (T16). Signature matches
#: ``(mode: str) -> NetworkApplyPlan`` and may raise :class:`NetworkModeError`.
NetworkModeApplicator = Callable[[str], "NetworkApplyPlan"]

_restricted_applicator: NetworkModeApplicator | None = None
#: Test/injectable override for any mode (including none). When set, used
#: instead of the built-in planner for enforce-on paths.
_injected_applicator: NetworkModeApplicator | None = None

_ALLOWLIST_SPLIT_RE = re.compile(r"[\s,;]+")


class NetworkModeError(Exception):
    """Fail-closed signal: enforce is on but the mode cannot be applied."""


@dataclass(frozen=True)
class NetworkApplyPlan:
    """Planned argv wrap for one agent-child spawn."""

    mode: str
    argv_prefix: tuple[str, ...] = ()
    backend: str = "noop"

    def __post_init__(self) -> None:
        if self.mode not in _VALID_MODES:
            raise ValueError(f"unknown network mode: {self.mode!r}")


def _env_truthy(name: str) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return raw in _TRUTHY_ENV


def network_mode_enforce_enabled() -> bool:
    """Whether NetworkMode enforce is on; default off."""
    return _env_truthy(NETWORK_MODE_ENFORCE_ENV)


def network_default_restricted_enabled() -> bool:
    """Whether construction defaults resolve to Restricted; default off."""
    return _env_truthy(NETWORK_DEFAULT_RESTRICTED_ENV)


def normalize_network_mode(mode: str) -> str:
    """Normalize a network mode string to ``none`` / ``restricted`` / ``full``.

    Raises:
        ValueError: unknown mode.
    """
    normalized = (mode or "").strip().lower()
    if normalized not in _VALID_MODES:
        raise ValueError(f"unknown network mode: {mode!r}")
    return normalized


def resolve_network_default() -> str:
    """Resolve product default ``network`` for construction helpers.

    Does **not** mutate ``SandboxConfig.network`` / ``DSEC_DEFAULTS`` literals.
    Priority:

    1. ``UC_NETWORK_DEFAULT_MODE`` when set to a valid mode (supersedes bool).
    2. ``restricted`` when ``UC_NETWORK_DEFAULT_RESTRICTED`` is truthy.
    3. ``full`` otherwise (prior compatible default).
    """
    raw_mode = os.environ.get(NETWORK_DEFAULT_MODE_ENV, "").strip()
    if raw_mode:
        return normalize_network_mode(raw_mode)
    if network_default_restricted_enabled():
        return "restricted"
    return "full"


def normalize_allowlist_host(value: str) -> str:
    """Extract a lowercase hostname from a URL or bare host (port ignored)."""
    raw = (value or "").strip()
    if not raw:
        return ""
    # Bare host / host:port — urlparse needs a netloc form.
    candidate = raw if "://" in raw else f"//{raw}"
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").strip().lower()
    return host


def parse_network_allowlist(
    raw: str | None = None,
) -> frozenset[str]:
    """Parse ``UC_NETWORK_ALLOWLIST`` (or ``raw``) into normalized hostnames.

    Separators: comma, semicolon, or whitespace. Empty / unset → empty set.
    """
    text = (
        raw
        if raw is not None
        else os.environ.get(NETWORK_ALLOWLIST_ENV, "")
    )
    hosts: set[str] = set()
    for part in _ALLOWLIST_SPLIT_RE.split(text.strip()):
        if not part:
            continue
        host = normalize_allowlist_host(part)
        if host:
            hosts.add(host)
    return frozenset(hosts)


def intended_llm_hosts(
    environ: dict[str, str] | None = None,
) -> frozenset[str]:
    """Hosts from present LLM/API ``*_BASE_URL`` env vars (preflight set).

    Unset URL envs contribute nothing. NATS/Gateway addresses are never
    included here — worker control-plane stays off the child allowlist.
    """
    env = environ if environ is not None else os.environ
    hosts: set[str] = set()
    for name in LLM_BASE_URL_ENVS:
        value = env.get(name, "")
        if not value or not str(value).strip():
            continue
        host = normalize_allowlist_host(str(value))
        if host:
            hosts.add(host)
    return frozenset(hosts)


def plan_restricted_allowlist(
    mode: str = "restricted",
    *,
    allowlist: frozenset[str] | None = None,
    intended: frozenset[str] | None = None,
) -> NetworkApplyPlan:
    """Restricted applicator: explicit allowlist preflight (T16 MVP).

    * Empty allowlist → :class:`NetworkModeError` (fail closed; never open world).
    * Every present intended LLM/API host must be ⊆ allowlist.
    * Returns ``backend=allowlist_preflight`` with **noop argv** — kernel
      selective egress is **not** active; this is a policy gate only.

    Extra allowlist entries are fine. NATS/Gateway hosts are not required.
    """
    normalized = normalize_network_mode(mode)
    if normalized != "restricted":
        raise NetworkModeError(
            f"Restricted applicator invoked with mode={normalized!r}; "
            "expected restricted"
        )

    hosts = parse_network_allowlist() if allowlist is None else frozenset(allowlist)
    if not hosts:
        raise NetworkModeError(
            "NetworkMode enforce is on and mode=restricted, but "
            f"{NETWORK_ALLOWLIST_ENV} is empty. Refusing start — Restricted "
            "requires an explicit LLM/API host allowlist (will not silently "
            "run Full)."
        )

    needed = intended_llm_hosts() if intended is None else frozenset(intended)
    missing = sorted(needed - hosts)
    if missing:
        raise NetworkModeError(
            "NetworkMode enforce is on and mode=restricted, but intended "
            f"LLM/API host(s) are not in {NETWORK_ALLOWLIST_ENV}: "
            f"{', '.join(missing)}. Refusing start — will not silently run Full."
        )

    logger.info(
        "NetworkMode Restricted: backend=%s allowlist=%s intended=%s "
        "(policy preflight only; kernel selective egress not applied)",
        ALLOWLIST_PREFLIGHT_BACKEND,
        ",".join(sorted(hosts)),
        ",".join(sorted(needed)) if needed else "(none)",
    )
    return NetworkApplyPlan(
        mode="restricted",
        argv_prefix=(),
        backend=ALLOWLIST_PREFLIGHT_BACKEND,
    )


def set_restricted_applicator(applicator: NetworkModeApplicator | None) -> None:
    """Register (or clear) the Restricted-mode applicator (T16 seam)."""
    global _restricted_applicator
    _restricted_applicator = applicator


def set_network_mode_applicator(applicator: NetworkModeApplicator | None) -> None:
    """Inject a planner for tests / alternate backends (any mode)."""
    global _injected_applicator
    _injected_applicator = applicator


def clear_network_mode_applicator() -> None:
    """Clear the injectable planner override (tests)."""
    set_network_mode_applicator(None)


def platform_supports_unshare_netns() -> bool:
    """Whether rootless ``unshare --user --map-root-user --net`` works here.

    Cheap cached probe: requires Linux, ``unshare`` on PATH, and a successful
    empty-netns spawn. GitHub runners with userns disabled should skip live
    None probes via this helper.
    """
    return _probe_unshare_netns()


@lru_cache(maxsize=1)
def _probe_unshare_netns() -> bool:
    if os.name != "posix":
        return False
    if not shutil.which("unshare"):
        return False
    try:
        completed = subprocess.run(
            [*UNSHARE_NETNS_PREFIX, "true"],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def plan_network_mode(
    mode: str,
    *,
    enforce: bool | None = None,
    restricted_applicator: NetworkModeApplicator | None = None,
) -> NetworkApplyPlan:
    """Plan argv wrap for ``mode`` under the enforce flag.

    When ``enforce`` is None, reads :func:`network_mode_enforce_enabled`.
    Enforce off → noop plan for any valid mode (compatible Full spawn).
    Enforce on + ``full`` → noop. Enforce on + ``none`` → unshare netns wrap
    (or injected applicator). Enforce on + ``restricted`` → registered /
    passed Restricted applicator, else :class:`NetworkModeError`.
    """
    normalized = normalize_network_mode(mode)
    active = network_mode_enforce_enabled() if enforce is None else bool(enforce)

    if not active:
        return NetworkApplyPlan(mode=normalized, argv_prefix=(), backend="noop")

    if _injected_applicator is not None:
        return _injected_applicator(normalized)

    if normalized == "full":
        return NetworkApplyPlan(mode="full", argv_prefix=(), backend="noop")

    if normalized == "none":
        return _plan_none()

    # restricted
    applicator = (
        restricted_applicator if restricted_applicator is not None else _restricted_applicator
    )
    if applicator is None:
        raise NetworkModeError(
            "NetworkMode enforce is on and mode=restricted, but no Restricted "
            "applicator is registered (T16). Refusing start — will not silently "
            "run Full."
        )
    plan = applicator("restricted")
    if plan.mode != "restricted":
        raise NetworkModeError(
            f"Restricted applicator returned mode={plan.mode!r}; expected restricted"
        )
    return plan


def _plan_none() -> NetworkApplyPlan:
    if not platform_supports_unshare_netns():
        raise NetworkModeError(
            "NetworkMode enforce is on and mode=none, but rootless "
            "unshare user+net namespaces are unavailable on this platform. "
            "Refusing start — will not silently run Full."
        )
    return NetworkApplyPlan(
        mode="none",
        argv_prefix=UNSHARE_NETNS_PREFIX,
        backend="unshare_netns",
    )


def apply_network_argv(
    command: str,
    args: list[str] | tuple[str, ...],
    plan: NetworkApplyPlan,
) -> tuple[str, list[str]]:
    """Prefix ``command``/``args`` with ``plan.argv_prefix`` (pure)."""
    prefix = plan.argv_prefix
    if not prefix:
        return command, list(args)
    # First prefix token becomes the new executable; remainder + original
    # command/args follow (e.g. unshare … -- <cmd> <args>).
    new_command = prefix[0]
    new_args = [*prefix[1:], command, *args]
    return new_command, new_args


def reset_network_mode_state_for_tests() -> None:
    """Clear injectables and the unshare probe cache (test isolation)."""
    clear_network_mode_applicator()
    set_restricted_applicator(None)
    _probe_unshare_netns.cache_clear()


def describe_plan(plan: NetworkApplyPlan) -> dict[str, Any]:
    """Small observability dict for logs / failure stderr."""
    return {
        "mode": plan.mode,
        "backend": plan.backend,
        "argv_prefix": list(plan.argv_prefix),
    }
