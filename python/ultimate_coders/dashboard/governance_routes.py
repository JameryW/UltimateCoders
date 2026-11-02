"""Composition point for the governance HTTP routers (PR04 / PR05 / PR06).

The three routers live in their own modules because each belongs to a different
concern — inbound triggers, attention/worklist, proactive control:

* :mod:`ultimate_coders.dashboard.trigger_api`   — ``/api/triggers/*``
* :mod:`ultimate_coders.dashboard.attention_api`  — ``/api/attention``,
  ``/api/capacity``, ``/api/cases*``
* :mod:`ultimate_coders.dashboard.proactive_api` — ``/api/proactive/*``, ``/api/watchdog/*``
* :mod:`ultimate_coders.dashboard.approval_api` — ``/api/approvals*``

They are included here rather than imported directly in ``app.py`` so that one
broken router (e.g. a missing optional dependency) degrades to a logged warning
for that surface instead of preventing the dashboard from booting at all. That
trade-off is deliberate but **not silent**: every skip is logged with the module
name and the exception, and :func:`governance_routers` returns what it actually
mounted so a test can assert all three are present.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Modules that expose an ``APIRouter`` named ``router``.
GOVERNANCE_ROUTER_MODULES = (
    "ultimate_coders.dashboard.trigger_api",
    "ultimate_coders.dashboard.attention_api",
    "ultimate_coders.dashboard.proactive_api",
    "ultimate_coders.dashboard.approval_api",
)


def governance_routers() -> list[Any]:
    """Return the mounted governance routers, in mount order.

    Missing or broken modules are logged and skipped so the dashboard still
    serves its existing surfaces.
    """
    routers: list[Any] = []
    for module_name in GOVERNANCE_ROUTER_MODULES:
        try:
            module = importlib.import_module(module_name)
        except Exception:  # pragma: no cover - defensive: a router must not brick boot
            logger.warning(
                "Governance router %s could not be imported; its endpoints are unavailable",
                module_name,
                exc_info=True,
            )
            continue
        router = getattr(module, "router", None)
        if router is None:
            logger.warning(
                "Governance module %s exposes no `router`; its endpoints are unavailable",
                module_name,
            )
            continue
        routers.append(router)
    return routers


def mount_governance_routers(app: Any) -> list[Any]:
    """Include the governance routers on ``app`` and keep ``app.routes`` flat.

    Why flatten: some Starlette/FastAPI versions append an ``_IncludedRouter``
    wrapper to ``app.routes`` instead of copying the routes into it (this one
    does, and the wrapper exposes neither ``path`` nor ``routes``). Code that
    iterates ``app.routes`` and reads ``route.path`` — several dashboard tests do,
    including the "no TUI websocket" guard — then fails with ``AttributeError``.

    Flattening keeps the mounted surface identical while preserving the flat
    ``app.routes`` contract the rest of the codebase, and the FastAPI version CI
    may pin, already rely on. Because the wrapper is opaque, the replacement uses
    the router objects *we* mounted (we know exactly which routes they carry):
    when ``include_router`` added exactly one object per router, wrappers are
    swapped for ``router.routes``; when it already flattened (older FastAPI),
    nothing is rewritten.
    """
    mounted = governance_routers()
    if not mounted:
        return mounted
    before = list(app.router.routes)
    for router in mounted:
        app.include_router(router)
    added = list(app.router.routes)[len(before) :]
    if len(added) != len(mounted):
        # Older FastAPI flattened the routes itself; leave its table alone.
        return mounted
    flat = list(before)
    for appended, router in zip(added, mounted):
        if getattr(appended, "path", None) is None:
            flat.extend(router.routes)
        else:
            flat.append(appended)
    app.router.routes[:] = flat
    return mounted


__all__ = [
    "GOVERNANCE_ROUTER_MODULES",
    "governance_routers",
    "mount_governance_routers",
]
