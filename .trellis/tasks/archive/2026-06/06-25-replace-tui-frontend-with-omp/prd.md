# Replace TUI Frontend with OMP (Enhanced)

## Goal

Delete the standalone Ink/React TUI (`tui/`) and make oh-my-pi's built-in TUI the sole user-facing frontend for UC. Use OMP's extension API to build **richer** orchestration visualization than the Ink TUI provided — subtask results rendered as native OMP OutputBlocks, progress widgets, custom overlays, and registered message renderers.

## What I already know

* **Current TUI** (`tui/`): 12 Ink/React components, gRPC client, 103+ tests — to be deleted
* **OMP TUI**: pi-tui framework with Component/Container/SelectList/Box/Text, overlay system, widgets, custom message renderers
* **UC Orchestrator**: Already an omp extension with `/uc` slash commands + `ctx.ui.notify()`/`ctx.ui.setWidget()` feedback
* **OMP Extension API capabilities**:
  - `ui.custom(factory, { overlay: true })` — fullscreen overlay with keyboard focus
  - `ui.setWidget(key, lines|factory)` — live-updating widget above/below editor (max 10 lines for string[])
  - `ui.setStatus(key, text)` — footer status bar text
  - `ui.setWorkingMessage(msg)` — streaming spinner label
  - `pi.registerMessageRenderer(customType, renderer)` — custom chat message rendering
  - `pi.registerTool({ renderCall, renderResult })` — custom tool output rendering
  - `pi.registerShortcut(keyId, { handler })` — editor-focused keyboard shortcuts
  - `pi.on(eventType, handler)` — 30+ event types including tool_execution_start/end
  - `pi.sendMessage({ customType, content, display, details })` — inject custom messages
* **Key limitations**:
  - No TreeList component — must build from Container/Text or adapt SelectList
  - `setFooter`/`setHeader` are stubs (unimplemented)
  - Widget interactivity requires manual focus management
  - Shortcuts only fire when editor has focus, not in overlays
  - No streaming custom message API — use setWidget for live progress

## Assumptions (temporary)

* The Rust gRPC server is still needed for distributed scenarios but NOT for local single-user use
* OMP's built-in editor is sufficient for input (it has CJK/IME support)
* We accept the 10-line limit for string-based widgets (or use Component factory for unlimited)
* Background gRPC watching is possible via setInterval + session_shutdown cleanup

## Requirements

### R1: Delete `tui/` directory
- Remove the entire `tui/` directory (Ink/React TUI)
- Remove related dependencies (ink, react, @grpc/grpc-js, etc.) from root package.json if present
- Update README/docs to reflect OMP-only frontend

### R2: Enhanced subtask progress visualization
- Replace the plain-text `updateWidget()` with a rich Component factory widget showing:
  - Wave progress bar (wave X/Y)
  - Subtask status icons with color (✓ completed, ● running, ○ pending, ✗ failed)
  - Currently running subtask names
- Update widget in real-time during subtask execution (not just after waves)
- Use `ctx.ui.setWorkingMessage()` to show current subtask during execution

### R3: SubtaskTree overlay (Ctrl+T)
- Build a SubtaskTree Component using Container/Text
- Render tree with status icons, descriptions, dependency edges
- Keyboard navigation (up/down, Enter for detail, Esc to close)
- Register via `pi.registerShortcut(Key.ctrl("t"), handler)`
- Open via `ui.custom(treeFactory, { overlay: true })`
- Show subtask detail on Enter (description, result/error, retries)

### R4: Custom message renderer for task results
- Register `pi.registerMessageRenderer("uc-task-result", renderer)` in extension.ts
- Render task completion messages as styled Component (not plain text):
  - Summary header with status icon
  - Subtask result list with status icons
  - Collapsed by default, expand on Enter

### R5: Enhanced `/uc status` output
- When called with a task ID, render structured output using Container/Box/Text instead of plain text
- Show subtask DAG, wave boundaries, progress indicators
- When called without task ID, show task list with status badges

### R6: Task list overlay
- Build a TaskList Component using SelectList
- Show all tasks with status, subtask counts, timestamps
- Navigate with keyboard, Enter to view detail
- Register shortcut (e.g., Ctrl+Shift+T)
- Open via `ui.custom(listFactory, { overlay: true })`

### R7: Interactive subtask retry from overlay
- In SubtaskTree overlay, pressing R on a failed subtask triggers retry
- Calls `orchestrator.retrySubtask(taskId, subtaskId)`
- Shows confirmation via `ui.confirm()`

### R8: Connection status in footer
- Use `ui.setStatus("uc", text)` to show gRPC connection state
- Update on connect/disconnect/reconnect
- Show active task count: `UC: connected | 2 active tasks`
- **StatusBar upgrade path**: Abstract status rendering behind a `StatusRenderer` interface with `setStatusField(key, text)` method. Current impl calls `ui.setStatus()`. When OMP implements `setFooter()`, swap to `setFooter(factory)` without changing callers. Mark with `ponytail: setStatus bridge, upgrade to setFooter when OMP implements it`.

### R9: JSONL event channel for uc-rpc-server
- Add event emission to `uc-rpc-server.ts` so Python OmpBridge receives progress events
- Orchestrator's internal event emitter (from R2) also writes to JSONL stdout when in RPC mode
- Event types: `task_progress` (wave/subtask updates), `task_complete`, `subtask_start`, `subtask_end`, `subtask_failed`
- Dual-channel: OMP TUI gets rich Component rendering; JSONL gets structured event payloads
- RPC server context wraps UI methods: `notify()` and `setWidget()` emit JSONL events instead of no-op
- This unblocks Python tooling (dashboards, scripts) that consume uc-rpc-server

## Acceptance Criteria

- [ ] `tui/` directory deleted
- [ ] `run-omp.sh` provides complete UC task lifecycle
- [ ] Subtask progress widget updates during execution (not just after waves)
- [ ] Ctrl+T opens SubtaskTree overlay with keyboard navigation
- [ ] Task completion messages render with custom renderer
- [ ] `/uc status` shows structured output
- [ ] Task list overlay accessible via keyboard shortcut
- [ ] Failed subtask retry works from SubtaskTree overlay
- [ ] Connection status visible in footer (with setFooter upgrade path)
- [ ] uc-rpc-server emits JSONL progress events (Python bridge receives real-time updates)
- [ ] Dual-channel rendering: OMP TUI gets rich components, JSONL gets structured events

## Definition of Done

* Tests added/updated for orchestrator changes
* Lint / typecheck / CI green
* README and run instructions updated
* `tui/` directory removed
* Old TUI dependencies cleaned from package.json

## Out of Scope

* Distributed/multi-user scenarios (gRPC server stays for those)
* Python agent layer changes
* Modifying omp core (vendor/ is a submodule)
* Chat search/bookmarks (OMP's built-in message log is sufficient for MVP)
* WorkerPanel visualization (nice-to-have, defer)
- Offline simulation mode (OMP requires API connection)

## Technical Approach

### Architecture

```
User → OMP TUI → UC Extension → UCOrchestrator ←── internal EventEmitter
                     │               │                    │
                     │               ↓                    │
                     │        GrpcBridge → Rust gRPC      │
                     │               ↓                    │
                     │          NATS events               │
                     │                                    │
                     ├── ui.setWidget()  ←── progress events
                     ├── ui.custom()     ←── overlay events
                     ├── pi.sendMessage() ←── completion events
                     ├── pi.registerMessageRenderer()
                     ├── pi.registerShortcut()
                     └── ui.setStatus()  ←── connection events
                                                  │
                    uc-rpc-server.ts ←────────────┘
                          │
                    JSONL stdout (Python bridge)
                    task_progress / subtask_start / subtask_end / ...
```

The extension becomes the sole UI layer. No separate process, no gRPC client in the frontend.
The internal EventEmitter serves both channels: OMP TUI (rich components) and JSONL stdout (structured events).

### Key Design Decisions

1. **Widget for progress, overlay for detail** — Widget shows always-visible progress; overlay provides depth on demand (same as Ink TUI's SubtaskTree + StatusBar model)

2. **Component factory for widgets** — Use `(tui, theme) => Component` instead of string[] to avoid 10-line limit and enable styled rendering

3. **Orchestrator emits events, extension renders** — Add an internal event emitter to UCOrchestrator. The extension subscribes and updates UI. This decouples orchestration logic from presentation.

4. **SelectList for TaskList, custom Component for SubtaskTree** — SelectList provides built-in keyboard navigation. SubtaskTree needs custom layout (tree structure, dependency lines) so build from Container/Text.

### Implementation Order

1. Add internal event emitter to UCOrchestrator (subtask_start, subtask_end, wave_start, wave_end, task_complete)
2. Build SubtaskProgressWidget Component (replaces updateWidget strings)
3. Build SubtaskTreeOverlay Component
4. Build TaskListOverlay Component (using SelectList)
5. Register uc-task-result message renderer
6. Enhance `/uc status` with structured rendering
7. Add interactive retry in SubtaskTree
8. Register keyboard shortcuts (Ctrl+T, Ctrl+Shift+T)
9. Wire everything in extension.ts
10. JSONL event channel: RPC server context emits events to stdout
11. StatusRenderer abstraction for footer (setStatus bridge + setFooter upgrade path)
12. Delete `tui/` directory

## Research References

* [`research/omp-extension-ui-api.md`](research/omp-extension-ui-api.md) — Full OMP extension UI API reference, Component system, overlay details, widget patterns
* [`research/omp-event-streaming.md`](research/omp-event-streaming.md) — Event system, sendMessage, registerMessageRenderer, background task patterns

## Technical Notes

* pi-tui Component: `render(width) => string[]`, `handleInput(data)`, `dispose()`
* Container: addChild/removeChild, memoized render
* SelectList: built-in keyboard nav, fuzzy filter, onSelect/onCancel
* Widget max 10 lines for string[], unlimited for Component factory
* Shortcut: `Key.ctrl("t")`, fires only when editor focused
- Overlay: `ui.custom(factory, { overlay: true })`, fullscreen modal, gets keyboard focus
