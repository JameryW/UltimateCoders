"""The governance docs must keep naming things that exist.

This repository already has a *reference* guard (`test_check_spec_refs.py`) for
paths and line numbers, but nothing checks **symbol names**. Rename a test, drop a
store method, add or remove a bridge op, and the acceptance tables keep pointing
at names that no longer exist — which is worse than no evidence at all, because a
table citing a missing test *reads* like proof.

Enforced here, from the docs themselves:

* every test name cited in the acceptance-evidence table and in the `测试：` lines
  exists as a Rust test function (`fn name(`) or a Python test (`def name(`);
* every `tests/python/test_*.py` file a doc names exists;
* the op count the docs claim equals what the bridge dispatches;
* every op the docs name exists in the bridge, and every op the bridge dispatches
  has a Python caller (an op nobody can call is dispatched for nobody);
* every governance gRPC service in the proto is named in the docs.

It reads files only: no network, no build, no Cargo.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GOVERNANCE_DIR = REPO_ROOT / "docs" / "governance"
GOVERNANCE_README = GOVERNANCE_DIR / "README.md"
ARCHITECTURE_DOC = REPO_ROOT / "docs" / "architecture.md"
BRIDGE_RS = REPO_ROOT / "crates" / "uc-grpc" / "src" / "governance" / "bridge.rs"
PROTO = REPO_ROOT / "crates" / "uc-grpc" / "proto" / "engine.proto"
PYTHON_CLIENT_DIR = REPO_ROOT / "python" / "ultimate_coders"

#: Backticked identifiers long enough to be a symbol name rather than prose.
IDENTIFIER = re.compile(r"`([a-z][a-z0-9_]{9,})`")

#: First segment of every governance op name.
OP_PREFIXES = (
    "submission",
    "case",
    "mandate",
    "authorize",
    "trigger",
    "attention",
    "approval",
    "capacity",
    "proactive",
    "watchdog",
)

#: Last segment of every governance op name (verbs). Requiring a known verb keeps
#: dotted *field* references like `case.id` from being read as op names.
OP_VERBS = frozenset(
    {
        "submit",
        "get",
        "list",
        "create",
        "transition",
        "open",
        "finish",
        "issue",
        "revoke",
        "ingest",
        "upsert",
        "delete",
        "raise",
        "resolve",
        "request",
        "decide",
        "set",
        "report",
    }
)
OP_TOKEN = re.compile(r"`([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]+)+)`")

GOVERNANCE_SERVICES = (
    "CaseService",
    "TriggerService",
    "AttentionService",
    "ProactiveService",
    "ApprovalService",
)

#: The acceptance rows of the top-level table: these claim "X is proven by Y".
ACCEPTANCE_ROWS = ("不重复", "不越权", "可持续", "可恢复", "少打扰", "可验证")


def _evidence_text(line: str) -> str | None:
    """The part of a line that claims evidence, or ``None``.

    An acceptance row has four columns (criterion, claim, where, **proof**); only
    the proof column is checked here, so the prose columns may mention field names
    freely. A `测试：` line is all evidence.
    """
    stripped = line.strip()
    if stripped.startswith(("测试：", "测试:")):
        return stripped
    if not stripped.startswith("|"):
        return None
    cells = stripped.split("|")
    if len(cells) >= 5 and cells[1].strip() in ACCEPTANCE_ROWS:
        return cells[4]
    return None


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _governance_docs() -> list[Path]:
    return sorted(GOVERNANCE_DIR.glob("*.md"))


@pytest.fixture(scope="module")
def source_corpus() -> str:
    """Every Rust and Python file, joined once for symbol lookups."""
    parts: list[str] = []
    for root, suffix in (
        (REPO_ROOT / "crates", ".rs"),
        (REPO_ROOT / "python", ".py"),
        (REPO_ROOT / "tests" / "python", ".py"),
    ):
        for path in root.rglob(f"*{suffix}"):
            parts.append(_read(path))
    return "\n".join(parts)


@pytest.fixture(scope="module")
def bridge_ops() -> list[str]:
    return re.findall(
        r'^\s+"([a-z][a-z0-9_.]+)" => \{', _read(BRIDGE_RS), flags=re.MULTILINE
    )


def _evidence_identifiers() -> list[tuple[str, str]]:
    """`(doc, name)` for identifiers cited on a line that claims evidence."""
    cited: set[tuple[str, str]] = set()
    for doc in _governance_docs():
        for line in _read(doc).splitlines():
            evidence = _evidence_text(line)
            if evidence is None:
                continue
            for name in IDENTIFIER.findall(evidence):
                cited.add((doc.name, name))
    return sorted(cited)


def _is_test_function(name: str, corpus: str) -> bool:
    """An evidence citation must be an actual test function, not a lookalike.

    The proof column names tests, so a field name that happens to share a prefix
    (`submission_id`) must not satisfy it: that is exactly the drift this guard
    exists to catch.
    """
    return bool(
        re.search(rf"(?:async\s+)?fn\s+{name}\s*\(|def\s+{name}\s*\(", corpus)
    )


def test_every_cited_test_exists(source_corpus: str) -> None:
    missing = [
        (doc, name)
        for doc, name in _evidence_identifiers()
        if not _is_test_function(name, source_corpus)
    ]
    assert not missing, (
        "governance docs cite test names that do not exist (update the doc, or the "
        "test was renamed): " + ", ".join(f"{doc}: {name}" for doc, name in missing)
    )


def test_cited_test_files_exist() -> None:
    cited = sorted(
        {
            name
            for doc in _governance_docs()
            for name in re.findall(r"`(tests/python/test_[a-z0-9_]+\.py)`", _read(doc))
        }
    )
    missing = [name for name in cited if not (REPO_ROOT / name).is_file()]
    assert not missing, f"governance docs cite test files that do not exist: {missing}"


def test_the_documented_op_count_matches_the_bridge(bridge_ops: list[str]) -> None:
    claimed: dict[str, set[int]] = {}
    for doc in [*_governance_docs(), ARCHITECTURE_DOC]:
        numbers = {
            int(value) for value in re.findall(r"(\d+)\s*(?:个\s*op|ops)", _read(doc))
        }
        if numbers:
            claimed[doc.name] = numbers

    assert claimed, "no op count is documented; the bridge grew without a doc update"
    total = len(bridge_ops)
    wrong = {doc: numbers for doc, numbers in claimed.items() if numbers != {total}}
    assert not wrong, (
        f"the bridge dispatches {total} ops, but these docs claim otherwise: {wrong}"
    )


def _documented_ops() -> set[str]:
    ops: set[str] = set()
    for doc in [*_governance_docs(), ARCHITECTURE_DOC]:
        for token in OP_TOKEN.findall(_read(doc)):
            head, _, last = token.rpartition(".")
            if head.split(".")[0] in OP_PREFIXES and last in OP_VERBS:
                ops.add(token)
    return ops


def test_every_documented_op_exists_in_the_bridge(bridge_ops: list[str]) -> None:
    unknown = sorted(_documented_ops() - set(bridge_ops))
    assert not unknown, f"docs name ops the bridge does not dispatch: {unknown}"


def test_every_dispatched_op_has_a_python_caller(bridge_ops: list[str]) -> None:
    python_corpus = "\n".join(_read(path) for path in PYTHON_CLIENT_DIR.rglob("*.py"))
    unreachable = sorted(op for op in bridge_ops if f'"{op}"' not in python_corpus)
    assert not unreachable, (
        "the bridge dispatches ops that no Python caller mentions (nobody can use them): "
        f"{unreachable}"
    )


def test_every_governance_service_is_documented() -> None:
    proto = _read(PROTO)
    declared = {
        name
        for name in re.findall(r"^service\s+(\w+)\s*\{", proto, flags=re.MULTILINE)
        if name in GOVERNANCE_SERVICES
    }
    assert declared == set(GOVERNANCE_SERVICES), (
        "the proto's governance service set changed; update GOVERNANCE_SERVICES: "
        f"{sorted(declared)}"
    )
    docs = _read(GOVERNANCE_README) + _read(GOVERNANCE_DIR / "operations.md")
    missing = sorted(name for name in declared if name not in docs)
    assert not missing, f"governance services missing from the docs: {missing}"


# ── the guard's own rules ─────────────────────────────────────


def test_only_the_proof_column_counts_as_evidence() -> None:
    row = "| 可恢复 | 崩溃点之后不丢提交 | `crates/x.rs` | `recovery_is_idempotent` |"
    assert _evidence_text(row) == " `recovery_is_idempotent` "
    assert _evidence_text("| 可恢复的东西 | 只是散文 | 没有证据列 |") is None
    assert _evidence_text("| 别的行 | a | b | c |") is None
    assert _evidence_text("测试：`recovery_is_idempotent`") == "测试：`recovery_is_idempotent`"


def test_the_proof_column_rejects_a_field_name_lookalike(source_corpus: str) -> None:
    """Otherwise `submission_id` would "prove" a claim by being mentioned in code."""
    assert _is_test_function("recovery_is_idempotent", source_corpus)
    assert not _is_test_function("submission_id", source_corpus)
