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
  CONTENT_MISMATCH
                 an *independent* advisory flag, not a verdict: the spec line
                 quotes code that occurs nowhere in the target.  Only quoted
                 spans containing `_`, `=`, `.` or `"` count as content anchors,
                 so grammar meta-variables (`!expr`, `a && b`) are exempt by
                 construction rather than reported.  Kept out of the verdict so
                 it can never hide, nor be hidden by, a STALE result -- the two
                 can hold for the same reference.  This is the anchor that makes
                 the planned 148-reference rewrite safe: once a line number is
                 dropped, the quoted code is the only evidence left.
  DANGLING       a *mention* (see below) that resolves to no file at all.

MENTIONS (issue #675 -- the blind spot this tool used to have):
  The same path written *without* a line number was invisible to this tool,
  even though "the file is gone" has nothing to do with line numbers.  A
  mention is a backticked, path-shaped span with no `:line`, **outside fenced
  code blocks** (a directory tree or a prose block lists paths that are not
  references).  Mentions are resolved exactly like references, but they are
  reported as ADVISORY (DANGLING), never as a structural failure, because the
  measured population mixes real drift with things that legitimately do not
  exist in this repo: a runtime config the operator supplies
  (`uc.scheduler.yaml`), an illustrative path in prose (`new_module/impl.rs`),
  a naming-convention example (`rate_limiter.py`), and one spec file describing
  an out-of-repo project (`tui/**`, 27 of the 47).  Making this gating before
  that triage would fail the build on ~47 items most of which are not defects
  -- the same shape as the 63-failure/61-false-positive census in #672.
  Scope note: this tool now *sees* that population and prints its size; it does
  not yet judge which of them are defects.  A green run still means "no
  structural failure", never "every path in the specs exists".

Usage:
    python scripts/check-spec-refs.py            # structural gate
    python scripts/check-spec-refs.py --audit    # + staleness / dangling tables
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
#
# `.scratch/` is this repo's gitignored scratch space (test harnesses, temp trees,
# rollback copies).  The walk below uses `os.walk`, not git, so leaving it in makes
# verdicts depend on whatever local scratch state happens to exist.  Measured
# (2026-09-16, T24 slice B): a rollback copy parked in `.scratch/` turned a unique
# mention AMBIGUOUS mid-run, and `.scratch/pt-test_*/**/lib.rs` inflated `lib.rs`
# from 4 candidates to 79.  Excluding it changes exactly one verdict in the whole
# corpus (`event-pipeline-spec.md:153` `dashboard/app.py`: ambiguous -> resolved)
# and no reference verdict at all.
EXCLUDE_DIRS = {".git", ".scratch", "target", "node_modules", "vendor", ".venv",
                "__pycache__", "dist", "build", ".pytest_cache"}

CODE_EXT = {".py", ".rs", ".ts", ".tsx", ".js", ".jsx", ".proto", ".toml",
            ".yml", ".yaml", ".json", ".sql", ".sh", ".md"}

REF_RE = re.compile(
    r"([A-Za-z0-9_][A-Za-z0-9_./\\-]*\.(?:py|rs|ts|tsx|js|jsx|proto|toml|yml|yaml|json|sql|sh))"
    r":(\d+)(?:\s*[-\u2013]\s*(\d+))?"
)
TICK_RE = re.compile(r"`([^`\n]+)`")
BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*$")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# A quoted span counts as a *content anchor* only if it looks like a code token:
# an underscore, an assignment, a member access, or a string literal.  Grammar
# meta-variables (`!expr`, `a && b`, `(expr)`) contain none of these and are
# therefore exempt by construction rather than reported as mismatches.
CONTENT_TOKEN_RE = re.compile(r"[_.=\"]")

# A quoted span that is nothing but a path (optionally with a line/range).
PATH_SPAN_RE = re.compile(
    r"^[\w./\\:-]+\.(?:py|rs|ts|tsx|js|jsx|proto|toml|yml|yaml|json|sql|sh)"
    r"(?::\d+(?:\s*[-\u2013]\s*\d+)?)?$"
)

# A fenced code block: paths inside one are illustrations, not references
# (`directory-structure.md` lists whole trees that way).
FENCE_RE = re.compile(r"^\s*(?:```|~~~)")

# Shape of a *mention* (a path quoted without a line number).  Deliberately
# different from PATH_SPAN_RE above, which answers a different question
# ("is this quoted span a path, so it is not a content anchor?"):
#   * `.md` IS included -- cross-spec links (`worker-service-spec.md`) are
#     references whose target can go missing;
#   * a leading `.` or `/` is NOT a mention -- `.mcp.json`, `./uc.scheduler.yaml`
#     and `/app/docker/...` are operator-supplied or container-side paths, not
#     repo-relative references (excluded by starting the pattern with `\w`);
#   * `:` is not allowed at all, so a `path:line` span can never also be a mention.
MENTION_PATH_RE = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9_./\\-]*\."
    r"(?:py|rs|ts|tsx|js|jsx|proto|toml|yml|yaml|json|sql|sh|md)"
)

# Mention verdicts.  Distinct names from the reference verdicts so that the
# existing reference filters cannot accidentally pick a mention up.
MENTION_OK = "MENTION_RESOLVED"
MENTION_AMBIGUOUS = "MENTION_AMBIGUOUS"
DANGLING = "DANGLING"

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
    """Symbols named on a spec line, in order, deduped.

    This repo's specs name symbols three ways, so all three are scanned: inline
    code (``Worker._execute_steps``), bold (**Task**) and headings
    (``### Task Properties``).  Scanning only inline code -- the first version --
    silently reported every bold/heading symbol as "no symbol on this line",
    which downgraded 13 real anchors to unjudgeable.

    A bold span counts only when it *is* an identifier: prose that merely
    contains such a word is not an anchor.  Measured (T24): the first version
    took ``**Mapping `MemoryWriteError` for delete operations**`` as naming the
    symbol ``delete``, which made ``error-handling.md:307`` report a *false*
    STALE off a prose word -- the reference points at a location, not at
    anything called `delete`.  Tightening it changes exactly one row in the
    whole corpus (STALE 27 -> 26, OK 113 -> 114) and drops that row out of the
    slice-B rewrite worklist -- measured, and correct rather than a loss: the
    prose word was the row's *only* anchor, so its line number was never safe
    to drop (line 307 leaves 52 eligible references at 51).  Headings stay
    prose-tolerant on purpose: ``### Task Properties`` is one of the restored
    13, and the heading carries no other syntax to key off.
    """
    spans: list[str] = TICK_RE.findall(spec_line)
    spans += [b for b in BOLD_RE.findall(spec_line) if IDENT_RE.fullmatch(b.strip())]
    heading = HEADING_RE.match(spec_line)
    if heading:
        spans.append(heading.group(1))
    out: list[str] = []
    for span in spans:
        span = span.strip()
        if pathlib.PurePath(span).suffix in CODE_EXT:
            continue
        for part in re.split(r"[.\s()\[\],=:]+", span):
            if len(part) > 3 and IDENT_RE.fullmatch(part) and part not in SYMBOL_STOPWORDS:
                if part not in out:
                    out.append(part)
    return out


def _content_forms(span: str) -> list[str]:
    """Literal forms of a quoted span, most specific first.

    `Worker._execute_steps` never occurs in worker.py (there it is
    ``def _execute_steps``), and ``abort_on_failure=True`` may be written as a
    bare identifier; without these fallbacks every qualified name would be a
    false CONTENT_MISMATCH.
    """
    forms = [span]
    tail = re.split(r"\.|::", span)[-1]
    if tail and tail != span:
        forms.append(tail)
    for cut in ("=(", "=", "(", " "):
        head = span.split(cut, 1)[0].strip()
        if head and head != span and len(head) >= 4:
            forms.append(head)
    return forms


def _content_anchor(spec_line: str, body: str) -> tuple[str | None, list[str]]:
    """Return (matched_literal, candidates) for a spec line against a target body.

    candidates are the code-shaped spans quoted on the line; an empty list means
    content matching does not apply (the line quotes no code).  A None match with
    a non-empty candidate list means nothing quoted on the line occurs in the
    target at all.
    """
    candidates: list[str] = []
    for span in TICK_RE.findall(spec_line):
        span = span.strip()
        if len(span) < 4 or not CONTENT_TOKEN_RE.search(span):
            continue
        if PATH_SPAN_RE.match(span) or pathlib.PurePath(span).suffix in CODE_EXT:
            continue
        if span not in candidates:
            candidates.append(span)
    for span in candidates:
        for form in _content_forms(span):
            if form in body:
                return form, candidates
    return None, candidates


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
            anchor: str | None = None
            content_ok = True
            content_candidates: list[str] = []

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

                # two advisory anchors: the symbol named on the line, and any
                # code the line quotes (the latter survives dropping a line no.)
                if structural is None:
                    body = (ROOT / target).read_bytes().decode("utf-8", "replace")
                    definitions = _definitions(body.split("\n"))
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
                    anchor, content_candidates = _content_anchor(
                        spec_lines[spec_line_no - 1], body)
                    content_ok = anchor is not None or not content_candidates

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
                "kind": "ref",
                "symbol": symbol,
                "def_line": definition,
                "offset": offset,
                "content": anchor,
                "content_ok": content_ok,
                "content_candidate_count": len(content_candidates),
            })

        # Mentions (#675): the same path written WITHOUT a line number used to
        # be invisible.  Scanned per line so the fence state is available; a
        # span carrying `:line` is a reference and is skipped here.
        in_fence = False
        for number, line in enumerate(spec_lines, start=1):
            if FENCE_RE.match(line):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            for raw_span in TICK_RE.findall(line):
                span = raw_span.strip()
                if REF_RE.search(span) or not MENTION_PATH_RE.fullmatch(span):
                    continue
                target, candidates, kind = _resolve(span, index)
                verdict = {
                    "exact": MENTION_OK,
                    "suffix_unique": MENTION_OK,
                    "suffix_ambiguous": MENTION_AMBIGUOUS,
                    "none": DANGLING,
                }[kind]
                rows.append({
                    "spec": spec_rel,
                    "spec_line": number,
                    "ref": span,
                    "start": 0,
                    "end": 0,
                    "target": target,
                    "candidates": candidates,
                    "verdict": verdict,
                    "kind": "mention",
                    "symbol": None,
                    "def_line": None,
                    "offset": None,
                    "content": None,
                    "content_ok": True,
                    "content_candidate_count": 0,
                })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--audit", action="store_true",
                        help="also print the advisory symbol-staleness table")
    parser.add_argument("--json", action="store_true", help="emit raw rows as JSON")
    args = parser.parse_args()

    rows = collect()
    refs = [r for r in rows if r["kind"] == "ref"]
    mentions = [r for r in rows if r["kind"] == "mention"]
    structural = [r for r in refs if r["verdict"] in {"MISSING_FILE", "PATH_FORM", "OUT_OF_RANGE"}]
    ambiguous = [r for r in refs if r["verdict"] == "AMBIGUOUS"]
    stale = [r for r in refs if r["verdict"] == "STALE"]
    content = [r for r in refs if not r["content_ok"]]
    ok = [r for r in refs if r["verdict"] == "OK"]
    dangling = [r for r in mentions if r["verdict"] == DANGLING]
    mention_ambiguous = [r for r in mentions if r["verdict"] == MENTION_AMBIGUOUS]

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 1 if structural else 0

    print(f"scanned {len(refs)} `path:line` references in "
          f"{len({r['spec'] for r in refs})} spec files")
    print(f"scanned {len(mentions)} line-free path mentions in "
          f"{len({r['spec'] for r in mentions})} spec files "
          f"({len(mentions) - len(dangling) - len(mention_ambiguous)} resolved / "
          f"{len(dangling)} dangling / {len(mention_ambiguous)} ambiguous)")

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

    if args.audit and content:
        print(f"\nADVISORY (independent of verdict): {len(content)} reference(s) quote code "
              f"that occurs nowhere in the target")
        print("  -- a line number is the only thing still pointing at anything:")
        for row in sorted(content, key=lambda r: (r["spec"], r["spec_line"])):
            where = f"{row['spec'].split('/')[-1]}:{row['spec_line']}"
            print(f"  {row['ref']:26s} -> {row['target'].split('/')[-1]:20s} ({where})")
    elif not args.audit and content:
        print(f"\nADVISORY: {len(content)} reference(s) quote code absent from the target "
              f"(run with --audit for the list).")

    if args.audit and dangling:
        print(f"\nADVISORY: {len(dangling)} line-free path mentions resolve to no file "
              f"(not a verdict -- the population mixes real drift with runtime "
              f"configs, illustrative paths and an out-of-repo project):")
        for row in sorted(dangling, key=lambda r: (r["spec"], r["spec_line"])):
            where = f"{row['spec'].split('/')[-1]}:{row['spec_line']}"
            print(f"  {row['ref']:52s} ({where})")
    elif not args.audit and dangling:
        print(f"\nADVISORY: {len(dangling)} line-free path mentions resolve to no file "
              f"(run with --audit for the list).")

    print(f"\nsummary: {len(ok)} ok / {len(stale)} stale(advisory) / "
          f"{len(ambiguous)} ambiguous(advisory) / {len(structural)} structural failure(s)")
    print(f"         {len(content)} of {len(refs)} have no matching quoted content "
          f"(orthogonal to the verdict above)")
    print(f"         mentions: {len(dangling)} of {len(mentions)} resolve to no file "
          f"(advisory, never failing -- see the module docstring)")

    if structural:
        print("spec reference audit FAILED.")
        return 1
    print("spec reference audit passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
