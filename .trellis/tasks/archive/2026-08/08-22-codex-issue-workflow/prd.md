# Codex issue workflow

## Goal

Create a durable, issue-driven Codex workflow based on the supplied reference: align on a problem, optionally prototype it, publish a specification, split it into dependency-aware tickets, and implement one ticket at a time in isolated contexts with TDD and code review.

## Confirmed Facts

* The requested reference flow uses `mattpocock/skills` as its engineering workflow layer.
* This repository already has Trellis task lifecycle, project-local Codex hooks, implement/check/research agent definitions, and shared skills under `.agents/skills/`.
* The repository remote is intended to use GitHub, but the local GitHub CLI authentication is currently invalid. No remote tracker configuration can be changed until it is re-authenticated.
* The relevant upstream skills are `setup-matt-pocock-skills`, `grill-with-docs`, `prototype`, `to-spec`, `to-tickets`, `implement`, `tdd`, and `code-review`.

## Requirements

* Vendor the required Matt Pocock skills into the repository's `.agents/skills/` directory so a fresh Codex checkout discovers the full workflow without relying on a developer's global skill folder.
* Create a project-local `ultimatecoders-issue-flow` skill as the single Codex entry point. It must route planning work to the Matt skills and route implementation work to a new Trellis task.
* Add concise repository configuration for the issue tracker, label vocabulary, and domain-document locations that the upstream skills expect.
* Prefer GitHub Issues when the GitHub CLI is authenticated; otherwise use the upstream local Markdown ticket convention. The fallback must be deterministic and must not create or change remote GitHub state.
* Keep one implementation ticket in one Trellis task and one fresh Codex context. Parallel work is permitted only for tickets with no unmet blockers and no overlapping file scope.
* Use TDD at agreed seams, project checks, and a final two-axis review. Trellis remains the only lifecycle and commit owner; the upstream `implement` skill supplies the quality sequence but does not create a second task or commit automatically.
* Provide a repository-local validation command that confirms the installed skills, configuration, entry point, and cross-links are present.

## Acceptance Criteria

* [ ] A fresh checkout exposes the required workflow skills in `.agents/skills/` and exposes `$ultimatecoders-issue-flow` as the project entry point.
* [ ] A contributor can follow the documented six-stage path from idea to reviewed ticket completion without needing undocumented global setup.
* [ ] `docs/agents/` identifies the GitHub-preferred tracker, the local fallback, the label vocabulary, and the project domain-document locations.
* [ ] The flow records a spec and ticket dependencies in the active tracker; tickets include their acceptance criteria and blockers.
* [ ] Invoking implementation for a ticket creates a new Trellis task that references the ticket rather than duplicating its dependency graph.
* [ ] The implementation route uses TDD, project validation, and code review without conflicting with Trellis's check and commit gates.
* [ ] The local validator passes without GitHub credentials; GitHub mutations require an authenticated CLI and an explicit user request.

## Decision (ADR-lite)

**Context:** The supplied workflow uses an issue tracker and Matt Pocock skills, while this repository already uses Trellis to persist tasks, inject project context, and check implementation work.

**Decision:** Use a two-layer workflow. Matt skills own product discovery, specs, and dependency-aware tickets. Trellis owns the execution task, worker/check orchestration, and commit gate. GitHub is preferred when available; local Markdown is the no-credential fallback.

**Consequences:** The integration adds one project-local routing skill and a small set of configuration documents. A GitHub issue becomes the source of truth for scope and blockers; a Trellis task is the source of truth for work performed in its isolated coding context.

## Out of Scope

* Editing or creating GitHub labels, issues, or dependency links during the initial repository setup. Those actions occur only during a later, explicitly requested GitHub-backed workflow run.
* Replacing the existing Trellis task engine or distributed worker runtime.
* Changing the global Codex skill directory; all additions must be project-local and version-controlled.

## Technical Notes

* Trellis workflow source: `.trellis/workflow.md`.
* Codex integration: `.codex/hooks.json`, `.codex/agents/*.toml`, and shared `.agents/skills/`.
* The current GitHub CLI credential is invalid; `gh auth refresh -h github.com` is required before a GitHub-backed run.
* Upstream references: https://github.com/mattpocock/skills
