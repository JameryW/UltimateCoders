# Codex Issue Flow

This project combines [Matt Pocock's engineering skills](https://github.com/mattpocock/skills) with Trellis. Matt skills own the discovery-to-ticket path; Trellis owns the isolated implementation task, project context, checking, and commit gate.

```text
idea or existing issue
  -> grill-with-docs (or wayfinder for a large unknown)
  -> prototype when a design question needs evidence
  -> to-spec
  -> to-tickets with blocking edges
  -> one unblocked ticket in one new Trellis task
  -> tdd -> implementation -> Trellis check + code-review -> Trellis commit gate
```

## Start

In Codex, invoke:

```text
$ultimatecoders-issue-flow <idea, issue URL, or ticket path>
```

The entry skill selects GitHub mode only when `gh auth status -h github.com` succeeds. Otherwise it uses the local Markdown tracker described in [issue-tracker.md](../agents/issue-tracker.md). Nothing in the setup creates or changes remote GitHub state.

## Work a ticket

Invoke the entry skill with the GitHub issue URL/number or local ticket path. Work only an unblocked implementation ticket. Start it in a new Codex context and let the skill create a matching Trellis task; its PRD links the ticket and retains only the ticket's acceptance criteria, agreed testing seams, and essential design decisions.

Trellis does not create a second dependency graph. The tracker remains the authority for ticket status and blockers. If another ticket already owns the current Codex/Trellis context, open a new Codex task before coding.

## Quality and completion

Use `$tdd` at the agreed public seams, run the relevant project checks, and run `$code-review` against the fixed point before the Trellis final check. The existing Trellis lifecycle remains the only route for task state and the user-confirmed commit. Updating or closing a GitHub ticket is a separate, explicitly requested remote action.

## Large initiatives

Use `$wayfinder` when the work is too uncertain or broad for a single planning context. Resolve its decision tickets first. Once the route is clear, turn the approved result into a spec and implementation tickets; do not treat the decision map itself as a coding backlog.
