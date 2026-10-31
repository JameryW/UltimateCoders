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
