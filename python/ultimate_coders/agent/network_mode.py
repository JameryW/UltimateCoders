"""Real NetworkMode enforcement at agent-subprocess spawn (P2 / D8 step 1).

``UC_NETWORK_MODE_ENFORCE`` defaults **off**. When off, spawn argv is unchanged
(compatible Full behaviour). When on, ``SandboxConfig.network`` is applied to
the **agent child only** via an argv prefix (or a registered Restricted
applicator); the worker process keeps NATS/Gateway connectivity.

Fail closed: if enforce is on and the platform cannot apply the requested
mode, refuse start — never silently run Full. Restricted host allowlist
product policy is T16; until a Restricted applicator is registered, Restricted
under enforce raises :class:`NetworkModeError`. Landlock FS is T17.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

NETWORK_MODE_ENFORCE_ENV = "UC_NETWORK_MODE_ENFORCE"

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

#: Optional Restricted-mode planner (T16). Signature matches
#: ``(mode: str) -> NetworkApplyPlan`` and may raise :class:`NetworkModeError`.
NetworkModeApplicator = Callable[[str], "NetworkApplyPlan"]

_restricted_applicator: NetworkModeApplicator | None = None
#: Test/injectable override for any mode (including none). When set, used
#: instead of the built-in planner for enforce-on paths.
_injected_applicator: NetworkModeApplicator | None = None


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


def network_mode_enforce_enabled() -> bool:
    """Whether NetworkMode enforce is on; default off."""
    raw = os.environ.get(NETWORK_MODE_ENFORCE_ENV, "").strip().lower()
    return raw in _TRUTHY_ENV


def normalize_network_mode(mode: str) -> str:
    """Normalize a network mode string to ``none`` / ``restricted`` / ``full``.

    Raises:
        ValueError: unknown mode.
    """
    normalized = (mode or "").strip().lower()
    if normalized not in _VALID_MODES:
        raise ValueError(f"unknown network mode: {mode!r}")
    return normalized


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
