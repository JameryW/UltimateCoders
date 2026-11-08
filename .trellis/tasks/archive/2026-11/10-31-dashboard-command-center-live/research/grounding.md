# Dashboard command-center grounding

## Visual contract (from user mock)

- Dark near-black canvas, monospace, thin neon borders
- Role colors: orange=lead, green=dispatch, blue=swarm, purple=advisor/health
- Layout: left advisor rail · center vertical flow with swarm row · bottom session log + status pills
- Dynamic: percent bars, ◐ running / ✓ done, live counters

## UC data → UI mapping

| Mock region | UC source |
|-------------|-----------|
| Advisor rail | `health.components`, `metrics`, latest `eventLog` lines as “advice” |
| Lead card | Primary running task (`in_progress`/`running`/`planning`) or selected task |
| Dispatch / fork | `pending_task_count`, worker online ratio, avg `load_percent` |
| Swarm cards | Up to 3 running subtasks (`percent`/`phase`/`step_status`) else workers |
| Review card | Selected/active task terminal status or “awaiting verify” |
| Session log | `eventLog` newest-first timestamps |
| Status pills | grpc/dash state, workers online/total, running tasks, event buffer |

## Touch points

- `dashboard/src/components/terminal/TerminalDashboard.tsx` — OverviewView, ClusterRail → CommandCenterOverview + AdvisorRail; default view `overview`; footer pills
- `dashboard/src/index.css` — new CSS variables (`--cc-lead`, `--cc-swarm`, `--cc-dispatch`, `--cc-advisor`) and command-center layout rules
- Keep `TasksPanel` / other views; inherit tokens where cheap

## Animation

- CSS transition on bar width (`transition: width 400ms ease`)
- Live pulse on `.terminal-live-dot.online` (already present; reuse)
- Optional subtle `@keyframes` for running ◐ spin
