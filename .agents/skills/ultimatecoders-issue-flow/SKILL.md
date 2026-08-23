---
name: ultimatecoders-issue-flow
description: Run UltimateCoders' issue-driven delivery flow when turning an idea or a named ticket into discovery, dependency-aware tracker work, and one isolated Trellis implementation task. Do not use for a small, self-contained edit that needs no tracker record.
---

# UltimateCoders Issue Flow

Use this skill as the project's entry point for a multi-step product change. It composes the installed Matt Pocock skills with the existing Trellis lifecycle; it does not replace either one.

## Load the project contract

Read these before choosing a path:

* `docs/agents/issue-tracker.md` — selects GitHub or local Markdown without silently mutating GitHub.
* `docs/agents/domain.md` — states the project domain-document layout.
* `docs/agents/triage-labels.md` — maps tracker states and labels.
* `.trellis/workflow.md` — owns the implementation task lifecycle and commit gate.

Run `python scripts/check-codex-issue-flow.py` when setup looks incomplete or after changing its wiring.
For the ownership model and parallel-work boundary, read [references/boundaries.md](references/boundaries.md).

## Route the work

### A new or unclear idea

* For a normal feature, invoke `$grill-with-docs` to settle the product and domain decisions. It owns the live, one-question-at-a-time conversation.
* For work too broad to map in one planning context, invoke `$wayfinder` first. Resolve decision tickets before creating implementation tickets.
* Invoke `$prototype` only when a logic or interface question needs a disposable artifact to settle it. Record the verdict, not prototype code, in the resulting spec.
* Once decisions are settled, invoke `$to-spec`, then `$to-tickets`. Tickets must be vertical slices with explicit blockers and acceptance criteria.

### An existing specification or implementation ticket

Read the tracker artifact using the active mode. Use `$triage` when an incoming issue still needs categorisation or an agent-ready brief. Do not start coding from a ticket with unresolved product decisions, unmet blockers, or `needs-info` status.

## Hand a ticket to Trellis

For one unblocked implementation ticket:

1. Use a **new Codex context**. If the current context already owns another implementation ticket, stop before coding and ask the user to open a new Codex task.
2. Create one Trellis task for this ticket and use its planning phase to write a compact PRD that links the source ticket. Carry forward only the ticket title, acceptance criteria, agreed test seams, and decisions essential to implementation. The tracker remains the authority for blockers and ticket status.
3. Follow `.trellis/workflow.md` for implementation, checking, and its user-confirmed commit gate. Do not create a second ticket graph inside Trellis.

## Quality sequence

Use `$tdd` at the agreed public seams, one red-green tracer bullet at a time. Run focused checks during the work and the relevant full suite at the end. Before the Trellis final check, invoke `$code-review` against a resolved fixed point so its Standards and Spec axes evaluate the completed diff independently.

The upstream `$implement` skill is a useful quality sequence, but Trellis remains the only owner of task state and commits in this repository. Apply its TDD and review guidance without bypassing Trellis's check or commit gate.

## Tracker safety

GitHub mode is available only after `gh auth status -h github.com` succeeds. Discovery is read-only. Creating or editing issues, labels, assignments, dependency links, comments, or closures requires the user's current request to authorize that remote change. When authentication fails, use the local Markdown tracker described in `docs/agents/issue-tracker.md`.
