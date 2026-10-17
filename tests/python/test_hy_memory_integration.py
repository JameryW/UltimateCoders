"""Tests for the Hy-Memory integration (ultimate_coders.agent.hy_memory).

MEASURED, NOT ASSUMED
---------------------
Every contract asserted here was verified against the real SDK
(hy-memory==2.0.2) before this file was written:

  * ``HyMemoryClient(mode=...)`` constructs synchronously and spawns its
    own event-loop threads; ``close()`` releases them.  Construction
    SUCCEEDS even with no credentials — failures only surface at
    ``add()``/``search()`` time.  preflight_check() therefore checks
    endpoint PRESENCE plus a construct/close cycle, never a paid call.
  * ``add(messages, scene="coding", ...)`` returns
    ``{"success": True, "scene": "productivity", "fact_ids": [...],
    "experience_ids": [...], "chain_rounds": ...}`` — verified with a real
    tool-call trace against a mock OpenAI-compatible endpoint.
  * ``search(query, scene="coding", ...)`` returns ``facts`` /
    ``experiences`` / ``chain_block`` channels — same mock run.
  * The SDK logs INFO to stdout from its FIRST client construction
    (``setup_logging`` binds ``StreamHandler(sys.stdout)`` once, reading
    ``MEMORY_LOG_LEVEL``).  The ``--json`` CLI path therefore pins the
    level to ERROR before building the report — that is a measured
    workaround, not decoration: without it the JSON stream is polluted.

The SDK itself is an OPTIONAL dependency. Tests here never import it for
real: every client is a fake, and the availability probes are monkeypatched,
so this file passes on hosts with and without hy-memory installed.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))

from ultimate_coders.agent import engine_mcp, hy_memory  # noqa: E402
from ultimate_coders.agent.hy_memory import HY_MEMORY_ENV_ALLOWLIST, CodingMemory  # noqa: E402
from ultimate_coders.agent.sandbox import SHARED_ENV_ALLOWLIST, SandboxConfig  # noqa: E402

# ── fakes ────────────────────────────────────────────────────────────


class FakeCodingClient:
    """Records the kwargs every call arrived with; returns canned shapes."""

    def __init__(self, *, add_result: dict[str, Any] | None = None, search_raises: bool = False):
        self.add_calls: list[dict[str, Any]] = []
        self.search_calls: list[dict[str, Any]] = []
        self.closed = False
        self._add_result = add_result or {
            "success": True,
            "scene": "productivity",
            "fact_ids": ["f-1"],
            "experience_ids": ["e-1"],
            "chain_rounds": 2,
        }
        self._search_raises = search_raises

    def add(self, data: Any, **kwargs: Any) -> dict[str, Any]:
        self.add_calls.append({"data": data, **kwargs})
        return dict(self._add_result)

    def search(self, query: Any = None, **kwargs: Any) -> dict[str, Any]:
        self.search_calls.append({"query": query, **kwargs})
        if self._search_raises:
            raise ConnectionError("endpoint down")
        return {
            "facts": [{"statement": "kept the 5s timeout", "score": 0.91}],
            "experiences": [{"task": "fix search timeout", "solution": "5s cap"}],
            "chain_block": {"text": "v1 -> v2 diff"},
        }

    def close(self) -> None:
        self.closed = True


def _trace() -> list[dict[str, Any]]:
    """One OpenAI-format tool-call trace (the coding-scene input shape)."""
    return [
        {"role": "user", "content": "Fix the timeout in the search client."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "apply_patch", "arguments": '{"path": "src/search.py"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "apply_patch",
            "content": "Patch applied successfully",
        },
    ]


@pytest.fixture(autouse=True)
def _clean_memory_env(monkeypatch: pytest.MonkeyPatch):
    """No ambient MEMORY_*/OPENAI_* keys: endpoint tests must see a clean slate."""
    for name in HY_MEMORY_ENV_ALLOWLIST:
        monkeypatch.delenv(name, raising=False)


# ── module probes ────────────────────────────────────────────────────


def test_available_matches_version() -> None:
    version = hy_memory.hy_memory_version()
    assert hy_memory.hy_memory_available() == (version is not None)


def test_version_floor_comparisons() -> None:
    assert hy_memory._version_at_least("2.0.2", "2.0.0")
    assert hy_memory._version_at_least("2.0.0", "2.0.0")
    assert not hy_memory._version_at_least("1.9.9", "2.0.0")
    # A build suffix must not flip the comparison.
    assert hy_memory._version_at_least("2.0.2-rc.1", "2.0.0")


def test_mask_key_never_leaks_the_middle() -> None:
    masked = hy_memory._mask_key("sk-abcdefghijklmnop")
    assert masked.startswith("sk-abc")
    assert masked.endswith("mnop")
    assert "ijkl" not in masked
    assert hy_memory._mask_key("short") == "***"


# ── CodingMemory wrapper ─────────────────────────────────────────────


def test_remember_passes_coding_scene_and_isolation_coordinates() -> None:
    client = FakeCodingClient()
    mem = CodingMemory(
        user_id="jameryw",
        workspace_id="github.com/JameryW/UltimateCoders",
        branch="feat/hy-memory",
        client=client,
    )
    result = mem.remember(_trace(), metadata={"task_id": "t-1"})

    assert result["success"] is True
    assert result["fact_ids"] == ["f-1"]
    call = client.add_calls[0]
    assert call["scene"] == "coding"
    assert call["user_id"] == "jameryw"
    assert call["workspace_id"] == "github.com/JameryW/UltimateCoders"
    assert call["branch"] == "feat/hy-memory"
    assert call["source"] == hy_memory.SOURCE_NAME
    assert call["metadata"] == {"task_id": "t-1"}
    assert call["data"] == _trace()


def test_recall_normalises_channels() -> None:
    client = FakeCodingClient()
    mem = CodingMemory(user_id="u", workspace_id="w", branch="b", client=client)
    hits = mem.recall("which timeout was kept?")

    assert hits["ok"] is True
    assert hits["facts"][0]["statement"] == "kept the 5s timeout"
    assert hits["experiences"][0]["solution"] == "5s cap"
    assert hits["chain_block"]["text"] == "v1 -> v2 diff"
    call = client.search_calls[0]
    assert call["scene"] == "coding"
    assert call["user_ids"] == ["u"]
    assert call["workspace_id"] == "w"
    assert call["branch"] == "b"
    assert call["limit"] == 5


def test_recall_survives_client_failure() -> None:
    """An unreachable endpoint must degrade to ok=False, not raise."""
    client = FakeCodingClient(search_raises=True)
    mem = CodingMemory(user_id="u", client=client)
    hits = mem.recall("anything")
    assert hits["ok"] is False
    assert "ConnectionError" in hits["error"]
    assert hits["facts"] == [] and hits["experiences"] == []
    assert hits["chain_block"] is None


def test_recall_tolerates_empty_channels() -> None:
    class EmptyClient(FakeCodingClient):
        def search(self, query: Any = None, **kwargs: Any) -> dict[str, Any]:
            return {"facts": [], "experiences": [], "chain_block": None}

    mem = CodingMemory(user_id="u", client=EmptyClient())
    hits = mem.recall("nothing")
    assert hits["ok"] is True
    assert hits["facts"] == [] and hits["chain_block"] is None


def test_lazy_client_built_once_and_closed_only_when_owned() -> None:
    built: list[FakeCodingClient] = []

    def factory(mode: str) -> FakeCodingClient:
        assert mode == hy_memory.DEFAULT_MODE
        client = FakeCodingClient()
        built.append(client)
        return client

    mem = CodingMemory(user_id="u", client_factory=factory)
    assert built == []  # not constructed until first use
    assert mem.client is mem.client  # cached
    assert len(built) == 1
    mem.close()
    assert built[0].closed is True  # owned → closed


def test_injected_client_is_never_closed_by_the_wrapper() -> None:
    client = FakeCodingClient()
    mem = CodingMemory(user_id="u", client=client)
    mem.close()
    assert client.closed is False


# ── preflight_check ─────────────────────────────────────────────────


def _patch_version(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    monkeypatch.setattr(hy_memory, "hy_memory_version", lambda: value)


def test_preflight_without_package(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_version(monkeypatch, None)
    ok, msg = hy_memory.preflight_check()
    assert ok is False
    assert "pip install" in msg and "hy-memory" in msg


def test_preflight_with_old_version(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_version(monkeypatch, "1.9.9")
    ok, msg = hy_memory.preflight_check()
    assert ok is False
    assert "upgrade" in msg.lower()


def test_preflight_missing_both_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_version(monkeypatch, "2.0.2")
    ok, msg = hy_memory.preflight_check()
    assert ok is False
    assert "MEMORY_LLM_API_KEY" in msg and "MEMORY_EMBEDDER_API_KEY" in msg


def test_preflight_missing_only_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_version(monkeypatch, "2.0.2")
    monkeypatch.setenv("MEMORY_LLM_API_KEY", "sk-llm")
    ok, msg = hy_memory.preflight_check()
    assert ok is False
    assert "MEMORY_EMBEDDER_API_KEY" in msg
    assert "MEMORY_LLM_API_KEY" not in msg


def test_preflight_openai_key_covers_both(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_version(monkeypatch, "2.0.2")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setattr(hy_memory, "_build_client", lambda mode: FakeCodingClient())
    ok, msg = hy_memory.preflight_check()
    assert ok is True
    assert "hy-memory 2.0.2 ready" in msg
    assert "no API calls made" in msg


def test_preflight_reports_construction_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_version(monkeypatch, "2.0.2")
    monkeypatch.setenv("MEMORY_LLM_API_KEY", "sk-llm")
    monkeypatch.setenv("MEMORY_EMBEDDER_API_KEY", "sk-emb")

    def boom(mode: str):
        raise RuntimeError("unwritable data dir")

    monkeypatch.setattr(hy_memory, "_build_client", boom)
    ok, msg = hy_memory.preflight_check()
    assert ok is False
    assert "unwritable data dir" in msg


# ── doctor report + CLI ─────────────────────────────────────────────


def test_doctor_report_masks_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_version(monkeypatch, "2.0.2")
    monkeypatch.setattr(hy_memory, "_build_client", lambda mode: FakeCodingClient())
    monkeypatch.setenv("MEMORY_LLM_API_KEY", "sk-llmsecret1234")
    monkeypatch.setenv("MEMORY_EMBEDDER_API_KEY", "sk-embsecret5678")
    report = hy_memory.doctor_report()
    assert report["installed"] is True
    assert report["endpoints"]["llm_key"] == "sk-llm…1234"
    assert report["endpoints"]["embedder_key"] == "sk-emb…5678"
    assert "secret" not in json.dumps(report).replace("sk-llm", "").replace("sk-em", "")
    json.dumps(report)  # must be JSON-serializable


def test_cli_check_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_version(monkeypatch, None)
    assert hy_memory.main(["--check"]) == 1
    _patch_version(monkeypatch, "2.0.2")
    monkeypatch.setenv("MEMORY_LLM_API_KEY", "sk-llm")
    monkeypatch.setenv("MEMORY_EMBEDDER_API_KEY", "sk-emb")
    monkeypatch.setattr(hy_memory, "_build_client", lambda mode: FakeCodingClient())
    assert hy_memory.main(["--check"]) == 0


def test_cli_json_is_pure_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _patch_version(monkeypatch, "2.0.2")
    monkeypatch.setenv("MEMORY_LLM_API_KEY", "sk-llm")
    monkeypatch.setenv("MEMORY_EMBEDDER_API_KEY", "sk-emb")
    monkeypatch.setattr(hy_memory, "_build_client", lambda mode: FakeCodingClient())
    monkeypatch.setenv("MEMORY_LOG_LEVEL", "INFO")  # explicit value must win
    assert hy_memory.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["preflight_ok"] is True


def test_cli_bad_timeout_is_usage_error() -> None:
    assert hy_memory.main(["--timeout", "abc"]) == 2


def test_cli_unknown_args_are_usage_error() -> None:
    assert hy_memory.main(["--frobnicate"]) == 2


# ── env allowlist ───────────────────────────────────────────────────


def test_allowlist_tuple_shape() -> None:
    assert isinstance(HY_MEMORY_ENV_ALLOWLIST, tuple)
    for name in (
        "MEMORY_LLM_API_KEY",
        "MEMORY_EMBEDDER_API_KEY",
        "MEMORY_DATA_DIR",
        "MEMORY_MODE",
    ):
        assert name in HY_MEMORY_ENV_ALLOWLIST
    # No wildcard: every entry is an exact name (deny-by-default posture).
    assert not any(n.endswith("*") for n in HY_MEMORY_ENV_ALLOWLIST)
    # Every entry is a MEMORY_* name -- see the module comment on
    # HY_MEMORY_ENV_ALLOWLIST for why OPENAI_API_KEY must stay out of it.
    assert all(n.startswith("MEMORY_") for n in HY_MEMORY_ENV_ALLOWLIST)


def test_openai_fallback_key_is_not_forwarded_to_sandbox() -> None:
    """Pin the adapter-scoped-credential decision at the behaviour level.

    The SDK's documented single-key fallback (OPENAI_API_KEY covering both
    endpoints) is honoured by the in-process paths, which read os.environ
    directly. Forwarding it through the shared allowlist would hand a host
    OpenAI key to *every* agent subprocess, including unknown external
    plugins -- the exact thing test_sandbox_env_allowlist.py pins against.
    """
    cfg = SandboxConfig(agent="some-external-plugin")
    child = cfg.build_child_env(
        {
            "MEMORY_LLM_API_KEY": "sk-llm",
            "OPENAI_API_KEY": "sk-openai-decoy",
            "SECRET_TOKEN": "decoy",
        }
    )
    assert child["MEMORY_LLM_API_KEY"] == "sk-llm"
    assert "OPENAI_API_KEY" not in child
    assert "SECRET_TOKEN" not in child


def test_shared_allowlist_contains_every_memory_name() -> None:
    """Single-source check: sandbox.py must not carry a stale copy."""
    assert set(HY_MEMORY_ENV_ALLOWLIST) <= set(SHARED_ENV_ALLOWLIST)


def test_build_child_env_forwards_memory_vars_and_blocks_decoys() -> None:
    cfg = SandboxConfig(agent="grok-build")
    child = cfg.build_child_env(
        {
            "MEMORY_LLM_API_KEY": "sk-llm",
            "MEMORY_EMBEDDER_BASE_URL": "https://emb.example/v1",
            "SECRET_TOKEN": "decoy",
            "AWS_SECRET_ACCESS_KEY": "decoy",
        }
    )
    assert child["MEMORY_LLM_API_KEY"] == "sk-llm"
    assert child["MEMORY_EMBEDDER_BASE_URL"] == "https://emb.example/v1"
    assert "SECRET_TOKEN" not in child
    assert "AWS_SECRET_ACCESS_KEY" not in child


# ── engine_mcp layer ────────────────────────────────────────────────


def test_memory_tools_hidden_without_package(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_mcp, "hy_memory_available", lambda: False)
    assert engine_mcp._memory_tools() == []


def test_memory_tools_present_with_package(monkeypatch: pytest.MonkeyPatch) -> None:
    if engine_mcp.Server is None:  # mcp package optional in this environment
        pytest.skip("mcp package not installed")
    monkeypatch.setattr(engine_mcp, "hy_memory_available", lambda: True)
    names = [t.name for t in engine_mcp._memory_tools()]
    assert names == ["recall_coding_memory", "remember_coding_memory"]


def test_memory_backend_none_without_package(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_mcp, "_MEMORY_BACKEND", None)
    monkeypatch.setattr(engine_mcp, "hy_memory_available", lambda: False)
    assert engine_mcp._memory_backend() is None


def test_memory_backend_reads_env_coordinates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_mcp, "_MEMORY_BACKEND", None)
    monkeypatch.setattr(engine_mcp, "hy_memory_available", lambda: True)
    monkeypatch.setenv("UC_MEMORY_USER_ID", "mcp-user")
    monkeypatch.setenv("UC_MEMORY_WORKSPACE", "github.com/org/repo")
    monkeypatch.setenv("UC_MEMORY_BRANCH", "main")
    backend = engine_mcp._memory_backend()
    assert isinstance(backend, CodingMemory)
    assert backend.user_id == "mcp-user"
    assert backend.workspace_id == "github.com/org/repo"
    assert backend.branch == "main"


def _require_mcp() -> None:
    """Handler-level tests need the mcp package; CI installs none.

    The gate mirrors test_memory_tools_present_with_package: without the
    package the descriptors are hidden, so calling the handlers directly
    would exercise a code path production never reaches.
    """
    if engine_mcp.Server is None:
        pytest.skip("mcp package not installed")


def test_mcp_recall_renders_three_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    _require_mcp()
    backend = CodingMemory(user_id="u", workspace_id="w", branch="b", client=FakeCodingClient())
    monkeypatch.setattr(engine_mcp, "_MEMORY_BACKEND", backend)
    out = asyncio.run(engine_mcp._recall_coding_memory(None, {"query": "timeout"}))
    text = out[0].text
    assert "[fact] kept the 5s timeout" in text
    assert "[experience] fix search timeout -> 5s cap" in text
    assert "[chain] v1 -> v2 diff" in text


def test_mcp_recall_scoping_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _require_mcp()
    client = FakeCodingClient()
    backend = CodingMemory(user_id="u", workspace_id="w", branch="b", client=client)
    monkeypatch.setattr(engine_mcp, "_MEMORY_BACKEND", backend)
    asyncio.run(engine_mcp._recall_coding_memory(None, {"query": "q", "branch": "other"}))
    assert client.search_calls[0]["branch"] == "other"


def test_mcp_recall_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _require_mcp()
    backend = CodingMemory(user_id="u", client=FakeCodingClient(search_raises=True))
    monkeypatch.setattr(engine_mcp, "_MEMORY_BACKEND", backend)
    out = asyncio.run(engine_mcp._recall_coding_memory(None, {"query": "q"}))
    assert "recall failed" in out[0].text


def test_mcp_remember_reports_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    _require_mcp()
    client = FakeCodingClient()
    backend = CodingMemory(user_id="u", workspace_id="w", branch="b", client=client)
    monkeypatch.setattr(engine_mcp, "_MEMORY_BACKEND", backend)
    out = asyncio.run(engine_mcp._remember_coding_memory(None, {"messages": _trace()}))
    assert "1 fact(s), 1 experience(s)" in out[0].text
    assert client.add_calls[0]["scene"] == "coding"


def test_mcp_remember_rejects_empty_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    _require_mcp()
    backend = CodingMemory(user_id="u", client=FakeCodingClient())
    monkeypatch.setattr(engine_mcp, "_MEMORY_BACKEND", backend)
    out = asyncio.run(engine_mcp._remember_coding_memory(None, {"messages": []}))
    assert "No messages" in out[0].text


def test_import_guard_stays_total_without_the_mcp_package(tmp_path: Path) -> None:
    """CI installs no mcp package; the import guard must define every name.

    Reproduces the CI environment in a subprocess (an ``mcp`` stub on
    PYTHONPATH that raises ImportError, exactly like the missing package
    does) and pins the module contract: ``Server``, ``Tool`` and
    ``TextContent`` are all None. The historical shape set only
    ``Server = None`` and left the other two undefined, which turned any
    handler-level call into a NameError on CI -- that is how this
    integration's first CI run went red.
    """
    stub_dir = tmp_path / "no-mcp"
    stub_dir.mkdir()
    (stub_dir / "mcp.py").write_text(
        "raise ImportError('blocked for the no-mcp parity test')\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    pythonpath = os.pathsep.join([str(stub_dir), str(REPO / "python"), env.get("PYTHONPATH", "")])
    env["PYTHONPATH"] = pythonpath
    code = (
        "import ultimate_coders.agent.engine_mcp as m\n"
        "assert m.Server is None, 'the stub must block the mcp import'\n"
        "assert m.Tool is None, 'Tool must be defined (None) without mcp'\n"
        "assert m.TextContent is None, 'TextContent must be defined (None)'\n"
        "print('ok')\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO),
    )
    assert r.returncode == 0, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    assert "ok" in r.stdout
