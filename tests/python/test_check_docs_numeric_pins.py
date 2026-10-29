"""Tests for scripts/check-docs-numeric-pins.py (D10 / T21).

Pins:

  * the real repo passes (non-vacuity from the outside)
  * pin↔doc drift reddens with a pin-drift message
  * undocumented uplift ``%`` on README reddens with uplift-undocumented
  * an allowlist entry without eval_role=report reddens with uplift-provenance

Mutations run in a sandbox copy so the real tree is never touched.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
GUARD = REPO / "scripts" / "check-docs-numeric-pins.py"
MANIFEST_REL = "docs/numeric-pins.yaml"
PY = sys.executable

SURFACES = (
    "README.md",
    "README.zh-CN.md",
    "docs/live-feedback-and-metainfer-verification.md",
)


def build_sandbox(tmp_path: pathlib.Path) -> pathlib.Path:
    """Copy the closed input set the guard reads into an isolated tree."""
    sb = tmp_path / "sb"
    (sb / "scripts").mkdir(parents=True)
    (sb / "docs").mkdir(parents=True)
    shutil.copy2(GUARD, sb / "scripts" / GUARD.name)
    shutil.copy2(REPO / MANIFEST_REL, sb / MANIFEST_REL)
    for rel in SURFACES:
        dest = sb / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, dest)
    return sb


def run(sb: pathlib.Path) -> tuple[int, str]:
    r = subprocess.run(
        [PY, str(sb / "scripts" / GUARD.name)],
        capture_output=True,
        cwd=str(sb),
    )
    out = (r.stdout or b"").decode("utf-8", "replace") + (
        r.stderr or b""
    ).decode("utf-8", "replace")
    return r.returncode, out


def problems(out: str) -> list[str]:
    return [
        line.strip().lstrip("- ").strip()
        for line in out.splitlines()
        if line.strip().startswith("- ")
    ]


def test_real_repo_passes() -> None:
    r = subprocess.run([PY, str(GUARD)], capture_output=True, cwd=str(REPO))
    out = (r.stdout or b"").decode("utf-8", "replace")
    assert r.returncode == 0, out
    assert "docs-numeric-pins check passed" in out
    assert "pin assertion(s) checked" in out


def test_pin_drift_reddens(tmp_path: pathlib.Path) -> None:
    sb = build_sandbox(tmp_path)
    readme = sb / "README.md"
    text = readme.read_text(encoding="utf-8")
    assert "6.394ms" in text
    readme.write_text(text.replace("6.394ms", "6.395ms", 1), encoding="utf-8")
    code, out = run(sb)
    assert code == 1, out
    msgs = problems(out)
    assert any("pin-drift" in m and "6.394ms" in m for m in msgs), msgs


def test_uplift_undocumented_reddens(tmp_path: pathlib.Path) -> None:
    sb = build_sandbox(tmp_path)
    readme = sb / "README.md"
    text = readme.read_text(encoding="utf-8")
    # Inject after the title line so CI prose parsers elsewhere are untouched.
    lines = text.splitlines(keepends=True)
    assert lines, "README empty"
    lines.insert(1, "Synthetic uplift claim: 40% faster on select.\n")
    readme.write_text("".join(lines), encoding="utf-8")
    code, out = run(sb)
    assert code == 1, out
    msgs = problems(out)
    assert any("uplift-undocumented" in m and "40%" in m for m in msgs), msgs


def test_uplift_provenance_requires_report(tmp_path: pathlib.Path) -> None:
    sb = build_sandbox(tmp_path)
    manifest_path = sb / MANIFEST_REL
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    data["uplift_allowed"] = [{"token": "40%", "eval_role": "select"}]
    manifest_path.write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
    )
    # Also inject the percentage so the ban scan would otherwise allow it.
    readme = sb / "README.md"
    lines = readme.read_text(encoding="utf-8").splitlines(keepends=True)
    lines.insert(1, "Claim: 40% uplift.\n")
    readme.write_text("".join(lines), encoding="utf-8")
    code, out = run(sb)
    assert code == 1, out
    msgs = problems(out)
    assert any("uplift-provenance" in m for m in msgs), msgs


def test_report_allowlist_accepts_percentage(tmp_path: pathlib.Path) -> None:
    sb = build_sandbox(tmp_path)
    manifest_path = sb / MANIFEST_REL
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    data["uplift_allowed"] = [{"token": "40%", "eval_role": "report"}]
    manifest_path.write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
    )
    readme = sb / "README.md"
    lines = readme.read_text(encoding="utf-8").splitlines(keepends=True)
    lines.insert(1, "Report-role claim: 40% uplift.\n")
    readme.write_text("".join(lines), encoding="utf-8")
    code, out = run(sb)
    assert code == 0, out
    assert "docs-numeric-pins check passed" in out


def test_missing_surface_reddens(tmp_path: pathlib.Path) -> None:
    sb = build_sandbox(tmp_path)
    (sb / "docs" / "live-feedback-and-metainfer-verification.md").unlink()
    code, out = run(sb)
    assert code == 1, out
    msgs = problems(out)
    assert any("surface-missing" in m for m in msgs), msgs
