# Workspace Index

> Records of all AI Agent work records across all developers

---

## Overview

This directory tracks records for all developers working with AI Agents on this project.

### File Structure

```
workspace/
|-- index.md              # This file - main index
+-- {developer}/          # Per-developer directory
    |-- index.md          # Personal index with session history
    |-- tasks/         # Task files
    |   |-- *.json        # Active tasks
    |   +-- archive/      # Archived tasks by month
    +-- journal-N.md     # Journal files (sequential: 1, 2, 3...)
```

---

## Active Developers

| Developer | Last Active | Sessions | Active File |
|-----------|-------------|----------|-------------|
| `Jamery Wang` | 2026-09-18 | 33 | `journal-2.md` |
| `JameryW` (retired identity, frozen) | 2026-08-06 | 123 | `journal-3.md` |

Session counts and dates are measured from the journals' `## Session` headings
and `**Date**` lines.
`JameryW/` belongs to a **retired `.developer` identity** -- `get_developer()`
returns `Jamery Wang`, so no script resolves that directory any more, and its
363 skeleton placeholders are **frozen** by `scripts/check-journal-ledger.py`
(`LEGACY_JOURNALS`) rather than backfilled. See
`.trellis/workspace/JameryW/index.md` for its own session index.

---

## Getting Started

### For New Developers

Run the initialization script:

```bash
python3 ./.trellis/scripts/init_developer.py <your-name>
```

This will:
1. Create your identity file (gitignored)
2. Create your progress directory
3. Create your personal index
4. Create initial journal file

### For Returning Developers

1. Get your developer name:
   ```bash
   python3 ./.trellis/scripts/get_developer.py
   ```

2. Read your personal index:
   ```bash
   cat .trellis/workspace/$(python3 ./.trellis/scripts/get_developer.py)/index.md
   ```

---

## Guidelines

### Journal File Rules

- **Max 2000 lines** per journal file
- When limit is reached, create `journal-{N+1}.md`
- Update your personal `index.md` when creating new files

### Session Record Format

Each session should include:
- Summary: One-line description
- Branch: Which branch the work was done on
- Main Changes: What was modified
- Git Commits: Commit hashes and messages
- Next Steps: What to do next

---

## Session Template

Use this template when recording sessions:

```markdown
## Session {N}: {Title}

**Date**: YYYY-MM-DD
**Task**: {task-name}
**Branch**: `{branch-name}`

### Summary

{One-line summary}

### Main Changes

- {Change 1}
- {Change 2}

### Git Commits

| Hash | Message |
|------|---------|
| `abc1234` | {commit message} |

### Testing

- [OK] {Test result}

### Status

[OK] **Completed** / # **In Progress** / [P] **Blocked**

### Next Steps

- {Next step 1}
- {Next step 2}
```

---

**Language**: All documentation must be written in **English**.
