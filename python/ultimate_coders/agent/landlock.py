"""Optional Landlock filesystem restriction for the agent child (P2 / D8 / T17).

``UC_LANDLOCK_ENFORCE`` defaults **off**. When off, spawn argv is unchanged.
When on, the agent child is wrapped with a ctypes Landlock helper that:

1. Sets ``PR_SET_NO_NEW_PRIVS``
2. Builds a ruleset (RO beneath ``/``, RW beneath worktree + ``/tmp`` / TMPDIR)
3. Calls ``landlock_restrict_self``
4. ``exec``s the remaining command

Child-only: never call ``landlock_restrict_self`` in the worker parent
(NATS / Gateway must keep working). Compose with NetworkMode by applying
Landlock **inner** and NetworkMode **outer** (``unshare`` must run before
Landlock sets ``PR_SET_NO_NEW_PRIVS``, or uid_map writes fail).

Fail closed: if enforce is on and Landlock cannot be applied, refuse start —
never silently run unrestricted FS. Non-Linux / ABI-unavailable platforms
raise :class:`LandlockError` under enforce.

Optional escape hatches (not required for AC): ``UC_LANDLOCK_EXTRA_RW`` /
``UC_LANDLOCK_EXTRA_RO`` (path lists, same separators as network allowlist).
"""

from __future__ import annotations

import os
import sys

# When this file is executed as a script, Python prepends its directory to
# ``sys.path``, which shadows the stdlib ``types`` module with
# ``agent/types.py``. Pop that entry before any further imports.
if __name__ == "__main__":  # pragma: no cover - script entry
    _here = os.path.dirname(os.path.abspath(__file__))
    while sys.path and os.path.abspath(sys.path[0] or ".") == _here:
        sys.path.pop(0)

import argparse
import ctypes
import logging
import platform
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

LANDLOCK_ENFORCE_ENV = "UC_LANDLOCK_ENFORCE"
LANDLOCK_EXTRA_RW_ENV = "UC_LANDLOCK_EXTRA_RW"
LANDLOCK_EXTRA_RO_ENV = "UC_LANDLOCK_EXTRA_RO"

_TRUTHY_ENV = frozenset({"1", "true", "yes", "on"})
_PATH_SPLIT_RE = re.compile(r"[\s,;]+")

#: Linux syscall numbers for Landlock (stable across x86_64 and aarch64).
_LANDLOCK_SYS: dict[str, tuple[int, int, int]] = {
    "x86_64": (444, 445, 446),
    "aarch64": (444, 445, 446),
}

PR_SET_NO_NEW_PRIVS = 38
LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
LANDLOCK_RULE_PATH_BENEATH = 1

# Filesystem access rights (linux/landlock.h).
ACCESS_FS_EXECUTE = 1 << 0
ACCESS_FS_WRITE_FILE = 1 << 1
ACCESS_FS_READ_FILE = 1 << 2
ACCESS_FS_READ_DIR = 1 << 3
ACCESS_FS_REMOVE_DIR = 1 << 4
ACCESS_FS_REMOVE_FILE = 1 << 5
ACCESS_FS_MAKE_CHAR = 1 << 6
ACCESS_FS_MAKE_DIR = 1 << 7
ACCESS_FS_MAKE_REG = 1 << 8
ACCESS_FS_MAKE_SOCK = 1 << 9
ACCESS_FS_MAKE_FIFO = 1 << 10
ACCESS_FS_MAKE_BLOCK = 1 << 11
ACCESS_FS_MAKE_SYM = 1 << 12
ACCESS_FS_REFER = 1 << 13
ACCESS_FS_TRUNCATE = 1 << 14

ACCESS_FS_ABI1 = (
    ACCESS_FS_EXECUTE
    | ACCESS_FS_WRITE_FILE
    | ACCESS_FS_READ_FILE
    | ACCESS_FS_READ_DIR
    | ACCESS_FS_REMOVE_DIR
    | ACCESS_FS_REMOVE_FILE
    | ACCESS_FS_MAKE_CHAR
    | ACCESS_FS_MAKE_DIR
    | ACCESS_FS_MAKE_REG
    | ACCESS_FS_MAKE_SOCK
    | ACCESS_FS_MAKE_FIFO
    | ACCESS_FS_MAKE_BLOCK
    | ACCESS_FS_MAKE_SYM
)

ACCESS_FS_READ = ACCESS_FS_EXECUTE | ACCESS_FS_READ_FILE | ACCESS_FS_READ_DIR

#: Product Landlock backend label — ctypes ABI restrict_self then exec.
LANDLOCK_ABI_BACKEND = "landlock_abi"

#: Injectable planner for tests / alternate backends.
LandlockApplicator = Callable[[str], "LandlockApplyPlan"]

_injected_applicator: LandlockApplicator | None = None


class LandlockError(Exception):
    """Fail-closed signal: enforce is on but Landlock cannot be applied."""


@dataclass(frozen=True)
class LandlockApplyPlan:
    """Planned argv wrap for one agent-child Landlock restriction."""

    argv_prefix: tuple[str, ...] = ()
    backend: str = "noop"
    ro_paths: tuple[str, ...] = ()
    rw_paths: tuple[str, ...] = ()


def _env_truthy(name: str) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return raw in _TRUTHY_ENV


def landlock_enforce_enabled() -> bool:
    """Whether Landlock FS enforce is on; default off."""
    return _env_truthy(LANDLOCK_ENFORCE_ENV)


def set_landlock_applicator(applicator: LandlockApplicator | None) -> None:
    """Inject a planner for tests / alternate backends."""
    global _injected_applicator
    _injected_applicator = applicator


def clear_landlock_applicator() -> None:
    """Clear the injectable planner override (tests)."""
    set_landlock_applicator(None)


def reset_landlock_state_for_tests() -> None:
    """Clear injectables and the platform probe cache (test isolation)."""
    clear_landlock_applicator()
    _probe_landlock_abi.cache_clear()


def _landlock_syscalls() -> tuple[int, int, int] | None:
    if sys.platform != "linux":
        return None
    return _LANDLOCK_SYS.get(platform.machine())


def landlock_abi_version() -> int:
    """Highest supported Landlock ABI, or ``0`` if unavailable."""
    return _probe_landlock_abi()


@lru_cache(maxsize=1)
def _probe_landlock_abi() -> int:
    syscalls = _landlock_syscalls()
    if syscalls is None:
        return 0
    sys_create, _, _ = syscalls
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    abi = int(
        libc.syscall(
            sys_create,
            None,
            ctypes.c_size_t(0),
            LANDLOCK_CREATE_RULESET_VERSION,
        )
    )
    if abi < 1:
        return 0
    return abi


def platform_supports_landlock() -> bool:
    """Whether this process can apply Landlock FS (Linux + ABI ≥ 1)."""
    return landlock_abi_version() >= 1


def _parse_path_list(raw: str | None) -> tuple[str, ...]:
    if not raw or not str(raw).strip():
        return ()
    paths: list[str] = []
    seen: set[str] = set()
    for part in _PATH_SPLIT_RE.split(str(raw).strip()):
        if not part:
            continue
        normalized = os.path.abspath(os.path.expanduser(part))
        if normalized in seen:
            continue
        seen.add(normalized)
        paths.append(normalized)
    return tuple(paths)


def parse_extra_rw(raw: str | None = None) -> tuple[str, ...]:
    """Parse ``UC_LANDLOCK_EXTRA_RW`` (or ``raw``) into absolute paths."""
    text = raw if raw is not None else os.environ.get(LANDLOCK_EXTRA_RW_ENV, "")
    return _parse_path_list(text)


def parse_extra_ro(raw: str | None = None) -> tuple[str, ...]:
    """Parse ``UC_LANDLOCK_EXTRA_RO`` (or ``raw``) into absolute paths."""
    text = raw if raw is not None else os.environ.get(LANDLOCK_EXTRA_RO_ENV, "")
    return _parse_path_list(text)


def resolve_landlock_paths(
    working_dir: str,
    *,
    environ: dict[str, str] | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Derive MVP RO / RW path sets from spawn cwd and optional extras.

    RO: ``/`` plus ``UC_LANDLOCK_EXTRA_RO``.
    RW: absolute ``working_dir``, ``/tmp``, absolute ``TMPDIR`` when set,
    plus ``UC_LANDLOCK_EXTRA_RW``.
    """
    env = environ if environ is not None else os.environ
    wd = (working_dir or "").strip()
    if not wd:
        raise LandlockError(
            "Landlock enforce is on, but working_dir is empty. "
            "Refusing start — will not silently run unrestricted FS."
        )
    rw_root = os.path.abspath(os.path.expanduser(wd))
    if not os.path.isdir(rw_root):
        raise LandlockError(
            "Landlock enforce is on, but working_dir "
            f"{rw_root!r} is missing or not a directory. "
            "Refusing start — will not silently run unrestricted FS."
        )

    ro: list[str] = ["/"]
    rw: list[str] = [rw_root, "/tmp"]

    # Spec: only absolute TMPDIR (relative values are ignored — do not
    # silently promote them via abspath against the worker cwd).
    tmpdir = (env.get("TMPDIR") or "").strip()
    if tmpdir:
        expanded = os.path.expanduser(tmpdir)
        if os.path.isabs(expanded):
            abs_tmp = os.path.abspath(expanded)
            if abs_tmp not in rw:
                rw.append(abs_tmp)

    for path in parse_extra_ro(env.get(LANDLOCK_EXTRA_RO_ENV)):
        if path not in ro:
            ro.append(path)
    for path in parse_extra_rw(env.get(LANDLOCK_EXTRA_RW_ENV)):
        if path not in rw:
            rw.append(path)

    return tuple(ro), tuple(rw)


def _handled_access_fs(abi: int) -> int:
    access = ACCESS_FS_ABI1
    if abi >= 2:
        access |= ACCESS_FS_REFER
    if abi >= 3:
        access |= ACCESS_FS_TRUNCATE
    return access


def _access_read(handled: int) -> int:
    return ACCESS_FS_READ & handled


def _access_rw(handled: int) -> int:
    return handled


class _RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
    ]


class _PathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]
    _pack_ = 1


def apply_landlock_self(
    ro_paths: Sequence[str],
    rw_paths: Sequence[str],
) -> None:
    """Apply Landlock to the **current** process (helper / tests only).

    Requires ``PR_SET_NO_NEW_PRIVS`` then ``landlock_restrict_self``. Raises
    :class:`LandlockError` on any failure. Must not be called in the worker
    parent process.
    """
    syscalls = _landlock_syscalls()
    if syscalls is None:
        raise LandlockError(
            "Landlock syscalls are unavailable on this platform "
            f"(os={sys.platform!r}, arch={platform.machine()!r})."
        )
    abi = landlock_abi_version()
    if abi < 1:
        raise LandlockError(
            "Landlock ABI is unavailable on this kernel. "
            "Refusing to restrict — will not silently run unrestricted FS."
        )

    sys_create, sys_add, sys_restrict = syscalls
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    libc.syscall.restype = ctypes.c_long

    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise LandlockError(
            f"prctl(PR_SET_NO_NEW_PRIVS) failed (errno={err}). "
            "Refusing start — will not silently run unrestricted FS."
        )

    handled = _handled_access_fs(abi)
    attr = _RulesetAttr(handled, 0)
    ruleset_fd = int(
        libc.syscall(sys_create, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    )
    if ruleset_fd < 0:
        err = ctypes.get_errno()
        raise LandlockError(
            f"landlock_create_ruleset failed (errno={err}). "
            "Refusing start — will not silently run unrestricted FS."
        )

    try:
        read_bits = _access_read(handled)
        rw_bits = _access_rw(handled)
        for path in ro_paths:
            _add_path_beneath(libc, sys_add, ruleset_fd, path, read_bits)
        for path in rw_paths:
            _add_path_beneath(libc, sys_add, ruleset_fd, path, rw_bits)
        if int(libc.syscall(sys_restrict, ruleset_fd, 0)) != 0:
            err = ctypes.get_errno()
            raise LandlockError(
                f"landlock_restrict_self failed (errno={err}). "
                "Refusing start — will not silently run unrestricted FS."
            )
    finally:
        try:
            os.close(ruleset_fd)
        except OSError:
            pass


def _add_path_beneath(
    libc: Any,
    sys_add: int,
    ruleset_fd: int,
    path: str,
    allowed_access: int,
) -> None:
    if not path:
        raise LandlockError("Landlock path rule is empty.")
    if not os.path.exists(path):
        raise LandlockError(
            f"Landlock path {path!r} does not exist. "
            "Refusing start — will not silently run unrestricted FS."
        )
    parent_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    try:
        pb = _PathBeneathAttr(allowed_access, parent_fd)
        rc = int(
            libc.syscall(
                sys_add,
                ruleset_fd,
                LANDLOCK_RULE_PATH_BENEATH,
                ctypes.byref(pb),
                0,
            )
        )
        if rc != 0:
            err = ctypes.get_errno()
            raise LandlockError(
                f"landlock_add_rule({path!r}) failed (errno={err}). "
                "Refusing start — will not silently run unrestricted FS."
            )
    finally:
        os.close(parent_fd)


def _landlock_helper_argv() -> tuple[str, ...]:
    """``python <landlock.py>`` tokens for argv wrap.

    Invoke by file path (not ``-m``) so the child does not need
    ``PYTHONPATH`` / an installed egg — the sandbox env allowlist strips
    most host vars, and the helper is stdlib-only.
    """
    return (sys.executable, os.path.abspath(__file__))


def _build_argv_prefix(
    ro_paths: Iterable[str],
    rw_paths: Iterable[str],
) -> tuple[str, ...]:
    parts: list[str] = [*_landlock_helper_argv()]
    for path in ro_paths:
        parts.extend(["--ro", path])
    for path in rw_paths:
        parts.extend(["--rw", path])
    parts.append("--")
    return tuple(parts)


def plan_landlock(
    working_dir: str,
    *,
    enforce: bool | None = None,
    environ: dict[str, str] | None = None,
) -> LandlockApplyPlan:
    """Plan argv wrap for Landlock under the enforce flag.

    When ``enforce`` is None, reads :func:`landlock_enforce_enabled`.
    Enforce off → noop. Enforce on → ctypes helper wrap, or injected
    applicator, or :class:`LandlockError` when unavailable / unusable.
    """
    active = landlock_enforce_enabled() if enforce is None else bool(enforce)
    if not active:
        return LandlockApplyPlan(backend="noop")

    if _injected_applicator is not None:
        return _injected_applicator(working_dir)

    if not platform_supports_landlock():
        raise LandlockError(
            "Landlock enforce is on, but Landlock is unavailable on this "
            "platform (need Linux with ABI ≥ 1). Refusing start — will not "
            "silently run unrestricted FS."
        )

    ro_paths, rw_paths = resolve_landlock_paths(working_dir, environ=environ)
    prefix = _build_argv_prefix(ro_paths, rw_paths)
    logger.info(
        "Landlock enforce: backend=%s abi=%s ro=%s rw=%s",
        LANDLOCK_ABI_BACKEND,
        landlock_abi_version(),
        ",".join(ro_paths),
        ",".join(rw_paths),
    )
    return LandlockApplyPlan(
        argv_prefix=prefix,
        backend=LANDLOCK_ABI_BACKEND,
        ro_paths=ro_paths,
        rw_paths=rw_paths,
    )


def apply_landlock_argv(
    command: str,
    args: list[str] | tuple[str, ...],
    plan: LandlockApplyPlan,
) -> tuple[str, list[str]]:
    """Prefix ``command``/``args`` with ``plan.argv_prefix`` (pure)."""
    prefix = plan.argv_prefix
    if not prefix:
        return command, list(args)
    new_command = prefix[0]
    new_args = [*prefix[1:], command, *args]
    return new_command, new_args


def describe_plan(plan: LandlockApplyPlan) -> dict[str, Any]:
    """Small observability dict for logs / failure stderr."""
    return {
        "backend": plan.backend,
        "argv_prefix": list(plan.argv_prefix),
        "ro_paths": list(plan.ro_paths),
        "rw_paths": list(plan.rw_paths),
    }


def _parse_helper_argv(argv: Sequence[str]) -> tuple[list[str], list[str], list[str]]:
    parser = argparse.ArgumentParser(
        prog="python landlock.py",
        description="Apply Landlock FS rules then exec the remaining command.",
    )
    parser.add_argument(
        "--ro",
        action="append",
        default=[],
        dest="ro",
        metavar="PATH",
        help="Path granted read/exec/readdir (repeatable).",
    )
    parser.add_argument(
        "--rw",
        action="append",
        default=[],
        dest="rw",
        metavar="PATH",
        help="Path granted read+write (repeatable).",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to exec after '--'.",
    )
    # Support both ``… -- cmd`` and bare remainder after options.
    ns = parser.parse_args(list(argv))
    cmd = list(ns.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        parser.error("missing command after '--'")
    return list(ns.ro), list(ns.rw), cmd


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry: apply Landlock then ``os.execvp`` the remaining argv."""
    ro, rw, cmd = _parse_helper_argv(argv if argv is not None else sys.argv[1:])
    try:
        apply_landlock_self(ro, rw)
    except LandlockError as exc:
        print(f"landlock: {exc}", file=sys.stderr)
        return 1
    try:
        os.execvp(cmd[0], cmd)
    except OSError as exc:
        print(f"landlock: exec failed: {exc}", file=sys.stderr)
        return 127
    return 0  # pragma: no cover — execvp does not return on success


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main())
