"""Tests for the mutation-augmentation acceptance CLI."""

from __future__ import annotations

import json

from ultimate_coders.acceptance.mutation_cli import main


def _tree(root, files):
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_cli_emits_candidates_for_a_changed_python_file(tmp_path, capsys):
    before_dir = tmp_path / "before"
    after_dir = tmp_path / "after"
    before_dir.mkdir()
    after_dir.mkdir()
    _tree(
        before_dir,
        {"src/check.py": "def safe(x):\n    if x == 1:\n        return True\n    return False\n"},
    )
    _tree(
        after_dir,
        {"src/check.py": "def safe(x):\n    if x == 2:\n        return True\n    return False\n"},
    )

    assert main(["--before", str(before_dir), "--after", str(after_dir)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == 1
    assert payload["batch"]["count"] >= 1
    assert "src/check.py" in {item["path"] for item in payload["batch"]["mutations"]}


def test_cli_reports_no_candidates_for_identical_trees(tmp_path, capsys):
    before_dir = tmp_path / "before"
    after_dir = tmp_path / "after"
    before_dir.mkdir()
    after_dir.mkdir()
    _tree(before_dir, {"README.md": "stable\n"})
    _tree(after_dir, {"README.md": "stable\n"})

    assert main(["--before", str(before_dir), "--after", str(after_dir)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["batch"] == {"mutations": [], "count": 0, "kinds": []}


def test_cli_writes_a_bounded_json_document(tmp_path):
    before_dir = tmp_path / "before"
    after_dir = tmp_path / "after"
    before_dir.mkdir()
    after_dir.mkdir()
    _tree(before_dir, {"config.py": "LIMIT = 1\n"})
    _tree(after_dir, {"config.py": "LIMIT = 2\n"})
    output = tmp_path / "out" / "mutations.json"

    assert main(
        ["--before", str(before_dir), "--after", str(after_dir), "--output", str(output)]
    ) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["batch"]["count"] >= 1
    assert output.read_text(encoding="utf-8").endswith("\n")


def test_cli_rejects_a_missing_tree(tmp_path, capsys):
    after_dir = tmp_path / "after"
    after_dir.mkdir()

    assert main(["--before", str(tmp_path / "missing"), "--after", str(after_dir)]) == 2
    assert "not a directory" in capsys.readouterr().err
