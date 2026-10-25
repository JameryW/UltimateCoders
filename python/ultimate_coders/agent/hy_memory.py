"""Hy-Memory cognitive memory backend — in-tree integration.

Integrates Tencent Hunyuan's Hy Memory SDK (https://github.com/Tencent-Hunyuan/Hy-Memory,
PyPI: ``hy-memory``) as the cognitive memory backend for coding agents. Where
``ultimate_coders.memory`` (ShortTermMemory / LongTermMemory) stores what an
agent *explicitly writes*, Hy Memory extracts facts and reusable experiences
*from the agent's tool-call traces* and — in the ``coding`` scene — keeps a
version-evolution diff chain so "which implementation was finally kept" stays
answerable across sessions.

Verified contract (hy-memory==2.0.2, real roundtrip):

    client.add(messages, scene="coding", user_id=..., workspace_id=...,
               branch=..., source="ultimate-coders")
      -> {"success": True, "scene": "productivity", "fact_ids": [...],
          "experience_ids": [...], "chain_rounds": ..., ...}

    client.search(query, scene="coding", user_ids=[...], workspace_id=...,
                  branch=...)
      -> {"facts": [...], "experiences": [...], "chain_block": ..., ...}

**Supersede-not-delete (D6 / T11 hard invariant):** ``CodingMemory.remember``
(and the MCP ``remember_coding_memory`` tool that calls it) never invokes a
delete / remove / drop / tombstone API on the Hy-Memory client for “update”
semantics. Optional ``supersede_fact_ids`` (or prior ids already listed under
``metadata["supersedes"]``) are merged into outgoing metadata as
``{"supersedes": [...], "supersede_policy": "mark_not_delete"}`` and echoed on
the return payload as ``superseded_ids``. History stays auditable; auto-inject
(T12) is out of scope for this module's write path.

The SDK speaks the OpenAI protocol on both the LLM and embedding side, so the
official DeepSeek example configuration works unchanged:

    MEMORY_LLM_BASE_URL=https://api.deepseek.com
    MEMORY_LLM_API_KEY=...            # or DEEPSEEK_API_KEY
    MEMORY_LLM_MODEL=deepseek-chat
    MEMORY_EMBEDDER_BASE_URL=...      # any OpenAI-compatible embedder
    MEMORY_EMBEDDER_API_KEY=...
    MEMORY_EMBEDDER_MODEL=text-embedding-v3

Selection / configuration is purely environmental — there is no
``UC_CODING_AGENT`` value for this: hy-memory is a memory backend, not a
coding agent, so it deliberately does NOT register in the agent plugin
registry. Consumers reach it through :class:`CodingMemory` (in-process) or
the ``recall_coding_memory`` / ``remember_coding_memory`` MCP tools exposed
by ``ultimate_coders.agent.engine_mcp``.

Boot diagnosis (no API cost):
    from ultimate_coders.agent.hy_memory import preflight_check
    ok, message = preflight_check()

Optional dependency: the ``hy-memory`` package is import-guarded. Without it
every entry point degrades to a clear "not installed" message — nothing in
this module is required for the rest of the agent stack to run.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import sys
import time
from collections.abc import Sequence
from typing import Any

#: PyPI distribution name of the memory SDK.
HY_MEMORY_PACKAGE = "hy-memory"

#: Minimum supported SDK version. 2.0 introduced the coding scene with the
#: version-evolution chain this integration is built on.
HY_MEMORY_MIN_VERSION = "2.0.0"

#: Default client mode. ``lite`` ignores ``scene`` and stores raw text only
#: (no extraction, no chain); ``ultra`` forces the graph DB on. ``pro`` is
#: the standard operating point: LLM extraction + reconcile, graph optional.
DEFAULT_MODE = "pro"

#: Value written into every memory's ``source`` field so recall can isolate
#: UltimateCoders writes from other MemHub-connected applications.
SOURCE_NAME = "ultimate-coders"

#: Env vars a sandboxed agent needs for its in-sandbox MCP memory tools.
#: Single-sourced: sandbox.py merges this tuple into SHARED_ENV_ALLOWLIST
#: and the tests pin the identity of the two copies.
#:
#: Deliberately ``MEMORY_*``-only: ``OPENAI_API_KEY`` is the SDK's documented
#: single-key fallback and is honoured by the in-process paths above (they
#: read ``os.environ`` directly), but it is an *adapter-scoped* credential in
#: this repo (codex / local-harness only) -- forwarding it through the shared
#: layer would hand a host OpenAI key to every agent subprocess, including
#: unknown external plugins. Inside the sandbox, operators who want the
#: single-key posture set the two ``MEMORY_*`` key names explicitly (the
#: compose override forwards exactly those).
HY_MEMORY_ENV_ALLOWLIST: tuple[str, ...] = (
    "MEMORY_LLM_API_KEY",
    "MEMORY_LLM_BASE_URL",
    "MEMORY_LLM_MODEL",
    "MEMORY_EMBEDDER_API_KEY",
    "MEMORY_EMBEDDER_BASE_URL",
    "MEMORY_EMBEDDER_MODEL",
    "MEMORY_EMBEDDING_DIMS",
    "MEMORY_DATA_DIR",
    "MEMORY_MODE",
    "MEMORY_ENABLE_GRAPH",
)

#: Coding-scene response channels normalised by :meth:`CodingMemory.recall`.
_RECALL_CHANNELS = ("facts", "experiences")

#: Metadata key listing prior fact ids that a remember write supersedes.
SUPERSEDES_METADATA_KEY = "supersedes"

#: Metadata key naming the UC supersede policy (never delete).
SUPERSEDE_POLICY_METADATA_KEY = "supersede_policy"

#: Only allowed supersede policy on UC write paths (D6 hard invariant).
SUPERSEDE_POLICY_MARK_NOT_DELETE = "mark_not_delete"


def _parse_version(text: str) -> tuple[int, ...]:
    """Parse ``"2.0.2"`` into ``(2, 0, 2)``; non-numeric parts become 0."""
    parts: list[int] = []
    for chunk in str(text).split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def hy_memory_version() -> str | None:
    """Installed SDK version, or None when the package is absent."""
    try:
        return importlib.metadata.version(HY_MEMORY_PACKAGE)
    except importlib.metadata.PackageNotFoundError:
        return None


def hy_memory_available() -> bool:
    """True when the SDK is importable (cheap check, no client build)."""
    try:
        import hy_memory  # noqa: F401
    except ImportError:
        return False
    return True


def _version_at_least(version: str, floor: str) -> bool:
    return _parse_version(version) >= _parse_version(floor)


def _mask_key(value: str) -> str:
    """``sk-abc…1234`` — enough to recognise a key, useless to steal."""
    value = value.strip()
    if len(value) <= 8:
        return "***"
    return f"{value[:6]}…{value[-4:]}"


def _endpoint_status() -> dict[str, Any]:
    """Which credential/endpoint vars the host environment provides."""
    llm_key = os.environ.get("MEMORY_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    embedder_key = os.environ.get("MEMORY_EMBEDDER_API_KEY") or os.environ.get("OPENAI_API_KEY")
    return {
        "llm_key": _mask_key(llm_key) if llm_key else None,
        "llm_base_url": os.environ.get("MEMORY_LLM_BASE_URL") or None,
        "llm_model": os.environ.get("MEMORY_LLM_MODEL") or None,
        "embedder_key": _mask_key(embedder_key) if embedder_key else None,
        "embedder_base_url": os.environ.get("MEMORY_EMBEDDER_BASE_URL") or None,
        "embedder_model": os.environ.get("MEMORY_EMBEDDER_MODEL") or None,
        "embedding_dims": os.environ.get("MEMORY_EMBEDDING_DIMS") or None,
    }


def _build_client(mode: str = DEFAULT_MODE) -> Any:
    """Construct a real HyMemoryClient (import-guarded)."""
    from hy_memory import HyMemoryClient

    return HyMemoryClient(mode=mode)


def _normalize_fact_ids(ids: Sequence[Any] | None) -> list[str]:
    """Coerce a sequence of fact ids to a de-duplicated list of non-empty strings.

    A bare ``str`` / ``bytes`` is treated as a single id (not iterated as
    characters) — ``str`` is a ``Sequence`` at runtime, which would otherwise
    split ``"fact-1"`` into one-character ids.
    """
    if not ids:
        return []
    if isinstance(ids, (str, bytes)):
        text = ids.decode() if isinstance(ids, bytes) else ids
        text = text.strip()
        return [text] if text else []
    seen: set[str] = set()
    out: list[str] = []
    for raw in ids:
        text = str(raw).strip() if raw is not None else ""
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _merge_supersede_metadata(
    metadata: dict[str, Any] | None,
    supersede_fact_ids: Sequence[str] | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Merge supersede ids into metadata without wiping unrelated caller keys.

    Returns ``(outgoing_metadata, superseded_ids)``. When there is nothing to
    supersede, ``outgoing_metadata`` is the original ``metadata`` reference
    (including ``None``) so callers that pass metadata through unchanged keep
    identity. When superseding, returns a new dict with ``supersedes`` and
    ``supersede_policy`` stamped; other keys are shallow-copied from the
    caller.
    """
    from_arg = _normalize_fact_ids(supersede_fact_ids)
    prior_raw = metadata.get(SUPERSEDES_METADATA_KEY) if metadata else None
    from_meta = _normalize_fact_ids(
        prior_raw if isinstance(prior_raw, (list, tuple)) else None
    )
    # Union: ids already in metadata plus any newly supplied via the arg.
    merged_ids = _normalize_fact_ids([*from_meta, *from_arg])
    if not merged_ids:
        return metadata, []

    outgoing = dict(metadata) if metadata else {}
    outgoing[SUPERSEDES_METADATA_KEY] = merged_ids
    outgoing[SUPERSEDE_POLICY_METADATA_KEY] = SUPERSEDE_POLICY_MARK_NOT_DELETE
    return outgoing, merged_ids


class CodingMemory:
    """Coding-scene memory over one HyMemoryClient.

    Wraps ``add``/``search`` in the ``coding`` scene with UltimateCoders'
    isolation coordinates (``user_id`` / ``workspace_id`` / ``branch``) and
    normalises the response shapes. The client is injected in tests; in
    production it is built lazily on first use.

    Usage:
        mem = CodingMemory(user_id="jameryw",
                           workspace_id="github.com/JameryW/UltimateCoders",
                           branch="feat/hy-memory")
        mem.remember(messages)          # one completed agent task trace
        hits = mem.recall("timeout fix")  # facts + experiences + chain block
        mem.close()
    """

    def __init__(
        self,
        user_id: str = "default",
        *,
        workspace_id: str | None = None,
        branch: str | None = None,
        mode: str = DEFAULT_MODE,
        client: Any = None,
        client_factory: Any = None,
    ) -> None:
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.branch = branch
        self.mode = mode
        self._client = client
        self._client_factory = client_factory or _build_client
        self._owns_client = client is None

    @property
    def client(self) -> Any:
        """Lazily construct the underlying HyMemoryClient."""
        if self._client is None:
            self._client = self._client_factory(self.mode)
        return self._client

    def remember(
        self,
        messages: list[dict[str, Any]],
        *,
        metadata: dict[str, Any] | None = None,
        supersede_fact_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Record one completed agent task trace (must contain tool calls).

        The coding scene extracts exactly one fact per session (what was
        done) plus zero or more reusable experiences, and appends the real
        diffs to the version-evolution chain. Returns the SDK response
        (always a dict copy); ``success`` is False when the SDK rejected
        the write (e.g. endpoints unreachable) — callers should log, not
        raise.

        Supersede-not-delete: when ``supersede_fact_ids`` is provided (or
        ``metadata`` already lists ``supersedes``), those ids are stamped
        into outgoing metadata with ``supersede_policy="mark_not_delete"``
        and echoed on the return payload as ``superseded_ids``. This method
        only calls ``client.add`` — never delete / remove / drop / tombstone.
        """
        outgoing_metadata, superseded_ids = _merge_supersede_metadata(
            metadata, supersede_fact_ids
        )
        raw = self.client.add(
            messages,
            scene="coding",
            user_id=self.user_id,
            workspace_id=self.workspace_id,
            branch=self.branch,
            source=SOURCE_NAME,
            metadata=outgoing_metadata,
        )
        payload = dict(raw) if isinstance(raw, dict) else {"success": False, "raw": raw}
        if superseded_ids:
            payload["superseded_ids"] = list(superseded_ids)
        return payload

    def recall(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Search this workspace's coding memory.

        Returns a normalised dict::

            {"ok": bool, "facts": [...], "experiences": [...],
             "chain_block": ..., "raw": <the full SDK response>}

        ``ok`` is False when the SDK call failed; ``facts`` / ``experiences``
        are then empty lists so callers can iterate unconditionally.
        """
        try:
            raw = self.client.search(
                query,
                scene="coding",
                user_ids=[self.user_id],
                workspace_id=self.workspace_id,
                branch=self.branch,
                limit=limit,
            )
        except Exception as e:  # endpoint down, bad config, ...
            return {
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "facts": [],
                "experiences": [],
                "chain_block": None,
                "raw": None,
            }
        facts = raw.get("facts") or []
        experiences = raw.get("experiences") or []
        return {
            "ok": True,
            "facts": facts,
            "experiences": experiences,
            "chain_block": raw.get("chain_block"),
            "raw": raw,
        }

    def close(self) -> None:
        """Release the client (only when this wrapper built it)."""
        if self._owns_client and self._client is not None:
            try:
                self._client.close()
            except Exception:  # pragma: no cover - close must not raise
                pass
        self._client = None


def preflight_check(timeout_secs: int = 20) -> tuple[bool, str]:
    """Validate that the memory backend can be used (no API cost).

    Checks, in order: package installed, version floor, LLM + embedder
    credentials/endpoints present, and that a client constructs and closes
    cleanly (catches bad config: invalid mode, unwritable data dir, ...).
    Returns ``(ok, message)`` — the message is actionable on failure.
    """
    version = hy_memory_version()
    if version is None:
        return False, (
            f"{HY_MEMORY_PACKAGE} is not installed; install it with: "
            f"pip install {HY_MEMORY_PACKAGE}==2.0.2"
        )
    if not _version_at_least(version, HY_MEMORY_MIN_VERSION):
        return False, (
            f"{HY_MEMORY_PACKAGE} {version} is older than the required "
            f"{HY_MEMORY_MIN_VERSION}; upgrade with: pip install -U {HY_MEMORY_PACKAGE}"
        )

    status = _endpoint_status()
    missing: list[str] = []
    if not status["llm_key"]:
        missing.append("MEMORY_LLM_API_KEY (or OPENAI_API_KEY)")
    if not status["embedder_key"]:
        missing.append("MEMORY_EMBEDDER_API_KEY (or OPENAI_API_KEY)")
    if missing:
        return False, ("hy-memory endpoints are not configured; set: " + ", ".join(missing))

    started = time.monotonic()
    try:
        client = _build_client(DEFAULT_MODE)
    except Exception as e:
        return False, f"HyMemoryClient(mode={DEFAULT_MODE!r}) failed to construct: {e}"
    try:
        client.close()
    except Exception:  # pragma: no cover - close must not raise
        pass
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return True, (
        f"hy-memory {version} ready (mode={DEFAULT_MODE}, "
        f"client built in {elapsed_ms}ms; no API calls made)"
    )


def doctor_report() -> dict[str, Any]:
    """Machine-readable environment report for ``--json`` output."""
    version = hy_memory_version()
    report: dict[str, Any] = {
        "package": HY_MEMORY_PACKAGE,
        "installed": version is not None,
        "version": version,
        "min_version": HY_MEMORY_MIN_VERSION,
        "importable": hy_memory_available(),
        "default_mode": DEFAULT_MODE,
        "source": SOURCE_NAME,
        "endpoints": _endpoint_status(),
        "data_dir": os.environ.get("MEMORY_DATA_DIR") or "(sdk default)",
    }
    ok, message = preflight_check()
    report["preflight_ok"] = ok
    report["preflight_message"] = message
    return report


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m ultimate_coders.agent.hy_memory [--check] [--json]``."""
    args = sys.argv[1:] if argv is None else argv
    timeout = 20
    if "--timeout" in args:
        i = args.index("--timeout")
        try:
            timeout = int(args[i + 1])
        except (IndexError, ValueError):
            print("--timeout expects an integer", file=sys.stderr)
            return 2
        del args[i : i + 2]

    if "--json" in args:
        # The SDK logs INFO to stdout from its first client construction,
        # which would corrupt machine-readable output. setup_logging() reads
        # MEMORY_LOG_LEVEL once, before any client exists — an operator's
        # explicit setting always wins (setdefault, not assignment).
        os.environ.setdefault("MEMORY_LOG_LEVEL", "ERROR")
        report = doctor_report()
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["preflight_ok"] else 1

    if "--check" in args or not args:
        ok, message = preflight_check(timeout_secs=timeout)
        print(("OK: " if ok else "FAILED: ") + message)
        return 0 if ok else 1

    print(
        "usage: python -m ultimate_coders.agent.hy_memory [--check] [--json] [--timeout N]",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
