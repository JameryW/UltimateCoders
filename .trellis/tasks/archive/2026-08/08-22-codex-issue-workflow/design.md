# Design: Codex Issue Workflow

## Boundary

The integration is a project-local layer. It does not modify Trellis internals or the distributed UltimateCoders runtime.

```text
Idea / existing issue
  -> Matt skills: discovery, prototype, spec, dependency-aware tickets
  -> tracker: GitHub when authenticated; local Markdown otherwise
  -> one selected unblocked ticket
  -> new Trellis task + fresh Codex context
  -> TDD -> implementation -> Trellis check + code review -> existing commit gate
```

## Sources of Truth

| Concern | Owner | Artifact |
|---|---|---|
| Discovery decisions and domain vocabulary | Matt skills | `CONTEXT.md` / `docs/adr/` when needed |
| Feature scope, acceptance criteria, blockers | Tracker | GitHub issue or `.scratch/<feature>/issues/<NN>-<slug>.md` |
| Implementation session and project-specific context | Trellis | `.trellis/tasks/<task>/` |
| Coding and quality evidence | Trellis task plus repository tests | task PRD, test output, code-review result |

## Reusable Surfaces

* `.agents/skills/` holds vendored upstream workflow skills and `ultimatecoders-issue-flow`.
* `docs/agents/issue-tracker.md`, `domain.md`, and `triage-labels.md` provide the durable configuration that upstream Matt skills read.
* `docs/workflows/codex-issue-flow.md` is the human-readable flow and quick start.
* `scripts/check-codex-issue-flow.py` verifies local wiring without network access.
* `AGENTS.md` carries only a narrow pointer to the entry skill, preserving the workflow itself as a single source of truth.

## Tracker Mode

The flow runs a non-mutating preflight. A working authenticated `gh` session selects GitHub mode; a missing CLI or failed authentication selects local Markdown mode. GitHub mode never creates labels as a side effect of setup; any missing configured label becomes an explicit approval point during a requested ticket publication.

## Execution Handoff

For an implementation ticket, the entry skill creates a Trellis task whose PRD links the source ticket and copies only its title, acceptance criteria, testing seam, and relevant design decisions. It does not duplicate upstream ticket dependencies. A new Codex context must be used for that Trellis task.

The entry skill treats the upstream `implement` skill as a quality sequence. Trellis remains responsible for task state, check agent dispatch, and the final user-confirmed commit. This prevents two lifecycle owners from committing or closing work independently.

## Validation

The validator checks expected skill folders and their declared names, required configuration files, the entry-skill references, documentation links, and the `AGENTS.md` pointer. It deliberately does not call GitHub or modify tracker state.

## Rollback

Removing the project-local entry skill and documents restores the existing Trellis-only behavior. Vendored upstream skills are additive and can be removed as a single directory group.
