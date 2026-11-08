"""Label corpus manifest for typed reviewers (P5 / D12).

Labels are human-authored and stored outside the repository. What the repository
holds is a *manifest*: which failure kind the corpus labels, who labelled it, how
many rows it holds, where it lives, and which eval roles the labelled attempts came
from. The manifest exists so a CV threshold artifact can be checked against the
corpus that supposedly produced it -- without the corpus, and without ever storing
label content in the repository or ``RuntimeState``.

Two boundaries are enforced here, both from earlier decisions:

* a label derived from the **report** hold-out is refused (D11's leakage boundary,
  applied to labels): calibrating a reviewer on the set it will be scored against is
  the same leakage;
* an agent/worker/service principal may *propose* labels but never certify them
  (D5: no "calibrated" claim without human labels).

Refusals are ``ValueError``, matching :mod:`ultimate_coders.agent.typed_review_thresholds`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ultimate_coders.agent.routing import FailureKind

#: Manifest schema this module understands. A different number is refused rather
#: than guessed at -- a silently reinterpreted corpus is worse than none.
SCHEMA_VERSION = 1

#: Allowed FailureKind string values (no parallel taxonomy, same source as the
#: threshold artifact loader).
_VALID_KINDS = frozenset(kind.value for kind in FailureKind)

#: Roles a labelled attempt may come from. ``report`` is the hold-out.
ALLOWED_SOURCE_ROLES = frozenset({"search", "select"})

#: Principal prefixes that may propose labels but never certify them.
_NON_HUMAN_PREFIXES = frozenset({"agent", "worker", "service"})

#: ``corpus_uri`` must carry a scheme, so a repo-relative path (which could be
#: committed and drift into the report set) is refused by construction.
_URI_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)

_REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "labeler",
        "labeled_at",
        "n_labels",
        "source_eval_roles",
        "corpus_uri",
    }
)


@dataclass(frozen=True)
class LabelManifest:
    """Provenance for one failure-kind label corpus (never its content)."""

    schema_version: int
    kind: str
    labeler: str
    labeled_at: str
    n_labels: int
    source_eval_roles: tuple[str, ...]
    corpus_uri: str


def _refuse(message: str) -> None:
    raise ValueError(message)


def _require_mapping(data: Any, *, source: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        _refuse(f"label manifest must be a JSON object ({source})")
    return data


def _is_human_labeler(labeler: str) -> bool:
    prefix = labeler.split(":", 1)[0].strip().lower()
    return prefix not in _NON_HUMAN_PREFIXES


def _parse_manifest(data: Mapping[str, Any], *, source: str) -> LabelManifest:
    missing = sorted(_REQUIRED_KEYS - set(data))
    if missing:
        _refuse(f"label manifest is missing {missing} ({source})")

    schema_version = data["schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        _refuse(f"label manifest schema_version must be an int ({source})")
    if schema_version != SCHEMA_VERSION:
        _refuse(
            f"label manifest schema_version={schema_version} is not {SCHEMA_VERSION} ({source})"
        )

    kind = data["kind"]
    if not isinstance(kind, str) or kind.strip().lower() not in _VALID_KINDS:
        _refuse(
            f"label manifest kind={kind!r} is not a FailureKind value "
            f"{sorted(_VALID_KINDS)} ({source})"
        )

    labeler = data["labeler"]
    if not isinstance(labeler, str) or not labeler.strip():
        _refuse(f"label manifest labeler must be a non-empty string ({source})")
    if not _is_human_labeler(labeler.strip()):
        _refuse(
            f"label manifest labeler={labeler!r} is not a human principal; "
            f"agents may propose labels but never certify them ({source})"
        )

    labeled_at = data["labeled_at"]
    if not isinstance(labeled_at, str) or not labeled_at.strip():
        _refuse(f"label manifest labeled_at must be a non-empty string ({source})")

    n_labels = data["n_labels"]
    if isinstance(n_labels, bool) or not isinstance(n_labels, int):
        _refuse(f"label manifest n_labels must be an int ({source})")
    if n_labels < 1:
        _refuse(f"label manifest n_labels must be >= 1, got {n_labels} ({source})")

    roles_raw = data["source_eval_roles"]
    if not isinstance(roles_raw, (list, tuple)) or not roles_raw:
        _refuse(f"label manifest source_eval_roles must be a non-empty list ({source})")
    roles: list[str] = []
    for role in roles_raw:
        if not isinstance(role, str) or not role.strip():
            _refuse(f"label manifest source_eval_roles entries must be strings ({source})")
        normalized = role.strip().lower()
        if normalized == "report":
            _refuse(
                "label manifest source_eval_roles must not contain 'report': "
                f"labels from the report hold-out leak it into calibration ({source})"
            )
        if normalized not in ALLOWED_SOURCE_ROLES:
            _refuse(
                f"label manifest source_eval_roles entry {role!r} is not one of "
                f"{sorted(ALLOWED_SOURCE_ROLES)} ({source})"
            )
        if normalized not in roles:
            roles.append(normalized)

    corpus_uri = data["corpus_uri"]
    if not isinstance(corpus_uri, str) or not _URI_SCHEME.match(corpus_uri.strip()):
        _refuse(
            "label manifest corpus_uri must be an external URI with a scheme "
            f"(e.g. s3://, https://, file://); got {corpus_uri!r} ({source})"
        )

    return LabelManifest(
        schema_version=schema_version,
        kind=kind.strip().lower(),
        labeler=labeler.strip(),
        labeled_at=labeled_at.strip(),
        n_labels=n_labels,
        source_eval_roles=tuple(roles),
        corpus_uri=corpus_uri.strip(),
    )


def load_label_manifest(path: str | Path) -> LabelManifest:
    """Load and validate one label corpus manifest.

    Refuses (raises :class:`ValueError`) on a missing file, corrupt JSON, missing
    fields, a schema it does not understand, a non-``FailureKind`` kind, a
    non-human labeler, a repo-relative ``corpus_uri``, an unknown source role, or a
    ``report`` source role.
    """
    manifest_path = Path(path)
    if not manifest_path.is_file():
        _refuse(f"label manifest not found: {manifest_path}")
    try:
        raw_text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        _refuse(f"label manifest unreadable: {manifest_path}: {exc}")
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        _refuse(f"label manifest corrupt JSON ({manifest_path}): {exc}")
    source = str(manifest_path)
    return _parse_manifest(_require_mapping(data, source=source), source=source)
