"""Emit deterministic mutation candidates for an acceptance dataset.

This is the dataset-builder entry point for :mod:`mutation_augment`.  It reads
two directory trees (the pre-patch and post-patch snapshots), derives the same
deterministic candidates as :func:`generate_mutations`, and writes a bounded
JSON document.  It deliberately does **not** execute the mutants, so it does
not claim a kill rate; running mutants is an acceptance-runner concern.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from ultimate_coders.acceptance.mutation_augment import generate_mutations

__all__ = ["build_parser", "main"]

_MAX_FILE_BYTES = 2_000_000


def _read_tree(root: Path) -> dict[str, str]:
    if not root.is_dir():
        raise ValueError(f"not a directory: {root}")
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            files[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return files


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mutation-cli",
        description="Generate deterministic mutation candidates for a patch delta.",
    )
    parser.add_argument("--before", required=True, type=Path, help="pre-patch tree")
    parser.add_argument("--after", required=True, type=Path, help="post-patch tree")
    parser.add_argument("--output", type=Path, default=None, help="write JSON here")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        before = _read_tree(args.before)
        after = _read_tree(args.after)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    batch = generate_mutations(before, after)
    payload = {
        "version": 1,
        "before": str(args.before),
        "after": str(args.after),
        "batch": batch.to_dict(),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
