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
