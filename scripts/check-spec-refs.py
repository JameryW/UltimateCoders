"""Audit `path:line` references inside `.trellis/spec/**`.

Background (issue #672): spec prose cites code as `worker.py:1035`.  Those line
numbers drift whenever code is inserted above the target, because nothing
verifies them.  The failure mode is *"valid line number, wrong content"* -- a
census on 2026-09-16 found 149 references of which **0** were out of range, so
purely structural checks cannot see the drift at all.  (149 is the pre-fix count:
the two fixes below deleted one dead reference, so the current inventory is 148.
Ticket prose that says 148 is quoting the post-fix number -- both counts agree
that not a single reference was out of range.)

This tool therefore separates two things:

STRUCTURAL (deterministic, fail-closed -- exit 1):
  MISSING_FILE   nothing in the repo matches the referenced path
  PATH_FORM      a reference WRITTEN WITH A DIRECTORY only resolves as a
                 suffix, i.e. the directory is wrong or missing
                 (`uc-engine/src/x.rs` where the file lives at
                 `crates/uc-engine/src/x.rs`)
  OUT_OF_RANGE   the file resolves but the referenced line does not exist

Resolution convention: a **bare basename** (`worker.py`) is sanctioned spec
shorthand -- it is OK when it resolves to exactly one file, AMBIGUOUS (advisory)
when it resolves to several, and MISSING_FILE when it resolves to none.
Only slashed paths are held to the exact-path standard.

ADVISORY (heuristic, never fails the build):
  AMBIGUOUS      a bare basename matches several files (`types.py`)
  STALE          a symbol named on the same spec line has its real definition
                 elsewhere; reported only with --audit.  Small offsets are
                 ambiguous (a reference may intentionally target a doc comment
                 or attribute directly above the definition), so this is
                 evidence for a human, not a gate.

Usage:
    python scripts/check-spec-refs.py            # structural gate
    python scripts/check-spec-refs.py --audit    # + symbol staleness table
    python scripts/check-spec-refs.py --json     # machine-readable rows
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import pathlib
import re
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC_DIR = ROOT / ".trellis" / "spec"

# Directories never scanned as *targets* of a reference.
EXCLUDE_DIRS = {".git", "target", "node_modules", "vendor", ".venv", "__pycache__",
                "dist", "build", ".pytest_cache"}

CODE_EXT = {".py", ".rs", ".ts", ".tsx", ".js", ".jsx", ".proto", ".toml",
            ".yml", ".yaml", ".json", ".sql", ".sh", ".md"}

REF_RE = re.compile(
    r"([A-Za-z0-9_][A-Za-z0-9_./\\-]*\.(?:py|rs|ts|tsx|js|jsx|proto|toml|yml|yaml|json|sql|sh))"
    r":(\d+)(?:\s*[-\u2013]\s*(\d+))?"
)
TICK_RE = re.compile(r"`([^`\n]+)`")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Attribute / keyword names that are never the intended symbol anchor.
SYMBOL_STOPWORDS = {
    "default", "serde", "derive", "cfg", "allow", "warn", "deny", "doc", "inline",
    "deprecated", "feature", "tokio", "true", "false", "none", "some", "self", "cls",
    "string", "option", "result", "value", "values", "items", "keys", "type", "bool",
    "dict", "list", "int", "str", "any", "new", "test", "read", "write", "open", "len",
}

DEF_PATTERNS = (
    re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait|mod|type|const|static)\s+([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"^\s*(?:pub\s+)?impl(?:<[^>]*>)?\s+([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:async\s*)?\("),
)


def _repo_index() -> dict[str, list[str]]:
    """basename -> sorted repo-relative paths. Tolerates unreadable links."""
    index: dict[str, list[str]] = collections.defaultdict(list)
    for dirpath, dirnames, filenames in os.walk(ROOT, onerror=lambda e: None):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for filename in filenames:
            if pathlib.PurePath(filename).suffix not in CODE_EXT:
                continue
            full = os.path.join(dirpath, filename)
            try:
                if not os.path.isfile(full):
                    continue
            except OSError:
                continue
            index[filename].append(pathlib.Path(full).relative_to(ROOT).as_posix())
    return index


def _line_count(rel: str) -> int | None:
    try:
        text = (ROOT / rel).read_bytes().decode("utf-8", "replace")
    except OSError:
        return None
    return len(text.split("\n"))


def _definitions(lines: list[str]) -> dict[str, list[int]]:
    found: dict[str, list[int]] = collections.defaultdict(list)
    for number, line in enumerate(lines, start=1):
        for pattern in DEF_PATTERNS:
            match = pattern.match(line)
            if match:
                found[match.group(1)].append(number)
                break
    return found


def _symbols_on(spec_line: str) -> list[str]:
    """Symbols named (backticked) on a spec line, in order, deduped."""
    out: list[str] = []
    for span in TICK_RE.findall(spec_line):
        span = span.strip()
        if pathlib.PurePath(span).suffix in CODE_EXT:
            continue
        for part in re.split(r"[.\s()\[\],=:]+", span):
            if len(part) > 3 and IDENT_RE.fullmatch(part) and part not in SYMBOL_STOPWORDS:
                if part not in out:
                    out.append(part)
    return out


def _resolve(ref: str, index: dict[str, list[str]]):
    """Return (target, candidates, kind).

    kind: exact | suffix_unique | suffix_ambiguous | none
    A bare basename is accepted when it is unique (spec shorthand); a slashed
    path is only accepted as written.
    """
    normalised = ref.replace("\\", "/")
    if (ROOT / normalised).is_file():
        return normalised, [normalised], "exact"
    candidates = sorted(index.get(normalised.split("/")[-1], []))
    if not candidates:
        return None, [], "none"
    if len(candidates) == 1:
        return candidates[0], candidates, "suffix_unique"
    return None, candidates, "suffix_ambiguous"


def collect() -> list[dict[str, Any]]:
    index = _repo_index()
    rows: list[dict[str, Any]] = []
    for spec in sorted(SPEC_DIR.rglob("*.md")):
        text = spec.read_text(encoding="utf-8", errors="replace")
        spec_lines = text.split("\n")
        spec_rel = spec.relative_to(ROOT).as_posix()
        for match in REF_RE.finditer(text):
            ref = match.group(1)
            start = int(match.group(2))
            end = int(match.group(3)) if match.group(3) else start
            spec_line_no = text[: match.start()].count("\n") + 1
            target, candidates, kind = _resolve(ref, index)

            verdict: str
            offset: int | None = None
            symbol: str | None = None
            definition: int | None = None

            # Two independent judgements, combined by precedence so that an
            # advisory (symbol) result can never mask a structural defect.
            structural: str | None = None
            if kind == "none":
                structural = "MISSING_FILE"
            elif kind != "suffix_ambiguous":
                # exact path, or a bare basename that resolves uniquely.
                # A *slashed* reference that only resolves as a suffix has a
                # wrong/missing directory prefix -> real defect.
                if kind == "suffix_unique" and "/" in ref.replace("\\", "/"):
                    structural = "PATH_FORM"
                count = _line_count(target)
                if count is None:
                    structural = "MISSING_FILE"
                elif start > count or end > count:
                    structural = "OUT_OF_RANGE"

                # symbol-anchored staleness (advisory)
                if structural is None:
                    lines = (ROOT / target).read_bytes().decode("utf-8", "replace").split("\n")
                    definitions = _definitions(lines)
                    best = None
                    for candidate in _symbols_on(spec_lines[spec_line_no - 1]):
                        for line_no in definitions.get(candidate, []):
                            distance = 0 if start <= line_no <= end else abs(line_no - start)
                            if best is None or distance < best[2]:
                                best = (candidate, line_no, distance)
                    if best is not None:
                        symbol, definition = best[0], best[1]
                        if not (start <= definition <= end):
                            offset = definition - start

            if kind == "none":
                verdict = "MISSING_FILE"
            elif kind == "suffix_ambiguous":
                verdict = "AMBIGUOUS"
            elif structural is not None:
                verdict = structural
            elif offset is not None:
                verdict = "STALE"
            else:
                verdict = "OK"

            rows.append({
                "spec": spec_rel,
                "spec_line": spec_line_no,
                "ref": ref,
                "start": start,
                "end": end,
                "target": target,
                "candidates": candidates,
                "verdict": verdict,
                "symbol": symbol,
                "def_line": definition,
                "offset": offset,
            })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--audit", action="store_true",
                        help="also print the advisory symbol-staleness table")
    parser.add_argument("--json", action="store_true", help="emit raw rows as JSON")
    args = parser.parse_args()

    rows = collect()
    structural = [r for r in rows if r["verdict"] in {"MISSING_FILE", "PATH_FORM", "OUT_OF_RANGE"}]
    ambiguous = [r for r in rows if r["verdict"] == "AMBIGUOUS"]
    stale = [r for r in rows if r["verdict"] == "STALE"]
    ok = [r for r in rows if r["verdict"] == "OK"]

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 1 if structural else 0

    print(f"scanned {len(rows)} `path:line` references in "
          f"{len({r['spec'] for r in rows})} spec files")

    if structural:
        print("\nSTRUCTURAL FAILURES (these break the build):")
        for row in sorted(structural, key=lambda r: (r["spec"], r["spec_line"])):
            extra = ""
            if row["verdict"] == "PATH_FORM":
                extra = f"  -> resolves as {row['target']}"
            if row["candidates"]:
                extra += f"  candidates={row['candidates'][:3]}"
            print(f"  {row['verdict']:13s} {row['spec']}:{row['spec_line']}  {row['ref']}{extra}")

    if ambiguous:
        print(f"\nADVISORY: {len(ambiguous)} bare-basename references are ambiguous "
              f"(under-specified, not counted as failures):")
        seen = collections.Counter(r["ref"] for r in ambiguous)
        for ref, count in seen.most_common():
            where = sorted({r["spec"] for r in ambiguous if r["ref"] == ref})
            print(f"  {ref:22s} x{count}  in {', '.join(where)}")

    if args.audit and stale:
        print(f"\nADVISORY: {len(stale)} references whose named symbol is defined elsewhere "
              f"(small offsets are ambiguous -- a reference may target a doc comment or "
              f"attribute above the definition):")
        for row in sorted(stale, key=lambda r: -abs(r["offset"] or 0)):
            where = f"{row['spec'].split('/')[-1]}:{row['spec_line']}"
            print(f"  off {row['offset']:+5d}  {row['ref']:26s} {row['symbol'] or '-':24s} "
                  f"defined at {row['def_line']}  ({where})")
    elif not args.audit and stale:
        print(f"\nADVISORY: {len(stale)} references look symbol-stale "
              f"(run with --audit for the table).")

    print(f"\nsummary: {len(ok)} ok / {len(stale)} stale(advisory) / "
          f"{len(ambiguous)} ambiguous(advisory) / {len(structural)} structural failure(s)")

    if structural:
        print("spec reference audit FAILED.")
        return 1
    print("spec reference audit passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
