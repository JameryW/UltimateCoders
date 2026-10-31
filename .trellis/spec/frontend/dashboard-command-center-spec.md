# Dashboard Command-Center Spec

> Executable contracts for the React operations dashboard “command center” overview —
> live UC runtime flow (lead → dispatch → swarm → review), advisor rail, session log,
> status pills, offline SAMPLE preview, and first-visit theme preference.

---

## Scenario: Live command-center overview

### 1. Scope / Trigger

- Trigger: Browser opens `/dashboard` (or `#/dashboard`) and `TerminalDashboard` mounts with default view `overview`.
- Cross-layer: Gateway/Dashboard gRPC-Web + SSE snapshots → `useDashboard` props → `OverviewView` / `AdvisorPanel` / `StatusPillBar`.
- Pure frontend; no new Gateway RPCs.

### 2. Signatures

```typescript
// dashboard/src/components/terminal/TerminalDashboard.tsx

export type CommandCenterStage = "idle" | "lead" | "dispatch" | "swarm" | "review";

export function pickAutofocusTaskId(
  tasks: Pick<TasksData, "tasks">,
  selectedTaskId: string | null | undefined,
): string | null;

export function shouldApplyCommandCenterDefaultDark(
  theme: Theme,
  readStored?: (key: string) => string | null,
): boolean;

export function shouldUseCommandCenterPreview(input: {
  connected: boolean;
  tasks: Pick<TasksData, "tasks" | "total">;
  workers: Pick<WorkersData, "workers" | "total">;
}): boolean;

export function deriveActiveStage(input: {
  connected: boolean;
  lead: TaskSummary | null;
  selectedTask: TaskSummary | null;
  pending: number;
  swarm: Array<{ running: boolean }>;
}): CommandCenterStage;

export function mergeSessionLogEvents(
  eventLog: DashboardEvent[],
  interactionEvents: TaskEvent[],
  limit?: number,
): DashboardEvent[];

export function sessionLogEventKey(event: DashboardEvent): string;

export function changedPillKeys(
  prev: StatusPillValues | null,
  next: StatusPillValues,
): Array<keyof StatusPillValues>;

export function isCommandCenterConnectedIdle(input: {
  connected: boolean;
  previewMode: boolean;
  stage: CommandCenterStage;
}): boolean;

export function formatElapsed(
  fromIso: string | null | undefined,
  nowMs: number,
): string;

export function viewForCommandCenterStage(
  stage: CommandCenterStage,
  hasRunningSubtasks?: boolean,
): "tasks" | "workers" | null;

export const COMMAND_CENTER_IDLE_TIP = "» submit a task to see live flow";
export const COMMAND_CENTER_LIVE_HANDOFF = "live · gateway connected";
```

### 3. Contracts

#### Preview gate

| Condition | Result |
|-----------|--------|
| `connected === true` | Preview **off** (live board) |
| Offline + any task/worker row or non-zero `total` | Preview **off** |
| Offline + empty tasks + empty workers + zero totals | Preview **on** (`data-cc-preview="sample"`, SAMPLE banner) |

Preview board must be unmistakably labeled SAMPLE / “not live data”. No Claude/Sonnet/Haiku/JEV product copy.

#### Stage derivation order (furthest-advanced wins)

1. Focus task in terminal status → `review`
2. Any running swarm card / running subtask → `swarm`
3. `pending > 0` **or** active lead in progress without running swarm → `dispatch`
4. Active lead otherwise → `lead`
5. Else → `idle` (including disconnected empty)

Active connectors (`cc-flow-active`) light only for edges **above** the active stage. Idle connectors use `cc-flow-idle`.

#### Autofocus

- When overview is mounted/updated and `selectedTaskId` is null/undefined, `pickAutofocusTaskId` returns the first task with status in `{in_progress, running, planning, assigned}`.
- Calling `onSelectTask(id)` must happen only when that helper returns a non-null id.
- If the user already selected a task (any id, including completed), autofocus returns null — never override.

#### Session log merge

- Merge `eventLog` + selected/lead `interactionLog` TaskEvents → DashboardEvent shape.
- Sort newest-first; dedupe by `sessionLogEventKey` (`timestamp|type|task_id|subtask_id`).
- Keys must be identity-stable (no list index) so enter animations survive prepends.
- Cap default 8 rows on the overview feed.

#### Status pill flash

- `changedPillKeys(null, next)` → `[]` (no flash on first paint).
- Subsequent diffs flash only changed keys via `cc-pill-flash` (~600ms).

#### First-visit dark theme

- Storage key: `uc_dashboard_theme`.
- `shouldApplyCommandCenterDefaultDark` is true **only** when the key is absent **and** current theme is `light`.
- If the key is set to `light` or `dark`, never auto-change.
- On apply, prefer `onSetTheme("dark")` (writes the key) over toggle.

#### Connected idle tip

- `isCommandCenterConnectedIdle` → show `COMMAND_CENTER_IDLE_TIP` on lead card + empty session log.
- Must **not** appear during SAMPLE preview.

#### Stage chip navigation

- Stage chip is a `<button>` (`data-testid="cc-stage-chip"`) with keyboard activation.
- `viewForCommandCenterStage` mapping:
  - `lead` / `review` → `tasks`
  - `dispatch` → `workers`
  - `swarm` → `tasks` when lead has running subtasks, else `workers`
  - `idle` → `null` (button disabled; no navigation)
- Existing card `onClick` behaviors stay unchanged; chip is an additional jump target.

#### Lead elapsed time

- Lead card meta shows `elapsed HH:MM:SS` via `formatElapsed(lead.updated_at || lead.created_at, nowMs)`.
- `formatElapsed`: missing/invalid ISO → `—`; negative delta clamps to `00:00:00`.
- Overview mounts a 1s clock while the view is mounted so active-task elapsed advances live.
- No lead → display `elapsed —`.

#### Preview → live handoff

- When `previewMode` flips `true → false` (gateway/data arrived), show `cc-live-banner` (`data-testid="cc-live-banner"`) with `COMMAND_CENTER_LIVE_HANDOFF` for ~2s.
- Must never appear alongside the SAMPLE preview banner.
- Reduced motion: keep the notice visible; skip enter animation only.

#### Reduced motion

- `@media (prefers-reduced-motion: reduce)` disables stage glow, connector particle/line animation, swarm enter, session-row enter, pill flash, live-handoff enter, and running ◐ pulse.

#### Role mapping (product chrome)

| Region | UC role | Color token |
|--------|---------|-------------|
| Lead / review | gateway · lead | `--cc-lead` |
| Dispatch | dispatch · queue | `--cc-dispatch` |
| Swarm | workers · swarm | `--cc-swarm` |
| Advisor / health | health · advisor | `--cc-advisor` |

### 4. Validation & Error Matrix

| Condition | Behavior |
|-----------|----------|
| Overview + null selection + active task | Autofocus selects that task once |
| Overview + explicit selection | No autofocus call |
| Offline empty cluster | SAMPLE preview board + banner |
| Preview → connected | SAMPLE off; live handoff banner ~2s |
| Connected empty cluster | Live idle tip, no SAMPLE |
| Stage chip click (swarm + running subs) | Navigate to tasks |
| Stage chip click (dispatch) | Navigate to workers |
| Theme key absent + light | One-shot set dark |
| Theme key `light` | Stay light |
| `prefers-reduced-motion` | Animations off; static particle position; live banner static |

### 5. Good / Base / Bad Cases

**Good**: Connected cluster with an `in_progress` task and running subtasks → stage `swarm`, autofocus selects lead, session log merges tool_call rows, pills flash on worker count change.

**Base**: Connected, no tasks → stage `idle`, idle tip visible, preview off.

**Bad**: Inventing Opus/Sonnet personas; forcing dark over stored light; treating residual offline `tasks.total > 0` as SAMPLE; overriding user `selectedTaskId`.

### 6. Tests Required

| Test | Assertion |
|------|-----------|
| `pickAutofocusTaskId` | Active when null selection; null when selected |
| Stage chip in DOM | `data-testid="cc-stage-chip"` + `data-stage`; chip is a button |
| `viewForCommandCenterStage` | lead/review→tasks; dispatch→workers; swarm→tasks|workers; idle→null |
| `formatElapsed` | valid ISO → HH:MM:SS; missing/invalid → `—` |
| Lead meta elapsed | `data-testid="cc-lead-elapsed"` present when lead exists |
| Live handoff | source + CSS `cc-live-banner`; reduced-motion skips animation |
| `deriveActiveStage` march | lead → dispatch → swarm → review → idle |
| Preview gate | offline empty on; connected/live data off |
| Session merge + pill keys | newest-first, dedupe, stable keys, flash diffs |
| CSS reduced-motion | stage-active / connector em animation none |
| Source/DOM | no Opus/Sonnet/Haiku/JEV branding |

### 7. Wrong vs Correct

#### Wrong: Override user selection on overview remount

```typescript
// BAD: always re-select lead even when user picked another task
onSelectTask(pickLeadTask(tasks, null)?.id ?? null);
```

#### Correct: Autofocus only when selection is empty

```typescript
const id = pickAutofocusTaskId(tasks, selectedTaskId);
if (id) onSelectTask(id);
```

#### Wrong: Force dark every mount

```typescript
// BAD: fights explicit light preference
useEffect(() => { onSetTheme("dark"); }, []);
```

#### Correct: First visit only

```typescript
if (shouldApplyCommandCenterDefaultDark(theme)) onSetTheme("dark");
```

#### Wrong: Index-suffixed session keys

```typescript
key={`${event.timestamp}-${index}`}  // shifts on prepend → remount flicker
```

#### Correct: Identity key

```typescript
key={sessionLogEventKey(event)}
```

---

## Scenario: Tasks view command-center polish

### 1. Scope / Trigger

- Trigger: User opens the **tasks** tab in `TerminalDashboard` (list + selected-task history).
- Pure frontend; reuses overview CC tokens (`--cc-lead|dispatch|swarm|advisor`); no new Gateway RPCs.
- Out of scope: overview/workers redesign; Opus/Sonnet/Haiku/JEV branding.

### 2. Signatures

```typescript
// dashboard/src/lib/taskProgress.ts
export function aggregateSubtaskProgress(subtasks): AggregateSubtaskProgress;
export function taskListProgressChip(task): { percent: number; phase: string } | null;
export function sessionRoleForEventType(type: string): SessionEventRole;
export function isSubtaskDone(subtask): boolean;
export function isSubtaskRunning(subtask): boolean;

// Components
TasksPanel — rows use `terminal-task-row` + `is-selected`; in_progress chip via `taskListProgressChip`
TaskDetail({ variant?: "default" | "terminal" })
InteractionLog({ variant?: "default" | "terminal" })
TerminalDashboard tasks history — `TaskHistoryLiveStrip` + `TaskDetail variant="terminal"`
```

### 3. Contracts

#### Status / action tokens

Under `.terminal-data-view` / `.terminal-task-layout` / `.terminal-task-history`:

| Class | Token |
|-------|-------|
| `.status-in_progress` / `.status-assigned` | `--cc-swarm` |
| `.status-completed` | `--cc-dispatch` |
| `.status-failed` | `--terminal-red` |
| `.status-paused` | `--cc-lead` |
| `.status-default` | `--terminal-dim` |
| `.btn-action-info` | swarm |
| `.btn-action-ok` | dispatch |
| `.btn-action-warn` | lead |
| `.btn-action-error` | red |

#### Selected-task live strip

- When selected task has subtasks, history panel shows `data-testid="terminal-task-live-strip"`:
  - `completed/total` + `terminal-mini-bar` at aggregate percent
  - Aggregate percent = mean of reported subtask `percent` values, else `completed/total * 100`
  - First running `phase` / `step_agent` in `data-testid="terminal-task-live-phase"` when present

#### InteractionLog (terminal variant)

- Rows use `.cc-session-row` + `.cc-session-role-{lead|swarm|dispatch|advisor}`
- Role map: `task_*` → lead, `subtask_*` → swarm, `tool_*` → dispatch, `llm_*` → advisor
- No emoji timeline icons on the terminal path; keep virtualization
- `TaskDetail variant="terminal"` omits `EventTimeline` (emoji folded into session log)

#### Subtask swarm strip

- Terminal TaskDetail renders `.terminal-swarm-row` with MiniBar + `◐ running` / `✓ done` / `◇ idle`, percent, phase, step_agent, assigned_worker

#### List row affordance

- `in_progress` / `running` rows show `.terminal-task-progress-chip` (`%` + phase when known)
- Selected row: `.terminal-task-row.is-selected` neon lead/swarm glow — **not** Tailwind `border-l-blue-500`

#### Reduced motion

- `.terminal-swarm-marker.running` pulse disabled under `prefers-reduced-motion: reduce`

### 4. Validation & Error Matrix

| Condition | Behavior |
|-----------|----------|
| Selected task, no subtasks | No live strip |
| Selected task, running subtasks | Strip + phase/agent when set |
| Terminal TaskDetail | Swarm rows; no emoji Timeline |
| Default TaskDetail | Legacy list + EventTimeline unchanged |
| Selected list row | `is-selected` class; no Card blue border |

### 5. Good / Base / Bad Cases

**Good**: in_progress task with 2/3 subtasks, one running at 42% phase `executing` → list chip, live strip, swarm ◐, session log role colors.

**Base**: completed task with all done subtasks → strip 100%, ✓ done markers, no progress chip.

**Bad**: Tailwind blue selected border; emoji tool/subtask timeline in terminal history; inventing model personas.

### 6. Tests Required

| Test | Assertion |
|------|-----------|
| List row selected | `terminal-task-row is-selected`; progress chip for in_progress |
| Live strip | MiniBar width + phase/agent text |
| InteractionLog terminal | `cc-session-row` + role class; no timeline emoji |
| Swarm rows | `◐ running` / `✓ done` + percent |
| CSS | status/btn-action tokens; reduced-motion for swarm marker |
| Helpers | `aggregateSubtaskProgress` / `sessionRoleForEventType` |

### 7. Wrong vs Correct

#### Wrong: Tailwind Card blue for selection

```tsx
className={selected && "border-l-blue-500"}
```

#### Correct: neon selected class

```tsx
className={cn("terminal-task-row", isSelected && "is-selected")}
```

#### Wrong: Emoji timeline in terminal history

```tsx
<EventTimeline events={interactionLog} />  // always
```

#### Correct: Session log only in terminal variant

```tsx
{!terminal && <EventTimeline events={interactionLog} />}
<InteractionLog variant={terminal ? "terminal" : "default"} ... />
```

---

## Scenario: Workers view command-center polish

### 1. Scope / Trigger

- Trigger: User opens the **workers** tab in `TerminalDashboard`.
- Pure frontend; reuses overview CC tokens (`--cc-lead|dispatch|swarm|advisor`); no new Gateway RPCs.
- Out of scope: overview redesign; Opus/Sonnet/Haiku/JEV branding.

### 2. Signatures

```typescript
// dashboard/src/components/panels/WorkersPanel.tsx
export type WorkerRowNeon = "online" | "loaded" | "offline" | "stale";
export function workerRowNeon(worker): WorkerRowNeon;
export function workerLoadTone(percent: number): "green" | "blue" | "amber" | "orange" | "purple" | "red";

WorkersPanel({
  workers, tasks, stale?, onJumpTask?, embedded?,
  variant?: "default" | "terminal",
})

// TerminalDashboard workers shell
<div className="terminal-data-view terminal-workers-layout">
  <WorkersPanel variant="terminal" onJumpTask={…} … />
</div>
```

### 3. Contracts

#### Shell

- Workers view wraps the panel in `.terminal-data-view.terminal-workers-layout` (parallel to `.terminal-task-layout`).
- `onJumpTask(parentTaskId)` still navigates to the tasks view.

#### Row neon

Under `.terminal-workers-layout` / `variant="terminal"`:

| State | Class | Token |
|-------|-------|-------|
| available + `load_percent > 0` | `.terminal-worker-row.is-loaded` | `--cc-swarm` |
| available + idle load | `.terminal-worker-row.is-online` | `--cc-swarm` |
| `!is_available` | `.terminal-worker-row.is-offline` | `--terminal-red` |
| `heartbeat_stale` (and available) | `.terminal-worker-row.is-stale` | `--cc-lead` |
| expanded | `.is-expanded` | lead/swarm glow (same idea as `.terminal-task-row.is-selected`) |

- Terminal path must **not** use Tailwind `border-l-green-500` / `border-l-blue-500` / cyan Card rails.
- Default variant may keep legacy Tailwind rails for non-CC surfaces.

#### Load meter

- Terminal rows use `.terminal-mini-bar` (testid `terminal-worker-load`) — **not** orphaned `load-bar` / `load-bar-fill`.
- Tone via `workerLoadTone`: `>=100` → red, `>=75` → orange (lead), `>=40` → blue (swarm), else green (dispatch).
- Monospace `current/max` + `%` beside the bar.

#### Assignment strip

- Join `tasks.tasks[].subtasks` where `assigned_worker === worker.id`.
- Render as `.terminal-swarm-row` under `.terminal-worker-assignments` with `◐ running` / `✓ done` / `◇ idle`, percent, phase, `step_agent`.
- Click / keyboard activates `onJumpTask(parentTaskId)` (map via subtask→task).

#### Capabilities

- Terminal chips use `.terminal-worker-cap` (monospace, dispatch-toned) — not generic surface pills alone.

#### Reduced motion

- `.terminal-worker-row` transition disabled under `prefers-reduced-motion: reduce` (swarm marker pulse already covered).

### 4. Validation & Error Matrix

| Condition | Behavior |
|-----------|----------|
| Online worker, load 0 | `is-online` swarm rail |
| Online worker, load > 0 | `is-loaded` swarm rail + load bar |
| Offline worker | `is-offline` red rail |
| Stale heartbeat (available) | `is-stale` lead rail |
| Expanded row | `is-expanded` glow |
| Assigned subtasks present | swarm rows + jump-to-task |
| No assignments | no assignments strip |
| Default variant | legacy green/red Tailwind rails allowed |

### 5. Good / Base / Bad Cases

**Good**: Online worker at 55% load with one `in_progress` assigned subtask at 42% phase `executing` → swarm neon, mini-bar, assignment `◐ running`, click jumps to parent task.

**Base**: Empty workers list → empty state; no neon rows.

**Bad**: Tailwind `border-l-green-500` on terminal path; orphaned `load-bar*` classes in terminal mode; inventing model personas.

### 6. Tests Required

| Test | Assertion |
|------|-----------|
| `workerRowNeon` / `workerLoadTone` | offline/stale/loaded/online + threshold tones |
| Terminal SSR row | `terminal-worker-row`, `data-neon`, mini-bar; no `border-l-green-500`/`border-l-blue-500` |
| Assignment strip | `terminal-swarm-row` when `assigned_worker` set |
| Caps | `terminal-worker-cap` monospace chips |
| Shell | `terminal-workers-layout` + `variant="terminal"` from TerminalDashboard |
| CSS | `.terminal-worker-row.is-expanded`, reduced-motion for worker-row |

### 7. Wrong vs Correct

#### Wrong: Tailwind green rail in terminal mode

```tsx
className={w.is_available ? "border-l-green-500" : "border-l-red-500"}
```

#### Correct: CC neon state class

```tsx
className={cn("terminal-worker-row", `is-${workerRowNeon(w)}`, expanded && "is-expanded")}
```

#### Wrong: Orphaned load-bar in terminal path

```tsx
<div className="load-bar"><div className={cn("load-bar-fill", loadBarColor(pct))} /></div>
```

#### Correct: shared mini-bar + CC tone

```tsx
<span className={`terminal-mini-bar ${workerLoadTone(pct)}`}><i style={{ width: `${pct}%` }} /></span>
```

---

## Scenario: Logs view command-center polish

### 1. Scope / Trigger

- Trigger: User opens the **logs** tab in `TerminalDashboard` (`EventLogPanel`).
- Pure frontend; reuses overview CC tokens + `sessionRoleForEventType`; no new Gateway RPCs.
- Out of scope: Search/Inference redesign (Search has its own scenario below); Opus/Sonnet/Haiku/JEV branding.

### 2. Signatures

```typescript
// dashboard/src/components/panels/EventLogPanel.tsx
EventLogPanel({
  events, stale?, onSelectTask?,
  variant?: "default" | "terminal",
})

// TerminalDashboard logs shell
<div className="terminal-data-view terminal-logs-layout">
  <EventLogPanel variant="terminal" onSelectTask={…} … />
</div>
```

### 3. Contracts

#### Shell

- Logs view wraps the panel in `.terminal-data-view.terminal-logs-layout`.
- Terminal path drops Card chrome; renders `.terminal-logs-panel` with `.cc-session-log-head` / `.terminal-logs-head`.
- `onSelectTask(taskId)` still navigates to the tasks view.

#### Rows

Under `variant="terminal"`:

| Element | Class / token |
|---------|----------------|
| Row | `.cc-session-row` + `.cc-session-role-{lead\|swarm\|dispatch\|advisor}` |
| Role map | `sessionRoleForEventType` — `task_*`→lead, `subtask_*`→swarm, `tool_*`→dispatch, `llm_*`→advisor |
| Line | `.cc-session-line` with `<time>` + role + msg |
| Task jump | `.terminal-logs-task-jump` (monospace id prefix) |

- Terminal path must **not** use Tailwind `border-l-green-500` / `border-l-blue-500` rails or dead `evt-*` background classes.
- Default variant may keep legacy Card + Tailwind type colors.

#### Toolbar

- Filters / type chips / Errors / Export / time-range use `.terminal-log-chip` (role-tinted; Errors → `.is-active.error`).
- Search uses `.terminal-logs-search` (not `focus:border-blue-500`).
- No blue/red Tailwind pills on the terminal path.

#### Stream / Latest

- Virtualized stream uses `.terminal-logs-stream` filling available height under `terminal-view-body` (not fixed `max-h-64` only).
- SSR / zero-metric scroll parents fall back to a capped static row list so role classes still appear in markup.
- “↓ Latest” uses `.terminal-logs-latest` CC control (not `bg-blue-500`).

#### Empty / stale

- Empty → `.terminal-empty-line` vocabulary (`waiting for live events…` / filter miss).
- Stale → `.terminal-logs-stale` banner + `.is-stale` panel border (lead-toned).

#### Reduced motion

- `.terminal-logs-latest` transition disabled under `prefers-reduced-motion: reduce`.

### 4. Validation & Error Matrix

| Condition | Behavior |
|-----------|----------|
| Terminal + events | `cc-session-row` + role class per event type |
| Terminal + empty | `terminal-empty-line`; no Card empty-state chrome |
| Terminal + stale | stale banner visible |
| Filter Errors on | only failed task/subtask rows |
| Default variant | Card + legacy Tailwind type colors allowed |
| Task id click | `onSelectTask` → tasks view |

### 5. Good / Base / Bad Cases

**Good**: Mixed `task_submitted` / `subtask_progress` / `tool_call` / `llm_request` stream → lead/swarm/dispatch/advisor row colors, type chips, export chip, virtualized stream.

**Base**: Empty event log → waiting line; no Tailwind blue Latest control.

**Bad**: Card chrome in terminal mode; `evt-*` / `border-l-green-500` on terminal path; inventing model personas.

### 6. Tests Required

| Test | Assertion |
|------|-----------|
| Terminal SSR rows | `cc-session-row` + `cc-session-role-*`; `data-session-role` |
| Chips | `terminal-log-chip` for Errors / types / ranges / Export |
| Shell | `terminal-logs-layout` + `variant="terminal"` from TerminalDashboard |
| No Tailwind rails | no `border-l-green-500` / `border-l-blue-500` / `bg-blue-500` Latest on terminal path |
| CSS | `.terminal-logs-layout`, `.terminal-log-chip.is-active.error`, reduced-motion for latest |
| Default variant | Card + legacy type colors still render |

### 7. Wrong vs Correct

#### Wrong: Card + Tailwind blue pills in terminal mode

```tsx
<Card><button className="bg-blue-600 text-white">All</button></Card>
```

#### Correct: terminal shell + chips

```tsx
<section className="terminal-logs-panel">
  <button className={cn("terminal-log-chip", active && "is-active swarm")}>All</button>
</section>
```

#### Wrong: Dead evt-* + green rail on terminal rows

```tsx
<div className={cn("evt-completed", "border-l-green-500")}>{evt.type}</div>
```

#### Correct: session role row

```tsx
<div className={cn("cc-session-row", `cc-session-role-${sessionRoleForEventType(evt.type)}`)} />
```

---

## Scenario: Search view command-center polish

### 1. Scope / Trigger

- Trigger: User opens the **search** tab in `TerminalDashboard` (`SearchPanel`).
- Pure frontend; reuses overview CC tokens (`--cc-*`); no new Gateway RPCs.
- Out of scope: Inference redesign; Opus/Sonnet/Haiku/JEV branding.

### 2. Signatures

```typescript
// dashboard/src/components/panels/SearchPanel.tsx
SearchPanel({
  grpcState?, onNavigateFile?, stale?,
  variant?: "default" | "terminal",
})

// TerminalDashboard search shell
<div className="terminal-data-view terminal-search-layout">
  <SearchPanel variant="terminal" onNavigateFile={…} … />
</div>
```

### 3. Contracts

#### Shell

- Search view wraps the panel in `.terminal-data-view.terminal-search-layout`.
- Terminal path drops Card chrome; renders `.terminal-search-panel` with `.terminal-search-head` / `.cc-session-log-head`.
- Stale → `.terminal-search-stale` + `.is-stale` panel border (lead-toned).
- `onNavigateFile({ repoId, path, line })` still opens FileBrowser.

#### Controls

- Query / run / mode / lang / limit use `.terminal-search-chip` (and `.terminal-search-query` / `.terminal-search-lang`).
- Mode chips tint by match type: text→dispatch/dim, semantic→advisor, ast→swarm.
- Terminal path must **not** use Tailwind `focus:border-blue-500`, `border-blue-500`, `text-blue-400`, `bg-purple-500`, or `bg-cyan-500`.

#### Rows

Under `variant="terminal"`:

| Element | Class / token |
|---------|----------------|
| Row | `.terminal-search-row` + `.match-{text\|semantic\|ast\|default}` |
| Neon rail | text→`--cc-dispatch` / dim, semantic→`--cc-advisor`, ast→`--cc-swarm` |
| Path jump | `.terminal-search-jump` (`data-testid="terminal-search-jump"`) |
| Score / kind | `.terminal-search-score` / `.terminal-search-kind` when present |
| Parent | `.terminal-search-parent` when `parentSymbol` is present on the mapped result |

- Surface `score` and optional `symbolKind` from existing `SearchResultItem` fields; wire `parentSymbol` through `mapResult` when the proto carries it — do not invent RPC fields.
- Default variant may keep Card + Tailwind blue/purple/cyan match rails.

#### Reduced motion

- `.terminal-search-chip` / `.terminal-search-row` transitions disabled under `prefers-reduced-motion: reduce`.

### 4. Validation & Error Matrix

| Condition | Behavior |
|-----------|----------|
| Terminal + idle | hint line; chips visible; no Card title |
| Terminal + stale | stale banner visible |
| Terminal + results | neon row rails + score/kind when set |
| Jump click | `onNavigateFile` with repo/path/line |
| Default variant | Card “Code Search” + legacy Tailwind colors allowed |

### 5. Good / Base / Bad Cases

**Good**: Mixed text / semantic / ast hits → dispatch / advisor / swarm neon rails, score + symbolKind chips, path jump.

**Base**: Empty/idle search → ready head + mode/limit chips; no Tailwind blue Search button.

**Bad**: Card chrome in terminal mode; `border-l-blue-500` / `bg-purple-500` / `bg-cyan-500` on terminal path; inventing model personas.

### 6. Tests Required

| Test | Assertion |
|------|-----------|
| Terminal SSR shell | `terminal-search-panel`; chips; query/lang testids |
| Rows | `terminal-search-row` + `data-match-type`; score/kind; jump |
| Shell | `terminal-search-layout` + `variant="terminal"` from TerminalDashboard |
| No Tailwind rails | no `border-l-blue-500` / `bg-purple-500` / `bg-cyan-500` / `text-blue-400` on terminal path |
| CSS | `.terminal-search-layout`, `.terminal-search-row.match-ast`, reduced-motion |
| Default variant | Card “Code Search” still renders; no terminal testids |

### 7. Wrong vs Correct

#### Wrong: Card + Tailwind blue/purple rails in terminal mode

```tsx
<Card><li className="border-l-blue-500"><span className="bg-purple-500/20">semantic</span></li></Card>
```

#### Correct: terminal shell + match-tone neon

```tsx
<section className="terminal-search-panel">
  <li className="terminal-search-row match-semantic" data-match-type="semantic" />
</section>
```
