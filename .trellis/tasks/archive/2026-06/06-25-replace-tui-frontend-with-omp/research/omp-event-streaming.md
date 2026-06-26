# Research: OMP Extension Event Streaming & Real-Time Update Mechanisms

- **Query**: How the oh-my-pi extension API handles events, streaming, and real-time updates. How a UC extension can push real-time subtask progress into the OMP TUI.
- **Scope**: Internal
- **Date**: 2026-06-26

## Findings

### 1. ExtensionAPI.on() — Full Event List

The `ExtensionAPI.on()` method (defined in `types.ts:948-1024`) supports the following events. Extensions subscribe by calling `pi.on(eventType, handler)`.

| Event Type | Fired When | Handler Result |
|---|---|---|
| `resources_discover` | After session_start, allows extensions to provide resource paths | `ResourcesDiscoverResult` (skill/prompt/theme paths) |
| `session_start` | Initial session load | void |
| `session_before_switch` | Before switching to another session (cancellable) | `SessionBeforeSwitchResult` (cancel?) |
| `session_switch` | After switching to another session | void |
| `session_before_branch` | Before branching (cancellable) | `SessionBeforeBranchResult` (cancel?, skipConversationRestore?) |
| `session_branch` | After branching | void |
| `session_before_compact` | Before context compaction (cancellable/customizable) | `SessionBeforeCompactResult` (cancel?, custom compaction?) |
| `session.compacting` | Before compaction summarization (customize prompts) | `SessionCompactingResult` (context?, prompt?, preserveData?) |
| `session_compact` | After context compaction | void |
| `session_shutdown` | Process exit (SIGINT/SIGTERM) | void |
| `session_before_tree` | Before navigating session tree (cancellable) | `SessionBeforeTreeResult` (cancel?, custom summary?) |
| `session_tree` | After navigating session tree | void |
| `context` | Before each LLM call (messages deep-copy, safe to modify) | `ContextEventResult` (replacement messages?) |
| `before_provider_request` | Before provider request is sent (can replace payload) | Replacement payload |
| `after_provider_response` | After provider response received, before stream body consumed | void |
| `before_agent_start` | After user submits prompt, before agent loop | `BeforeAgentStartEventResult` (custom message?, systemPrompt override?) |
| `agent_start` | Agent loop starts (once per user prompt) | void |
| `agent_end` | Agent loop ends | void |
| `session_stop` | Main-agent turn about to settle (can request continuation) | `SessionStopEventResult` (continue?, additionalContext?, decision?, reason?) |
| `turn_start` | Start of each turn | void |
| `turn_end` | End of each turn | void |
| `message_start` | Message starts (user, assistant, or toolResult) | void |
| `message_update` | During assistant message streaming (token-by-token) | void |
| `message_end` | Message ends | void |
| `tool_execution_start` | Tool starts executing | void |
| `tool_execution_update` | During tool execution (partial/streaming output) | void |
| `tool_execution_end` | Tool finishes executing | void |
| `auto_compaction_start` | Auto-compaction starts | void |
| `auto_compaction_end` | Auto-compaction ends | void |
| `auto_retry_start` | Auto-retry starts | void |
| `auto_retry_end` | Auto-retry ends | void |
| `ttsr_triggered` | TTSR rule matching interrupts generation | void |
| `todo_reminder` | Todo reminder logic detects unfinished todos | void |
| `goal_updated` | Goal state changes | void |
| `credential_disabled` | AuthStorage soft-disables a credential | void |
| `input` | User submits input (interactive mode only) | `InputEventResult` (handled?, text?, images?) |
| `tool_approval_requested` | Tool requires approval | void |
| `tool_approval_resolved` | Tool approval resolved | void |
| `tool_call` | Before tool executes (can block) | `ToolCallEventResult` (block?, reason?) |
| `tool_result` | After tool executes (can modify result) | `ToolResultEventResult` (content?, details?, isError?) |
| `user_bash` | User executes bash via ! or !! prefix | `UserBashEventResult` (result?) |
| `user_python` | User executes Python via $ or $$ prefix | `UserPythonEventResult` (result?) |

**Key insight for UC**: The `tool_execution_start/update/end` events fire during agent execution and include `toolCallId`, `toolName`, `args`, and `partialResult`/`result`. Extensions CAN subscribe to these to observe tool activity. The `message_start/update/end` events provide token-level streaming visibility.

**Caveat**: `message_update` events are high-frequency (one per streaming token delta). The `ExtensionRunner.emit()` method (runner.ts:590) lazy-allocates the ExtensionContext to avoid overhead when no handlers are registered, but subscribing to `message_update` without careful throttling could impact performance.

### 2. sendMessage() / sendUserMessage()

Both are defined on `ExtensionAPI` (types.ts:1093-1102).

**`sendMessage()`**:
```typescript
sendMessage<T = unknown>(
  message: Pick<CustomMessage<T>, "customType" | "content" | "display" | "details" | "attribution">,
  options?: { triggerTurn?: boolean; deliverAs?: "steer" | "followUp" | "nextTurn" },
): void;
```
- Injects a `CustomMessage` (role: "custom") into the session
- `customType` is a string identifier — extensions can register a renderer for it via `registerMessageRenderer()`
- `display: true` causes the message to appear in the TUI chat
- `deliverAs` modes:
  - `"steer"` — steering message (wrapped in a special envelope, appears as user interjection)
  - `"followUp"` — queued as a follow-up message
  - `"nextTurn"` — hidden from pending-message UI, consumed on next turn
- `triggerTurn: true` (with `deliverAs: "nextTurn"`) schedules an internal continuation that consumes the message

**`sendUserMessage()`**:
```typescript
sendUserMessage(
  content: string | (TextContent | ImageContent)[],
  options?: { deliverAs?: "steer" | "followUp" },
): void;
```
- Injects a user message into the conversation
- `"steer"` wraps content in a steering envelope for mid-turn direction
- `"followUp"` queues it for the next turn

**Can extensions inject "system" messages?** Not directly. There is no `sendSystemMessage()`. However:
1. `before_agent_start` handler can return `systemPrompt` overrides (types.ts:883-888)
2. `sendMessage()` with `display: false` creates a message that exists in the session but is not visible
3. Custom messages with `display: true` render using the registered `MessageRenderer` (or default rendering)

### 3. Streaming Output

**How OMP streams LLM output to the TUI**:

The `AgentSession` (agent-session.ts) emits events through the agent's event system. The `EventController` (event-controller.ts) subscribes to these events and updates TUI components in real time:

1. `message_start` (assistant) -> Creates `AssistantMessageComponent`, starts streaming
2. `message_update` -> Updates the component with new tokens, `StreamingRevealController` handles smooth rendering
3. `message_end` -> Finalizes the component, updates usage display
4. `tool_execution_start` -> Creates `ToolExecutionComponent`, shows spinner
5. `tool_execution_update` -> Updates partial tool output
6. `tool_execution_end` -> Finalizes tool output display

The `EventController` also manages `setWorkingMessage()` based on `intent` fields from the streaming response (event-controller.ts:190-199).

**Can extensions hook into the stream?** Yes, through `message_start/update/end` and `tool_execution_start/update/end` event subscriptions. However, these are **observation-only** — the handler return types for these events are `void`. Extensions cannot modify the streaming content itself.

**Can extensions add their own streaming content blocks?** Not directly into the LLM stream. Extensions CAN:
1. Use `ctx.ui.setWidget()` to show live-updating content above/below the editor
2. Use `ctx.ui.setStatus()` to show text in the footer/status bar
3. Use `ctx.ui.setWorkingMessage()` to replace the loading/streaming status
4. Use `pi.sendMessage()` with `display: true` to inject custom messages that render in the chat

### 4. registerMessageRenderer()

Defined in `ExtensionAPI` (types.ts:1077):
```typescript
registerMessageRenderer<T = unknown>(customType: string, renderer: MessageRenderer<T>): void;
```

**MessageRenderer interface** (types.ts:905-909):
```typescript
type MessageRenderer<T = unknown> = (
  message: CustomMessage<T>,
  options: MessageRenderOptions,
  theme: Theme,
) => Component | undefined;
```

**MessageRenderOptions** (types.ts:901-903):
```typescript
interface MessageRenderOptions {
  expanded: boolean;
}
```

**CustomMessage type** (messages.ts:355-364):
```typescript
interface CustomMessage<T = unknown> {
  role: "custom";
  customType: string;
  content: string | (TextContent | ImageContent)[];
  display: boolean;
  details?: T;
  attribution?: MessageAttribution;
  timestamp: number;
}
```

**How it works**:
1. Extension calls `pi.registerMessageRenderer("my-type", rendererFn)` during initialization
2. The renderer is stored on the `Extension` object in `messageRenderers: Map<string, MessageRenderer>`
3. When a `CustomMessage` with `customType: "my-type"` is rendered, the `ExtensionRunner.getMessageRenderer()` (runner.ts:446-454) looks up the renderer across all extensions
4. The renderer receives the `CustomMessage` (with its `content`, `details`, `attribution`), render options, and the current theme
5. The renderer returns an Ink `Component` (from `@oh-my-pi/pi-tui`) or `undefined` (falls back to default rendering)

**Example**: The swarm extension uses `pi.sendMessage({ customType: "swarm-result", ... })` and could register a renderer for `"swarm-result"` to customize how the swarm result appears in the chat.

There is also `registerAssistantThinkingRenderer()` (types.ts:1080) for customizing how assistant thinking blocks render.

### 5. setWorkingMessage()

Defined on `ExtensionUIContext` (types.ts:199):
```typescript
setWorkingMessage(message?: string): void;
```

**Implementation** (interactive-mode.ts:3442-3457):
- If called with a string while the loading animation is active, it updates the animation's message text
- If called with `undefined`, it restores the default working message
- If called before the loading animation starts, the message is queued as `#pendingWorkingMessage` and applied when the animation starts

The loading animation is the spinner/progress indicator shown during streaming. `setWorkingMessage()` changes the text label next to the spinner.

**Use case for UC**: An extension can call `ctx.ui.setWorkingMessage("UC: Subtask st-3 running...")` during tool execution to show progress in the streaming indicator area. The `EventController` automatically sets this from the agent's `intent` field, but extensions can override it.

### 6. Background Tasks in Extensions

**Can an extension run background work?** Yes. Extensions are standard TypeScript modules loaded by Bun. They can:

1. **Start background intervals/timeouts** using `setInterval`/`setTimeout` in event handlers or during factory initialization
2. **Maintain internal state** in closures or class instances
3. **Push updates to the TUI** via `ctx.ui` methods (setWidget, setStatus, setWorkingMessage, notify)

**Lifecycle hooks for cleanup**:

1. **`session_shutdown` event** (types.ts:989): Fires on SIGINT/SIGTERM. Has a **2-second timeout** (runner.ts:86) instead of the normal 30-second timeout, so cleanup must be fast. This is the primary cleanup hook.

2. **`ToolDefinition.onSession`** (types.ts:461): Fires on session lifecycle events — `"start" | "switch" | "branch" | "tree" | "shutdown"`. This is per-tool, not per-extension, and is designed for tools that need to reconstruct state or cleanup resources.

3. **No explicit "extension destroy" hook**: There is no `dispose()` or `destroy()` method on the extension itself. The `session_shutdown` event is the closest thing.

**Pattern for background gRPC watching**:
```typescript
export default function ucExtension(pi: ExtensionAPI): void {
  let grpcStream: GrpcEventStream | undefined;
  let pollInterval: Timer | undefined;

  pi.on("session_start", async (_event, ctx) => {
    // Start background work
    grpcStream = connectGrpcStream();
    pollInterval = setInterval(() => {
      const update = grpcStream?.poll();
      if (update) {
        ctx.ui.setWidget("uc-progress", formatProgress(update));
        ctx.ui.setStatus("uc", `Subtask ${update.id}: ${update.status}`);
      }
    }, 1000);
  });

  pi.on("session_shutdown", async () => {
    // Cleanup — must complete within 2 seconds!
    clearInterval(pollInterval);
    await grpcStream?.close();
  });
}
```

**Important**: The `EventBus` (event-bus.ts) shared between extensions (`pi.events`) allows inter-extension communication via named channels. This could be used for coordination between the UC orchestrator and a hypothetical TUI controller extension.

### 7. How the UC Orchestrator Currently Pushes Updates

The UC orchestrator (orchestrator.ts) uses these mechanisms to notify the user:

1. **`ctx.ui.notify()`** — Shows notifications in the TUI for task lifecycle events:
   - `"Task ${taskId}: planning..."` on task start
   - `"Task ${taskId}: ${subtaskDefs.length} subtasks, ${waves.length} wave(s)"` after decomposition
   - `"Task ${taskId}: wave ${waveIdx + 1}/${waves.length}"` per wave
   - `"Task ${taskId}: ${task.status}"` on completion

2. **`ctx.ui.setWidget()`** — Shows a live-updating progress widget above the editor:
   ```typescript
   private updateWidget(ctx, key, task): void {
     const lines = [`UC Task: ${task.id}`, `Status: ${task.status}`, "Subtasks:"];
     for (const st of task.subtasks) {
       const icon = st.status === "completed" ? "✓" : st.status === "running" ? "●" : "○";
       lines.push(`  ${icon} ${st.id}: ${st.description.slice(0, 50)}`);
     }
     ctx.ui.setWidget(key, lines);
   }
   ```
   - Widget is created at task start, updated after each wave, and cleared on completion
   - Uses `ctx.ui.setWidget(widgetKey, undefined)` to remove

3. **`pi.sendMessage()`** — Injects a `CustomMessage` with `customType: "uc-task-result"` into the conversation at task completion:
   ```typescript
   pi.sendMessage({
     customType: "uc-task-result",
     content: [{ type: "text", text: summary }],
     display: true,
     details: { taskId, status, subtaskCount },
   }, { triggerTurn: false });
   ```

4. **`GrpcBridge` sync** — Fire-and-forget sync to the Rust engine via gRPC:
   - `syncTaskToGrpc()` after each wave and on state changes
   - `bridge.writeMemory()` for subtask results, reviews, and checkpoints

5. **`ControlSignalSubscriber`** — Listens for NATS control events (pause/resume/cancel) from the TUI/Dashboard, enabling bidirectional control.

**Current gap**: The orchestrator updates the widget only after each wave completes, not during individual subtask execution. Subtask-level real-time updates (like streaming output from a worker agent) are not pushed to the TUI. The `executeWave` method runs subtasks concurrently with `maxConcurrency` workers and only syncs state after each completes.

### Files Found

| File Path | Description |
|---|---|
| `vendor/oh-my-pi/packages/coding-agent/src/extensibility/extensions/types.ts` | ExtensionAPI interface, all event types, tool/message types |
| `vendor/oh-my-pi/packages/coding-agent/src/extensibility/shared-events.ts` | Shared event/result types between extensions and hooks |
| `vendor/oh-my-pi/packages/coding-agent/src/extensibility/extensions/runner.ts` | ExtensionRunner — emits events, manages lifecycle, creates contexts |
| `vendor/oh-my-pi/packages/coding-agent/src/extensibility/extensions/wrapper.ts` | ExtensionToolWrapper — intercepts tool calls/results |
| `vendor/oh-my-pi/packages/coding-agent/src/extensibility/extensions/loader.ts` | Loads extension modules, creates ConcreteExtensionAPI |
| `vendor/oh-my-pi/packages/coding-agent/src/session/messages.ts` | CustomMessage type, message transformation |
| `vendor/oh-my-pi/packages/coding-agent/src/modes/controllers/extension-ui-controller.ts` | ExtensionUiController — wires extension UI methods to TUI |
| `vendor/oh-my-pi/packages/coding-agent/src/modes/controllers/event-controller.ts` | EventController — bridges agent events to TUI components |
| `vendor/oh-my-pi/packages/coding-agent/src/utils/event-bus.ts` | Simple EventBus for inter-extension communication |
| `vendor/oh-my-pi/packages/coding-agent/src/modes/interactive-mode.ts` | setWorkingMessage implementation |
| `vendor/oh-my-pi/packages/swarm-extension/src/extension.ts` | Real-world extension example (swarm orchestrator) |
| `vendor/oh-my-pi/packages/swarm-extension/src/swarm/render.ts` | Swarm progress rendering |
| `packages/uc-orchestrator/src/orchestrator/orchestrator.ts` | UC orchestrator — current update mechanisms |
| `packages/uc-orchestrator/src/extension.ts` | UC orchestrator extension entry point |

### Code Patterns

**Extension factory pattern** (loader.ts:285-312):
```typescript
export default function myExtension(pi: ExtensionAPI): void | Promise<void> {
  pi.on("session_start", (event, ctx) => { ... });
  pi.registerTool({ name: "my_tool", ... });
  pi.registerCommand("my-cmd", { handler: async (args, ctx) => { ... } });
  pi.registerMessageRenderer("my-type", (msg, opts, theme) => { ... });
}
```

**Widget-based real-time progress** (swarm extension pattern):
```typescript
const widgetKey = "my-widget";
const updateWidget = () => {
  const lines = renderProgress(state);
  ctx.ui.setWidget(widgetKey, lines);
};
updateWidget();  // Initial
// ... after each state change:
updateWidget();
// ... on completion:
ctx.ui.setWidget(widgetKey, undefined);  // Remove
```

**Custom message injection** (orchestrator pattern):
```typescript
pi.sendMessage({
  customType: "uc-task-result",
  content: [{ type: "text", text: summary }],
  display: true,
  details: { taskId, status, subtaskCount },
}, { triggerTurn: false });
```

### Related Specs

- `.trellis/tasks/06-25-tui-omp-unified-control-path-seamless-handoff/` — Related task for unified control path

## Caveats / Not Found

1. **No extension-level dispose hook**: There is only `session_shutdown` (2-second timeout) and per-tool `onSession`. Long-running background tasks must be designed for fast cleanup.

2. **Widget content is string arrays or Component factories**: `ctx.ui.setWidget()` accepts `string[] | ExtensionUiComponentFactory | undefined`. String arrays are simple but limited (max 10 lines). Component factories allow rich TUI components but require understanding the Ink-like component model.

3. **Event handler timeout**: All extension event handlers have a 30-second timeout (except `session_shutdown` at 2 seconds). Handlers that exceed this are terminated and logged as errors. Background work should not be done inside event handlers — it should be started by them and run independently.

4. **No "streaming custom message" API**: There is no way for an extension to push incremental content into a chat message. `sendMessage()` is fire-and-forget. To show live progress, extensions must use `setWidget()`, `setStatus()`, or `setWorkingMessage()`.

5. **UC orchestrator stub context**: When called from the RPC server (no OMP interactive context), the orchestrator uses a stub `ExtensionCommandContext` that no-ops all UI methods. This means UI updates are lost when tasks are initiated via the RPC server rather than the TUI.

6. **Inter-extension communication**: The shared `EventBus` (`pi.events`) allows named-channel pub/sub between extensions, but this is not type-safe and has no built-in schema validation.
