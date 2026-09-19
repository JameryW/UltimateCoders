"""Guard: every tracked text file must use ONE kind of line ending.

Why this exists
---------------
`core.autocrlf` (true on this machine, from the sandbox's PortableGit *system*
gitconfig -- not a project choice) normalises CRLF to LF on `git add`.  So a file
whose working-tree copy mixes CRLF and lone LF still compares equal to its index
blob: `git status` and `git diff` both report it as clean.  Nothing else notices
either -- the only mixed-ending detector in the repo is the journal ledger's
ADVISORY (`check-journal-ledger.py`), which is scoped to journals and explicitly
"printed, never a verdict".  The gap was scope and armament, never knowledge.

The cost of a mixed file is paid by every byte-level tool: an anchor written with a
bare newline matches zero times inside a CRLF region, and a writer that emits a bare
newline deepens the mixture.  "Measure the ending before you touch a file" has been
hand-run on every ticket; this makes it a judgment.

What it judges
--------------
J1  non-vacuity: the scan saw tracked files, and the binary classifier actually
    classified some as binary (a classifier that stops classifying would report
    PNG/MP4 bytes as mixed line endings -- false positives, not a silent pass).
J2  the INDEX blob of a tracked file must not mix endings.  Deterministic everywhere:
    index bytes are identical on every machine, so this is the judgment CI can always
    make.
J3  the WORKING TREE copy must not mix endings.  Environment-dependent by nature (this
    machine checks out CRLF, CI checks out LF), so it only ever judges "mixed", never
    "which one" -- judging the latter would report CRLF as a violation and be wrong.
    J3 is green in CI because CI's checkout is genuinely uniform, which is the correct
    verdict rather than a vacuous one: a mixed blob committed from a non-autocrlf
    machine arrives as a mixed checkout and J3 reddens.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent

# A scan of zero files is a broken index source, not a pass.  The threshold stays at
# 1 so the guard also works in a sandbox with a handful of files (the test harness);
# the production signal is the printed count.
MIN_FILES = 1
MIN_BINARY = 1


def tracked_files() -> tuple[list[str], int]:
    """The index is the only source that matches what another checkout sees.

    Using `git ls-files` instead of walking the tree means ignored directories
    (`.workbuddy/`, `target/`, `tmp/`) simply never appear -- no blacklist needed.
    Returns the file paths and the number of gitlinks (submodules) skipped: a
    submodule is a commit, not a blob, so `:<path>` cannot read it as content.
    """
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "--stage", "-z"],
        capture_output=True, check=True,
    ).stdout
    files: list[str] = []
    gitlinks = 0
    for entry in out.split(b"\x00"):
        if not entry:
            continue
        meta, _, raw_path = entry.partition(b"\t")
        parts = meta.split()
        if len(parts) >= 3 and parts[0] == b"160000":
            gitlinks += 1
            continue
        files.append(raw_path.decode("utf-8"))
    return files, gitlinks


def index_blobs(paths: list[str]) -> dict[str, bytes | None]:
    """One `git cat-file --batch` for the whole corpus; `:<path>` reads the index.

    Measured: 1835 paths come back in ~0.4s, so there is no reason to read the
    working tree as a proxy for what is committed.
    """
    stdin = b"".join(b":" + p.encode("utf-8") + b"\n" for p in paths)
    out = subprocess.run(
        ["git", "-C", str(REPO), "cat-file", "--batch"],
        input=stdin, capture_output=True, check=True,
    ).stdout
    blobs: dict[str, bytes | None] = {}
    i = 0
    for p in paths:
        nl = out.index(b"\n", i)
        header = out[i:nl].split()
        i = nl + 1
        if len(header) != 3 or header[1] != b"blob":
            # `<sha> missing` or something we do not understand: record it so the
            # caller reports it instead of silently treating the file as clean.
            blobs[p] = None
            continue
        size = int(header[2])
        blobs[p] = out[i:i + size]
        i += size + 1  # the batch stream appends one newline after each body
    return blobs


def endings(data: bytes) -> tuple[int, int]:
    """(CRLF, lone LF).  A file is mixed iff both are non-zero."""
    crlf = data.count(b"\r\n")
    return crlf, data.count(b"\n") - crlf


def is_binary(data: bytes) -> bool:
    """Self-contained and testable -- independent of git's own text detection."""
    return b"\x00" in data


def main() -> int:
    paths, gitlinks = tracked_files()
    blobs = index_blobs(paths)

    problems: list[str] = []
    scanned = 0
    binary = 0

    for p in paths:
        blob = blobs.get(p)
        if blob is None:
            problems.append(f"{p}: could not read the index blob")
            continue
        if is_binary(blob):
            binary += 1
            continue

        scanned += 1
        crlf, lone = endings(blob)
        if crlf and lone:
            problems.append(
                f"{p}: index blob mixes line endings ({crlf} CRLF, {lone} lone LF)")

        disk = REPO / p
        if not disk.is_file():
            problems.append(f"{p}: in the index but missing from the working tree")
            continue
        data = disk.read_bytes()
        if is_binary(data):
            continue
        crlf, lone = endings(data)
        if crlf and lone:
            problems.append(
                f"{p}: working tree mixes line endings ({crlf} CRLF, {lone} lone LF)")

    # --- J1: a scan that saw nothing is not a pass -------------------------
    if scanned < MIN_FILES:
        problems.append(f"scanned {scanned} tracked file(s) (expected >= {MIN_FILES})")
    if binary < MIN_BINARY:
        problems.append("no binary file was skipped -- the classifier is broken")

    for line in problems:
        print(f"FAIL: {line}")
    if problems:
        print(f"line endings check failed: {len(problems)} problem(s)")
        return 1

    print(f"tracked file(s): {len(paths)}, text scanned: {scanned}, "
          f"binary skipped: {binary}, gitlink(s) skipped: {gitlinks}")
    print("line endings check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
