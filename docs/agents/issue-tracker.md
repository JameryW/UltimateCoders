# Issue Tracker

The preferred tracker for UltimateCoders is GitHub Issues in `JameryW/UltimateCoders`. It is the source of truth for feature specifications, implementation tickets, acceptance criteria, and blocking dependencies.

## Choose the active mode

Before reading or publishing a tracker artifact, run this read-only preflight:

```powershell
gh auth status -h github.com
```

* **GitHub mode:** select it only when the command confirms an authenticated account. Use `gh` from this repository so it resolves the remote automatically.
* **Local mode:** use it when `gh` is unavailable or unauthenticated. Keep the same artifacts under `.scratch/<feature-slug>/` until GitHub access is restored.

The fallback is deliberate: failed authentication must not block planning or cause a silent remote mutation.

## GitHub mode

* Publish a specification as one parent issue.
* Publish implementation work as one issue per tracer-bullet ticket. Create blockers before dependants and use GitHub's native dependency relationship when it is available; otherwise add a `## Blocked by` section to the ticket body.
* Use the configured labels from `triage-labels.md`. If a configured label does not exist, show the missing label and ask before creating it or publishing without it.
* Reading, listing, and drafting are safe discovery actions. Creating, editing, assigning, labelling, linking dependencies, commenting, or closing issues requires the user's current request to authorize that remote change.

## Local mode

* Store the feature specification at `.scratch/<feature-slug>/spec.md`.
* Store one implementation ticket per file at `.scratch/<feature-slug>/issues/<NN>-<slug>.md`.
* Each ticket contains `What to build`, `Acceptance criteria`, `Blocked by`, and `Status`. A ticket is available only when every listed blocker is resolved.
* A local tracker is version-controlled project work, not a private scratch note. It can be migrated to GitHub after authentication is restored.

## Wayfinding

For an effort larger than one planning context, use `/wayfinder`. GitHub mode uses a map issue with decision tickets; local mode uses `.scratch/<effort>/map.md` with one child file per decision. Decision tickets clarify the route; they are not implementation tickets and must not start a Trellis coding task.
