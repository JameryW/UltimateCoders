# Dashboard command-center live status redesign

## Goal

Restyle the operations dashboard overview into a dark, monospaced “command center” inspired by the Claude Code subagent tip mock: color-coded role panels, live progress bars, hierarchical flow (lead → dispatch → swarm → review), scrolling session log, and a bottom status pill bar — all driven by **real** UC runtime state (tasks, workers, subtask progress, events, health), not fake agent personas.

## Source

- User visual reference: command-center TUI (dark bg, neon role borders, progress bars, status pills)
- Existing surface: `dashboard/src/components/terminal/TerminalDashboard.tsx` + `dashboard/src/index.css` terminal section
- Live data: `useDashboard` / gRPC-Web snapshots + `TaskEvent` / `SubtaskSummary.percent|phase|step_*`

## What I already know

* Dashboard already uses a terminal aesthetic (IBM Plex Mono, dark theme, topology overview).
* Real progress exists on subtasks (`percent`, `phase`, `step_status`, worker load).
* Overview today is a vertical topology + vitals; user wants the **live flow + left advisor rail + bottom pill bar** composition.
* Product maps: Gateway/Orchestrator = lead; Scheduler/dispatch = fork layer; Workers/subtasks = swarm; Health/events = advisor + log.

## Assumptions (temporary)

* Scope = **overview command-center** + shared chrome (top legend, bottom status pills); other views keep existing panels but inherit new CSS tokens.
* Default view becomes `overview` so the live board is first paint.
* No Claude/Sonnet/Haiku branding in product chrome — use UC role names (gateway / dispatch / workers / health).
* Light theme remains supported via CSS variables; dark is the designed target matching the mock.

## Decision (ADR-lite)

**Context**: User asked to restyle the dashboard like the command-center mock with dynamic running status.  
**Decision**: **Approach A** — redesign `OverviewView` + left rail + footer status into a live UC command board; reuse existing hooks/props; no new backend APIs.  
**Consequences**: Pure frontend change in `dashboard/`; dynamic bars/status from workers + selected/running task subtasks + event log.

## Requirements

* Header: title + color legend for lead / dispatch / workers / health.
* Left purple-toned advisor/health rail: focus cues from health components + last event “advice” lines + metrics (uptime, event count).
* Center flow (live):
  - Lead card: active/running task description + effort/load meter
  - Dispatch card: pending queue + worker-match style bars (load / queue / online ratio)
  - Swarm row: up to 3 workers or running subtasks with live `%` bars and running/done icons
  - Review card: selected task status / verify cue
* Bottom: timestamped session log from `eventLog` / interaction events; status pills (`workers [n/m]`, `tasks [running]`, `grpc [state]`, …)
* Animate progress updates when SSE/gRPC events change percent/status
* Preserve existing views (tasks/workers/search/logs/inference) and command bar
* Mobile: stack flow; keep pills readable

## Out of scope

* Inventing multi-model agent personas (Opus/Sonnet/Haiku/JEV) as product copy
* New Gateway RPCs
* Redesigning HomePage marketing surface

## Follow-up: Tasks page Command-Center polish

Extend the same visual language onto the **tasks** view (list + detail), still frontend-only.

### Requirements (tasks view)

1. Terminal status/action tokens under `.terminal-task-*` using CC neon (`--cc-*`); define missing `.status-*` / `.btn-action-*` scoped to tasks.
2. Selected-task history head live strip: aggregate subtask progress via `terminal-mini-bar` + first running `phase` / `step_agent`.
3. `InteractionLog` under `.terminal-task-history` uses `.cc-session-row` role colors (task→lead, subtask→swarm, tool→dispatch, llm→advisor); drop emoji timeline or fold into session log.
4. Subtask list as compact swarm rows (MiniBar + running/done markers).
5. List rows: in_progress %/phase chip; selected row neon border glow (not Tailwind Card blue).

### Out of scope

Overview/workers redesign, backend APIs.

## Follow-up: Workers page Command-Center polish

Extend the same visual language onto the **workers** view — frontend-only.

### Requirements (workers view)

1. Shell `.terminal-workers-layout` under terminal-data-view (parallel to tasks).
2. Row neon: online/load→--cc-swarm, offline→red, stale→amber/lead; expanded glow like task selected.
3. Load meter via `terminal-mini-bar` (replace orphaned load-bar*).
4. Active assignment strip as swarm rows with jump-to-task.
5. SSR tests locking CC classes (no Tailwind green/blue rails).

### Out of scope

Overview redesign, backend APIs.

## Follow-up: Logs page Command-Center polish

Extend the same visual language onto the **logs** (`EventLogPanel`) view — frontend-only.

### Requirements (logs view)

1. `variant="terminal"` + shell `.terminal-logs-layout`; drop Card chrome in terminal mode.
2. Rows reuse `sessionRoleForEventType` / `.cc-session-row` role colors; replace dead `evt-*` + Tailwind status pills.
3. Filters/export/Errors as terminal chips (not blue/red Tailwind).
4. Virtualized stream fills `terminal-view-body`; “↓ Latest” as CC control.
5. Empty/stale/header aligned with `cc-session-log-head`; keep onSelectTask → tasks.
6. SSR tests locking CC classes.

### Out of scope

Search/Inference redesign, backend APIs.

## Follow-up: Search page Command-Center polish

Extend the same visual language onto the **search** (`SearchPanel`) view — frontend-only.

### Requirements (search view)

1. `variant="terminal"` + shell `.terminal-search-layout`; no Card in terminal mode.
2. Query / modes / lang / limit as `.terminal-search-chip` controls.
3. Result rows `.terminal-search-row` with match-type neon via `--cc-*` (no Tailwind blue/purple/cyan rails).
4. Surface `score` / `symbolKind` when present; keep `onNavigateFile` jump (`terminal-search-jump`).
5. SSR tests locking CC classes + default Card path unchanged.

### Out of scope

Inference redesign, backend/RPC changes.
