# Research: OMP Extension UI API

- **Query**: In-depth research on oh-my-pi extension API's component and rendering capabilities for building rich custom UIs
- **Scope**: Internal (vendor/oh-my-pi source code)
- **Date**: 2026-06-26

## Findings

### 1. pi-tui Component System

#### Component Interface

**File**: `vendor/oh-my-pi/packages/tui/src/tui.ts:139-176`

The `Component` interface is the foundational contract for all UI elements:

```typescript
export interface Component {
  render(width: number): readonly string[];
  handleInput?(data: string): void;
  wantsKeyRelease?: boolean;
  invalidate?(): void;
  setIgnoreTight?(ignore: boolean): any;
  dispose?(): void;
}
```

**Lifecycle methods**:
- `render(width)` — **Required**. Returns array of styled ANSI strings (one per terminal row). Must return the same array reference when content is unchanged (reference equality is the engine's proof of byte-identical content). Must return a fresh array when content changes.
- `handleInput(data)` — **Optional**. Receives raw terminal input string when the component has keyboard focus. Called from TUI's `#handleInput` method.
- `invalidate()` — **Optional**. Called when theme changes or when component needs to re-render from scratch.
- `dispose()` — **Optional**. Teardown hook called when component is permanently removed from the live tree. Must be idempotent. Containers propagate dispose to their children.
- `setIgnoreTight(ignore)` — **Optional**. Controls tight layout mode behavior.
- `wantsKeyRelease` — **Optional** boolean. If true, component receives Kitty key release events. Default false.

#### Supplementary Interfaces

| Interface | File:Line | Purpose |
|---|---|---|
| `Focusable` | tui.ts:322-327 | Components that can receive focus and display a cursor. Has `focused: boolean` and optional `setUseTerminalCursor()` |
| `OverlayFocusOwner` | tui.ts:179-182 | Lets an overlay root delegate keyboard focus to sub-components via `ownsOverlayFocusTarget()` |
| `NativeScrollbackLiveRegion` | tui.ts:220-224 | Append-only scrollback commit protocol for streaming content |
| `RenderStablePrefix` | tui.ts:273-275 | Opt-in for in-place mutation of render arrays across frames |
| `ViewportTailProvider` | tui.ts:302-304 | Fast-path tail rendering during resize drags |

#### Container Class

**File**: `vendor/oh-my-pi/packages/tui/src/tui.ts:498-589`

`Container` implements `Component` and holds `children: Component[]`. Key methods:
- `addChild(component)` / `removeChild(component)` / `clear()`
- Memoized concatenation: skips rebuilding when every child returned the same array reference
- Propagates `invalidate()` and `dispose()` to children

#### The TUI Class (Root Container)

**File**: `vendor/oh-my-pi/packages/tui/src/tui.ts:911-3695`

`TUI extends Container` is the main rendering engine. Key APIs:
- `setFocus(component)` / `getFocused()` — Focus management
- `showOverlay(component, options?)` → `OverlayHandle` — Overlay system
- `addInputListener(handler)` → unsubscribe fn — Raw terminal input interception
- `requestRender(force?)` / `requestComponentRender(component)` — Render scheduling
- `start()` / `stop()` — Lifecycle

---

### 2. ExtensionUIContext.custom()

**Type definition**: `vendor/oh-my-pi/packages/coding-agent/src/extensibility/extensions/types.ts:213-222`

```typescript
custom<T>(
  factory: (
    tui: TUI,
    theme: Theme,
    keybindings: KeybindingsManager,
    done: (result: T) => void,
  ) => ExtensionUiComponent | Promise<ExtensionUiComponent>,
  options?: { overlay?: boolean },
): Promise<T>;
```

**Implementation**: `vendor/oh-my-pi/packages/coding-agent/src/modes/controllers/extension-ui-controller.ts:698-752`

#### How it works:

1. **Factory function**: Receives `tui`, `theme`, `keybindings`, and a `done` callback. Must return a `Component & { dispose?() }`.
2. **`done(result)`**: The component calls this to resolve the Promise and close the custom UI. The result is returned to the caller.
3. **Without overlay mode** (`overlay?: false` or omitted):
   - Saves current editor text
   - Clears `editorContainer` and adds the custom component
   - Sets keyboard focus to the custom component via `tui.setFocus(component)`
   - On close: restores the editor and its saved text
4. **With overlay mode** (`overlay: true`):
   - Calls `tui.showOverlay(component, { anchor: "bottom-center", width: "100%", maxHeight: "100%", margin: 0 })`
   - The overlay renders on top of the entire terminal content
   - On close: calls `overlayHandle.hide()`
5. **Keyboard focus**: The component receives keyboard input via its `handleInput(data)` method when it has focus (set by `tui.setFocus()`).
6. **Keybindings**: A fresh `KeybindingsManager.inMemory()` is created for each custom UI invocation, isolating it from the main app keybindings.

#### Key observations:
- The overlay mode uses `anchor: "bottom-center"` with full width/height and zero margin, effectively making it a fullscreen modal
- The non-overlay mode replaces the editor area (below the chat, above the status bar)
- The component must call `done()` to close itself; there is no automatic close on Escape (the component must implement that itself via `handleInput`)

---

### 3. registerTool() with renderCall/renderResult

**Type definition**: `vendor/oh-my-pi/packages/coding-agent/src/extensibility/extensions/types.ts:428-473`

```typescript
interface ToolDefinition<TParams, TDetails> {
  name: string;
  label: string;
  description: string;
  parameters: TParams;
  execute(...): Promise<AgentToolResult<TDetails>>;
  renderCall?: (args, options: ToolRenderResultOptions, theme: Theme) => Component;
  renderResult?: (result, options: ToolRenderResultOptions, theme, args?) => Component;
}
```

**ToolRenderResultOptions**: `types.ts:408-415`
```typescript
interface ToolRenderResultOptions {
  expanded: boolean;
  isPartial: boolean;
  spinnerFrame?: number;
}
```

**How tool renderers integrate**: Built-in tool renderers are in `vendor/oh-my-pi/packages/coding-agent/src/tools/renderers.ts`. The `ToolRenderer` type adds `mergeCallAndResult?`, `inline?`, and `provisionalPendingPreview?` flags. Each renderer returns a `Component` that is embedded in the chat transcript's output stream.

**Pattern**: Tool renderers typically use `Container` + `Text` + `Box` to compose styled output. They receive `Theme` for consistent styling. The `expanded` flag controls whether to show full or collapsed output. `isPartial` indicates streaming state.

**Extension tool example** (api-demo.ts): Only defines `execute`; no custom renderer. The default renderer is used.

**Built-in example**: `vendor/oh-my-pi/packages/coding-agent/src/tools/todo.ts:822-880` — The todo tool's `renderCall` and `renderResult` both return `Component` instances using Container/Text.

---

### 4. setWidget()

**Type definition**: `vendor/oh-my-pi/packages/coding-agent/src/extensibility/extensions/types.ts:156-164`

```typescript
type WidgetPlacement = "aboveEditor" | "belowEditor";
type ExtensionUiComponent = Component & { dispose?(): void };
type ExtensionUiComponentFactory = (tui: TUI, theme: Theme) => ExtensionUiComponent;
type ExtensionWidgetContent = string[] | ExtensionUiComponentFactory | undefined;

interface ExtensionWidgetOptions {
  placement?: WidgetPlacement;  // default: "aboveEditor"
}
```

**API**: `ui.setWidget(key: string, content: ExtensionWidgetContent, options?: ExtensionWidgetOptions): void`

**Implementation**: `vendor/oh-my-pi/packages/coding-agent/src/modes/controllers/extension-ui-controller.ts:254-319`

#### How it works:

1. **Placement**: Widgets render either `aboveEditor` or `belowEditor` the main input area. Two separate maps: `#hookWidgetsAbove` and `#hookWidgetsBelow`.
2. **String array content**: Each string becomes a `Text` line. Capped at `MAX_WIDGET_LINES = 10`. Truncation message shown if exceeded.
3. **Component factory content**: `content(tui, theme)` is called to create an `ExtensionUiComponent`. This factory can return an interactive component.
4. **Removing**: Call `setWidget(key, undefined)` to remove a widget.
5. **Rendering**: Widgets are placed in `ctx.hookWidgetContainerAbove` and `ctx.hookWidgetContainerBelow` Container instances. When above: includes a leading Spacer(1). When empty and above: still renders a Spacer(1).
6. **Interactivity**: Widget component factories receive the TUI instance, so they CAN create interactive components. However, widgets do NOT automatically receive keyboard focus — the editor retains focus. For interactive widgets, the component would need to use `tui.addInputListener()` or the extension would need to redirect focus.

**Example usage** (plan-mode.ts:234-246):
```typescript
ctx.ui.setWidget("plan-todos", lines);  // string array
ctx.ui.setWidget("plan-todos", undefined);  // remove
```

**Example usage** (swarm-extension/extension.ts:138-142):
```typescript
const lines = renderSwarmProgress(stateTracker.state);
ctx.ui.setWidget(widgetKey, lines);
```

---

### 5. registerShortcut()

**Type definition**: `vendor/oh-my-pi/packages/coding-agent/src/extensibility/extensions/types.ts:1047-1054`

```typescript
registerShortcut(
  shortcut: KeyId,
  options: {
    description?: string;
    handler: (ctx: ExtensionContext) => Promise<void> | void;
  },
): void;
```

**Registration**: `vendor/oh-my-pi/packages/coding-agent/src/extensibility/extensions/loader.ts:172-179`
Shortcuts are stored in `extension.shortcuts: Map<KeyId, ExtensionShortcut>`.

**Dispatch**: `vendor/oh-my-pi/packages/coding-agent/src/modes/controllers/input-controller.ts:1606-1626`
```typescript
registerExtensionShortcuts(): void {
  const shortcuts = runner.getShortcuts();
  for (const [keyId, shortcut] of shortcuts) {
    this.ctx.editor.setCustomKeyHandler(keyId, () => {
      const ctx = runner.createCommandContext();
      shortcut.handler(ctx);
    });
  }
}
```

#### Key observations:
- Shortcuts are registered on the **editor's custom key handler**, meaning they fire only when the editor has focus (the default state).
- They use `KeyId` type which supports full modifier combos: `Key.ctrl("x")`, `Key.shift("p")`, `Key.ctrlShift("x")`, etc.
- **Cannot override default keybindings** — the custom key handler is checked by the editor, which has its own binding resolution. If a shortcut conflicts with an editor binding, the editor binding typically wins.
- **Overlay mode**: Shortcuts registered on the editor won't fire when an overlay has focus, because input goes to the focused overlay component instead.
- **Multiple extensions can register the same key** — last registration wins (Map overwrites).

---

### 6. Existing Extension Examples

| File | What it demonstrates |
|---|---|
| `examples/extensions/api-demo.ts` | Tool registration with `execute()`, event subscription, logger/zod/pi module access |
| `examples/extensions/tools.ts` | `ui.custom()` with `SettingsList` component, `Container`, inline Component class, `handleInput` delegation |
| `examples/extensions/plan-mode.ts` | `registerShortcut()`, `registerFlag()`, `setStatus()`, `setWidget()`, `ui.select()`, `ui.input()`, event filtering, context injection |
| `examples/extensions/thinking-note.ts` | `registerAssistantThinkingRenderer()` with Container/Text |
| `examples/extensions/chalk-logger.ts` | Basic tool with logger |
| `examples/extensions/hello.ts` | Minimal extension skeleton |
| `examples/extensions/pirate.ts` | Event interception pattern |
| `examples/extensions/reload-runtime.ts` | Extension reload |
| `packages/swarm-extension/src/extension.ts` | `setWidget()` for progress display, `registerCommand()` with argument completions |

---

### 7. TreeList Component

**Not found**. There is no `TreeList` component in the pi-tui package.

**Available list-like components**:

| Component | File | Interactive? |
|---|---|---|
| `SelectList` | `packages/tui/src/components/select-list.ts` | Yes — keyboard navigation (up/down/page/enter/esc), type-to-filter, mouse hover/click/wheel |
| `SettingsList` | `packages/tui/src/components/settings-list.ts` | Yes — keyboard navigation, type-to-filter, value cycling, submenus, mouse hover/click/wheel |
| `ExtensionList` | `packages/coding-agent/src/modes/components/extensions/extension-list.ts` | Yes — keyboard navigation, toggle, fuzzy search |

**SelectList** (`packages/tui/src/components/select-list.ts`):
- Implements `Component` with `render()` and `handleInput()`
- Key callbacks: `onSelect`, `onCancel`, `onSelectionChange`
- Features: fuzzy filtering, scroll view, mouse hover/click, configurable theme
- Constructor: `SelectList(items, maxVisible, theme, layoutOptions?)`
- Reusable by extensions: yes, it's exported from `@oh-my-pi/pi-tui`

**SettingsList** (`packages/tui/src/components/settings-list.ts`):
- Implements `Component` with `render()` and `handleInput()`
- Features: cycling values, submenus, section navigation, split sidebar layout
- Constructor: `SettingsList(items, maxVisible, theme, onChange, onCancel, options?)`
- Reusable by extensions: yes, exported from `@oh-my-pi/pi-tui` (confirmed via tools.ts example importing it)

---

### 8. OutputBlock / StatusLine

#### StatusLine

Extensions can set status text via `ui.setStatus(key, text)`. Implementation:
```typescript
setHookStatus(key: string, text: string | undefined): void {
  this.ctx.statusLine.setHookStatus(key, text);
  this.ctx.ui.requestRender();
}
```

The `statusLine` is a Component in the footer area. Multiple extensions can set status by key — they coexist. Setting `text: undefined` removes that key's status.

**Extensions cannot create new StatusLine instances** — they can only set text on the shared one.

#### OutputBlock

There is no `OutputBlock` class exported from pi-tui. The chat transcript is managed by internal container components (`chatContainer`, `pendingMessagesContainer`). Extensions interact with output via:
- `ui.notify(message, type)` — Shows an inline notification (info/warning/error)
- `ui.custom()` — Replaces the editor area with a custom component
- `ui.setWidget()` — Adds content above/below the editor
- Tool `renderCall`/`renderResult` — Custom rendering in the tool output stream
- `registerMessageRenderer()` — Custom rendering for custom message types
- `registerAssistantThinkingRenderer()` — Custom rendering for thinking blocks

#### Other UI surface available to extensions

From `ExtensionUIContext` (`types.ts:175-274`):

| Method | Purpose |
|---|---|
| `select(title, options, dialogOptions?)` | Show a selector overlay in the editor area |
| `confirm(title, message, dialogOptions?)` | Show Yes/No selector |
| `input(title, placeholder?, dialogOptions?)` | Show text input in editor area |
| `notify(message, type?)` | Show notification in chat |
| `onTerminalInput(handler)` | Listen to raw terminal input |
| `setStatus(key, text?)` | Set status bar text |
| `setWorkingMessage(message?)` | Set loading/streaming message |
| `setWidget(key, content, options?)` | Widget above/below editor |
| `setFooter(factory?)` | Custom footer component |
| `setHeader(factory?)` | Custom header component |
| `setTitle(title)` | Set terminal window title |
| `custom(factory, options?)` | Full custom component with keyboard focus |
| `setEditorText(text)` | Set input editor text |
| `pasteToEditor(text)` | Paste into editor |
| `getEditorText()` | Read input editor text |
| `editor(title, prefill?, dialogOptions?)` | Multi-line editor dialog |
| `setEditorComponent(factory?)` | Replace the main editor component |
| `theme` | Current Theme object |
| `getAllThemes()` / `getTheme()` / `setTheme()` | Theme management |
| `getToolsExpanded()` / `setToolsExpanded()` | Tool output expansion |

---

### Files Found

| File Path | Description |
|---|---|
| `vendor/oh-my-pi/packages/tui/src/tui.ts` | Core TUI engine: Component interface, Container, TUI class, overlay system |
| `vendor/oh-my-pi/packages/tui/src/index.ts` | Public exports of all pi-tui components |
| `vendor/oh-my-pi/packages/tui/src/keybindings.ts` | KeybindingsManager, TUI_KEYBINDINGS definitions |
| `vendor/oh-my-pi/packages/tui/src/keys.ts` | Key helper object, KeyId type, Kitty protocol support |
| `vendor/oh-my-pi/packages/tui/src/components/select-list.ts` | Interactive SelectList component |
| `vendor/oh-my-pi/packages/tui/src/components/settings-list.ts` | Interactive SettingsList component |
| `vendor/oh-my-pi/packages/tui/src/components/box.ts` | Box container with padding, background, border |
| `vendor/oh-my-pi/packages/coding-agent/src/extensibility/extensions/types.ts` | Extension API type definitions (all interfaces) |
| `vendor/oh-my-pi/packages/coding-agent/src/extensibility/extensions/loader.ts` | Extension loading, registerShortcut registration |
| `vendor/oh-my-pi/packages/coding-agent/src/modes/controllers/extension-ui-controller.ts` | Implementation of ui.custom(), setWidget(), setStatus(), etc. |
| `vendor/oh-my-pi/packages/coding-agent/src/modes/controllers/input-controller.ts` | Shortcut dispatch via editor.setCustomKeyHandler |
| `vendor/oh-my-pi/packages/coding-agent/examples/extensions/tools.ts` | Example: ui.custom() with SettingsList |
| `vendor/oh-my-pi/packages/coding-agent/examples/extensions/plan-mode.ts` | Example: registerShortcut, setWidget, setStatus, ui.select |
| `vendor/oh-my-pi/packages/coding-agent/examples/extensions/thinking-note.ts` | Example: registerAssistantThinkingRenderer |
| `vendor/oh-my-pi/packages/swarm-extension/src/extension.ts` | Example: setWidget for progress, registerCommand |
| `vendor/oh-my-pi/packages/coding-agent/src/tools/renderers.ts` | Built-in tool renderer registry |

### Code Patterns

**Pattern 1: Custom component via ui.custom()**
(from tools.ts)
```typescript
await ctx.ui.custom((tui, theme, _keybindings, done) => {
  const container = new Container();
  const settingsList = new SettingsList(items, maxVisible, theme, onChange, onCancel);
  container.addChild(settingsList);
  return {
    render(width) { return container.render(width); },
    invalidate() { container.invalidate(); },
    handleInput(data) { settingsList.handleInput(data); tui.requestRender(); },
  };
});
```

**Pattern 2: Overlay mode custom component**
(from extension-ui-controller.ts)
```typescript
await ctx.ui.custom((tui, theme, keybindings, done) => {
  const component = buildMyComponent(tui, theme, done);
  return component;
}, { overlay: true });
```
When `overlay: true`, the component is shown via `tui.showOverlay(component, { anchor: "bottom-center", width: "100%", maxHeight: "100%", margin: 0 })`.

**Pattern 3: Widget with string lines**
(from plan-mode.ts)
```typescript
const lines: string[] = [];
lines.push(theme.fg("success", "☑ ") + theme.fg("dim", item.text)));
ctx.ui.setWidget("plan-todos", lines);
```

**Pattern 4: Tool with custom renderers**
```typescript
pi.registerTool({
  name: "my_tool",
  renderCall(args, options, theme) { return new Container(); },
  renderResult(result, options, theme, args) { return new Container(); },
  ...
});
```

### Overlay System Details

**File**: `vendor/oh-my-pi/packages/tui/src/tui.ts:436-481, 1416-1504`

OverlayOptions:
```typescript
interface OverlayOptions {
  width?: SizeValue;          // number or "50%"
  minWidth?: number;
  maxHeight?: SizeValue;      // number or "50%"
  anchor?: OverlayAnchor;     // center, top-left, top-right, etc.
  offsetX?: number;
  offsetY?: number;
  row?: SizeValue;
  col?: SizeValue;
  margin?: OverlayMargin | number;
  visible?: (termWidth, termHeight) => boolean;
  fullscreen?: boolean;       // Uses alternate screen buffer
}
```

OverlayHandle:
```typescript
interface OverlayHandle {
  hide(): void;               // Permanently remove
  setHidden(hidden: boolean); // Temporarily hide/show
  isHidden(): boolean;
}
```

Key behaviors:
- Overlays composite over the base content — they do NOT replace it
- Multiple overlays can stack; later ones render on top
- Overlays freeze scrollback commits while visible
- `fullscreen: true` borrows the terminal's alternate screen buffer (vim/less idiom)
- Mouse tracking is enabled automatically for fullscreen overlays
- Focus is set to the overlay component when shown, and restored to previous focus when hidden
- The `OverlayFocusOwner` interface lets overlay roots delegate focus to sub-components

### Available Components Exported from pi-tui

From `packages/tui/src/index.ts`:
- `Autocomplete`, `AutocompleteItem`
- `Box` (padding, background, border)
- `CancellableLoader`
- `Editor`, `EditorComponent` (type only)
- `Image`, `ImageBudget`
- `Input`
- `Loader`
- `Markdown`
- `ScrollView`
- `SelectList`, `SelectItem`, `SelectListTheme`
- `SettingsList`, `SettingItem`, `SettingsListTheme`
- `Spacer`
- `TabBar`
- `Text`
- `TruncatedText`
- `Container`, `Component` (from tui.ts)
- `TUI`, `Focusable`, `OverlayHandle`, `OverlayOptions`
- `Key`, `KeyId`, `KeybindingsManager`
- `FuzzyFilter` utilities
- Terminal utilities (`Terminal`, `TERMINAL`, capabilities)

## Caveats / Not Found

1. **No TreeList component** — Does not exist in the codebase. Extensions needing tree navigation would need to build it from scratch using Container/Text or adapt SelectList.
2. **OutputBlock** — Not a public API. Chat output is managed internally.
3. **Shortcut override behavior** — Extensions register shortcuts via `editor.setCustomKeyHandler()`, which is a secondary handler. It is unclear whether extensions can truly override default keybindings; the editor's own binding resolution likely takes priority.
4. **Widget interactivity** — While widget component factories receive the TUI, widgets don't automatically get keyboard focus. Making a widget interactive requires manual focus management.
5. **Overlay mouse support** — Mouse tracking (SGR extended coordinates) is only enabled for fullscreen overlays. Non-fullscreen overlays do not receive mouse events.
6. **setFooter/setHeader** — The implementation stubs are empty (`setFooter: () => {}`), suggesting these are planned but not yet functional.
