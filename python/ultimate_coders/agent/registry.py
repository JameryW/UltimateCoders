"""Agent adapter plugin registry.

Plugin-based extension point for the Orchestrator's coding agents. An
agent is described by an :class:`AgentPluginSpec` and registered into the
module-level :data:`registry`. Everything that used to be a hard-coded
``if agent == "grok-build" ... elif agent == "codex"`` chain now asks the
registry instead:

- ``sandbox.create_adapter(name)`` / ``SandboxManager._create_adapter``
- ``sandbox.available_agents()``
- ``SandboxConfig._build_env_vars`` (per-agent API key env var)
- ``Worker._derive_capabilities`` (CLI probing for capability advertising)

Two ways to add an agent WITHOUT touching core code:

1. **In-tree plugin** — a module under ``ultimate_coders.agent`` that calls
   ``register_agent(AgentPluginSpec(...))`` at import time and is imported
   by :func:`ensure_builtin_plugins` (or lazily by its own consumers).
   ``harness_deepseek`` is the reference implementation.

2. **External plugin** — either an installed package exposing an entry
   point in the ``ultimate_coders.agent_adapters`` group, or a ``*.py``
   file / package directory listed in the ``UC_AGENT_PLUGINS`` environment
   variable (colon/semicolon-separated paths). Both are discovered by
   :func:`discover_plugins`, which imports them; the plugin module itself
   must call ``register_agent(...)`` at import time. Example::

       # my_agent.py  (UC_AGENT_PLUGINS=/path/to/my_agent.py)
       from ultimate_coders.agent.registry import AgentPluginSpec, register_agent
       from ultimate_coders.agent.sandbox import AgentAdapter

       class MyAdapter(AgentAdapter):
           def name(self): return "my-agent"
           def build_request(self, prompt, working_dir, config, subtask_config=None):
               return {"command": "my-cli", "args": ["-p", prompt], ...}
           def parse_output(self, result):
               return AgentOutput(summary=result.stdout)

       register_agent(AgentPluginSpec(
           name="my-agent", factory=lambda: MyAdapter(),
           api_key_env="MY_API_KEY", cli_probe="my-cli",
       ))
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

#: Entry-point group scanned for installed agent plugin packages.
ENTRY_POINT_GROUP = "ultimate_coders.agent_adapters"

#: Env var with extra plugin import paths (``:``/``;``-separated).
PLUGIN_PATH_ENV = "UC_AGENT_PLUGINS"

Factory = Callable[[], "object"]  # Callable[[], AgentAdapter]


@dataclass(frozen=True)
class AgentPluginSpec:
    """Description of one pluggable coding agent.

    Attributes:
        name: Canonical agent name (the value ``UC_CODING_AGENT`` /
            ``agent=`` accepts). Must be unique across registrations.
        aliases: Alternative names accepted by the same adapter.
        factory: Zero-arg callable constructing a fresh AgentAdapter.
        api_key_env: Env var ``SandboxConfig.api_key`` is exported to for
            this agent (None = no per-agent key mapping).
        cli_probe: Executable name probed on PATH when advertising worker
            capabilities. None = API-backed agent, always advertiseable.
        description: One-line human-readable summary for ``available_agents``.
        discoverable: Whether the agent shows up in capability advertising /
            ``available_agents()``. Internal adapters (like the decompose
            helper) set this to False.
    """

    name: str
    factory: Factory
    aliases: tuple[str, ...] = ()
    api_key_env: str | None = None
    cli_probe: str | None = None
    description: str = ""
    discoverable: bool = True


class AgentAdapterRegistry:
    """Thread-safe registry mapping agent names/aliases to plugin specs."""

    def __init__(self) -> None:
        self._specs: dict[str, AgentPluginSpec] = {}
        self._lock = threading.Lock()

    def register(self, spec: AgentPluginSpec, *, replace: bool = False) -> None:
        """Register a plugin spec under its name and all aliases.

        Args:
            spec: The plugin description.
            replace: Allow overwriting an existing registration. Built-in
                re-registration (module imported twice) is idempotent when
                the factories match; anything else requires replace=True so
                accidental shadowing is loud.
        """
        with self._lock:
            existing = self._specs.get(spec.name)
            if existing is not None:
                if replace or existing == spec:
                    pass  # idempotent re-registration of the same spec
                else:
                    raise ValueError(
                        f"Agent '{spec.name}' already registered "
                        f"(existing={existing!r}, new={spec!r}); pass replace=True to override"
                    )
            self._specs[spec.name] = spec
            for alias in spec.aliases:
                self._specs.setdefault(alias, spec)

    def get_spec(self, name: str) -> AgentPluginSpec | None:
        """Look up the spec for a canonical name or alias."""
        with self._lock:
            return self._specs.get(name)

    def resolve(self, name: str) -> AgentPluginSpec:
        """Like :meth:`get_spec` but raises for unknown names."""
        spec = self.get_spec(name)
        if spec is None:
            raise ValueError(
                f"Unknown agent: {name}. Available: {self.available()}"
            )
        return spec

    def create(self, name: str):
        """Construct a fresh AgentAdapter for the given name/alias."""
        return self.resolve(name).factory()

    def available(self) -> list[str]:
        """All registered agent names INCLUDING aliases and internal helpers.

        Backward-compatible with the pre-registry ``available_agents()``
        list (which also exposed the claude-code-decompose helper).
        Capability advertising uses :meth:`capability_names`, which applies
        the ``discoverable`` filter.
        """
        with self._lock:
            return list(self._specs.keys())

    def api_key_env(self, agent: str) -> str | None:
        """Env var name this agent's API key is exported under, if any."""
        spec = self.get_spec(agent)
        return spec.api_key_env if spec else None

    def capability_names(self, probe: Callable[[str], bool]) -> list[str]:
        """Agent names advertiseable as worker capabilities.

        CLI-backed agents are advertised (under the canonical name AND all
        aliases — the scheduler may match either) only when ``probe(
        cli_probe)`` finds the executable; API-backed agents
        (``cli_probe=None``) are always advertiseable.
        """
        with self._lock:
            result: list[str] = []
            seen: set[str] = set()
            for spec in self._specs.values():
                if not spec.discoverable or spec.name in seen:
                    continue
                seen.add(spec.name)
                if spec.cli_probe is not None and not probe(spec.cli_probe):
                    continue
                result.append(spec.name)
                result.extend(spec.aliases)
            return result


#: Module-level default registry used by the whole package.
registry = AgentAdapterRegistry()

_BUILTINS_LOADED = False
_BUILTINS_LOCK = threading.Lock()


def ensure_builtin_plugins() -> None:
    """Register the in-tree adapters exactly once (idempotent, thread-safe).

    Imports the sandbox adapter classes and the DeepSeek harness plugin.
    Kept lazy so importing this module never pulls the full sandbox stack
    (plugins may want a lightweight import for registration only).
    """
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    with _BUILTINS_LOCK:
        if _BUILTINS_LOADED:
            return
        from ultimate_coders.agent import harness_deepseek
        from ultimate_coders.agent.sandbox import (
            GROK_AGENT_ALIASES,
            ClaudeCodeAdapter,
            CodexAdapter,
            DecomposeAdapter,
            GrokBuildAdapter,
        )

        registry.register(AgentPluginSpec(
            name="grok-build",
            aliases=GROK_AGENT_ALIASES,
            factory=GrokBuildAdapter,
            api_key_env="XAI_API_KEY",
            cli_probe="grok",
            description="xAI Grok Build terminal coding agent",
        ))
        registry.register(AgentPluginSpec(
            name="claude-code",
            factory=ClaudeCodeAdapter,
            api_key_env="ANTHROPIC_API_KEY",
            cli_probe="claude",
            description="Anthropic Claude Code CLI",
        ))
        registry.register(AgentPluginSpec(
            name="claude-code-decompose",
            factory=DecomposeAdapter,
            api_key_env="ANTHROPIC_API_KEY",
            cli_probe="claude",
            description="Claude Code single-turn decomposition helper",
            discoverable=False,
        ))
        registry.register(AgentPluginSpec(
            name="codex",
            factory=CodexAdapter,
            api_key_env="OPENAI_API_KEY",
            cli_probe="codex",
            description="OpenAI Codex CLI",
        ))
        harness_deepseek.register(registry)
        _BUILTINS_LOADED = True


def discover_plugins() -> list[str]:
    """Import external plugins and return what became newly registered.

    Sources, both optional:
    - installed packages with an entry point in
      ``ultimate_coders.agent_adapters``
    - ``UC_AGENT_PLUGINS`` paths (``.py`` files or importable package dirs)

    Each plugin module must call ``register_agent(...)`` on import. Errors
    are logged and skipped — a broken plugin must never take down worker
    startup.
    """
    ensure_builtin_plugins()
    loaded: list[str] = []

    # 1. Installed-package entry points.
    try:
        from importlib import metadata

        eps = metadata.entry_points()
        # Python >=3.10 dict interface; fall back to select() for 3.9.
        if hasattr(eps, "select"):
            group = eps.select(group=ENTRY_POINT_GROUP)
        else:  # pragma: no cover - py3.9
            group = eps.get(ENTRY_POINT_GROUP, [])
        for ep in group:
            _load_plugin_module(ep.name, ep.load)
    except Exception:
        logger.debug("Entry-point plugin discovery failed", exc_info=True)

    # 2. Filesystem paths from UC_AGENT_PLUGINS.
    raw = os.environ.get(PLUGIN_PATH_ENV, "")
    for path_str in raw.replace(";", ":").split(":"):
        path_str = path_str.strip()
        if not path_str:
            continue
        path = Path(path_str)
        if not path.exists():
            logger.warning("UC_AGENT_PLUGINS path not found, skipping: %s", path)
            continue
        try:
            if path.is_dir():
                loaded.append(_import_package_dir(path))
            else:
                loaded.append(_import_file(path))
        except Exception:
            logger.warning("Failed to import agent plugin %s", path, exc_info=True)

    if loaded:
        logger.info("Agent plugin discovery loaded: %s", ", ".join(loaded))
    # Mark the once-guard too: a direct discover_plugins() call already did
    # the work; create_adapter()'s discover_once() must not re-scan.
    global _DISCOVERED
    _DISCOVERED = True
    return registry.available()


def _load_plugin_module(name: str, loader: Callable[[], object]) -> None:
    try:
        loader()
        logger.info("Loaded agent plugin from entry point: %s", name)
    except Exception:
        logger.warning("Agent plugin entry point %s failed", name, exc_info=True)


def _import_file(path: Path) -> str:
    """Import a ``.py`` file as ``uc_agent_plugin_<stem>`` (idempotent).

    Re-executing the same file (discovery running twice — e.g. a direct
    ``discover_plugins()`` call followed by ``create_adapter``'s internal
    ``discover_once()``) would rebuild every class object, making the
    re-registered spec unequal to the existing one and firing a spurious
    duplicate-conflict warning. A module already in ``sys.modules`` has
    registered itself on first exec — skip it.
    """
    module_name = f"uc_agent_plugin_{path.stem}"
    if module_name in sys.modules:
        return module_name
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"Cannot load plugin file: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    logger.info("Loaded agent plugin from file: %s", path)
    return module_name


def _import_package_dir(path: Path) -> str:
    """Import a package directory (must contain ``__init__.py``)."""
    if not (path / "__init__.py").exists():
        raise ImportError(f"Plugin dir {path} has no __init__.py")
    module_name = f"uc_agent_plugin_{path.name}"
    if module_name in sys.modules:  # idempotent — see _import_file
        return module_name
    spec = importlib.util.spec_from_file_location(
        module_name, path / "__init__.py", submodule_search_locations=[str(path)]
    )
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"Cannot load plugin package: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    logger.info("Loaded agent plugin package: %s", path)
    return module_name


# ── Convenience module-level API (what core code uses) ──────────────

def register_agent(spec: AgentPluginSpec, *, replace: bool = False) -> None:
    """Register a plugin into the default registry."""
    registry.register(spec, replace=replace)


def api_key_env_for(agent: str) -> str | None:
    """Env var name for an agent's API key (loads built-ins first)."""
    ensure_builtin_plugins()
    return registry.api_key_env(agent)


def create_adapter(name: str):
    """Create an adapter by name/alias from the default registry."""
    ensure_builtin_plugins()
    discover_once()
    return registry.create(name)


def available_agents() -> list[str]:
    """List discoverable agent names from the default registry."""
    ensure_builtin_plugins()
    discover_once()
    return registry.available()


_DISCOVERED = False
_DISCOVER_LOCK = threading.Lock()


def discover_once() -> None:
    """Run external plugin discovery exactly once per process."""
    global _DISCOVERED
    if _DISCOVERED:
        return
    with _DISCOVER_LOCK:
        if _DISCOVERED:
            return
        try:
            discover_plugins()
        except Exception:
            logger.warning("Agent plugin discovery failed", exc_info=True)
        _DISCOVERED = True
