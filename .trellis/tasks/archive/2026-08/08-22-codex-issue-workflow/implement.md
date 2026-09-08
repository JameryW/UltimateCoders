# Implementation Plan: Codex Issue Workflow

## Order

1. Install the selected upstream Matt Pocock workflow skills into `.agents/skills/` using the approved skill installer; verify names and supporting files.
2. Add tracker, label, and domain configuration under `docs/agents/` with GitHub-preferred and local-Markdown fallback semantics.
3. Create the project-local `ultimatecoders-issue-flow` skill and a concise `AGENTS.md` context pointer.
4. Add the human-readable workflow guide and offline validator.
5. Run the validator and inspect the installed skills' frontmatter and cross-references.
6. Run repository documentation/style checks that apply to the changed files.

## Required Upstream Skills

`setup-matt-pocock-skills`, `grill-with-docs`, `grilling`, `domain-modeling`, `codebase-design`, `prototype`, `to-spec`, `to-tickets`, `wayfinder`, `implement`, `tdd`, and `code-review`.

## Validation

```powershell
python scripts/check-codex-issue-flow.py
git diff --check
```

## Risk Controls

* Do not use `gh` to create or change labels, issues, or dependency links in this task.
* Keep vendor installation scoped to the repository's `.agents/skills/` directory.
* Preserve existing Trellis workflow hooks and lifecycle files; the integration is additive.
